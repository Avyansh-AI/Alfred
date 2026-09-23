#!/usr/bin/env bash
# tools/verify.sh - rebuild, then verify everything that can be verified offline.
#
#   ./tools/verify.sh
#
set -uo pipefail
cd "$(dirname "$0")/.."

pass=0; fail=0
step () { printf '\n\033[1m== %s\033[0m\n' "$1"; }
mark () { if [ "$1" -eq 0 ]; then echo "   -> ok"; pass=$((pass+1)); else echo "   -> FAILED"; fail=$((fail+1)); fi; }

step "indexing the notes (build.py --check)"
python3 build.py --check; mark $?

step "viewer/index.html headless verification (node tools/verify.mjs)"
if command -v node >/dev/null 2>&1; then node tools/verify.mjs | tail -6; mark ${PIPESTATUS[0]}; else echo "   -> skipped (node not installed)"; fi

step "server + brain end-to-end (python3 tools/verify.py)"
python3 tools/verify.py | tail -8; mark ${PIPESTATUS[0]}

step "real browser render + interactions (node tools/browser-check.mjs)"
if [ "${SKIP_BROWSER:-0}" = "1" ]; then
  echo "   -> skipped (SKIP_BROWSER=1)"
elif ! command -v node >/dev/null 2>&1; then
  echo "   -> skipped (node not installed)"
else
  node tools/browser-check.mjs 2>&1 | tail -14
  code=${PIPESTATUS[0]}
  [ "$code" -eq 2 ] && echo "   -> skipped (no browser available; see tools/browser-check.mjs header)"
  mark $([ "$code" -eq 2 ] && echo 0 || echo "$code")
fi

printf '\n\033[1m== summary\033[0m\n%d groups passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
echo "
Everything checks out. Start the server with:  python3 server.py"
