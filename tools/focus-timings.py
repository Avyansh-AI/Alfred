#!/usr/bin/env python3
"""Print the focus sessions' field numbers, from a running server.

The knobs in focus.py are tuned from numbers measured on the machine that felt the
problem - never from memory. This is the tool that produces those numbers.

  python3 tools/focus-timings.py                       # against http://127.0.0.1:4700
  python3 tools/focus-timings.py --url http://127.0.0.1:4720 --window 8
  python3 tools/focus-timings.py --json                # the same numbers, for a script

What it prints is what the server knows: the knobs it is actually running with (from
/health), the live counters (from /focus), and a short reading of them. The reading is
deliberate about what a number does and does not justify - in particular it will not tell
you to touch GRACE_MS while the reader or the tick is the slow part.

Read the BAND line first. A drift is only counted on a tick that finds it already older
than the grace, so a callout lands somewhere between TICK_S and TICK_S + GRACE_MS after the
drift began - 1.0s to 1.8s at the defaults, depending on where in the second you wandered.
A callout can therefore never be instantaneous, and a late one is not automatically the
grace's fault: if callouts feel late, compare the field's detect_ms with that band first.

Only the standard library is used, and nothing is written.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:4700"
DEFAULT_WINDOW_S = 6.0            # long enough to see several ticks, short enough to be kind
POLL_S = 0.25
REQUEST_TIMEOUT_S = 5.0


# --------------------------------------------------------------------------- #
# talking to the server
# --------------------------------------------------------------------------- #
def get_json(url, path):
    """GET a JSON document. Returns (document, error) - never raises for an HTTP answer."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + path, timeout=REQUEST_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8")), ""
    except urllib.error.HTTPError as err:
        return None, "HTTP %s" % err.code
    except Exception as err:                                  # noqa: BLE001 - reported
        return None, "%s: %s" % (err.__class__.__name__, err)


def sample(url, window_s):
    """Watch the counters for a window. Returns a dict of what moved, and by how much."""
    first, err = get_json(url, "/focus")
    if first is None:
        return None, "the server did not answer GET /focus (%s)" % err
    started = time.monotonic()
    seen = [(started, first)]
    while time.monotonic() - started < window_s:
        time.sleep(POLL_S)
        doc, _ = get_json(url, "/focus")
        if doc is not None:
            seen.append((time.monotonic(), doc))
    elapsed = seen[-1][0] - seen[0][0]
    ticks = seen[-1][1]["timings"]["ticks"] - seen[0][1]["timings"]["ticks"]
    runs = seen[-1][1]["timings"]["reader_runs"] - seen[0][1]["timings"]["reader_runs"]
    return {"elapsed_s": elapsed, "ticks": ticks, "reader_runs": runs,
            "state": seen[-1][1], "samples": len(seen)}, ""


# --------------------------------------------------------------------------- #
# the reading
# --------------------------------------------------------------------------- #
def reading(state, health, window):
    """A sentence or two about what the numbers mean - and what they do not justify."""
    timings = state.get("timings", {})
    tick_s = float(state.get("tick_s") or (health or {}).get("tick_s") or 1.0)
    grace_ms = int(state.get("grace_ms") or (health or {}).get("grace_ms") or 0)
    earliest_ms = int(round(tick_s * 1000.0))
    latest_ms = earliest_ms + grace_ms
    lines = []

    if state.get("phase") == "idle" and not timings.get("ticks"):
        return ["nothing has been watched yet: not one tick has run, so there is nothing",
                "to measure. Start a session (the FOCUS button, or \"thirty minutes on",
                "this\") and run this again while it is running."]

    if window["ticks"]:
        spacing = window["elapsed_s"] / window["ticks"]
        lines.append("the tick is firing every %.2fs over the last %.1fs (%d tick(s))"
                     % (spacing, window["elapsed_s"], window["ticks"]))
        if abs(spacing - tick_s) > max(0.25, tick_s * 0.35):
            lines.append("  ^ that is not TICK_S = %.2fs, so the loop is being starved (load, or a"
                         " reader that overruns its own second)" % tick_s)
        if window["reader_runs"] < window["ticks"]:
            lines.append("  ^ and there were FEWER reader runs (%d) than ticks (%d): every tick"
                         " must take a FRESH query" % (window["reader_runs"], window["ticks"]))
    else:
        lines.append("no tick ran in the last %.1fs: the session is paused, has ended, or the"
                     " loop is stuck" % window["elapsed_s"])

    reader_max = int(timings.get("reader_ms_max") or 0)
    lag_max = int(timings.get("tick_lag_max_ms") or 0)
    detect = int(timings.get("detect_ms") or 0)
    callout = int(timings.get("callout_ms") or 0)
    nag_gap = int(timings.get("nag_gap_ms") or 0)

    if detect:
        lines.append("the last drift was counted when it was already %dms old, inside the band"
                     " TICK_S..TICK_S + GRACE_MS = %d..%dms, and called out at %dms"
                     % (detect, earliest_ms, latest_ms, callout or detect))
        if detect > latest_ms + max(250, grace_ms):
            lines.append("  ^ that is well past the band. Look at the reader and the lag below"
                         " BEFORE touching GRACE_MS: a slow reader looks exactly like a slow"
                         " grace, and only one of them is a knob worth moving")
    else:
        lines.append("no drift has been counted yet in this session, so there is no measured"
                     " callout delay to argue about")

    if reader_max > grace_ms:
        lines.append("the reader itself took up to %dms, which is longer than GRACE_MS = %dms:"
                     " no grace value can make a callout earlier than the read that finds it"
                     % (reader_max, grace_ms))
    if lag_max >= (1.0 + grace_ms / 1000.0) * 1000.0:
        lines.append("the worst tick lag is %dms, past a whole second plus the grace: the loop,"
                     " not the grace, is what makes a callout feel late" % lag_max)
    if nag_gap:
        lines.append("the gap between callouts was last measured at %dms (the cadence asked for"
                     " is %ss)" % (nag_gap, state.get("nag_s")))
    lines.append("GRACE_MS is the last knob to move: only when detect_ms sits in the band, the"
                 " reader is quick and the tick is on time, is a late callout the grace's fault")
    return lines


# --------------------------------------------------------------------------- #
# printing
# --------------------------------------------------------------------------- #
def report(url, window_s):
    health, health_err = get_json(url, "/health")
    focus_health = (health or {}).get("focus") if isinstance(health, dict) else None
    window, err = sample(url, window_s)
    if window is None:
        print("focus timings - %s" % url)
        print("  %s" % err)
        if health_err:
            print("  and /health said: %s" % health_err)
        return 2

    state = window["state"]
    timings = state.get("timings", {})
    tick_s = float(state.get("tick_s") or (focus_health or {}).get("tick_s") or 1.0)
    grace_ms = int(state.get("grace_ms") or (focus_health or {}).get("grace_ms") or 0)
    earliest_ms = int(round(tick_s * 1000.0))
    latest_ms = earliest_ms + grace_ms
    lines = reading(state, focus_health, window)

    if not focus_health and state.get("tick_s") is None:
        print("  ! /health has no focus block on this server: it may be older than focus "
              "sessions, or started without them")

    print("focus timings - %s" % url)
    print("  knobs        : tick %.2fs, grace %dms, nag %ss%s"
          % (tick_s, grace_ms, state.get("nag_s"),
             "" if focus_health else " (from /focus; /health has no focus block)"))
    print("  phase        : %s, reader %s%s"
          % (state.get("phase"), state.get("reader"),
             (", session %s" % state.get("id")) if state.get("id") else ""))
    print("  the band     : a callout lands between TICK_S and TICK_S + GRACE_MS = %d..%dms"
          " after a drift begins" % (earliest_ms, latest_ms))
    print("  the field    : detect %sms, callout %sms, nag gap %sms"
          % (timings.get("detect_ms", "-"), timings.get("callout_ms", "-"),
             timings.get("nag_gap_ms", "-")))
    print("  the reader   : %s run(s), %s fail(s), %sms avg, %sms worst"
          % (timings.get("reader_runs", "-"), timings.get("reader_fails", "-"),
             timings.get("reader_ms_avg", "-"), timings.get("reader_ms_max", "-")))
    print("  the tick     : %s tick(s) watched, %sms avg, %sms worst, lag %sms last / %sms worst"
          % (timings.get("ticks", "-"), timings.get("tick_ms_avg", "-"),
             timings.get("tick_ms_max", "-"), timings.get("tick_lag_ms", "-"),
             timings.get("tick_lag_max_ms", "-")))
    print("  this window  : %d sample(s) over %.1fs, %d tick(s), %d reader run(s)"
          % (window["samples"], window["elapsed_s"], window["ticks"], window["reader_runs"]))
    if focus_health:
        print("  the record   : %s session(s), %s clean, streak %s (best %s), ledger %s"
              % (focus_health.get("sessions"), focus_health.get("clean_sessions"),
                 focus_health.get("streak"), focus_health.get("best_streak"),
                 "on" if focus_health.get("ledger") else "off"))
    print("  and what it means:")
    for line in lines:
        print("    %s" % line)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Print the focus sessions' field numbers.")
    parser.add_argument("--url", default=DEFAULT_URL, help="the server to ask (default %(default)s)")
    parser.add_argument("--window", type=float, default=DEFAULT_WINDOW_S,
                        help="seconds to watch the counters for (default %(default)s)")
    parser.add_argument("--json", action="store_true", help="print the numbers as JSON")
    args = parser.parse_args()
    if args.json:
        window, err = sample(args.url, args.window)
        health, _ = get_json(args.url, "/health")
        if window is None:
            print(json.dumps({"ok": False, "error": err}))
            return 2
        print(json.dumps({"ok": True, "window": {k: v for k, v in window.items() if k != "state"},
                          "state": window["state"],
                          "health": (health or {}).get("focus")}, indent=2, sort_keys=True))
        return 0
    return report(args.url, args.window)


if __name__ == "__main__":
    raise SystemExit(main())
