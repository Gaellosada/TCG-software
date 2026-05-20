#!/usr/bin/env bash
# Library smoke-install — proves @tcg/frontend is consumable by a fresh,
# vanilla Angular host without any TCG-specific scaffolding.
#
# What this script does:
#   1. Builds @tcg/frontend via ng-packagr → dist/tcg-frontend/.
#   2. Generates a brand-new Angular workspace in a temp directory via
#      `ng new tcg-smoke-host` (skip-git, skip-install).
#   3. In the smoke host, runs `npm install` then installs the freshly
#      built library via `npm install --no-save file:<path-to-dist>`.
#   4. Patches the host's `app.config.ts` to provide TCG_API_BASE_URL
#      and spread `tcgRoutes` into provideRouter.
#   5. Builds the host: `ng build`.
#   6. Prints SUCCESS / FAIL.
#
# Run from the workspace root (frontend-angular/):
#   bash tools/smoke-install.sh
#
# Exit code: 0 on success, non-zero on any step failure.

set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LIB_DIST="$WORKSPACE_ROOT/dist/tcg-frontend"
LOG_DIR="$WORKSPACE_ROOT/tools/.smoke-install-logs"

mkdir -p "$LOG_DIR"
ng_log="$LOG_DIR/ng-new.log"
install_log="$LOG_DIR/npm-install.log"
build_lib_log="$LOG_DIR/ng-build-lib.log"
build_host_log="$LOG_DIR/ng-build-host.log"

TMP_BASE="${TMPDIR:-/tmp}/tcg-smoke-$(date +%s)-$$"
HOST_DIR="$TMP_BASE/tcg-smoke-host"

cleanup() {
  if [ "${KEEP_SMOKE_HOST:-0}" = "1" ]; then
    echo "[smoke] KEEP_SMOKE_HOST=1 — leaving $TMP_BASE for inspection"
  else
    rm -rf "$TMP_BASE"
  fi
}
trap cleanup EXIT

step() {
  printf '\n[smoke] %s\n' "$*"
}

fail() {
  printf '\n[smoke] FAIL: %s\n' "$*" 1>&2
  exit 1
}

# ----------------------------------------------------------------------
# Step 1: build the library (if not already built or out-of-date).
# ----------------------------------------------------------------------
step "Building @tcg/frontend library (ng build tcg-frontend)"
cd "$WORKSPACE_ROOT" || fail "workspace root not found"
if ! npx ng build tcg-frontend > "$build_lib_log" 2>&1; then
  tail -40 "$build_lib_log"
  fail "library build failed — see $build_lib_log"
fi
[ -f "$LIB_DIST/package.json" ] || fail "library dist missing package.json at $LIB_DIST"
echo "[smoke] library built at $LIB_DIST"

# ----------------------------------------------------------------------
# Step 2: ng new tcg-smoke-host (standalone, routing, skip-install).
# ----------------------------------------------------------------------
mkdir -p "$TMP_BASE"
step "Creating fresh Angular workspace at $HOST_DIR"
cd "$TMP_BASE" || fail "tmp dir not writable"

# `ng new` in non-interactive mode with the canonical host preset:
# standalone components, routing, css styles, skip git+install.
if ! npx -y -p @angular/cli@19 ng new tcg-smoke-host \
    --standalone --routing --style=css \
    --skip-git --skip-install --skip-tests \
    --defaults \
    > "$ng_log" 2>&1; then
  tail -40 "$ng_log"
  fail "ng new failed — see $ng_log"
fi
[ -d "$HOST_DIR/src/app" ] || fail "ng new did not produce expected src/app directory"
echo "[smoke] host generated"

# ----------------------------------------------------------------------
# Step 3: install host deps + install the library tarball.
# ----------------------------------------------------------------------
step "Installing host dependencies (npm install)"
cd "$HOST_DIR" || fail "host dir not present"
if ! npm install --no-audit --no-fund > "$install_log" 2>&1; then
  tail -40 "$install_log"
  fail "host npm install failed — see $install_log"
fi
echo "[smoke] host deps installed"

step "Installing @tcg/frontend from $LIB_DIST + @angular/cdk peer dep"
# Important: install the library AND the @angular/cdk peer dep in a
# single `npm install` call. If we ran two consecutive installs with
# `--no-save`, the second call would re-resolve against package.json and
# strip out anything not recorded there — including the first install.
# So we save the library to package.json (closer to how a real host
# would consume it anyway).
if ! npm install --no-audit --no-fund \
    "file:$LIB_DIST" \
    @angular/cdk@19 \
    >> "$install_log" 2>&1; then
  tail -40 "$install_log"
  fail "library + cdk install failed — see $install_log"
fi
[ -d "$HOST_DIR/node_modules/@tcg/frontend" ] \
  || fail "node_modules/@tcg/frontend not present after install"
[ -d "$HOST_DIR/node_modules/@angular/cdk" ] \
  || fail "node_modules/@angular/cdk not present after install"
echo "[smoke] library + cdk installed into host node_modules"

# Surface any peer-dep warnings — these matter for a real host.
if grep -E 'npm warn ERESOLVE|npm warn peer dep' "$install_log" > /dev/null; then
  echo "[smoke] peer-dep warnings from npm:"
  grep -E 'npm warn ERESOLVE|npm warn peer dep' "$install_log" | sed 's/^/  /'
fi

# ----------------------------------------------------------------------
# Step 4: patch host app.config.ts and app.routes.ts to consume the
# library. This is the canonical host-consumption pattern documented in
# the porting plan.
# ----------------------------------------------------------------------
step "Patching host app.config.ts + app.routes.ts to consume @tcg/frontend"

cat > "$HOST_DIR/src/app/app.config.ts" <<'EOF'
import { ApplicationConfig, provideZoneChangeDetection } from '@angular/core';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';

import { TCG_API_BASE_URL } from '@tcg/frontend';

import { routes } from './app.routes';

export const appConfig: ApplicationConfig = {
  providers: [
    provideZoneChangeDetection({ eventCoalescing: true }),
    provideRouter(routes),
    provideHttpClient(),
    { provide: TCG_API_BASE_URL, useValue: 'http://localhost:8000' },
  ],
};
EOF

cat > "$HOST_DIR/src/app/app.routes.ts" <<'EOF'
import { Routes } from '@angular/router';
import { tcgRoutes } from '@tcg/frontend';

// Smoke host mounts every library route at the root, exactly how a real
// host would (or behind a `/tcg/` prefix child route).
export const routes: Routes = [
  ...tcgRoutes,
];
EOF

# Ng 19 `ng new` produces a root standalone component with a router-
# outlet template by default, but the boilerplate sometimes wraps the
# outlet inside the welcome page. Replace the template with a minimal
# outlet to avoid any extra imports being required at smoke-build time.
cat > "$HOST_DIR/src/app/app.component.html" <<'EOF'
<router-outlet></router-outlet>
EOF

# Component .ts also stripped down — bare RouterOutlet import, no extra
# component dependencies.
cat > "$HOST_DIR/src/app/app.component.ts" <<'EOF'
import { Component } from '@angular/core';
import { RouterOutlet } from '@angular/router';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [RouterOutlet],
  templateUrl: './app.component.html',
  styleUrl: './app.component.css',
})
export class AppComponent {
  title = 'tcg-smoke-host';
}
EOF

echo "[smoke] host wired to @tcg/frontend"

# ----------------------------------------------------------------------
# Step 5: build the host. This exercises the full library consumption
# path: types, FESM bundle, peer-dep resolution, lazy-chunk emission.
# ----------------------------------------------------------------------
step "Building smoke host (ng build)"
if ! npx ng build --configuration production > "$build_host_log" 2>&1; then
  tail -60 "$build_host_log"
  fail "host build failed — see $build_host_log"
fi

# Inspect build output for telltale lazy-chunks (Plotly) and TCG symbols.
host_dist="$HOST_DIR/dist/tcg-smoke-host"
[ -d "$host_dist" ] || fail "host build produced no dist directory"
echo "[smoke] host build artifacts:"
find "$host_dist" -type f -name '*.js' -printf '  %f  %s bytes\n' 2>/dev/null | sort | head -20

# ----------------------------------------------------------------------
# SUCCESS
# ----------------------------------------------------------------------
printf '\n[smoke] SUCCESS — @tcg/frontend installs cleanly and a fresh ng-new host builds against it.\n'
printf '[smoke] logs in %s\n' "$LOG_DIR"
exit 0
