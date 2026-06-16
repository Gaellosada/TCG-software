# ============================================================================
#  TCG Software -- One-Click Launcher
#  Double-click start.bat to run this. Do not run directly.
# ============================================================================

$ErrorActionPreference = "Continue"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Write-Section {
    param([string]$Title)
    Write-Host ""
    Write-Host "=== $Title ===" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "  [OK] $Message" -ForegroundColor Green
}

function Write-Info {
    param([string]$Message)
    Write-Host "  $Message" -ForegroundColor Gray
}

function Write-Fail {
    param([string]$Message)
    Write-Host "  [ERROR] $Message" -ForegroundColor Red
}

function Write-Warn {
    param([string]$Message)
    Write-Host "  [WARNING] $Message" -ForegroundColor Yellow
}

function Refresh-Path {
    # Reload PATH from the registry so newly-installed tools are visible,
    # while preserving any process-level PATH entries (conda, venvs, etc.).
    $currentProcess = $env:Path
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath    = [Environment]::GetEnvironmentVariable("Path", "User")
    # Merge and deduplicate: registry paths first, then existing process paths.
    $allPaths = ("$machinePath;$userPath;$currentProcess") -split ";" |
        Where-Object { $_ -ne "" } | Select-Object -Unique
    $env:Path = $allPaths -join ";"
}

function Has-Winget {
    return [bool](Get-Command winget -ErrorAction SilentlyContinue)
}

function Get-FreePort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    return $port
}

function Get-PythonCmd {
    # Returns the first working Python command (>= 3.12), or $null.
    foreach ($cmd in @("python", "python3", "py")) {
        try {
            $ver = & $cmd --version 2>&1
            if ($ver -match "Python (\d+)\.(\d+)") {
                $major = [int]$Matches[1]
                $minor = [int]$Matches[2]
                if ($major -ge 3 -and $minor -ge 12) {
                    return $cmd
                }
            }
        } catch { Write-Verbose "Python candidate '$cmd' failed: $_" }
    }
    return $null
}

function Get-NodeMajor {
    try {
        $ver = & node --version 2>&1
        if ($ver -match "v(\d+)") { return [int]$Matches[1] }
    } catch { Write-Verbose "Node.js check failed: $_" }
    return 0
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "  TCG Software Launcher" -ForegroundColor White -BackgroundColor DarkBlue
Write-Host "  Trading platform starting up..." -ForegroundColor DarkCyan
Write-Host ""

$BackendPort  = Get-FreePort
$FrontendPort = Get-FreePort

if (-not $BackendPort -or -not $FrontendPort) {
    Write-Fail "Could not allocate free ports. Check firewall or network settings."
    exit 1
}

Write-Ok "Backend  -> http://localhost:$BackendPort"
Write-Ok "Frontend -> http://localhost:$FrontendPort"

# ---------------------------------------------------------------------------
# Step 1 -- Check .env
# ---------------------------------------------------------------------------

Write-Section "Checking configuration"

$envFile = Join-Path $ProjectRoot ".env"
$envExample = Join-Path $ProjectRoot ".env.example"

if (-not (Test-Path $envFile)) {
    Write-Fail "No .env file found."
    Write-Host ""
    Write-Host "  You need a .env file with your MongoDB connection details." -ForegroundColor Yellow
    Write-Host "  To create one:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "    1. Open the folder: $ProjectRoot" -ForegroundColor White
    Write-Host "    2. Copy '.env.example' and rename the copy to '.env'" -ForegroundColor White
    Write-Host "    3. Edit '.env' and set MONGO_URI to your MongoDB address" -ForegroundColor White
    Write-Host ""
    if (Test-Path $envExample) {
        Write-Host "  The .env.example file is at:" -ForegroundColor Gray
        Write-Host "    $envExample" -ForegroundColor White
    }
    Write-Host ""
    exit 1
}

$envContent = Get-Content $envFile -Raw

# Check for valid configuration: either MONGO_URI or SSM tunnel enabled
$hasMongo = $envContent -match '(?m)^MONGO_URI\s*=\s*\S+'
$hasTunnel = $envContent -match '(?m)^SSM_TUNNEL_ENABLED\s*=\s*true'

if (-not $hasMongo -and -not $hasTunnel) {
    Write-Fail "Your .env file needs either MONGO_URI or SSM_TUNNEL_ENABLED=true."
    Write-Host "  Edit '$envFile' and configure your MongoDB connection." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Option 1: Set MONGO_URI for direct connection" -ForegroundColor White
    Write-Host "  Option 2: Set SSM_TUNNEL_ENABLED=true for bastion tunnel" -ForegroundColor White
    Write-Host ""
    exit 1
}

if ($hasTunnel) {
    Write-Ok "Configuration file found (SSM tunnel mode)"

    # --- AWS CLI ---
    $awsCmd = Get-Command aws -ErrorAction SilentlyContinue
    if (-not $awsCmd) {
        if (Has-Winget) {
            Write-Warn "AWS CLI not found. Installing via winget..."
            try {
                winget install --id Amazon.AWSCLI --exact --source winget --accept-package-agreements --accept-source-agreements
                Refresh-Path
                $awsCmd = Get-Command aws -ErrorAction SilentlyContinue
            } catch { Write-Warn "Auto-install failed: $_" }
        } else {
            Write-Warn "AWS CLI not found and winget is not available for auto-install."
        }

        if (-not $awsCmd) {
            Write-Fail "Could not install AWS CLI automatically."
            Write-Host ""
            Write-Host "  The SSM tunnel requires AWS CLI v2. Install manually:" -ForegroundColor Yellow
            Write-Host "    https://aws.amazon.com/cli/" -ForegroundColor White
            Write-Host ""
            exit 1
        }
    }
    Write-Ok "AWS CLI found"

    # --- Session Manager plugin ---
    # The plugin is a separate binary invoked by the AWS CLI during SSM sessions.
    # It installs to a well-known path on Windows.
    $ssmPluginPath = "C:\Program Files\Amazon\SessionManagerPlugin\bin\session-manager-plugin.exe"
    $ssmPlugin = (Get-Command session-manager-plugin -ErrorAction SilentlyContinue) -or (Test-Path $ssmPluginPath)

    if (-not $ssmPlugin) {
        Write-Warn "Session Manager plugin not found. Installing..."
        $installerUrl = "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/windows/SessionManagerPluginSetup.exe"
        $installerPath = Join-Path $env:TEMP "SessionManagerPluginSetup.exe"
        try {
            Write-Info "Downloading Session Manager plugin installer..."
            Invoke-WebRequest -Uri $installerUrl -OutFile $installerPath -UseBasicParsing
            Write-Info "Running installer (you may see a UAC prompt)..."
            $installProc = Start-Process -FilePath $installerPath -ArgumentList "/quiet" -Wait -PassThru
            if ($installProc.ExitCode -eq 0) {
                Refresh-Path
                $ssmPlugin = (Get-Command session-manager-plugin -ErrorAction SilentlyContinue) -or (Test-Path $ssmPluginPath)
            }
        } catch {
            Write-Warn "Auto-install failed: $_"
        } finally {
            if (Test-Path $installerPath) { Remove-Item $installerPath -Force -ErrorAction SilentlyContinue }
        }

        if (-not $ssmPlugin) {
            Write-Fail "Could not install Session Manager plugin automatically."
            Write-Host ""
            Write-Host "  The SSM tunnel will not work without it. Install manually:" -ForegroundColor Yellow
            Write-Host "    https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html" -ForegroundColor White
            Write-Host ""
            exit 1
        }
    }
    Write-Ok "Session Manager plugin found"
} else {
    Write-Ok "Configuration file found"
}

# ---------------------------------------------------------------------------
# Step 2 -- Python 3.12+
# ---------------------------------------------------------------------------

Write-Section "Checking Python"

$pythonCmd = Get-PythonCmd

if (-not $pythonCmd) {
    if (Has-Winget) {
        Write-Warn "Python 3.12+ not found. Installing via winget..."
        try {
            winget install --id Python.Python.3.12 --exact --source winget --accept-package-agreements --accept-source-agreements
            Refresh-Path
            $pythonCmd = Get-PythonCmd
        } catch {
            Write-Warn "Auto-install failed: $_"
        }
    } else {
        Write-Warn "Python 3.12+ not found and winget is not available for auto-install."
    }

    if (-not $pythonCmd) {
        Write-Fail "Could not install Python automatically."
        Write-Host ""
        Write-Host "  Please install Python 3.12 or newer manually:" -ForegroundColor Yellow
        Write-Host "    https://www.python.org/downloads/" -ForegroundColor White
        Write-Host ""
        Write-Host "  During installation, check 'Add Python to PATH'." -ForegroundColor Yellow
        Write-Host "  Then run this launcher again." -ForegroundColor Yellow
        Write-Host ""
        exit 1
    }
}

$pyVersion = & $pythonCmd --version 2>&1
Write-Ok "$pyVersion"

# ---------------------------------------------------------------------------
# Step 3 -- Node.js 18+
# ---------------------------------------------------------------------------

Write-Section "Checking Node.js"

$nodeMajor = Get-NodeMajor

if ($nodeMajor -lt 18) {
    if (Has-Winget) {
        Write-Warn "Node.js 18+ not found. Installing via winget..."
        try {
            winget install --id OpenJS.NodeJS.LTS --exact --source winget --accept-package-agreements --accept-source-agreements
            Refresh-Path
            $nodeMajor = Get-NodeMajor
        } catch { Write-Warn "Auto-install failed: $_" }
    } else {
        Write-Warn "Node.js 18+ not found and winget is not available for auto-install."
    }

    if ($nodeMajor -lt 18) {
        Write-Fail "Could not install Node.js automatically."
        Write-Host ""
        Write-Host "  Please install Node.js 18 or newer manually:" -ForegroundColor Yellow
        Write-Host "    https://nodejs.org/" -ForegroundColor White
        Write-Host ""
        Write-Host "  Then run this launcher again." -ForegroundColor Yellow
        Write-Host ""
        exit 1
    }
}

$nodeVersion = & node --version 2>&1
Write-Ok "Node.js $nodeVersion"

# ---------------------------------------------------------------------------
# Step 4 -- Python virtual environment
# ---------------------------------------------------------------------------

Write-Section "Setting up Python environment"

$venvDir   = Join-Path $ProjectRoot ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$venvPip    = Join-Path $venvDir "Scripts\pip.exe"
$freshVenv = $false

if (-not (Test-Path $venvPython)) {
    Write-Info "Creating virtual environment..."
    & $pythonCmd -m venv $venvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Failed to create virtual environment."
        exit 1
    }
    $freshVenv = $true
    Write-Ok "Virtual environment created"
} else {
    Write-Ok "Virtual environment exists"
}

# ---------------------------------------------------------------------------
# Step 5 -- Python dependencies
# ---------------------------------------------------------------------------

$depsMarker = Join-Path $venvDir ".deps-installed"

# Re-install only when the dependency manifests actually change (instead of
# skipping forever once a .venv exists). Keeps normal launches fast while
# auto-healing after a pyproject.toml / uv.lock change (e.g. a new dependency).
$depFiles = @("pyproject.toml", "uv.lock") |
    ForEach-Object { Join-Path $ProjectRoot $_ } |
    Where-Object { Test-Path $_ } |
    Sort-Object
$depsHash = if ($depFiles) {
    (($depFiles | ForEach-Object { (Get-FileHash -Algorithm SHA256 -LiteralPath $_).Hash }) -join '-')
} else { '' }
$storedHash = if (Test-Path $depsMarker) {
    $markerText = Get-Content $depsMarker -Raw -ErrorAction SilentlyContinue
    if ($markerText) { $markerText.Trim() } else { '' }
} else { '' }

if ($freshVenv -or ($storedHash -ne $depsHash)) {
    Write-Section "Installing Python dependencies"
    if ($freshVenv) {
        Write-Info "This may take a minute on first run..."
    } else {
        Write-Info "Dependency manifest changed since last run -- reinstalling..."
    }

    Push-Location $ProjectRoot
    try {
        & $venvPip install -e . 2>&1 | ForEach-Object { Write-Info "$_" }
        $pipExit = $LASTEXITCODE
    } finally {
        Pop-Location
    }

    if ($pipExit -ne 0) {
        Write-Fail "Failed to install Python dependencies. See the output above for details."
        exit 1
    }
    Set-Content -Path $depsMarker -Value $depsHash -NoNewline
    Write-Ok "Python dependencies installed"
} else {
    Write-Section "Python dependencies"
    Write-Ok "Already up to date (pyproject.toml/uv.lock unchanged)"
}

# ---------------------------------------------------------------------------
# Step 6 -- npm dependencies
# ---------------------------------------------------------------------------

Write-Section "Checking frontend dependencies"

$frontendDir = Join-Path $ProjectRoot "frontend"
$nodeModules = Join-Path $frontendDir "node_modules"
$npmMarker   = Join-Path $nodeModules ".deps-installed"
$freshModules = -not (Test-Path $nodeModules)

# Re-install only when the dependency manifests actually change (instead of
# skipping forever once node_modules exists). Mirrors the Python venv logic so a
# new frontend dependency (e.g. @tanstack/react-query) self-heals on next launch.
$npmDepFiles = @("package.json", "package-lock.json") |
    ForEach-Object { Join-Path $frontendDir $_ } |
    Where-Object { Test-Path $_ } |
    Sort-Object
$npmDepsHash = if ($npmDepFiles) {
    (($npmDepFiles | ForEach-Object { (Get-FileHash -Algorithm SHA256 -LiteralPath $_).Hash }) -join '-')
} else { '' }
$npmStoredHash = if (Test-Path $npmMarker) {
    $m = Get-Content $npmMarker -Raw -ErrorAction SilentlyContinue
    if ($m) { $m.Trim() } else { '' }
} else { '' }

if ($freshModules -or ($npmStoredHash -ne $npmDepsHash)) {
    if ($freshModules) {
        Write-Info "Installing npm packages (first run)..."
    } else {
        Write-Info "Dependency manifest changed since last run -- reinstalling..."
    }
    Push-Location $frontendDir
    try {
        # Use cmd.exe /c -- PowerShell's npm.ps1 shim can mangle arguments.
        # Pipe stderr into stdout inside cmd to preserve $LASTEXITCODE across the pipeline.
        & cmd.exe /c "npm install 2>&1"
        $npmExit = $LASTEXITCODE
    } finally {
        Pop-Location
    }

    if ($npmExit -ne 0) {
        Write-Fail "npm install failed. Check the output above."
        exit 1
    }
    Set-Content -Path $npmMarker -Value $npmDepsHash -NoNewline
    Write-Ok "Frontend dependencies installed"
} else {
    Write-Ok "Already up to date (package.json/package-lock.json unchanged)"
}

# ---------------------------------------------------------------------------
# Step 7 & 8 -- Start backend and frontend
# ---------------------------------------------------------------------------

Write-Section "Starting application"

# Check if ports are already in use (e.g. leftover from a previous run).
# Filter: only LISTENING sockets with a real PID (not PID 0 / TIME_WAIT).
$portBackend  = Get-NetTCPConnection -LocalPort $BackendPort -ErrorAction SilentlyContinue |
    Where-Object { $_.OwningProcess -ne 0 -and $_.State -eq 'Listen' } | Select-Object -First 1
$portFrontend = Get-NetTCPConnection -LocalPort $FrontendPort -ErrorAction SilentlyContinue |
    Where-Object { $_.OwningProcess -ne 0 -and $_.State -eq 'Listen' } | Select-Object -First 1

# Expected process names for each port -- only auto-kill these, warn on anything else.
# Regex matches: python, python3, python3.12, pythonw, node, vite (with optional version suffix).
$backendExpected  = @("python", "pythonw", "uvicorn")
$frontendExpected = @("node", "vite")

function Stop-PortProcess {
    param([object]$Conn, [int]$Port, [string[]]$Expected)
    $proc = Get-Process -Id $Conn.OwningProcess -ErrorAction SilentlyContinue
    if (-not $proc) { return }
    # Match process names with optional version suffix (e.g. python3.12).
    $isTcg = $false
    foreach ($name in $Expected) {
        if ($proc.Name -match "^${name}(\d[\d.]*)?$") { $isTcg = $true; break }
    }
    if ($isTcg) {
        Write-Info "Killing leftover $($proc.Name) (PID $($proc.Id)) on port $Port"
        & taskkill /T /F /PID $proc.Id 2>$null | Out-Null
    } else {
        Write-Warn "Port $Port is used by '$($proc.Name)' (PID $($proc.Id)) -- not a TCG process. Skipping."
    }
}

if ($portBackend -or $portFrontend) {
    $occupied = @()
    if ($portBackend)  { $occupied += "$BackendPort (backend, PID $($portBackend.OwningProcess))" }
    if ($portFrontend) { $occupied += "$FrontendPort (frontend, PID $($portFrontend.OwningProcess))" }
    Write-Warn "Port(s) already in use: $($occupied -join ', ')"
    Write-Host ""
    Write-Host "  This usually means a previous session was not shut down cleanly." -ForegroundColor Yellow
    Write-Host ""

    if ($portBackend)  { Stop-PortProcess $portBackend  $BackendPort  $backendExpected }
    if ($portFrontend) { Stop-PortProcess $portFrontend $FrontendPort $frontendExpected }
    # Brief pause for ports to release.
    Start-Sleep -Seconds 2

    # Re-check ports. If still busy, wait a bit more and try once more.
    $stillBusy = $false
    foreach ($p in @($BackendPort, $FrontendPort)) {
        $check = Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue |
            Where-Object { $_.OwningProcess -ne 0 -and $_.State -eq 'Listen' }
        if ($check) { $stillBusy = $true; break }
    }
    if ($stillBusy) {
        Write-Info "Ports still releasing, waiting a few more seconds..."
        Start-Sleep -Seconds 3
    }

    Write-Ok "Old processes cleared"
}

# --- Tunnel port check (SSM mode only) ---
# An orphaned session-manager-plugin.exe from a previous run can hold
# the forwarded port indefinitely. Prompt the user before killing it.
if ($hasTunnel) {
    # Read LOCAL_PORT from .env; default 27017.
    $tunnelPort = 27017
    if ($envContent -match '(?m)^LOCAL_PORT\s*=\s*(\d+)') {
        $tunnelPort = [int]$Matches[1]
    }
    $portTunnel = Get-NetTCPConnection -LocalPort $tunnelPort -ErrorAction SilentlyContinue |
        Where-Object { $_.OwningProcess -ne 0 -and $_.State -eq 'Listen' } | Select-Object -First 1
    if ($portTunnel) {
        $tunnelProc = Get-Process -Id $portTunnel.OwningProcess -ErrorAction SilentlyContinue
        $tunnelProcName = if ($tunnelProc) { $tunnelProc.Name } else { "unknown" }
        Write-Warn "SSM tunnel port $tunnelPort is already in use by '$tunnelProcName' (PID $($portTunnel.OwningProcess))"
        Write-Host ""
        Write-Host "  This is likely an orphaned session-manager-plugin from a previous run." -ForegroundColor Yellow
        Write-Host ""
        $answer = Read-Host "  Kill it? (y/n)"
        if ($answer -match '^[Yy]') {
            Write-Info "Killing PID $($portTunnel.OwningProcess)..."
            & taskkill /T /F /PID $portTunnel.OwningProcess 2>$null | Out-Null
            Start-Sleep -Seconds 2
            # Verify it's gone.
            $recheck = Get-NetTCPConnection -LocalPort $tunnelPort -ErrorAction SilentlyContinue |
                Where-Object { $_.OwningProcess -ne 0 -and $_.State -eq 'Listen' }
            if ($recheck) {
                Write-Fail "Port $tunnelPort is still in use. Please kill the process manually and retry."
                exit 1
            }
            Write-Ok "Tunnel port $tunnelPort freed"
        } else {
            Write-Fail "Cannot start SSM tunnel while port $tunnelPort is occupied. Aborting."
            exit 1
        }
    }
}

# Track child processes for cleanup.
$script:backendProcess = $null
$script:frontendProcess = $null
$script:logsDir = Join-Path $ProjectRoot "logs"
if (-not (Test-Path $script:logsDir)) {
    try {
        New-Item -Path $script:logsDir -ItemType Directory -Force -ErrorAction Stop | Out-Null
    } catch {
        Write-Fail "Cannot create log directory: $($script:logsDir) -- $_"
        exit 1
    }
}

# Truncate log files from previous runs to prevent unbounded growth.
foreach ($logName in @("backend.log", "backend-error.log", "frontend.log", "frontend-error.log")) {
    $logPath = Join-Path $script:logsDir $logName
    if (Test-Path $logPath) { Clear-Content $logPath -ErrorAction SilentlyContinue }
}

function Stop-App {
    Write-Host ""
    Write-Section "Shutting down"

    # Use taskkill /T to kill entire process trees (uvicorn workers, node children).
    if ($script:backendProcess -and -not $script:backendProcess.HasExited) {
        Write-Info "Stopping backend (PID $($script:backendProcess.Id))..."
        & taskkill /T /F /PID $script:backendProcess.Id 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "Could not stop backend PID $($script:backendProcess.Id) -- it may have already exited."
        }
    }
    if ($script:frontendProcess -and -not $script:frontendProcess.HasExited) {
        Write-Info "Stopping frontend (PID $($script:frontendProcess.Id))..."
        & taskkill /T /F /PID $script:frontendProcess.Id 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "Could not stop frontend PID $($script:frontendProcess.Id) -- it may have already exited."
        }
    }

    Write-Ok "All processes stopped. Goodbye!"
    Write-Host ""
}

# Register Ctrl+C via CancelKeyPress -- works reliably in cmd-launched PowerShell.
$script:exitRequested = $false
$null = [Console]::add_CancelKeyPress({
    param($sender, $e)
    $e.Cancel = $true       # Prevent immediate termination.
    $script:exitRequested = $true
})

# Wrap the entire startup + run phase in try/finally so Ctrl+C during startup
# still triggers Stop-App cleanup (fixes race where handler fires before main loop).
try {

# --- Backend ---
Write-Info "Starting backend server (port $BackendPort)..."

$backendLog = Join-Path $script:logsDir "backend.log"
# Launch via `python -m tcg.core` (not bare uvicorn): on Windows it installs the
# SelectorEventLoop policy psycopg requires before uvicorn creates the loop.
$backendArgs = "-m tcg.core --port $BackendPort"
$env:TCG_CORS_ORIGINS = "http://localhost:$FrontendPort"
try {
    $script:backendProcess = Start-Process -FilePath $venvPython `
        -ArgumentList $backendArgs `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $backendLog `
        -RedirectStandardError (Join-Path $script:logsDir "backend-error.log") `
        -PassThru
} catch {
    Write-Fail "Failed to start backend: $_"
    exit 1
}

# --- Frontend ---
Write-Info "Starting frontend dev server (port $FrontendPort)..."

# Run vite directly via its bin script -- avoids PATH issues with npm+Start-Process.
$viteCmd = Join-Path $frontendDir "node_modules\.bin\vite.cmd"
if (-not (Test-Path $viteCmd)) {
    Write-Fail "Vite not found at $viteCmd -- try deleting frontend/node_modules and running again."
    & taskkill /T /F /PID $script:backendProcess.Id 2>$null | Out-Null
    exit 1
}
$env:TCG_BACKEND_PORT = $BackendPort
$env:TCG_FRONTEND_PORT = $FrontendPort
try {
    $script:frontendProcess = Start-Process -FilePath $viteCmd `
        -WorkingDirectory $frontendDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $script:logsDir "frontend.log") `
        -RedirectStandardError (Join-Path $script:logsDir "frontend-error.log") `
        -PassThru
} catch {
    Write-Fail "Failed to start frontend: $_"
    & taskkill /T /F /PID $script:backendProcess.Id 2>$null | Out-Null
    exit 1
}

# ---------------------------------------------------------------------------
# Step 9 -- Wait for frontend and open browser immediately
# ---------------------------------------------------------------------------

Write-Info "Waiting for frontend to be ready..."
$frontendReady = $false
for ($i = 0; $i -lt 25; $i++) {
    if ($script:exitRequested) { break }
    $tcp = $null
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.Connect("127.0.0.1", $FrontendPort)
        $frontendReady = $true
        break
    } catch { } finally {
        if ($tcp) { $tcp.Dispose() }
    }
    Start-Sleep -Milliseconds 200
}

if ($frontendReady) {
    Write-Ok "Frontend is ready"
} else {
    Write-Warn "Frontend did not respond within 5 seconds (it may still be starting)"
}

# Open the browser as soon as the frontend is reachable. The backend may
# still be starting (SSM tunnel + MongoDB), but the UI handles that
# gracefully — API calls show an error state until the backend is up.
# Previously the launcher waited up to 30 s for the backend health check
# before opening the browser, making every cold start feel slow.
Write-Section "Ready!"
Write-Host ""
Write-Host "  Opening http://localhost:$FrontendPort in your browser..." -ForegroundColor White
Start-Process "http://localhost:$FrontendPort"

# ---------------------------------------------------------------------------
# Step 10 -- Backend health check (non-blocking, just log the result)
# ---------------------------------------------------------------------------

Write-Info "Waiting for backend to be ready (this runs in the background)..."

# Use the /health endpoint instead of a raw TCP check.  Uvicorn binds
# the port BEFORE the lifespan runs (SSM tunnel + MongoDB connection),
# so a raw TCP connect would return "ready" prematurely.  The /health
# endpoint returns 200 only after the lifespan has completed.
$ready = $false
for ($i = 0; $i -lt 150; $i++) {
    if ($script:exitRequested) { break }

    # Detect early crash -- backend exited before responding.
    if ($script:backendProcess.HasExited) {
        Write-Fail "Backend crashed before it could start."
        $errLog = Join-Path $script:logsDir "backend-error.log"
        if (Test-Path $errLog) {
            $errContent = Get-Content $errLog -Tail 10 -ErrorAction SilentlyContinue
            if ($errContent) {
                Write-Host ""
                Write-Host "  Last lines from backend-error.log:" -ForegroundColor Yellow
                $errContent | ForEach-Object { Write-Host "    $_" -ForegroundColor Gray }
            }
        }
        Write-Host ""
        Write-Host "  Common causes:" -ForegroundColor Yellow
        Write-Host "    - Wrong MONGO_URI in .env (check your connection string)" -ForegroundColor White
        Write-Host "    - MongoDB server is not running or unreachable" -ForegroundColor White
        Write-Host "    - Port $BackendPort is already in use by another program" -ForegroundColor White
        Write-Host ""
        Write-Host "  Full logs at: $($script:logsDir)" -ForegroundColor Gray
        Write-Host ""
        # Clean up frontend since backend failed.
        if (-not $script:frontendProcess.HasExited) {
            & taskkill /T /F /PID $script:frontendProcess.Id 2>$null | Out-Null
        }
        exit 1
    }

    Start-Sleep -Milliseconds 200
    try {
        $wc = New-Object System.Net.WebClient
        $resp = $wc.DownloadString("http://127.0.0.1:$BackendPort/health")
        if ($resp -match '"ok"') {
            $ready = $true
            break
        }
    } catch {
        # Not ready yet -- connection refused or 503.
    } finally {
        if ($wc) { $wc.Dispose() }
    }
}

if ($ready) {
    Write-Ok "Backend is ready"
} else {
    Write-Warn "Backend did not respond within 30 seconds (it may still be starting)"
    Write-Host "  Check logs at: $($script:logsDir)" -ForegroundColor Gray
}

# ---------------------------------------------------------------------------
# Step 11 -- Wait for exit
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "  The app is running. Press Ctrl+C to stop." -ForegroundColor Yellow
Write-Host "  Logs are in: $($script:logsDir)" -ForegroundColor Gray
Write-Host ""

    while (-not $script:exitRequested) {
        # Detect if both processes died unexpectedly.
        if ($script:backendProcess.HasExited -and $script:frontendProcess.HasExited) {
            Write-Warn "Both backend and frontend have stopped unexpectedly."
            Write-Host "  Check logs at: $($script:logsDir)" -ForegroundColor Gray
            break
        }

        # Detect backend-only crash while running.
        if ($script:backendProcess.HasExited -and -not $script:frontendProcess.HasExited) {
            Write-Warn "Backend stopped unexpectedly. The app may not work correctly."
            Write-Host "  Check backend-error.log at: $($script:logsDir)" -ForegroundColor Gray
            break
        }

        # Detect frontend-only crash while running.
        if ($script:frontendProcess.HasExited -and -not $script:backendProcess.HasExited) {
            Write-Warn "Frontend stopped unexpectedly. Try refreshing your browser."
            Write-Host "  Check frontend-error.log at: $($script:logsDir)" -ForegroundColor Gray
            break
        }

        Start-Sleep -Milliseconds 500
    }

} finally {
    Stop-App
    exit 0
}
