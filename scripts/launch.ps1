# ============================================================================
#  TCG Software -- One-Click Launcher
#  Double-click start.bat to run this. Do not run directly.
# ============================================================================

$ErrorActionPreference = "Continue"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$BackendPort  = 8000
$FrontendPort = 5173

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
$hasMongo = $envContent -match 'MONGO_URI\s*=\s*\S+'
$hasTunnel = $envContent -match 'SSM_TUNNEL_ENABLED\s*=\s*true'

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
    # Check for AWS CLI
    $awsCmd = Get-Command aws -ErrorAction SilentlyContinue
    if (-not $awsCmd) {
        Write-Warn "AWS CLI not found in PATH. The SSM tunnel requires AWS CLI v2."
        Write-Host "  Install from: https://aws.amazon.com/cli/" -ForegroundColor Yellow
    } else {
        Write-Ok "AWS CLI found"
    }
} else {
    Write-Ok "Configuration file found"
}

# ---------------------------------------------------------------------------
# Step 2 -- Python 3.12+
# ---------------------------------------------------------------------------

Write-Section "Checking Python"

$pythonCmd = Get-PythonCmd

if (-not $pythonCmd) {
    Write-Warn "Python 3.12+ not found. Installing via winget..."
    try {
        winget install --id Python.Python.3.12 --exact --source winget --accept-package-agreements --accept-source-agreements
        Refresh-Path
        $pythonCmd = Get-PythonCmd
    } catch {
        Write-Warn "Auto-install failed: $_"
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
    Write-Warn "Node.js 18+ not found. Installing via winget..."
    try {
        winget install --id OpenJS.NodeJS.LTS --exact --source winget --accept-package-agreements --accept-source-agreements
        Refresh-Path
        $nodeMajor = Get-NodeMajor
    } catch { Write-Warn "Auto-install failed: $_" }

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

if ($freshVenv -or -not (Test-Path $depsMarker)) {
    Write-Section "Installing Python dependencies"
    Write-Info "This may take a minute on first run..."

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
    New-Item -Path $depsMarker -ItemType File -Force | Out-Null
    Write-Ok "Python dependencies installed"
} else {
    Write-Section "Python dependencies"
    Write-Ok "Already installed (delete .venv to force reinstall)"
}

# ---------------------------------------------------------------------------
# Step 6 -- npm dependencies
# ---------------------------------------------------------------------------

Write-Section "Checking frontend dependencies"

$frontendDir = Join-Path $ProjectRoot "frontend"
$nodeModules = Join-Path $frontendDir "node_modules"

if (-not (Test-Path $nodeModules)) {
    Write-Info "Installing npm packages (first run)..."
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
    Write-Ok "Frontend dependencies installed"
} else {
    Write-Ok "Already installed (delete frontend/node_modules to force reinstall)"
}

# ---------------------------------------------------------------------------
# Step 7 & 8 -- Start backend and frontend
# ---------------------------------------------------------------------------

Write-Section "Starting application"

# Check if ports are already in use (e.g. leftover from a previous run).
$portBackend  = Get-NetTCPConnection -LocalPort $BackendPort -ErrorAction SilentlyContinue | Select-Object -First 1
$portFrontend = Get-NetTCPConnection -LocalPort $FrontendPort -ErrorAction SilentlyContinue | Select-Object -First 1

# Expected process names for each port -- only auto-kill these, warn on anything else.
$backendExpected  = @("python", "python3", "pythonw", "uvicorn")
$frontendExpected = @("node", "vite")

function Stop-PortProcess {
    param([object]$Conn, [int]$Port, [string[]]$Expected)
    $proc = Get-Process -Id $Conn.OwningProcess -ErrorAction SilentlyContinue
    if (-not $proc) { return }
    if ($proc.Name -in $Expected) {
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
    Write-Ok "Old processes cleared"
}

# Track child processes for cleanup.
$script:backendProcess = $null
$script:frontendProcess = $null
$script:logsDir = Join-Path $ProjectRoot "logs"
if (-not (Test-Path $script:logsDir)) {
    New-Item -Path $script:logsDir -ItemType Directory -Force | Out-Null
}

function Stop-App {
    Write-Host ""
    Write-Section "Shutting down"

    # Use taskkill /T to kill entire process trees (uvicorn workers, node children).
    if ($script:backendProcess -and -not $script:backendProcess.HasExited) {
        Write-Info "Stopping backend (PID $($script:backendProcess.Id))..."
        & taskkill /T /F /PID $script:backendProcess.Id 2>$null | Out-Null
    }
    if ($script:frontendProcess -and -not $script:frontendProcess.HasExited) {
        Write-Info "Stopping frontend (PID $($script:frontendProcess.Id))..."
        & taskkill /T /F /PID $script:frontendProcess.Id 2>$null | Out-Null
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
$backendArgs = "-m uvicorn tcg.core.app:app --port $BackendPort --reload"
$script:backendProcess = Start-Process -FilePath $venvPython `
    -ArgumentList $backendArgs `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $backendLog `
    -RedirectStandardError (Join-Path $script:logsDir "backend-error.log") `
    -PassThru

# --- Frontend ---
Write-Info "Starting frontend dev server (port $FrontendPort)..."

# Run vite directly via its bin script -- avoids PATH issues with npm+Start-Process.
$viteCmd = Join-Path $frontendDir "node_modules\.bin\vite.cmd"
if (-not (Test-Path $viteCmd)) {
    Write-Fail "Vite not found at $viteCmd -- try deleting frontend/node_modules and running again."
    & taskkill /T /F /PID $script:backendProcess.Id 2>$null | Out-Null
    exit 1
}
$script:frontendProcess = Start-Process -FilePath $viteCmd `
    -WorkingDirectory $frontendDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $script:logsDir "frontend.log") `
    -RedirectStandardError (Join-Path $script:logsDir "frontend-error.log") `
    -PassThru

# ---------------------------------------------------------------------------
# Step 9 -- Wait for backend with crash detection
# ---------------------------------------------------------------------------

Write-Info "Waiting for backend to be ready..."

$ready = $false
for ($i = 0; $i -lt 50; $i++) {
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
    $tcp = $null
    try {
        # TCP check -- works on all PowerShell versions (Invoke-WebRequest throws on 404 in PS 5.1).
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.Connect("127.0.0.1", $BackendPort)
        $ready = $true
        break
    } catch {
        # Connection refused -- keep trying.
    } finally {
        if ($tcp) { $tcp.Dispose() }
    }
}

if ($ready) {
    Write-Ok "Backend is ready"
} else {
    Write-Warn "Backend did not respond within 10 seconds (it may still be starting)"
    Write-Host "  Check logs at: $($script:logsDir)" -ForegroundColor Gray
}

# ---------------------------------------------------------------------------
# Step 10 -- Wait for frontend, then open browser
# ---------------------------------------------------------------------------

Write-Info "Waiting for frontend to be ready..."
$frontendReady = $false
for ($i = 0; $i -lt 25; $i++) {
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

Write-Section "Ready!"
Write-Host ""
if ($frontendReady) {
    Write-Host "  Opening http://localhost:$FrontendPort in your browser..." -ForegroundColor White
} else {
    Write-Warn "Frontend did not respond within 5 seconds (it may still be starting)"
    Write-Host "  Opening browser anyway..." -ForegroundColor Gray
}
Start-Process "http://localhost:$FrontendPort"

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
