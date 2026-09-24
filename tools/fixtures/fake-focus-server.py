#!/usr/bin/env python3
"""A focus server that answers over HTTP and is wrong on purpose.

This exists so that preflight.py's focus check can be proven to FAIL, not just to pass:
a check nobody has ever seen fail is a check that might be checking nothing. It is used by
tools/verify-preflight.py, which runs preflight against each mode and asserts the failure
it is supposed to produce.

  python3 tools/fixtures/fake-focus-server.py <mode> <port>

modes:
  good     a minimal server that behaves: the clock ticks and the reader is asked every time
             -> preflight check 14 must PASS
  frozen   the session starts, but the clock never moves and no tick ever runs
             -> the cached-notification failure in its purest form (a long-lived server
                that goes on answering while nothing is being watched)
  nocache  the ticks advance, but the reader is never asked again
             -> "reports the first app forever"
  leaky    everything works, but the state carries a host in a string
             -> the privacy promise, broken

FAKE_UPTIME_S=<seconds> makes /health claim the process has been up that long, which is how
preflight's check 10 ("the running server is not older than the code it runs") is proven to
notice a stale process without waiting for one.

Nothing here is imported by the project, and nothing here talks to anything real: it is a
few dozen lines of stdlib http.server with a deliberately wrong tick.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = sys.argv[1] if len(sys.argv) > 1 else "good"
if MODE not in ("good", "frozen", "nocache", "leaky"):
    raise SystemExit("unknown mode %r: good, frozen, nocache or leaky" % MODE)
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 4721
LOCK = threading.Lock()

STATE = {
    "ok": True, "phase": "idle", "id": 0,
    "tick_s": 1.0, "grace_ms": 800, "nag_s": 30.0, "remaining_s": 0.0,
    "reader": "live", "reader_why": "ok", "reason": "none", "drifting": False,
    "timings": {"ticks": 0, "reader_runs": 0},
}
FAKE_UPTIME_S = os.environ.get("FAKE_UPTIME_S")
HEALTH = {"ok": True, "notes": 12, "model": "gpt-6-astra",
          "focus": {"phase": "idle", "can_see": True, "reader": "command", "ledger": False,
                    "tick_s": 1.0, "grace_ms": 800, "nag_s": 30.0, "sessions": 0,
                    "clean_sessions": 0, "streak": 0, "best_streak": 0}}
# preflight's check 10 compares the age of the process with the mtime of the files it runs.
# Claiming an age is how that check is proven to fire without waiting two hours for it.
# A stub with no claim says nothing, and the check says so instead of guessing.
if FAKE_UPTIME_S is not None:
    HEALTH["uptime_s"] = float(FAKE_UPTIME_S)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            return self._send(HEALTH)
        if path == "/focus":
            with LOCK:
                return self._send(dict(STATE))
        return self._send({"ok": False, "error": "nothing here"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        if self.path.split("?")[0] != "/focus":
            return self._send({"ok": False, "error": "nothing here"}, 404)
        text = str(body.get("text") or body.get("action") or "").lower()
        with LOCK:
            if "start" in text or "minutes" in text:
                STATE.update({"phase": "running", "id": 7, "remaining_s": 1800.0,
                              "timings": {"ticks": 0, "reader_runs": 0}})
                if MODE == "leaky":
                    STATE["reader_why"] = "ok, watching work.example.com"
                return self._send({"ok": True, "code": "focus_started", "answer": "Started.",
                                   "focus": dict(STATE)})
            if "end" in text or "abort" in text:
                STATE.update({"phase": "ended", "remaining_s": 0.0})
                return self._send({"ok": True, "code": "focus_ended", "answer": "That was a poke.",
                                   "report": {"counted": False},
                                   "focus": dict(STATE)})
        return self._send({"ok": True, "code": "focus_state", "answer": "", "focus": dict(STATE)})


def tick():
    """The tick, as a broken server does it: `nocache` never asks the reader again."""
    while True:
        threading.Event().wait(1.0)
        with LOCK:
            if STATE["phase"] != "running":
                continue
            if MODE == "frozen":
                continue                                  # the clock does not move at all
            STATE["timings"]["ticks"] += 1
            STATE["timings"]["reader_runs"] += 1
            STATE["remaining_s"] = max(0.0, STATE["remaining_s"] - 1.0)
            if MODE == "nocache":
                STATE["timings"]["reader_runs"] = 1       # asked once, then never again


threading.Thread(target=tick, daemon=True).start()
ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
