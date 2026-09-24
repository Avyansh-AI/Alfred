#!/usr/bin/env python3
"""
focus.py - focus sessions: an accountability timer that watches where you actually are.

Say **"thirty minutes on this"** (or press FOCUS in the viewer) and a session starts,
locked to the app in front of you. The session lives HERE, on the server, on a one-second
tick - so a reloaded tab rejoins it, a closed tab does not end it, and the countdown is
computed from a wall clock rather than counted in a browser.

THE TARGET
    The lock is the frontmost app's key (a bundle ID on macOS), and - when that app is a
    Chrome-family browser - the active TAB, locked by the HASH OF THE URL'S HOST and never
    the full URL. Your work site is a single-page app whose path changes with every click;
    site level is the honest granularity, and a path change must never read as a drift.

    The frontmost app is read with a FRESH QUERY EVERY TICK, from a command-line tool.
    Never a cached notification API: those freeze in a long-lived headless process and go
    on reporting the first app they ever saw, which is worse than useless in an
    accountability timer - it is confidently wrong. `Reader.runs` counts the subprocess
    launches, and the tests compare that count with the tick count.

PRIVACY, STRUCTURALLY
    Identities are compared and DISCARDED inside the reader. The app key and the host are
    hashed the moment they are parsed, the two hashes are compared with the lock, and the
    per-tick observation dies at the end of that function - there is nowhere for it to
    live. What leaves the reader is a verdict of booleans, and what the client is ever
    shown is a WHITELIST: booleans, counters, and a handful of enum words
    (`PHASES`, `REASONS`, `READER_STATES`, `READER_WHY`). `check_public_state()` enforces
    that, `check_line_templates()` enforces that no spoken line can hold anything but
    numbers, and `tools/verify.py` feeds distinctive secrets through a live session and
    then looks for them in the state, the health payload, the server log and the ledger.

    The only two identities that exist anywhere in this process are the lock (two hashes)
    and the reader's short memory of the last place you were in that was NOT his own tab
    (see REMEMBER_LAST_PLACE_S). Both live inside the Reader object, both are cleared when
    the session ends, and neither is ever serialised.

DRIFT
    Grace, then a spoken callout from three tiers of canned lines that escalate as one
    excursion drags on; a nag cadence while it persists, settable by voice; a snooze
    ("give me fifteen seconds") that buys quiet without buying forgiveness; an excuse
    ("it's okay, I'm doing research") that refunds the whole excursion and stays quiet
    until you are back. Pause, resume, extend and end all work by voice. The Jarvis tab
    itself is HOME BASE: coming back to talk to him is never a drift.

EVERY KNOB IS A NAMED CONSTANT, AT THE TOP OF THIS FILE, with the field number that
chose it where there is one. Tune them from measurements, never from memory:
`tools/focus-timings.py` prints the live tick numbers, and if a callout feels late, read
that before touching GRACE_MS.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import subprocess
import threading
import time
import urllib.parse

# --------------------------------------------------------------------------- #
# THE KNOBS
# --------------------------------------------------------------------------- #

TICK_S = 1.0                    # the tick. One second, by requirement, not by taste
GRACE_MS = 800                  # a switch shorter than this is a flick, not a drift
READER_TIMEOUT_MS = 1500        # one read may take this long before it is a failed read
TICK_GAP_MAX_S = 5.0            # the most one tick may count. A laptop that slept for eight
                                # hours did not spend eight hours in the wrong app, and a
                                # session that freezes while he could not see is honest
READER_FAIL_PATIENCE = 3        # consecutive failed reads before he admits he is blind
READER_LOG_EVERY_S = 30.0       # while blind, say so at most this often

TIER_2_AFTER_S = 20.0           # an excursion this old moves to tier 2
TIER_3_AFTER_S = 60.0           # and this old, to tier 3
NAG_S = 30.0                    # repeat the callout every this many seconds, same drift
NAG_MIN_S = 10.0                # what "call me out every X" is clamped to
NAG_MAX_S = 600.0
SNOOZE_S = 15.0                 # "give me fifteen seconds" with no number in it
SNOOZE_MIN_S = 5.0
SNOOZE_MAX_S = 120.0

MIN_MINUTES = 0.1               # a session shorter than this is not a session
MAX_MINUTES = 240.0
DEFAULT_MINUTES = 30.0          # what the FOCUS button starts

CLEAN_PCT = 85.0                # a session at least this clean grows the streak
CLEAN_PCT_BASE = 1.0            # with almost no time on the clock, anything is "clean"

HOST_HASH_CHARS = 24            # how much of the SHA-256 of a host is kept
REMEMBER_LAST_PLACE_S = 120.0   # how long the reader remembers the last place that was
                                # not his own tab, so "on this" said TO him means the
                                # work you were in a moment ago. 0 disables the memory

CALLOUT_REPLAY_S = 6.0          # a callout this fresh is still delivered to a page that
                                # has just reloaded; older ones are not replayed out loud
CALLOT_KEEP = 40                # callouts kept for delivery (bounded)
MAX_EXCURSIONS = 4000           # counter list bound for a very long session

LEDGER_PATH = "focus-ledger.json"   # aggregates only. See LEDGER_FIELDS
LEDGER_MIN_SESSION_S = 30.0     # a session shorter than this was a poke, not a sitting,
                                # and it does not go in the ledger at all
LEDGER_FIELDS = ("sessions", "clean_sessions", "streak", "best_streak",
                 "minutes_on", "minutes_planned", "drifts", "refunds")
LEDGER_TMP = "focus-ledger.json.tmp"

# The frontmost-app query. FRESH EVERY TICK - it is launched, read and thrown away.
#
# What it must print (the whole protocol):
#   line 1 : the frontmost app's key (macOS: the bundle identifier)
#   line 2 : optional - the active tab's URL, only for a Chrome-family browser
# Anything else is ignored. A non-zero exit, no output, or no first line is a FAILED
# read, and a failed read never becomes a drift: he says he cannot see rather than
# guessing. Point --focus-reader at your own script for any platform this does not
# cover; the default needs macOS, because bundle IDs and Chrome's active tab are macOS's.
READER_COMMAND_MACOS = (
    "osascript", "-e",
    r'''
set theApp to ""
set theName to ""
tell application "System Events"
  set frontProc to first application process whose frontmost is true
  try
    set theApp to bundle identifier of frontProc
  end try
  try
    set theName to name of frontProc
  end try
end tell
if theApp is "" then set theApp to theName
set theURL to ""
try
  if theApp is in {"com.google.Chrome", "com.google.Chrome.canary", "com.google.Chrome.beta", "com.google.Chrome.dev", "com.microsoft.edgemac", "com.microsoft.edgemac.Dev", "com.brave.Browser", "com.brave.Browser.beta", "com.vivaldi.Vivaldi", "company.thebrowser.Browser", "com.operasoftware.Opera", "com.pushplaylabs.sidekick"} then
    tell application theName to set theURL to URL of active tab of front window
  end if
end try
return theApp & linefeed & theURL
''',
)
# Any other platform needs --focus-reader (or the env var, which is the same knob).
READER_COMMAND = os.environ.get("FOCUS_READER") or READER_COMMAND_MACOS

# Hosts that ARE home base: his own tab. Coming back to talk to him is never a drift.
HOME_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")

# --------------------------------------------------------------------------- #
# THE LINES (the character, in one place, so no other file spells a sentence)
# --------------------------------------------------------------------------- #
# Every placeholder below is checked against ALLOWED_LINE_FIELDS by
# check_line_templates(): numbers, enum words and nothing else. That is why no line can
# ever carry an app or a host - there is no field for one.
ALLOWED_LINE_FIELDS = frozenset((
    "minutes", "on", "planned", "pct", "drifts", "refunds", "streak", "seconds",
    "off", "added", "n", "verb", "drift_word", "refund_word", "left",
    # composed, and only ever from FOCUS_STREAK_UP / FOCUS_STREAK_HELD / FOCUS_STREAK_LOST,
    # all three of which are checked by this same function
    "streak_line",
))

FOCUS_START_LINE = ("{minutes} minutes, sir. I have my eye on this one - and I shall say "
                    "so if it wanders.")
FOCUS_START_LINE_TIMED = ("{minutes} minutes from now, sir. {left} to be good.")
FOCUS_BUSY_LINE = ("There is already a session running, sir. Say \"extend by ten minutes\" "
                   "for more of it, or \"end the session\" to close it.")
FOCUS_NONE_LINE = ("There is no session to {verb}, sir. Say \"thirty minutes on this\" and "
                   "I shall start one.")
FOCUS_HOME_LINE = ("You are looking at me, sir, and I can only lock what is in front of "
                   "you. Open the work, then say it again - or say it while you are in it.")
FOCUS_BLIND_LINES = {
    "no_command": ("I have no way to see the front app on this machine, sir - so I could "
                   "not honestly watch you drift. Start me with --focus-reader pointing at "
                   "a script, and I shall."),
    "not_mac": ("Bundle identifiers and browser tabs are a macOS thing, sir, and this is "
                "not one. Point --focus-reader at a script of your own and I shall watch "
                "whatever it prints."),
    "failed": ("The front-app reader is answering badly, sir - so I cannot watch. Nothing "
               "has been started: I would rather tell you than pretend to keep an eye on "
               "you."),
    "timeout": ("The front-app reader is too slow to be trusted, sir - so nothing has been "
                "started."),
}
FOCUS_LOST_LINE = ("I have lost sight of the front app, sir - the clock is stopped until I "
                   "can see again. No drift is being counted, and none is being missed.")
FOCUS_REGAINED_LINE = "I can see the front app again, sir. The clock is running."
FOCUS_PAUSE_LINE = "Paused, sir. The clock stops with you."
FOCUS_RESUME_LINE = "Running again, sir - {left} left to be good."
FOCUS_EXTEND_LINE = "{added} minutes added, sir - {planned} in all."
FOCUS_SNOOZE_LINE = ("Very good, sir - {seconds} seconds of silence. I shall be back on "
                     "the subject after that.")
FOCUS_NAG_LINE = "As you like, sir - I shall call it out every {seconds} seconds."
FOCUS_EXCUSE_LINE = ("As you say, sir. Research it is. That excursion is struck from the "
                     "record, and I shall hold my tongue until you are back.")
FOCUS_EXCUSE_CLEAN_LINE = ("As you say, sir - no harm done and none recorded. I shall say "
                           "nothing until you are back.")

# The callouts, in three tiers. Escalation is by how long the SAME excursion has lasted.
CALLOUT_TIERS = {
    1: (
        "That is not where the work is, sir.",
        "You have wandered, sir. I am counting.",
        "A gentle word, sir: not that.",
        "The work is still where you left it, sir.",
    ),
    2: (
        "Sir. The clock is running and you are not on it.",
        "{off} off target, sir, and counting.",
        "This is the part where the work happens, sir.",
        "I shall keep counting, sir. It is my only real pleasure.",
    ),
    3: (
        "{off} off target, sir. I am obliged to be honest with you.",
        "I have written this one down, sir: {off}, and the session is going nowhere.",
        "The session is not going to do itself, sir, and {off} is a long time.",
        "Shall I fetch the report card early, sir, or are we pretending?",
    ),
}

# The report card. Numbers only - and SHORT is the honesty clause: a five-second poke is
# not a session and the ledger stays out of it.
FOCUS_REPORT_LINE = ("Session closed, sir. {on} of {planned} minutes on target - {pct} "
                     "percent clean, {drifts} {drift_word} on the tally, {refunds} "
                     "{refund_word}. {streak_line}")
FOCUS_SHORT_LINE = ("That was {seconds} seconds, sir - a poke, not a session. Nothing has "
                    "gone in the ledger, and no streak was risked.")
FOCUS_STREAK_UP = "That is {streak} clean in a row."
FOCUS_STREAK_HELD = "The streak stands at {streak}."
FOCUS_STREAK_LOST = "The streak goes back to nothing, sir - {pct} will not do."

# --------------------------------------------------------------------------- #
# the vocabulary the client is allowed to see, and the checker that enforces it
# --------------------------------------------------------------------------- #
PHASES = ("idle", "running", "paused", "ended")
REASONS = ("none", "app", "tab", "home")
READER_STATES = ("live", "blind")
READER_WHY = ("ok", "no_command", "not_mac", "failed", "timeout")
# which reader is configured: the built-in one (a command, on macOS) or nothing at all.
# A word, never a path: /health is read by the page, and the page gets no paths.
READER_KINDS = ("command", "none")
PUBLIC_STRINGS = frozenset(PHASES + REASONS + READER_STATES + READER_WHY + READER_KINDS)

# The keys of the public state, per section. A field that is not listed here cannot reach
# the client, because public_state() builds the payload from these names and nothing else.
PUBLIC_FIELDS = frozenset((
    "ok", "phase", "id", "locked", "tab_locked", "on_target", "reason", "drifting",
    "tier", "excused", "snoozed", "snooze_s_left", "reader", "reader_why", "planned_s",
    "remaining_s", "elapsed_s", "on_s", "off_s", "off_open_s", "refunded_s", "noise_s",
    "drifts", "refunds", "ignores", "clean_pct", "nag_s", "grace_ms", "tick_s",
    "streak", "best_streak", "sessions", "clean_sessions", "callouts", "since",
    "speak", "report", "timings", "lines",
))
SPEAK_FIELDS = frozenset(("seq", "text", "tier", "at_s"))
REPORT_FIELDS = frozenset(("on", "planned", "pct", "drifts", "refunds", "streak",
                           "clean", "counted", "seconds", "text"))
TIMING_FIELDS = frozenset((
    "ticks", "tick_ms_last", "tick_ms_avg", "tick_ms_max", "reader_ms_last",
    "reader_ms_avg", "reader_ms_max", "reader_runs", "reader_fails", "detect_ms",
    "callout_ms", "nag_gap_ms", "tick_lag_ms", "tick_lag_max_ms", "blind_since_s",
))


class LineFieldError(Exception):
    """A spoken line was written with a placeholder that is not allowed."""


def _fields_in(template: str) -> set:
    return set(re.findall(r"\{(\w+)\}", template))


def check_line_templates() -> None:
    """Every line in this module may only hold numbers and enum words.

    This is the structural half of the privacy promise. The lines are the only free text
    the client ever receives, so if no line has a field that could hold an app or a host,
    no app or host can reach the client through a line - whatever happens elsewhere.
    """
    lines = [v for v in globals().values() if isinstance(v, str) and "{" in v]
    lines += [v for tier in CALLOUT_TIERS.values() for v in tier]
    lines += list(FOCUS_BLIND_LINES.values())
    for line in lines:
        bad = _fields_in(line) - ALLOWED_LINE_FIELDS
        if bad:
            raise LineFieldError("%r has unsupported field(s): %s" % (line[:60], sorted(bad)))
    for name in ("FOCUS_REPORT_LINE", "FOCUS_SHORT_LINE", "FOCUS_START_LINE"):
        if not globals().get(name):
            raise LineFieldError("%s is empty" % name)
    for name in ("FOCUS_STREAK_UP", "FOCUS_STREAK_HELD", "FOCUS_STREAK_LOST"):
        if _fields_in(globals()[name]) - ALLOWED_LINE_FIELDS:
            raise LineFieldError("%s is not composed of counters alone" % name)


check_line_templates()


def check_public_state(payload: dict) -> None:
    """Refuse to hand out anything that is not on the whitelist.

    Called by the server on every /focus response and by the tests directly. Booleans,
    counters, the enum words, and the two text fields (the callout queue and the report),
    whose text is built from templates that can only hold numbers.
    """
    if not isinstance(payload, dict):
        raise ValueError("public state must be a dict")
    unknown = set(payload) - PUBLIC_FIELDS
    if unknown:
        raise ValueError("not on the whitelist: %s" % sorted(unknown))
    for key in ("phase", "reason", "reader", "reader_why"):
        if payload.get(key) not in PUBLIC_STRINGS:
            raise ValueError("%s=%r is not in the allowed vocabulary" % (key, payload.get(key)))
    for entry in payload.get("speak") or []:
        if set(entry) - SPEAK_FIELDS:
            raise ValueError("callout carries %s" % sorted(set(entry) - SPEAK_FIELDS))
    report = payload.get("report")
    if report is not None and set(report) - REPORT_FIELDS:
        raise ValueError("report carries %s" % sorted(set(report) - REPORT_FIELDS))
    timings = payload.get("timings")
    if timings is not None and set(timings) - TIMING_FIELDS:
        raise ValueError("timings carry %s" % sorted(set(timings) - TIMING_FIELDS))
    for section in (payload.get("speak") or []):
        for k, v in section.items():
            if not isinstance(v, (bool, int, float, str)):
                raise ValueError("callout.%s is %r" % (k, type(v)))


# --------------------------------------------------------------------------- #
# the reader: the only place an identity ever exists
# --------------------------------------------------------------------------- #
def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:HOST_HASH_CHARS]


def host_of(url: str) -> str:
    """The HOST of a URL, lowercased. Never the path, never the query.

    This is the whole reason a single-page app can be locked at all: every click changes
    the path, and a path is not a place.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    if "//" not in text:
        text = "//" + text
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return ""
    host = (parsed.hostname or "").strip().lower()
    return host


class Reader:
    """Fresh frontmost-app query, one per tick, identities in and hashes out.

    The only two things that persist here are `_place` (two hashes: what the session is
    locked to) and `_recent` (the last place that was not home base, for the length of
    REMEMBER_LAST_PLACE_S). Everything observed per tick is a local variable that dies
    inside `snapshot()`.
    """

    def __init__(self, command=None, home_hosts=HOME_HOSTS, timeout_ms=READER_TIMEOUT_MS):
        cmd = command if command is not None else READER_COMMAND
        self.command = tuple(cmd) if isinstance(cmd, (list, tuple)) else tuple(_split_command(cmd))
        self.home = {_hash(h) for h in home_hosts}
        self.timeout_s = max(0.05, timeout_ms / 1000.0)
        self.platform_ok = self.command and not (self.command[0] == "osascript" and not _which("osascript"))
        self._place = None            # {"app": hash, "host": hash|None, "at": t} - the LOCK
        self._recent = None           # {"app": hash, "host": hash|None, "at": t} - last
                                      # place that was not home base
        self.runs = 0                 # subprocess launches (fresh query every tick)
        self.fails = 0
        self.fail_streak = 0
        self.last_error = "ok"
        self.read_ms_last = 0.0
        self._read_ms_total = 0.0

    # ---- reading ---------------------------------------------------------- #
    def _read_once(self):
        """One fresh query. Returns hashes, or None. The app key and the host are local
        variables from here to the end of this function and are never stored."""
        started = time.monotonic()
        if not self.command:
            self._fail("no_command")
            return None
        try:
            proc = subprocess.run(list(self.command), capture_output=True, text=True,
                                  timeout=self.timeout_s, check=False)
        except subprocess.TimeoutExpired:
            self._fail("timeout")
            return None
        except OSError:
            self._fail("failed")
            return None
        self.runs += 1
        self.read_ms_last = (time.monotonic() - started) * 1000.0
        self._read_ms_total += self.read_ms_last
        if proc.returncode != 0:
            self._fail("failed")
            return None
        lines = [ln.strip() for ln in (proc.stdout or "").splitlines()]
        app = next((ln for ln in lines if ln), "")
        if not app:
            self._fail("failed")
            return None
        url = next((ln for ln in lines[1:] if ln), "")
        host = host_of(url) if url else ""
        self.fail_streak = 0
        self.last_error = "ok"
        return {
            "app": _hash(app),
            "host": _hash(host) if host else None,
            "home": bool(host) and _hash(host) in self.home,
        }

    def _fail(self, why: str):
        self.fails += 1
        self.fail_streak += 1
        self.last_error = why

    # ---- the lock --------------------------------------------------------- #
    def locked(self) -> bool:
        return self._place is not None

    def tab_locked(self) -> bool:
        """True when the lock includes the site, not just the app."""
        return bool(self._place and self._place["host"] is not None)

    def lock_now(self, now=None) -> str:
        """Lock to what is in front of you RIGHT NOW, with a fresh query.

        Returns "ok", "home" (you are looking at his own tab and he has no memory of where
        you were) or a failure word. The lock is two hashes and nothing else.
        """
        seen = self._read_once()
        if seen is None:
            return self.last_error or "failed"
        if seen["home"]:
            remembered = self._remembered(now)
            if remembered is None:
                return "home"
            seen = remembered
        self._place = {"app": seen["app"], "host": seen["host"], "at": time.monotonic()}
        return "ok"

    def verdict_now(self) -> dict:
        """What he would say if the tick ran this instant, without reading again.

        Used the moment a lock is made: the lock IS what is in front of you, so the state
        is on-target from that second rather than a tick later.
        """
        return {"live": True, "on_target": True, "reason": "none",
                "tab_known": self.tab_locked()}

    def clear(self):
        """Drop the lock and the memory. Called when a session ends."""
        self._place = None
        self._recent = None

    def _remember(self, seen, now):
        if REMEMBER_LAST_PLACE_S <= 0 or seen["home"]:
            return
        self._recent = {"app": seen["app"], "host": seen["host"], "at": now}

    def _remembered(self, now=None):
        now = time.monotonic() if now is None else now
        if not self._recent:
            return None
        if now - self._recent["at"] > REMEMBER_LAST_PLACE_S:
            return None
        return dict(self._recent)

    # ---- the per-tick verdict --------------------------------------------- #
    def snapshot(self, now=None) -> dict:
        """One fresh read, compared, and the identities gone before this returns.

        The dict below is ALL that leaves this class for a tick: three booleans, one enum
        word, and a duration. There is no field for an app or a host to travel in.
        """
        now = time.monotonic() if now is None else now
        seen = self._read_once()
        if seen is None:
            return {"live": False, "on_target": False, "reason": "none", "tab_known": False}
        self._remember(seen, now)
        if seen["home"]:
            return {"live": True, "on_target": True, "reason": "home", "tab_known": True}
        place = self._place
        if place is None:
            return {"live": True, "on_target": False, "reason": "none", "tab_known": bool(seen["host"])}
        same_app = seen["app"] == place["app"]
        if not same_app:
            return {"live": True, "on_target": False, "reason": "app", "tab_known": bool(seen["host"])}
        if place["host"] is None:
            return {"live": True, "on_target": True, "reason": "none", "tab_known": bool(seen["host"])}
        if seen["host"] is None:
            # In the right app, but the tab could not be read this time. He does not call
            # that a drift - he cannot see it, and inventing one would be a guess.
            return {"live": True, "on_target": True, "reason": "none", "tab_known": False}
        if seen["host"] == place["host"]:
            return {"live": True, "on_target": True, "reason": "none", "tab_known": True}
        return {"live": True, "on_target": False, "reason": "tab", "tab_known": True}

    def stats(self) -> dict:
        runs = max(1, self.runs)
        return {
            "reader_runs": self.runs,
            "reader_fails": self.fails,
            "reader_ms_last": int(self.read_ms_last),
            "reader_ms_avg": int(self._read_ms_total / runs) if self.runs else 0,
            "reader_ms_max": int(self.read_ms_last),   # last is what matters for tuning
        }

    def __repr__(self):
        # even the repr cannot leak: there is nothing in here but hashes, and they stay
        return "<Reader runs=%d fails=%d locked=%s>" % (self.runs, self.fails, self.locked())


def _split_command(command) -> list:
    import shlex
    return shlex.split(command) if isinstance(command, str) else list(command or [])


def _which(name: str) -> bool:
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if folder and os.path.exists(os.path.join(folder, name)):
            return True
    return False


# --------------------------------------------------------------------------- #
# the ledger: aggregates only, and it says so by construction
# --------------------------------------------------------------------------- #
def load_ledger(path) -> dict:
    if not path:
        return {k: 0 for k in LEDGER_FIELDS}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    # Only the whitelist is read back, and every value is forced to a number: a ledger
    # that cannot hold an identity cannot remember one.
    return {k: _number(raw.get(k, 0)) for k in LEDGER_FIELDS}


def save_ledger(path, ledger: dict) -> bool:
    if not path:
        return False
    # Built from the whitelist, in a fixed order: whatever is in `ledger`, only these keys
    # are written, and only ever as numbers.
    payload = {k: _number(ledger.get(k, 0)) for k in LEDGER_FIELDS}
    tmp = os.path.join(os.path.dirname(os.path.abspath(path)) or ".", LEDGER_TMP)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _number(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0
    if out != out or out in (float("inf"), float("-inf")):
        return 0
    return int(out) if float(out).is_integer() else round(out, 2)


# --------------------------------------------------------------------------- #
# the session
# --------------------------------------------------------------------------- #
class Excursion:
    """One stretch away from the target. Seconds are ACCUMULATED rather than taken from
    wall-clock start/end, so a pause in the middle of an excursion freezes it too."""

    __slots__ = ("seconds", "excused", "counted", "tier", "last_callout_s")

    def __init__(self):
        self.seconds = 0.0
        self.excused = False
        self.counted = False
        self.tier = 0
        self.last_callout_s = None


class FocusService:
    """The session, the tick thread, the callouts and the ledger - all server-side."""

    def __init__(self, reader: Reader, ledger_path=None, tick_s=TICK_S, log=None):
        self.reader = reader
        self.ledger_path = ledger_path
        self.tick_s = float(tick_s)
        self.log = log or (lambda msg: None)
        self.lock = threading.RLock()
        self.session = None            # counters only; the identities are in the reader
        self.phase = "idle"
        self.next_id = 1
        self.nag_s = NAG_S
        self.callouts = []             # [{seq, text, tier, at_s}] - text only
        self.seq = 0
        self.report = None
        self.ledger = load_ledger(ledger_path)
        self.sessions = self.ledger["sessions"]
        self.last_verdict = {"live": True, "on_target": True, "reason": "none", "tab_known": False}
        self.timings = {
            "ticks": 0, "tick_ms_last": 0, "tick_ms_avg": 0.0, "tick_ms_max": 0,
            "reader_ms_last": 0, "reader_ms_avg": 0.0, "reader_ms_max": 0,
            "reader_runs": 0, "reader_fails": 0, "detect_ms": 0, "callout_ms": 0,
            "nag_gap_ms": 0, "tick_lag_ms": 0, "tick_lag_max_ms": 0, "blind_since_s": 0.0,
        }
        self._thread = None
        self._stop = threading.Event()
        self._last_tick = None
        self.blind_since = None
        self.last_blind_word = 0.0
        self._tier_last_pick = {}

    # -- lifecycle ---------------------------------------------------------- #
    def start_thread(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="focus-tick", daemon=True)
        self._thread.start()

    def stop_thread(self, timeout=2.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self):
        next_at = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now < next_at:
                self._stop.wait(min(max(next_at - now, 0.002), 0.2))
                continue
            lag = now - next_at
            watched = False
            try:
                watched = self.tick_once(now)
            except Exception as err:                      # never let the tick die
                self.log("focus: tick failed (%s: %s)" % (err.__class__.__name__, err))
            if watched:
                with self.lock:
                    self.timings["tick_lag_ms"] = int(lag * 1000)
                    self.timings["tick_lag_max_ms"] = max(self.timings["tick_lag_max_ms"],
                                                          int(lag * 1000))
            next_at += self.tick_s
            if next_at < time.monotonic() - 5 * self.tick_s:
                next_at = time.monotonic() + self.tick_s    # fell a long way behind

    # -- the tick ---------------------------------------------------------- #
    def tick_once(self, now=None) -> bool:
        """One second of watching. Returns False when there was nothing to watch.

        A session that has not started, or one that has already closed, is not watched: a
        headless server must not spawn a front-app reader every second for nobody. Ticks and
        reader runs therefore count only the seconds he was actually looking, which is also
        what makes the timings honest.
        """
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.session is None or self.phase == "ended":
                self._last_tick = None
                return False
        started = time.monotonic()
        verdict = self.reader.snapshot(now)
        read_ms = (time.monotonic() - started) * 1000.0
        with self.lock:
            # The first tick of a session is measured from the moment the session started,
            # not from nothing: otherwise the first second of every session is silently
            # uncounted, and a drift that begins immediately would be missed by a tick.
            anchor = self._last_tick
            if anchor is None:
                anchor = (self.session or {}).get("anchor", now) if self.session else now
            dt = max(0.0, min(now - anchor, TICK_GAP_MAX_S))
            self._last_tick = now
            self.last_verdict = verdict
            t = self.timings
            t["ticks"] += 1
            t["reader_ms_last"] = int(read_ms)
            t["reader_ms_max"] = max(t["reader_ms_max"], int(read_ms))
            t["reader_ms_avg"] = round((t["reader_ms_avg"] * (t["ticks"] - 1) + read_ms) / t["ticks"], 1)
            t["reader_runs"] = self.reader.runs
            t["reader_fails"] = self.reader.fails
            self._account(verdict, now, dt)
            total = time.monotonic() - started
            t["tick_ms_last"] = int(total * 1000)
            t["tick_ms_max"] = max(t["tick_ms_max"], int(total * 1000))
            t["tick_ms_avg"] = round((t["tick_ms_avg"] * (t["ticks"] - 1) + total * 1000) / t["ticks"], 1)
        return True

    def _account(self, verdict, now, dt):
        session = self.session
        if session is None or self.phase == "ended":
            return
        if not verdict["live"]:
            if self.blind_since is None and self.reader.fail_streak >= READER_FAIL_PATIENCE:
                self.blind_since = now
                self.timings["blind_since_s"] = 0.0
                if self.phase == "running":
                    self._emit(FOCUS_LOST_LINE, 0)
                    self.log("focus: the reader has failed %d times (%s) - the clock is stopped"
                             % (self.reader.fail_streak, self.reader.last_error))
            return
        if self.blind_since is not None:
            self.blind_since = None
            self.timings["blind_since_s"] = 0.0
            if self.phase == "running":
                self._emit(FOCUS_REGAINED_LINE, 0)
                self.log("focus: the reader is answering again")
        if self.phase == "paused":
            return                                                    # the clock stops
        session["elapsed_s"] += dt
        if verdict["on_target"]:
            self._close_excursion(now)
            session["on_s"] += dt
            if session["excused"]:
                session["excused"] = False        # he is back; the quiet ends with it
        else:
            excursion = session["excursion"]
            if excursion is None:
                excursion = Excursion()
                session["excursion"] = excursion
                session["excursion_started"] = now
            excursion.seconds += dt
            session["off_open_s"] = excursion.seconds
            self._judge_excursion(excursion, now, session)
        if session["remaining_s"] <= 0:
            self.finish("time")

    def _judge_excursion(self, excursion, now, session):
        grace_s = GRACE_MS / 1000.0
        if not excursion.counted and excursion.seconds >= grace_s and not excursion.excused:
            excursion.counted = True
            session["drifts"] += 1
            excursion.tier = self._tier_for(excursion.seconds)
            # the excursion's age when he called it a drift: the tick found it, and it had
            # already outlived the grace. The floor for a person is TICK_S + GRACE_MS, and
            # that is why tools/focus-timings.py prints both before anyone edits GRACE_MS
            session["detect_ms"] = int(excursion.seconds * 1000)
            self.timings["detect_ms"] = session["detect_ms"]
            self._callout(excursion, now, session)
            return
        if not excursion.counted or excursion.excused:
            return
        tier = self._tier_for(excursion.seconds)
        if tier > excursion.tier:
            excursion.tier = tier
            self._callout(excursion, now, session)
            return
        if (excursion.last_callout_s is not None
                and excursion.seconds - excursion.last_callout_s >= self.nag_s):
            self._callout(excursion, now, session)

    @staticmethod
    def _tier_for(seconds: float) -> int:
        if seconds >= TIER_3_AFTER_S:
            return 3
        if seconds >= TIER_2_AFTER_S:
            return 2
        return 1

    def _close_excursion(self, now):
        session = self.session
        if session is None or session["excursion"] is None:
            return
        excursion = session["excursion"]
        session["excursion"] = None
        session["off_open_s"] = 0.0
        session["excursion_started"] = None
        if excursion.excused:
            session["refunded_s"] += excursion.seconds
        elif excursion.seconds >= GRACE_MS / 1000.0:
            session["off_s"] += excursion.seconds
        else:
            session["ignores"] += 1
            session["noise_s"] += excursion.seconds

    def _callout(self, excursion, now, session):
        if excursion.excused:
            return
        if session["snooze_until"] is not None and now < session["snooze_until"]:
            return                                    # snoozed: quiet, but still counting
        tier = excursion.tier or 1
        pool = CALLOUT_TIERS.get(tier) or CALLOUT_TIERS[1]
        text = self._pick(pool, tier)
        off = _human_seconds(excursion.seconds)
        line = text.format(off=off) if "{off}" in text else text
        gap = (excursion.seconds - (excursion.last_callout_s or 0.0)) * 1000.0
        self._emit(line, tier)
        excursion.last_callout_s = excursion.seconds
        session["callouts"] += 1
        session["callout_ms"] = int(excursion.seconds * 1000)
        self.timings["callout_ms"] = session["callout_ms"]
        if session["callouts"] > 1:
            self.timings["nag_gap_ms"] = int(gap)
        self.log("focus: callout tier %d at %.1fs off target" % (tier, excursion.seconds))

    def _pick(self, pool, tier):
        """Never the same line twice in a row for the same tier."""
        if len(pool) == 1:
            return pool[0]
        last = self._tier_last_pick.get(tier)
        choice = random.choice([line for line in pool if line != last] or list(pool))
        self._tier_last_pick[tier] = choice
        return choice

    def _emit(self, text: str, tier: int):
        self.seq += 1
        self.callouts.append({"seq": self.seq, "text": text, "tier": int(tier),
                              "at_s": round(time.time(), 3)})
        if len(self.callouts) > CALLOT_KEEP:
            del self.callouts[:len(self.callouts) - CALLOT_KEEP]

    # -- the commands ------------------------------------------------------- #
    def start(self, minutes=None, now=None) -> dict:
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.session is not None and self.phase != "ended":
                return self._reply(False, "focus_busy", FOCUS_BUSY_LINE)
            why = self._blind_why()
            if why:
                return self._reply(False, "focus_blind", FOCUS_BLIND_LINES[why])
            minutes = _clamp(minutes if minutes else DEFAULT_MINUTES, MIN_MINUTES, MAX_MINUTES)
            locked = self.reader.lock_now(now)
            if locked == "home":
                return self._reply(False, "focus_home", FOCUS_HOME_LINE)
            if locked != "ok":
                return self._reply(False, "focus_blind",
                                   FOCUS_BLIND_LINES.get(locked, FOCUS_BLIND_LINES["failed"]))
            self.session = {
                "id": self.next_id, "minutes": minutes, "planned_s": minutes * 60.0,
                "elapsed_s": 0.0, "on_s": 0.0, "off_s": 0.0, "off_open_s": 0.0,
                "refunded_s": 0.0, "noise_s": 0.0, "drifts": 0, "refunds": 0, "ignores": 0,
                "excursion": None, "excursion_started": None, "excused": False,
                "snooze_until": None, "callouts": 0, "detect_ms": 0, "callout_ms": 0,
                "started_at": now, "anchor": now, "tab_locked": self.reader.tab_locked(),
            }
            self.next_id += 1
            self.phase = "running"
            self.report = None
            self.session["remaining_s"] = self.session["planned_s"]
            self.last_verdict = self.reader.verdict_now()
            line = FOCUS_START_LINE.format(minutes=_num(minutes),
                                           left=_human_minutes(minutes * 60.0))
            return self._reply(True, "focus_started", line)

    def pause(self) -> dict:
        with self.lock:
            if not self._live_session():
                return self._reply(False, "focus_none", FOCUS_NONE_LINE.format(verb="pause"))
            if self.phase == "paused":
                return self._reply(False, "focus_paused", FOCUS_PAUSE_LINE)
            self.phase = "paused"
            return self._reply(True, "focus_paused", FOCUS_PAUSE_LINE)

    def resume(self, now=None) -> dict:
        with self.lock:
            if not self._live_session():
                return self._reply(False, "focus_none", FOCUS_NONE_LINE.format(verb="resume"))
            self.phase = "running"
            # do not count the pause as elapsed: the next tick is measured from here
            self._last_tick = None
            self.session["anchor"] = time.monotonic() if now is None else now
            return self._reply(True, "focus_resumed",
                               FOCUS_RESUME_LINE.format(left=_human_minutes(self.session["remaining_s"])))

    def extend(self, minutes) -> dict:
        with self.lock:
            if not self._live_session():
                return self._reply(False, "focus_none", FOCUS_NONE_LINE.format(verb="extend"))
            before = self.session["planned_s"]
            self.session["planned_s"] = min(MAX_MINUTES * 60.0, before + max(0.0, minutes or 0) * 60.0)
            added = (self.session["planned_s"] - before) / 60.0
            self.session["remaining_s"] = max(0.0, self.session["remaining_s"] + added * 60.0)
            return self._reply(True, "focus_extended",
                               FOCUS_EXTEND_LINE.format(added=_num(added),
                                                        planned=_num(self.session["planned_s"] / 60.0)))

    def snooze(self, seconds=None, now=None) -> dict:
        with self.lock:
            if not self._live_session():
                return self._reply(False, "focus_none", FOCUS_NONE_LINE.format(verb="snooze"))
            seconds = _clamp(seconds if seconds else SNOOZE_S, SNOOZE_MIN_S, SNOOZE_MAX_S)
            self.session["snooze_until"] = (time.monotonic() if now is None else now) + seconds
            return self._reply(True, "focus_snoozed",
                               FOCUS_SNOOZE_LINE.format(seconds=int(round(seconds))))

    def excuse(self) -> dict:
        with self.lock:
            if not self._live_session():
                return self._reply(False, "focus_none", FOCUS_NONE_LINE.format(verb="excuse"))
            session = self.session
            excursion = session["excursion"]
            session["excused"] = True
            session["snooze_until"] = None
            if excursion is None:
                # Nothing open yet: forgiving the next one. Quiet until he is back anyway.
                return self._reply(True, "focus_excused_clean", FOCUS_EXCUSE_CLEAN_LINE)
            excursion.excused = True
            if excursion.counted:
                excursion.counted = False
                session["drifts"] = max(0, session["drifts"] - 1)
                session["refunds"] += 1
                session["refunded_s"] += excursion.seconds
                return self._reply(True, "focus_excused", FOCUS_EXCUSE_LINE)
            return self._reply(True, "focus_excused_clean", FOCUS_EXCUSE_CLEAN_LINE)

    def set_nag(self, seconds) -> dict:
        with self.lock:
            self.nag_s = _clamp(seconds if seconds else NAG_S, NAG_MIN_S, NAG_MAX_S)
            return self._reply(True, "focus_nag", FOCUS_NAG_LINE.format(seconds=int(round(self.nag_s))))

    def finish(self, reason="abort") -> dict:
        with self.lock:
            if self.session is None or self.phase == "ended":
                return self._reply(False, "focus_none", FOCUS_NONE_LINE.format(verb="end"))
            session = self.session
            if session["excursion"] is not None:
                self._close_excursion(time.monotonic())
            session["remaining_s"] = max(0.0, session["planned_s"] - session["elapsed_s"])
            self.phase = "ended"
            self.reader.clear()                    # the lock dies with the session
            report = self._make_report(session, reason)
            self.report = report
            self._emit(report["text"], 0)
            return dict(self._reply(True, "focus_ended", report["text"]), report=report)

    # -- the report card ---------------------------------------------------- #
    def _make_report(self, session, reason) -> dict:
        on_s, planned_s = session["on_s"], session["planned_s"]
        elapsed = session["elapsed_s"]
        counted = on_s + session["off_s"]
        pct = (on_s / counted * 100.0) if counted > CLEAN_PCT_BASE else 100.0
        drifts, refunds = session["drifts"], session["refunds"]
        report = {
            "on": round(on_s / 60.0, 1), "planned": round(planned_s / 60.0, 1),
            "pct": int(round(pct)), "drifts": drifts, "refunds": refunds,
            "seconds": int(round(elapsed)), "clean": bool(pct >= CLEAN_PCT),
            "counted": bool(elapsed >= LEDGER_MIN_SESSION_S), "streak": self.ledger["streak"],
        }
        if elapsed < LEDGER_MIN_SESSION_S:
            report["text"] = FOCUS_SHORT_LINE.format(seconds=int(round(elapsed)))
            report["counted"] = False
            return report
        self.ledger["sessions"] = int(self.ledger["sessions"]) + 1
        self.ledger["minutes_on"] = _number(self.ledger["minutes_on"] + on_s / 60.0)
        self.ledger["minutes_planned"] = _number(self.ledger["minutes_planned"] + planned_s / 60.0)
        self.ledger["drifts"] = int(self.ledger["drifts"]) + drifts
        self.ledger["refunds"] = int(self.ledger["refunds"]) + refunds
        if report["clean"]:
            self.ledger["clean_sessions"] = int(self.ledger["clean_sessions"]) + 1
            self.ledger["streak"] = int(self.ledger["streak"]) + 1
            self.ledger["best_streak"] = max(int(self.ledger["best_streak"]), int(self.ledger["streak"]))
        else:
            self.ledger["streak"] = 0
        report["streak"] = self.ledger["streak"]
        self.sessions = self.ledger["sessions"]
        if report["clean"]:
            streak_line = (FOCUS_STREAK_UP if self.ledger["streak"] > 1 else FOCUS_STREAK_HELD)
            streak_line = streak_line.format(streak=self.ledger["streak"])
        else:
            streak_line = FOCUS_STREAK_LOST.format(pct=report["pct"])
        report["text"] = FOCUS_REPORT_LINE.format(
            on=_num(report["on"]), planned=_num(report["planned"]), pct=report["pct"],
            drifts=drifts, refunds=refunds,
            drift_word="drift" if drifts == 1 else "drifts",
            refund_word="refund" if refunds == 1 else "refunds",
            streak_line=streak_line)
        save_ledger(self.ledger_path, self.ledger)
        return report

    # -- the state the client may see --------------------------------------- #
    def public_state(self, since=0, now=None) -> dict:
        now = time.monotonic() if now is None else now
        with self.lock:
            session = self.session
            verdict = self.last_verdict
            if session is None or self.phase == "ended":
                phase = self.phase if session is None else "ended"
            else:
                phase = self.phase
            planned = session["planned_s"] if session else 0.0
            elapsed = session["elapsed_s"] if session else 0.0
            remaining = max(0.0, planned - elapsed) if session else 0.0
            if self.phase == "ended" and session is not None:
                remaining = max(0.0, session["planned_s"] - session["elapsed_s"])
            on_s = session["on_s"] if session else 0.0
            off_closed = session["off_s"] if session else 0.0
            off_open = session["off_open_s"] if session else 0.0
            excursion = session["excursion"] if session else None
            # an excused excursion is refunded, so it does not count against the percentage
            # while it is still open either - the refund is not something you wait for
            open_counts = 0.0 if (excursion and excursion.excused) else off_open
            counted = on_s + off_closed + open_counts
            pct = (on_s / counted * 100.0) if counted > CLEAN_PCT_BASE else 100.0
            snoozed = bool(session and session["snooze_until"] and now < session["snooze_until"])
            drifting = bool(session and session["excursion"] and session["excursion"].counted
                            and not session["excursion"].excused and self.phase == "running")
            tier = session["excursion"].tier if drifting else 0
            fresh = [c for c in self.callouts
                     if c["seq"] > int(since or 0) and (time.time() - c["at_s"]) <= CALLOUT_REPLAY_S]
            payload = {
                "ok": True,
                "phase": phase,
                "id": session["id"] if session else 0,
                "locked": bool(self.reader.locked()) and self.phase != "ended",
                "tab_locked": bool(session and session.get("tab_locked")),
                "on_target": bool(verdict["on_target"]) if self.reader.locked() else True,
                "reason": verdict["reason"] if self.reader.locked() else "none",
                "drifting": drifting,
                "tier": tier,
                "excused": bool(session and session["excused"]),
                "snoozed": snoozed,
                "snooze_s_left": round(max(0.0, (session["snooze_until"] - now)), 1) if snoozed else 0.0,
                "reader": "live" if verdict["live"] else "blind",
                "reader_why": self.reader.last_error if verdict["live"] else (self.reader.last_error or "failed"),
                "planned_s": round(planned, 1),
                "remaining_s": round(remaining, 1),
                "elapsed_s": round(elapsed, 1),
                "on_s": round(on_s, 1),
                "off_s": round(session["off_s"] if session else 0.0, 1),
                "off_open_s": round(off_open, 1),
                "refunded_s": round(session["refunded_s"] if session else 0.0, 1),
                "noise_s": round(session["noise_s"] if session else 0.0, 1),
                "drifts": int(session["drifts"]) if session else 0,
                "refunds": int(session["refunds"]) if session else 0,
                "ignores": int(session["ignores"]) if session else 0,
                "clean_pct": int(round(pct)),
                "nag_s": round(self.nag_s, 1),
                "grace_ms": GRACE_MS,
                "tick_s": self.tick_s,
                "streak": int(self.ledger["streak"]),
                "best_streak": int(self.ledger["best_streak"]),
                "sessions": int(self.ledger["sessions"]),
                "clean_sessions": int(self.ledger["clean_sessions"]),
                "callouts": int(session["callouts"]) if session else 0,
                "since": self.callouts[-1]["seq"] if self.callouts else 0,
                "speak": [{"seq": c["seq"], "text": c["text"], "tier": c["tier"],
                           "at_s": c["at_s"]} for c in fresh],
                "report": dict(self.report) if self.report else None,
                "timings": {
                    "ticks": int(self.timings["ticks"]),
                    "tick_ms_last": int(self.timings["tick_ms_last"]),
                    "tick_ms_avg": int(round(float(self.timings["tick_ms_avg"]))),
                    "tick_ms_max": int(self.timings["tick_ms_max"]),
                    "reader_ms_last": int(self.timings["reader_ms_last"]),
                    "reader_ms_avg": int(round(float(self.timings["reader_ms_avg"]))),
                    "reader_ms_max": int(self.timings["reader_ms_max"]),
                    "reader_runs": int(self.timings["reader_runs"]),
                    "reader_fails": int(self.timings["reader_fails"]),
                    "detect_ms": int(self.timings["detect_ms"]),
                    "callout_ms": int(self.timings["callout_ms"]),
                    "nag_gap_ms": int(self.timings.get("nag_gap_ms", 0)),
                    "tick_lag_ms": int(self.timings["tick_lag_ms"]),
                    "tick_lag_max_ms": int(self.timings["tick_lag_max_ms"]),
                    "blind_since_s": round(self.timings["blind_since_s"], 1),
                },
                "lines": {"start": FOCUS_START_LINE, "busy": FOCUS_BUSY_LINE,
                          "report": FOCUS_REPORT_LINE, "short": FOCUS_SHORT_LINE},
            }
        check_public_state(payload)
        return payload

    # -- helpers ------------------------------------------------------------ #
    def _reply(self, ok, code, line) -> dict:
        return {"ok": bool(ok), "changed": bool(ok), "code": code,
                "answer": line, "spoken": line,
                "focus": self.public_state()}

    def _live_session(self) -> bool:
        return self.session is not None and self.phase in ("running", "paused")

    def _blind_why(self):
        """Why he cannot honestly watch - as a word from READER_WHY, never a sentence."""
        if not self.reader.command:
            return "no_command"
        if not self.reader.platform_ok:
            return "not_mac" if self.reader.command and self.reader.command[0] == "osascript" else "no_command"
        if self.reader.fail_streak >= READER_FAIL_PATIENCE:
            return self.reader.last_error if self.reader.last_error in ("failed", "timeout") else "failed"
        return ""

    def handle(self, action=None, text=None, minutes=None, seconds=None, since=0) -> dict:
        """One entry point for the endpoint: an action from a button, or words from voice."""
        if text:
            words = parse_focus(text)
            if words["code"] == "focus_none":
                return self._reply(False, "focus_not_command", "")
            action = words.get("action") or action
            minutes = words.get("minutes") if words.get("minutes") is not None else minutes
            seconds = words.get("seconds") if words.get("seconds") is not None else seconds
        if not action:
            return {"ok": False, "error": "No focus action was named.",
                    "hint": 'Send {"action": "start", "minutes": 30}, or {"text": '
                            '"thirty minutes on this"}.'}
        action = str(action).lower()
        if action == "start":
            return self.start(minutes)
        if action == "pause":
            return self.pause()
        if action == "resume":
            return self.resume()
        if action == "extend":
            return self.extend(minutes if minutes is not None else 5)
        if action == "snooze":
            return self.snooze(seconds)
        if action == "excuse":
            return self.excuse()
        if action == "nag":
            return self.set_nag(seconds)
        if action in ("end", "abort", "stop", "finish"):
            return self.finish(action)
        if action == "state":
            return dict(self._reply(True, "focus_state", ""), focus=self.public_state(since))
        # an action nobody defines is a bad request, not a session: it carries a code so the
        # endpoint can say 400 rather than 200-with-a-shrug
        return {"ok": False, "code": "focus_unknown_action",
                "error": "Unknown focus action: %s" % action,
                "hint": "start, pause, resume, extend, snooze, excuse, nag, end"}


# --------------------------------------------------------------------------- #
# what a person says -> an action
# --------------------------------------------------------------------------- #
NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
# Spoken units. Single letters ("s", "m", "h") are deliberately NOT here: they are only
# read when they are attached to a digit ("45s"), because a stray "s" in "let's do forty
# five minutes" would otherwise turn forty five minutes into forty five seconds.
UNIT_WORDS = {"second": 1, "seconds": 1, "sec": 1, "secs": 1,
              "minute": 60, "minutes": 60, "min": 60, "mins": 60,
              "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600}
UNIT_LETTERS = {"s": 1, "m": 60, "h": 3600}

# Anchored on purpose: a question that merely contains one of these words must stay a
# question. The client routes on the same shapes (isFocusCommand in viewer/index.html).
FOCUS_END_RE = re.compile(r"^\s*(?:please\s+)?(?:end|abort|finish|close)\b(?:\s+(?:the|this|my))?"
                          r"(?:\s+(?:session|focus|focus\s+session))?\s*$|^\s*(?:i'?m|i\s+am)\s+done"
                          r"(?:\s+with\s+this)?\s*$|^\s*call\s+it(\s+a\s+session)?\s*$|^that'?s\s+enough\s*$", re.I)
FOCUS_PAUSE_RE = re.compile(r"^\s*(?:please\s+)?(?:pause|hold\s+on|take\s+a\s+break|break)\b"
                            r"(?:\s+(?:the|this|my))?(?:\s+(?:session|focus|focus\s+session|it))?\s*$", re.I)
FOCUS_RESUME_RE = re.compile(r"^\s*(?:please\s+)?(?:resume|unpause|continue|carry\s+on|back\s+to\s+work)\b"
                             r"(?:\s+(?:the|this|my))?(?:\s+(?:session|focus|focus\s+session|it))?\s*$", re.I)
FOCUS_NAG_RE = re.compile(r"^\s*(?:please\s+)?(?:call\s+me\s+out|nag\s+me|tell\s+me|remind\s+me|"
                          r"check\s+on\s+me|call\s+it\s+out)\s+every\s+(?P<dur>.+?)\s*$", re.I)
FOCUS_SNOOZE_RE = re.compile(r"^\s*(?:snooze(?:\s+for)?|give\s+me|just\s+give\s+me|shush|quiet)\s+"
                             r"(?P<dur>.+?)\s*$", re.I)
FOCUS_EXTEND_RE = re.compile(r"^\s*(?:please\s+)?(?:extend|add|give\s+me)\b\s*(?:by\s+)?(?P<dur>.+?)"
                             r"(?:\s+more)?(?:\s+(?:to|on|of))?(?:\s+(?:the|this|my))?"
                             r"(?:\s+(?:session|focus|focus\s+session|timer))?\s*$", re.I)
FOCUS_EXCUSE_RE = re.compile(
    r"^\s*(?:it'?s|its|it\s+is|that'?s|thats|that\s+is)\s+(?:ok|okay|fine|alright|all\s+right)\b"
    r"|^\s*(?:i'?m|im|i\s+am)\s+(?:just\s+)?(?:doing\s+|in\s+)?research\b"
    r"|^\s*(?:this|it)\s+is\s+research\b|^\s*excused\b", re.I)
FOCUS_START_RE = re.compile(
    r"(?P<dur>[\w\s\.]{1,28}?)\s*(?:minutes?|mins?|hours?|hrs?)\b[^?]*?"
    r"\b(?:on\s+this|on\s+it|focus|of\s+work|of\s+focus|session)\b", re.I)
FOCUS_START_ALT_RE = re.compile(
    r"\b(?:focus|work)\b[^?]*?\b(?:for|on)\b\s*(?P<dur>[\w\s\.]{1,28}?)\s*"
    r"(?:minutes?|mins?|hours?|hrs?)\b", re.I)


def parse_duration(text):
    """Seconds, from "thirty", "10 minutes", "an hour", "half an hour", "45s", "0.2 minutes".

    Everything that is not a number or a unit is ignored on purpose: people do not speak
    in arguments ("let's do forty five minutes on this" is forty five minutes), and a
    parser that needs a clean sentence would be a parser that refuses to work by voice.
    """
    raw = str(text or "").strip().lower()
    if not raw:
        return None
    total = 0.0
    found = False
    # digits, with a unit attached to them if there is one ("45s", "30 minutes", "0.2 m")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([a-z]+)?", raw)
    if match:
        unit = match.group(2) or ""
        factor = UNIT_LETTERS.get(unit) or UNIT_WORDS.get(unit) or 60
        total += float(match.group(1)) * factor
        found = True
        raw = raw[:match.start()] + " " + raw[match.end():]
    number = 0.0
    unit_factor = None
    seen_number = False
    half = False
    for word in re.findall(r"[a-z]+", raw):
        if word in ("half",):
            half = True
            continue
        if word in UNIT_WORDS:
            factor = UNIT_WORDS[word]
            if unit_factor is None or factor < unit_factor:
                unit_factor = factor
            continue
        if word in ("a", "an"):
            # "a minute" is a minute; the "a" in "a 25 minute session" is not a number
            if not seen_number and unit_factor is None and not found:
                number += 1
                seen_number = True
            continue
        if word in NUMBER_WORDS:
            value = NUMBER_WORDS[word]
            seen_number = True
            # "twenty five": a small number after a round ten is part of it
            if value < 10 and number >= 20 and number % 10 == 0:
                number += value
            else:
                number += value
    if seen_number or (half and unit_factor):
        if half:
            number = number * 0.5 if number else 0.5
        if unit_factor is None:
            unit_factor = 60                     # a bare number in this vocabulary is minutes
        total += number * unit_factor
        found = True
    if not found or total <= 0:
        return None
    return total


def parse_focus(text) -> dict:
    """What did he just say? One of: start, pause, resume, extend, snooze, excuse, nag, end.

    Returns {"code": "focus_<action>", "action": ..., "minutes"|"seconds": ...} or
    {"code": "focus_none"} when this was not a command about the session. Anchored, and
    short: anything long, or with a question mark, is a question and stays one.
    """
    said = str(text or "").strip()
    low = said.lower()
    out = {"code": "focus_none", "action": None}
    if not said or len(said) > 90 or "?" in said:
        return out
    if len(said.split()) > 10:
        return out
    if FOCUS_END_RE.match(low):
        return {"code": "focus_end", "action": "end"}
    if FOCUS_PAUSE_RE.match(low):
        return {"code": "focus_pause", "action": "pause"}
    if FOCUS_RESUME_RE.match(low):
        return {"code": "focus_resume", "action": "resume"}
    match = FOCUS_NAG_RE.match(low)
    if match:
        seconds = parse_duration(match.group("dur"))
        if seconds:
            return {"code": "focus_nag", "action": "nag", "seconds": seconds}
        return out
    match = FOCUS_EXCUSE_RE.match(low)
    if match:
        return {"code": "focus_excuse", "action": "excuse"}
    match = FOCUS_SNOOZE_RE.match(low)
    if match:
        seconds = parse_duration(match.group("dur"))
        if seconds and ("snooze" in low or "shush" in low or "quiet" in low or seconds <= 120):
            return {"code": "focus_snooze", "action": "snooze", "seconds": seconds}
    match = FOCUS_EXTEND_RE.match(low)
    if match:
        seconds = parse_duration(match.group("dur"))
        if seconds:
            return {"code": "focus_extend", "action": "extend", "minutes": seconds / 60.0}
    for pattern in (FOCUS_START_RE, FOCUS_START_ALT_RE):
        if pattern.search(low):
            # the pattern is the gate ("this is a command about a session"); the number is
            # read off the whole sentence, so "let's do forty five minutes on this" works
            seconds = parse_duration(low)
            if seconds and seconds >= MIN_MINUTES * 60:
                return {"code": "focus_start", "action": "start", "minutes": seconds / 60.0}
    return out


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _clamp(value, low, high):
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = low
    return max(low, min(high, number))


def _num(value) -> str:
    number = float(value)
    return str(int(number)) if float(number).is_integer() else ("%.1f" % number)


def _human_seconds(seconds: float) -> str:
    seconds = float(seconds)
    if seconds < 90:
        return "%d seconds" % int(round(seconds))
    minutes = seconds / 60.0
    if minutes < 10:
        return "%.1f minutes" % minutes
    return "%d minutes" % int(round(minutes))


def _human_minutes(seconds: float) -> str:
    seconds = float(seconds)
    minutes = seconds / 60.0
    if minutes >= 90:
        return "%s hours" % _num(minutes / 60.0)
    if abs(minutes - round(minutes)) < 0.05:
        return "%d minutes" % int(round(minutes))
    return "%.1f minutes" % minutes
