#!/usr/bin/env bash
# Host-coexistence audit for @tcg/frontend.
#
# Runs G3-G8 structural checks against projects/tcg-frontend/. Each
# check prints PASS or FAIL: <evidence>. Final summary line: "Total
# PASS=<n> FAIL=<n>". Exits non-zero on any FAIL.
#
# Run from the frontend-angular workspace root:
#   bash tools/host-coexistence-audit.sh
#
# Guardrails enforced:
#   G3: no BrowserModule / RouterModule.forRoot / provideRouter in
#       library code; no library-scoped styles in angular.json.
#   G4: every public-api export prefixed Tcg / TCG_ / tcg<Cap>.
#   G5: only TcgApiService is providedIn: 'root'.
#   G6: no hardcoded http://, localhost, 127.0.0.1 in library code
#       outside .spec.ts and doc-comment lines.
#   G7: @angular/* in peerDependencies only; no @angular/* in
#       dependencies. peerDependencies contains the documented set.
#   G8: zero @NgModule decorators; @Component count == standalone: true
#       count.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LIB_SRC="$WORKSPACE_ROOT/projects/tcg-frontend/src"
LIB_PKG="$WORKSPACE_ROOT/projects/tcg-frontend/package.json"
ANGULAR_JSON="$WORKSPACE_ROOT/angular.json"
PUBLIC_API="$LIB_SRC/public-api.ts"

PASS=0
FAIL=0

pass() {
  printf '  PASS  %s\n' "$1"
  PASS=$((PASS + 1))
}

fail() {
  printf '  FAIL  %s\n' "$1"
  FAIL=$((FAIL + 1))
}

section() {
  printf '\n[%s] %s\n' "$1" "$2"
}

# ---------------------------------------------------------------------
# G3 — no BrowserModule / forRoot / provideRouter in library code,
# no library-scoped styles in angular.json.
# ---------------------------------------------------------------------
section G3 "Library does not bootstrap routing / global modules"

# Greps that exclude lines starting with '*' (jsdoc) or '//' (line comment).
g3_hits() {
  local pattern="$1"
  grep -RnE "$pattern" "$LIB_SRC" --include='*.ts' 2>/dev/null \
    | awk -F: '{
        # Reconstruct content (everything after first 2 colons).
        line = $0
        sub(/^[^:]*:[^:]*:/, "", line)
        # Strip leading whitespace.
        sub(/^[ \t]+/, "", line)
        # Skip JSDoc continuation lines and pure line comments.
        if (line ~ /^\*/) next
        if (line ~ /^\/\//) next
        # Skip empty.
        if (line == "") next
        print
      }'
}

g3_browser=$(g3_hits 'BrowserModule' || true)
if [ -z "$g3_browser" ]; then
  pass "no BrowserModule references in library code"
else
  fail "BrowserModule referenced: $(echo "$g3_browser" | head -3 | tr '\n' '|')"
fi

g3_forroot=$(g3_hits 'RouterModule\.forRoot' || true)
if [ -z "$g3_forroot" ]; then
  pass "no RouterModule.forRoot calls in library code"
else
  fail "RouterModule.forRoot referenced: $(echo "$g3_forroot" | head -3 | tr '\n' '|')"
fi

g3_provideRouter=$(g3_hits 'provideRouter' || true)
if [ -z "$g3_provideRouter" ]; then
  pass "no provideRouter calls in library code"
else
  fail "provideRouter referenced: $(echo "$g3_provideRouter" | head -3 | tr '\n' '|')"
fi

# angular.json library styles: ensure no styles array entries point into
# projects/tcg-frontend/ (the library has no build target with `styles`
# in this workspace; the existing styles entries point at the dev-harness).
if [ -f "$ANGULAR_JSON" ]; then
  bad_styles=$(grep -nE '"projects/tcg-frontend/' "$ANGULAR_JSON" | grep -Ei '\.css"|\.scss"' || true)
  if [ -z "$bad_styles" ]; then
    pass "angular.json has no styles entries pointing into the library"
  else
    fail "angular.json references library stylesheets: $bad_styles"
  fi
else
  fail "angular.json not found at $ANGULAR_JSON"
fi

# ---------------------------------------------------------------------
# G4 — every public-api export prefixed Tcg / TCG_ / tcg<Cap>.
# ---------------------------------------------------------------------
section G4 "Every public-api export is prefixed"

if [ ! -f "$PUBLIC_API" ]; then
  fail "public-api.ts not found at $PUBLIC_API"
else
  # Extract identifiers from `export { Foo, Bar as Baz }` and
  # `export type { Qux }` lines (single or multi-line braces). We
  # collapse the file by stripping newlines inside export-braces.
  symbols=$(awk '
    BEGIN { buf = ""; in_export = 0 }
    {
      line = $0
      # Skip pure-comment lines.
      if (line ~ /^[ \t]*\/\//) next
      if (line ~ /^[ \t]*\*/) next
      if (line ~ /^[ \t]*\/\*/) next
      if (in_export) {
        buf = buf " " line
        if (line ~ /}/) {
          print buf
          buf = ""
          in_export = 0
        }
        next
      }
      if (line ~ /^[ \t]*export[ \t]+(type[ \t]+)?\{/) {
        buf = line
        if (line ~ /}/) {
          print buf
          buf = ""
        } else {
          in_export = 1
        }
      }
    }
  ' "$PUBLIC_API" \
    | sed -E 's/.*\{(.*)\}.*/\1/' \
    | tr ',' '\n' \
    | sed -E 's/[[:space:]]+/ /g; s/^[[:space:]]+//; s/[[:space:]]+$//' \
    | awk '/^$/ {next} {
        # If "X as Y", take Y; else take X.
        n = split($0, parts, /[[:space:]]+as[[:space:]]+/)
        if (n == 2) print parts[2]; else print parts[1]
      }' \
    | sort -u)

  if [ -z "$symbols" ]; then
    fail "could not extract any exports from public-api.ts"
  else
    bad=$(echo "$symbols" | grep -vE '^(Tcg[A-Z0-9_]|TCG_|tcg[A-Z])' || true)
    if [ -z "$bad" ]; then
      total=$(echo "$symbols" | wc -l)
      pass "all $total public-api exports prefixed (Tcg* / TCG_* / tcg<Cap>)"
    else
      fail "unprefixed export(s): $(echo "$bad" | tr '\n' ' ')"
    fi
  fi
fi

# ---------------------------------------------------------------------
# G5 — only TcgApiService is providedIn: 'root'.
# ---------------------------------------------------------------------
section G5 "Only TcgApiService is providedIn: 'root'"

root_hits=$(grep -RnE "providedIn:[[:space:]]*'root'" "$LIB_SRC" --include='*.ts' \
  | awk -F: '{
      content = $0
      sub(/^[^:]*:[^:]*:/, "", content)
      sub(/^[ \t]+/, "", content)
      if (content ~ /^\*/) next
      if (content ~ /^\/\//) next
      print
    }' || true)

root_count=$(echo "$root_hits" | grep -c . || true)

if [ "$root_count" = "1" ] && echo "$root_hits" | grep -q "tcg-api.service.ts"; then
  pass "single providedIn: 'root' (TcgApiService): $root_hits"
else
  fail "expected 1 providedIn: 'root' on TcgApiService, found $root_count: $root_hits"
fi

# ---------------------------------------------------------------------
# G6 — no hardcoded API URLs in library code (excluding *.spec.ts and
# doc-comment lines starting with '*').
# ---------------------------------------------------------------------
section G6 "No hardcoded http://, localhost, 127.0.0.1 in library code"

g6_hits=$(grep -RnE 'http://|localhost|127\.0\.0\.1' "$LIB_SRC" \
  --include='*.ts' \
  --exclude='*.spec.ts' 2>/dev/null \
  | awk -F: '{
      file = $1
      content = $0
      sub(/^[^:]*:[^:]*:/, "", content)
      sub(/^[ \t]+/, "", content)
      # Exclude jsdoc continuation lines and pure line comments.
      if (content ~ /^\*/) next
      if (content ~ /^\/\//) next
      # Exclude lines that are purely inside a /** block opener/closer.
      if (content ~ /^\/\*\*/) next
      if (content ~ /^\*\//) next
      print
    }' || true)

if [ -z "$g6_hits" ]; then
  pass "no hardcoded URLs in non-spec library code (doc comments excluded)"
else
  fail "hardcoded URL(s) found: $(echo "$g6_hits" | head -3 | tr '\n' '|')"
fi

# ---------------------------------------------------------------------
# G7 — @angular/* in peerDependencies only.
# ---------------------------------------------------------------------
section G7 "Angular runtime in peerDependencies only"

if [ ! -f "$LIB_PKG" ]; then
  fail "library package.json not found at $LIB_PKG"
else
  # Use python (always available in this env) for safer JSON parsing.
  py_out=$(python3 - "$LIB_PKG" <<'PY'
import json, sys
pkg = json.load(open(sys.argv[1]))
deps = pkg.get("dependencies", {})
peers = pkg.get("peerDependencies", {})
ng_in_deps = [k for k in deps if k.startswith("@angular/")]
required_peers = {"@angular/core", "@angular/common", "@angular/router", "@angular/forms", "@angular/cdk", "rxjs"}
missing = sorted(required_peers - set(peers))
print("ng_in_deps:", ",".join(ng_in_deps) if ng_in_deps else "none")
print("missing_peers:", ",".join(missing) if missing else "none")
PY
)
  echo "  $py_out" | sed 's/^/  /'
  ng_in_deps_line=$(echo "$py_out" | grep '^ng_in_deps:' | sed 's/^ng_in_deps: //')
  missing_line=$(echo "$py_out" | grep '^missing_peers:' | sed 's/^missing_peers: //')

  if [ "$ng_in_deps_line" = "none" ]; then
    pass "no @angular/* in dependencies"
  else
    fail "@angular/* in dependencies: $ng_in_deps_line"
  fi

  if [ "$missing_line" = "none" ]; then
    pass "peerDependencies includes core/common/router/forms/cdk/rxjs"
  else
    fail "missing peerDependencies: $missing_line"
  fi
fi

# ---------------------------------------------------------------------
# G8 — standalone components only.
# ---------------------------------------------------------------------
section G8 "Standalone components only"

ngmodule_hits=$(grep -RnE '@NgModule' "$LIB_SRC" --include='*.ts' 2>/dev/null \
  | awk -F: '{
      content = $0
      sub(/^[^:]*:[^:]*:/, "", content)
      sub(/^[ \t]+/, "", content)
      if (content ~ /^\*/) next
      if (content ~ /^\/\//) next
      print
    }' || true)

if [ -z "$ngmodule_hits" ]; then
  pass "zero @NgModule decorators in library"
else
  fail "@NgModule decorator(s) found: $(echo "$ngmodule_hits" | head -3 | tr '\n' '|')"
fi

# Component / standalone parity. We count component files (one
# @Component per file in this library) and standalone-true marker lines.
component_files=$(grep -RlE '@Component\b' "$LIB_SRC" --include='*.ts' 2>/dev/null \
  | grep -v '\.spec\.ts$' | sort -u)
n_components=$(echo "$component_files" | grep -c . || true)
n_standalone=$(grep -RlE 'standalone:[[:space:]]*true' "$LIB_SRC" --include='*.ts' 2>/dev/null \
  | grep -v '\.spec\.ts$' | sort -u | wc -l)

if [ "$n_components" = "$n_standalone" ] && [ "$n_components" -gt 0 ]; then
  pass "$n_components @Component file(s) == $n_standalone standalone:true file(s)"
else
  fail "parity mismatch: @Component=$n_components vs standalone:true=$n_standalone"
fi

# ---------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------
printf '\n================================================================\n'
printf 'Summary: PASS=%d  FAIL=%d\n' "$PASS" "$FAIL"
printf '================================================================\n'

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
