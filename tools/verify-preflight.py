#!/usr/bin/env python3
"""prove that preflight.py's focus check can FAIL, not just pass.

A check nobody has ever seen fail is a check that might be checking nothing at all - which
is how the tick of a long-lived server can go on answering while nothing is watched, or a
reader can go on reporting the first app forever, with every suite green. So preflight's
check 14 is run here against four deliberately-built servers, three of them wrong on purpose
(tools/fixtures/fake-focus-server.py), and each failure has to be the RIGHT one:

    good     -> check 14 passes
    frozen   -> fails, and says the clock is not moving
    nocache  -> fails, and says the front app is not queried fresh
    leaky    -> fails, and says the state carries a name

Nothing is imported from the project: preflight.py is run as a subprocess over HTTP, exactly
as a person runs it. Only the standard library is used.

    python3 tools/verify-preflight.py

Exit code is 0 when all four behave as they must, 1 otherwise.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PREFLIGHT = os.path.join(ROOT, "preflight.py")
FAKE = os.path.join(HERE, "fixtures", "fake-focus-server.py")
CHECK = "14."
BOOT_TIMEOUT_S = 12.0
PREFLIGHT_TIMEOUT_S = 120.0

# mode -> (the mark check 14 must carry, a phrase its detail must contain, and why)
CASES = (
    ("good", "pass", "ticked",
     "a server that ticks, asks the reader every second, and keeps its state clean passes"),
    ("frozen", "fail", "CLOCK IS NOT MOVING",
     "a server whose session never ticks is caught - the failure that has no error message"),
    ("nocache", "fail", "NOT BEING QUERIED FRESH",
     "a server that reports the first app forever is caught"),
    ("leaky", "fail", "NAMES YOU",
     "a state that carries a host in a string is caught"),
)

pass_ = 0
fail = 0


def ok(message):
    global pass_
    pass_ += 1
    print("  ok    %s" % message)


def bad(message):
    global fail
    fail += 1
    print("  FAIL  %s" % message)


def check(condition, message):
    ok(message) if condition else bad(message)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(port, limit=BOOT_TIMEOUT_S):
    """Block until the stub answers /health, or give up. Returns True when it is up."""
    deadline = time.time() + limit
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=2):
                return True
        except Exception:                                        # noqa: BLE001 - not up yet
            time.sleep(0.15)
    return False


def run_preflight(port):
    """Run preflight against the stub. Returns (check 14's record, the whole document)."""
    process = subprocess.run(
        [sys.executable, PREFLIGHT, "--url", "http://127.0.0.1:%d" % port,
         "--no-swap", "--json", "--timeout", "5", "--no-color"],
        cwd=ROOT, capture_output=True, text=True, timeout=PREFLIGHT_TIMEOUT_S)
    try:
        document = json.loads(process.stdout)
    except ValueError:
        return None, {"stdout": process.stdout[-800:], "stderr": process.stderr[-800:]}
    for record in document.get("checks") or []:
        if str(record.get("check") or "").startswith(CHECK):
            return record, document
    return None, document


def main():
    print("\nAlfred - preflight's focus check, against servers that are wrong on purpose")
    if not os.path.exists(FAKE):
        print("  the fixture is missing: %s" % FAKE)
        return 1
    for mode, want_mark, want_phrase, why in CASES:
        port = free_port()
        stub = subprocess.Popen([sys.executable, FAKE, mode, str(port)],
                                cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            if not wait_for(port):
                bad("%s: the fixture never came up on port %d" % (mode, port))
                continue
            record, document = run_preflight(port)
            if record is None:
                bad("%s: preflight did not report check 14 at all (%s)"
                    % (mode, json.dumps(document)[:200]))
                continue
            detail = str(record.get("detail") or "")
            mark = record.get("mark")
            check(mark == want_mark, "%s: check 14 is %s (%s)" % (mode, mark, why))
            if mark == want_mark:
                check(want_phrase in detail, "%s: and it says why: %r" % (mode, detail[:110]))
                check(any(want_phrase in str(n) or want_phrase in detail
                          for n in (record.get("notes") or [])
                          or [detail]),
                      "%s: the reason is on the report, not only in the mark" % mode)
        finally:
            stub.terminate()
            try:
                stub.wait(timeout=5)
            except subprocess.TimeoutExpired:
                stub.kill()

    # and the check must not be fooled by a session that is already running: preflight must
    # leave a person's own session alone
    port = free_port()
    stub = subprocess.Popen([sys.executable, FAKE, "good", str(port)],
                            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if wait_for(port):
            request = urllib.request.Request(
                "http://127.0.0.1:%d/focus" % port, method="POST",
                data=json.dumps({"text": "thirty minutes on this"}).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=5) as response:
                started = json.loads(response.read().decode("utf-8"))
            check(started.get("ok") is True, "a session is running before preflight is called")
            record, _doc = run_preflight(port)
            check(record is not None and record.get("mark") == "warn",
                  "preflight reports it and does not touch it: %s"
                  % (record or {}).get("detail"))
            with urllib.request.urlopen("http://127.0.0.1:%d/focus" % port, timeout=5) as response:
                after = json.loads(response.read().decode("utf-8"))
            check(after.get("phase") == "running" and after.get("id") == 7,
                  "and the session is exactly where it was: %s id %s"
                  % (after.get("phase"), after.get("id")))
    finally:
        stub.terminate()
        try:
            stub.wait(timeout=5)
        except subprocess.TimeoutExpired:
            stub.kill()

    print("\n%d checks, %d passed, %d failed" % (pass_ + fail, pass_, fail))
    if fail:
        print("\n  RESULT: FAILED - preflight's focus check is not catching what it must")
        return 1
    print("\n  RESULT: PASSED - the focus check passes a good server and fails each broken one,\n"
          "  with the right reason out loud")
    return 0


if __name__ == "__main__":
    sys.exit(main())
