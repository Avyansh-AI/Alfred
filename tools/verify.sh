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

step "voice logic: speaking, listening, the finish window, ?mute=1 (node tools/verify-voice.mjs)"
if command -v node >/dev/null 2>&1; then node tools/verify-voice.mjs | tail -6; mark ${PIPESTATUS[0]}; else echo "   -> skipped (node not installed)"; fi

step "growing the brain by voice: filing, gluing, glowing, failing loudly (node tools/verify-capture.mjs)"
if command -v node >/dev/null 2>&1; then node tools/verify-capture.mjs | tail -6; mark ${PIPESTATUS[0]}; else echo "   -> skipped (node not installed)"; fi

step "provenance logic: does the galaxy show where an answer came from (node tools/verify-provenance.mjs)"
if command -v node >/dev/null 2>&1; then node tools/verify-provenance.mjs | tail -6; mark ${PIPESTATUS[0]}; else echo "   -> skipped (node not installed)"; fi

step "sight logic: the held share, the loud indicator, one frame at the ask (node tools/verify-sight.mjs)"
if command -v node >/dev/null 2>&1; then node tools/verify-sight.mjs | tail -6; mark ${PIPESTATUS[0]}; else echo "   -> skipped (node not installed)"; fi

step "changing the brain: the chip, the routing, and the refusal rule (node tools/verify-brain.mjs)"
if command -v node >/dev/null 2>&1; then node tools/verify-brain.mjs | tail -6; mark ${PIPESTATUS[0]}; else echo "   -> skipped (node not installed)"; fi

step "focus sessions: the card, the noise, and the ticks (node tools/verify-focus.mjs)"
if command -v node >/dev/null 2>&1; then node tools/verify-focus.mjs | tail -6; mark ${PIPESTATUS[0]}; else echo "   -> skipped (node not installed)"; fi

step "preflight's focus check can fail: four servers, three wrong on purpose (python3 tools/verify-preflight.py)"
python3 tools/verify-preflight.py | tail -6; mark ${PIPESTATUS[0]}

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

  step "real browser voice check: spoken answers, mic, pause window, mute tab (node tools/browser-voice-check.mjs)"
  node tools/browser-voice-check.mjs 2>&1 | tail -14
  code=${PIPESTATUS[0]}
  [ "$code" -eq 2 ] && echo "   -> skipped (no browser available; see tools/browser-check.mjs header)"
  mark $([ "$code" -eq 2 ] && echo 0 || echo "$code")

  step "real browser capture: write a note, watch the star be born, ask about it"
  node tools/browser-capture-check.mjs 2>&1 | tail -16
  code=${PIPESTATUS[0]}
  [ "$code" -eq 2 ] && echo "   -> skipped (no browser available; see tools/browser-check.mjs header)"
  mark $([ "$code" -eq 2 ] && echo 0 || echo "$code")

  step "real browser sight: a real screen share, a real frame, the answer spoken (node tools/browser-sight-check.mjs)"
  node tools/browser-sight-check.mjs 2>&1 | tail -16
  code=${PIPESTATUS[0]}
  [ "$code" -eq 2 ] && echo "   -> skipped (no browser available; see tools/browser-check.mjs header)"
  mark $([ "$code" -eq 2 ] && echo 0 || echo "$code")

  step "real browser brain: switch, refuse a version that does not exist, restart (node tools/browser-brain-check.mjs)"
  node tools/browser-brain-check.mjs 2>&1 | tail -16
  code=${PIPESTATUS[0]}
  [ "$code" -eq 2 ] && echo "   -> skipped (no browser available; see tools/browser-check.mjs header)"
  mark $([ "$code" -eq 2 ] && echo 0 || echo "$code")

  step "real browser provenance: one note flies, a cluster lights, small talk holds still"
  node tools/browser-provenance-check.mjs 2>&1 | tail -18
  code=${PIPESTATUS[0]}
  [ "$code" -eq 2 ] && echo "   -> skipped (no browser available; see tools/browser-check.mjs header)"
  mark $([ "$code" -eq 2 ] && echo 0 || echo "$code")
fi

printf '\n\033[1m== summary\033[0m\n%d groups passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
echo "
Everything checks out. Start the server with:  python3 server.py"
