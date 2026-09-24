#!/usr/bin/env python3
"""
preflight.py - the live chain, end to end, against the machine it is running on.

This is not a unit test and there are no mocks in it. Every check talks to the running
system the way a person does: real HTTP to the real server, the real config.json, the real
notes folder, a real JPEG, a real call to the model. The failures that hurt are the ones
where every unit passes and a whole chain is dead - a server started before your last edit,
a viewer served from a file that no longer matches the disk, a probe that sends a media
type the client never sends - and those are exactly what this file is for.

It also never imports the app. If preflight cannot see something over HTTP, the browser
cannot see it either. Check 13 wears another brain for a moment (and puts the config brain
back before it finishes) so the swap is proven for real; --no-swap skips it. Check 14 starts
a focus session for a few seconds, watches it tick with no browser open, and ends it again -
a session that is already running is reported and left alone; --no-focus skips it.

    python3 preflight.py                       # the server on http://127.0.0.1:4700
    python3 preflight.py --url http://127.0.0.1:4711
    python3 preflight.py --json                # one JSON object, for scripts
    python3 preflight.py --config other.json   # a config other than ./config.json
    python3 preflight.py --no-swap             # skip check 13 (the brain swap)
    python3 preflight.py --no-focus            # skip check 14 (the focus session probe)

Marks:

    ✔  this link of the live chain works
    ✗  this link is BROKEN                     -> exit code 1
    !  could not be proven here, or a real problem that is not fatal - the reason is
       printed under it, and the run still exits 0 when nothing is red

Exit code is 1 if anything failed, 0 otherwise. Warnings never fail the run: with the
placeholder key still in config.json there is no brain to prove, and saying so is not the
same as being broken.

Every check that was added because something actually broke on this project says so in a
comment headed "incident". That is how this file is meant to grow: one check per scar.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import re
import string
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = HERE
DEFAULT_URL = "http://127.0.0.1:4700"
DEFAULT_CONFIG = os.path.join(ROOT, "config.json")
DEFAULT_BASE_URL = "https://api.openai.com/v1"
VIEWER = os.path.join(ROOT, "viewer")
SCREEN_FIXTURE = os.path.join(ROOT, "tools", "fixtures", "screen-frame.jpg")
PROBE_TITLE_PREFIX = "The Preflight Probe"

# Every response body fetched during this run is kept here, so the last check can sweep the
# lot for the key. Nothing in this file writes to disk except the /remember probe note.
SEEN = []


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
class Report:
    def __init__(self, color: bool = True, quiet: bool = False):
        self.color = color and sys.stdout.isatty()
        self.quiet = quiet
        self.passes = self.fails = self.warns = 0
        self.records = []

    def paint(self, text: str, code: str) -> str:
        return "\033[%sm%s\033[0m" % (code, text) if self.color else text

    def say(self, text: str) -> None:
        if not self.quiet:
            print(text)

    def line(self, mark: str, name: str, detail: str, notes=()) -> None:
        label = {"pass": "✔", "fail": "✗", "warn": "!"}[mark]
        colour = {"pass": "32", "fail": "31", "warn": "33"}[mark]
        self.say("%s %s  %s" % (self.paint(label, colour), name.ljust(58), detail))
        for note in notes:
            self.say("    %s" % self.paint("-> " + note, "2"))
        self.records.append({"mark": mark, "check": name.strip(), "detail": detail,
                             "notes": [str(n) for n in notes]})
        if mark == "pass":
            self.passes += 1
        elif mark == "fail":
            self.fails += 1
        else:
            self.warns += 1

    def section(self, title: str) -> None:
        self.say("\n%s" % self.paint(title, "1"))

    def info(self, text: str) -> None:
        self.say("    %s" % self.paint(text, "2"))


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #
def http(url: str, method: str = "GET", payload=None, timeout: float = 30.0, headers=None):
    """One real request. Returns (status, headers, body_text, error).

    A 4xx/5xx is a status, not an exception: refusals are part of what is being checked.
    """
    hdrs = dict(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
            SEEN.append((url, text))
            return resp.status, dict(resp.headers), text, None
    except urllib.error.HTTPError as err:
        text = err.read().decode("utf-8", "replace")
        SEEN.append((url, text))
        return err.code, dict(err.headers), text, None
    except Exception as err:                                  # noqa: BLE001 - reported, not raised
        return None, {}, "", "%s: %s" % (err.__class__.__name__, err)


def get(url: str, timeout: float = 30.0):
    return http(url, "GET", timeout=timeout)


def post(url: str, payload, timeout: float = 30.0):
    return http(url, "POST", payload=payload, timeout=timeout)


def as_json(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def masked(key: str) -> str:
    if not key:
        return "(empty)"
    return key[:6] + "..." + key[-4:] if len(key) > 12 else "***"


def read_config(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def key_state(key) -> str:
    """The same three states the server uses, judged here independently."""
    text = (key or "").strip()
    if not text:
        return "missing"
    if text == "PUT-YOUR-KEY-HERE" or text.upper().startswith("PUT-"):
        return "placeholder"
    return "set"


def sniff(data: bytes) -> str:
    """What these bytes actually are, by magic number.

    Deliberately a second, independent implementation: checking the server with the
    server's own parser would prove nothing.
    """
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return ""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def read_file(path: str):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def brain_label(model_id: str) -> str:
    """The label rule, implemented here a second time on purpose: a server that stops
    obeying it is a server whose chip lies, and this catches that.

    Only a hyphen between two DIGITS is a version dot.

        openai/gpt-6-astra         -> GPT 6 ASTRA
        anthropic/claude-fable-5-1 -> CLAUDE FABLE 5.1
    """
    name = str(model_id or "").split("/")[-1].replace("_", "-")
    name = re.sub(r"(?<=\d)-(?=\d)", ".", name)
    return re.sub(r"\s+", " ", name.replace("-", " ")).strip().upper()


def minutes(seconds: float) -> str:
    if seconds < 90:
        return "%.0fs" % seconds
    if seconds < 5400:
        return "%.1f min" % (seconds / 60.0)
    return "%.1f h" % (seconds / 3600.0)


def error_message(body: str, fallback: str = "") -> str:
    data = as_json(body) or {}
    err = data.get("error")
    if isinstance(err, dict):
        err = err.get("message")
    return str(err or fallback or body)[:200]


def brain_probe(api_base: str, key: str, model: str, timeout: float):
    """One real, minimal call to the model the config names: one token, one word."""
    return http(api_base + "/chat/completions", "POST",
                {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                timeout, headers={"Authorization": "Bearer %s" % key})


# --------------------------------------------------------------------------- #
# 1. the server, and the page it is serving
# --------------------------------------------------------------------------- #
def check_server(report: Report, base: str, timeout: float) -> dict:
    name = "1. server is up and serving the viewer"
    status, headers, body, err = get(base + "/", timeout)
    if status is None:
        report.line("fail", name, "could not reach %s" % base,
                    ["%s" % err,
                     "start it with:  python3 server.py",
                     "or point preflight at it:  python3 preflight.py --url http://127.0.0.1:PORT"])
        return {}
    if status != 200:
        report.line("fail", name, "GET / returned HTTP %s" % status, [body.strip()[:200]])
        return {}

    ctype = headers.get("Content-Type", "")
    markers = ['id="stage"', "graph-data.js", 'id="ask-form"', 'id="sight"']
    missing = [m for m in markers if m not in body]
    if missing:
        report.line("fail", name, "something is answering, but it is not the viewer (missing %s)"
                    % ", ".join(missing), ["GET / -> %s" % ctype, body.strip()[:160]])
        return {}

    notes = []
    head_status, head_headers, _b, _e = http(base + "/", "HEAD", timeout=timeout)
    if head_status != 200 or "Content-Length" not in head_headers:
        notes.append("HEAD / did not answer with a Content-Length - proxies and previews need it")
    report.line("pass" if not notes else "warn", name,
                "GET / -> 200, %s, %d KB, viewer markers present"
                % (ctype.split(";")[0] or "no content-type", len(body) // 1024), notes)
    return {"html": body}


def check_graph(report: Report, base: str, timeout: float) -> dict:
    name = "2. graph data loads, and has nodes"
    status, headers, body, err = get(base + "/graph-data.js", timeout)
    if status != 200:
        report.line("fail", name, "GET /graph-data.js -> HTTP %s" % status,
                    [err or body.strip()[:200], "run: python3 build.py"])
        return {}
    match = re.search(r"const GRAPH\s*=\s*(\{.*\});?\s*$", body, re.S | re.M)
    if not match:
        report.line("fail", name, "the served graph-data.js has no `const GRAPH = {...}` in it",
                    ["the viewer would boot to an empty galaxy"])
        return {}
    try:
        graph = json.loads(match.group(1))
    except ValueError as exc:
        report.line("fail", name, "the served GRAPH is not valid JSON: %s" % exc)
        return {}

    nodes, links = graph.get("nodes"), graph.get("links")
    if not isinstance(nodes, list) or not isinstance(links, list):
        report.line("fail", name, "GRAPH has no nodes[] and links[] arrays")
        return {}
    if not nodes:
        report.line("fail", name, "nodes[] is empty - there is nothing in the galaxy",
                    ["put markdown in notes/ and run: python3 build.py"])
        return {}

    bad_ids = [n for n in nodes if n.get("id") != n.get("index")]
    bad_links = [l for l in links
                 if not (isinstance(l.get("source"), int) and isinstance(l.get("target"), int)
                         and 0 <= l["source"] < len(nodes) and 0 <= l["target"] < len(nodes))]
    bare = [n for n in nodes if not n.get("label")]
    if bad_ids or bad_links or bare:
        report.line("fail", name, "the served graph breaks the viewer's contract",
                    ["%d node(s) with id != index" % len(bad_ids),
                     "%d link(s) pointing outside nodes[]" % len(bad_links),
                     "%d node(s) with no label" % len(bare)])
        return {}
    report.line("pass", name, "%d nodes, %d links, every id == its index" % (len(nodes), len(links)))
    return {"graph": graph}


# --------------------------------------------------------------------------- #
# 3. /chat - a real question, a well-formed answer, a nodes array
# --------------------------------------------------------------------------- #
def check_chat(report: Report, base: str, graph: dict, brain_configured: bool, timeout: float) -> dict:
    name = "3. /chat answers a real question, with a nodes array"
    nodes = (graph or {}).get("nodes") or []
    if not nodes:
        report.line("warn", name, "no graph to build a question from", ["see check 2"])
        return {}
    # The question is built from a real note title, so retrieval is genuinely exercised
    # rather than hoped for.
    note = nodes[0]
    question = 'what do my notes say about "%s"?' % note["label"]
    status, _h, body, err = post(base + "/chat", {"question": question}, timeout)
    if status is None:
        report.line("fail", name, "could not reach /chat (%s)" % err)
        return {}
    if status != 200:
        report.line("fail", name, "POST /chat -> HTTP %s" % status, [body.strip()[:200]])
        return {}
    data = as_json(body)
    if not isinstance(data, dict):
        report.line("fail", name, "/chat did not return a JSON object", [body.strip()[:200]])
        return {}

    got = data.get("nodes")
    if not isinstance(got, list):
        report.line("fail", name, "the answer carried no nodes array at all",
                    [json.dumps(data)[:200]])
        return {}
    outside = [i for i in got if not (isinstance(i, int) and 0 <= i < len(nodes))]
    if outside:
        report.line("fail", name, "nodes[] points outside the served graph: %s" % outside[:6],
                    ["the galaxy would light a star that does not exist (graph has %d nodes)"
                     % len(nodes)])
        return {}

    answer = data.get("answer")
    if not got:
        report.line("warn", name, "the answer is well-formed but matched no notes",
                    ["asked: %s" % question,
                     "the vault may simply not cover that - try a question about a real note"])
        return {"asked": question}
    if not isinstance(answer, str) or not answer.strip():
        report.line("fail", name, "no answer text came back", [json.dumps(data)[:200]])
        return {}
    if data.get("ok") is False:
        code = data.get("code")
        if code in ("placeholder_api_key", "missing_api_key") and not brain_configured:
            report.line("warn", name, "notes retrieval works (%d node(s), all valid) but there is no key"
                        % len(got),
                        ["asked: %s" % question,
                         "the server said: %s" % str(data.get("error"))[:120],
                         "paste your key into config.json and restart server.py to prove the brain half"])
            return {"asked": question, "nodes": got}
        report.line("fail", name, "/chat reported a failure with a key in place",
                    ["code=%s" % code, error_message(body)])
        return {"asked": question, "nodes": got}

    report.line("pass", name, "%d chars from %s, %d node(s) used, every index valid"
                % (len(answer), data.get("model") or "the model", len(got)))
    return {"asked": question, "nodes": got, "answer": answer}


# --------------------------------------------------------------------------- #
# 4. the key is valid - one real minimal call
# --------------------------------------------------------------------------- #
def check_key(report: Report, config_path: str, health: dict, timeout: float) -> dict:
    name = "4. the API key in config.json is valid"
    cfg = read_config(config_path)
    key = (cfg.get("openai_api_key") or "").strip()
    state = key_state(key)
    model = (cfg.get("model") or "").strip()
    api_base = (health.get("api_base_url") or DEFAULT_BASE_URL).rstrip("/")

    notes = []
    live_path = (health.get("key") or {}).get("path")
    if live_path and os.path.abspath(live_path) != os.path.abspath(config_path):
        notes.append("the RUNNING SERVER is reading %s, not %s - check which one you edited"
                     % (live_path, config_path))
    if state == "missing":
        report.line("warn", name, "config.json has no openai_api_key",
                    notes + ['add {"openai_api_key": "sk-...", "model": "..."} to %s' % config_path])
        return {"key": "", "api_base": api_base, "configured": False}
    if state == "placeholder":
        report.line("warn", name, "config.json still holds the placeholder (%s)" % masked(key),
                    notes + ["nothing real to verify yet - paste your key and restart server.py",
                             "until then /chat and /see answer with the clean 'paste your key' error"])
        return {"key": key, "api_base": api_base, "configured": False}

    # A real call to the provider with this key. The models list is the cheapest proof there
    # is; if the key is scoped so that it cannot read that list, ask the endpoint that
    # actually matters instead of calling a working key broken.
    status, _h, body, err = http(api_base + "/models", "GET", timeout=timeout,
                                 headers={"Authorization": "Bearer %s" % key})
    if status == 200:
        data = as_json(body) or {}
        models = [m.get("id") for m in data.get("data") or [] if isinstance(m, dict)]
        report.line("pass", name, "one real GET %s/models with %s -> 200 (%d models)"
                    % (api_base, masked(key), len(models)), notes)
        return {"key": key, "api_base": api_base, "configured": True, "models": models}
    if status is None:
        report.line("fail", name, "the key is set, but %s could not be reached" % api_base,
                    notes + [str(err), "every answer will be an error until this machine can reach it"])
        return {"key": key, "api_base": api_base, "configured": True}
    if status in (401, 403) and model:
        # One last real chance: some keys may not list models but still answer.
        status2, _h2, body2, err2 = brain_probe(api_base, key, model, timeout)
        if status2 == 200:
            report.line("pass", name, "%s/models was refused (HTTP %s) but a real call with the key worked"
                        % (api_base, status), notes + ["so the key is live, just not allowed to list models"])
            return {"key": key, "api_base": api_base, "configured": True}
        report.line("fail", name, "THE KEY WAS REJECTED by %s (HTTP %s, and the model call said %s)"
                    % (api_base, status, status2 or err2),
                    notes + [error_message(body2) or error_message(body), "config.json holds %s" % masked(key)])
        return {"key": key, "api_base": api_base, "configured": True}
    report.line("fail", name, "%s/models returned HTTP %s for a key that is set" % (api_base, status),
                notes + [error_message(body)])
    return {"key": key, "api_base": api_base, "configured": True}


# --------------------------------------------------------------------------- #
# 5. the model in config.json is one the key can reach
# --------------------------------------------------------------------------- #
def check_model(report: Report, config_path: str, health: dict, key_info: dict, timeout: float) -> None:
    name = "5. the model in config.json is reachable"
    cfg = read_config(config_path)
    wanted = (cfg.get("model") or "").strip()
    # The CONFIG brain, not the one in the chair. A runtime swap is a legitimate state, so
    # what has to match config.json is the model a RESTART would use - health["config_model"].
    running = (health.get("config_model") or health.get("model") or "").strip()
    api_base = (health.get("config_api_base_url") or key_info.get("api_base")
                or DEFAULT_BASE_URL)
    notes = []
    if health.get("swapped"):
        notes.append("a runtime swap is in the chair right now (%s); a restart goes back to "
                     "%s, and that is what this check tries" % (health.get("model"), wanted or "?"))
    if not wanted:
        report.line("fail", name, "config.json names no model",
                    ["set \"model\" explicitly, or the server silently uses its own default"])
        return
    if running and running != wanted:
        report.line("fail", name, "a restart would use %r but config.json says %r" % (running, wanted),
                    ["the running server started before that edit - restart server.py"])
        return
    if not key_info.get("configured"):
        report.line("warn", name, "config.json names %r and the server agrees, but the key is not set"
                    % wanted, notes + ["reachability cannot be proven without a key"])
        return

    # One real minimal completion. A listed model can still be unreachable on a plan, and a
    # gateway that does not list models can still serve one, so this asks the model itself.
    status, _h, body, err = brain_probe(api_base, key_info.get("key", ""), wanted, timeout)
    if status is None:
        report.line("fail", name, "could not reach %s to try %r" % (api_base, wanted), notes + [str(err)])
        return
    if status == 404:
        report.line("fail", name, "THE KEY CANNOT REACH %r (HTTP 404 from %s)" % (wanted, api_base),
                    notes + [error_message(body),
                             "models this key can see: %s" % (", ".join(key_info.get("models") or [])[:180]
                                                             or "(it would not list them)")])
        return
    if status in (401, 403):
        report.line("fail", name, "%r was refused with HTTP %s" % (wanted, status),
                    notes + [error_message(body)])
        return
    if status != 200:
        report.line("fail", name, "a one-token call to %r returned HTTP %s" % (wanted, status),
                    notes + [error_message(body)])
        return
    data = as_json(body) or {}
    if not data.get("choices"):
        report.line("fail", name, "%r answered 200 with no choices" % wanted, [body.strip()[:200]])
        return
    report.line("pass", name, "one real 1-token call to %r -> 200 (served as %r)"
                % (wanted, data.get("model") or wanted), notes)


# --------------------------------------------------------------------------- #
# 6. /remember writes a real file, and /chat can use it immediately
# --------------------------------------------------------------------------- #
def check_remember(report: Report, base: str, health: dict, brain_configured: bool, timeout: float,
                   keep: bool) -> None:
    name = "6. /remember writes a real file, searchable at once"
    notes_dir = health.get("notes_dir")
    if not notes_dir or not os.path.isdir(notes_dir):
        report.line("fail", name, "the server did not report a readable notes folder",
                    ["notes_dir=%r" % notes_dir, "start the server with --notes <folder>"])
        return

    # Two random strings, and the reason matters. `handle` goes in the note AND in the
    # question, so the question can name this one note and no other - which is what lets this
    # check be run twice against the same server without crying wolf, because a long-lived
    # server keeps every probe note from every earlier run in its memory, and a generic
    # question matches all of them equally. `canary` goes in the note but never in the
    # question, so the canary assertion below still proves the model READ the note rather
    # than parroting the question back at us.
    handle = "".join(random.choice(string.ascii_lowercase) for _ in range(6))
    canary = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(6))
    sentence = ("remember that the preflight probe %s proves the write path end to end "
                "with canary %s" % (handle, canary))
    status, _h, body, err = post(base + "/remember", {"text": sentence}, timeout)
    data = as_json(body) or {}
    # A file may exist in the vault from this moment on - even on a failure, because the
    # server reports where it wrote before it complains. Whatever happens next, the vault is
    # left exactly as it was found: a check that fails must not leave its own rubbish in your
    # notes folder for the next run to trip over. (Learned the hard way: the early returns
    # used to skip the tidy-up, and a red check 6 left a probe note behind.)
    rel = data.get("file") or ""
    path = os.path.join(notes_dir, rel) if rel else ""

    def tidy():
        if keep or not path:
            return
        try:
            if os.path.exists(path):
                os.remove(path)
                folder = os.path.dirname(path)
                if os.path.isdir(folder) and not os.listdir(folder):
                    os.rmdir(folder)
            report.info("probe note removed again (%s); the running server holds it in memory "
                        "until it restarts" % rel)
        except OSError as exc:
            report.info("could not remove the probe note (%s): %s" % (path, exc))

    try:
        if status is None:
            report.line("fail", name, "could not reach /remember (%s)" % err)
            return
        if status != 200 or not data.get("ok"):
            report.line("fail", name, "POST /remember -> HTTP %s" % status,
                        ["code=%s" % data.get("code"), error_message(body),
                         "the file %s" % (rel or "(no path was reported)")])
            return

        index = data.get("index")
        on_disk = read_file(path) if path else None
        problems = []
        if not (data.get("filed") and data.get("indexed")):
            problems.append("the server reported filed=%r indexed=%r"
                            % (data.get("filed"), data.get("indexed")))
        if on_disk is None:
            problems.append("no file at %s" % (path or "(no path was reported)"))
        else:
            text = on_disk.decode("utf-8", "replace")
            if handle not in text or canary not in text:
                problems.append("the file does not carry both words that were filed")
            if time.strftime("%Y-%m-%d") not in text:
                problems.append("the file carries no date")
        if problems:
            report.line("fail", name, "the capture did not land", problems)
            return

        # ... and the whole point: the note is answerable NOW, no rebuild and no restart.
        #    The question names this note by its own word and NOT by its canary, so it can
        #    only be answered by a brain that really has the file - and so that running
        #    preflight twice does not leave two probe notes racing for the same question.
        question = ("which note carries the canary for the preflight probe %s? read it back "
                    "to me" % handle)
        status2, _h2, body2, _err2 = post(base + "/chat", {"question": question}, timeout)
        reply = as_json(body2) or {}
        got = reply.get("nodes") or []
        if index not in got:
            report.line("fail", name, "the file was written but /chat cannot find it",
                        ["%s is on disk (%d bytes), index %s" % (rel, len(on_disk), index),
                         "asked: %s" % question,
                         "/chat returned nodes %s" % got[:8],
                         "filing a note indexes it immediately - if this fails, the write and "
                         "the index disagree"])
            return

        answer = str(reply.get("answer") or "")
        lines = ["file: %s (%d bytes)" % (rel, len(on_disk)),
                 "asked: %s" % question,
                 "/chat used node %s - the note filed a second earlier" % index]
        if brain_configured and reply.get("ok") is not False and canary not in answer.lower():
            report.line("warn", name,
                        "the file was written and found, but the answer came back without the canary",
                        lines + ["the answer said: %r" % answer[:140]])
        else:
            report.line("pass", name, "wrote %s and /chat answered from it immediately" % rel,
                        lines)
    finally:
        tidy()


# --------------------------------------------------------------------------- #
# 7. /see answers a real JPEG - with the media type the client really sends
# --------------------------------------------------------------------------- #
def check_see(report: Report, base: str, health: dict, html: str, brain_configured: bool,
              timeout: float) -> None:
    name = "7. /see answers a real JPEG"
    frame = read_file(SCREEN_FIXTURE)
    if frame is None:
        report.line("fail", name, "the probe frame is missing: %s" % SCREEN_FIXTURE)
        return

    # The media type a probe should send is the one the CLIENT sends. Reading it out of the
    # page the browser is actually being served is what stops a PNG probe from 400-ing and
    # looking like a dead endpoint: if these two ever disagree, this check is wrong, not /see.
    match = re.search(r"toDataURL\(\s*['\"](image/[a-z]+)['\"]", html or "")
    if not match:
        # No page at all means check 1 already failed and there is nothing to be strict about;
        # a page that IS served but no longer says how it encodes is a real break, and the
        # probe refusing to guess is the whole point of reading it out of the page.
        if not html:
            report.line("warn", name, "no page was fetched, so the probe cannot know which media type the "
                        "client sends", ["see check 1 - /see itself was not tested"])
            return
        report.line("fail", name, "the served viewer no longer says which media type it encodes",
                    ["so a probe cannot be trusted to send what the client sends",
                     "fix the viewer or this probe before believing a green /see line"])
        return
    client_type = match.group(1)
    actual = sniff(frame)
    if actual != client_type:
        report.line("fail", name, "the viewer sends %s but this probe holds a %s" % (client_type, actual),
                    ["a probe with the wrong media type 400s and mimics a dead endpoint",
                     "put a real %s frame at %s" % (client_type, SCREEN_FIXTURE)])
        return

    before = ((as_json(get(base + "/health", timeout)[2]) or {}).get("sight") or {}).get("frames")
    now = int(time.time() * 1000)
    status, _h, body, err = post(base + "/see", {
        "question": "what is on this screen?",
        "image": "data:%s;base64,%s" % (client_type, base64.b64encode(frame).decode("ascii")),
        "media_type": client_type,
        "asked_at": now, "captured_at": now,
    }, timeout)
    if status is None:
        report.line("fail", name, "could not reach /see (%s)" % err)
        return
    data = as_json(body) or {}
    info = data.get("frame") or {}

    if status != 200 or data.get("ok") is False:
        code = data.get("code")
        if code in ("placeholder_api_key", "missing_api_key") and not brain_configured:
            # Even without a key the frame itself is a real chain: the server sniffed it,
            # measured it and refused to invent anything. Check that, then warn about the key.
            problems = []
            if info.get("media_type") != client_type:
                problems.append("the server read the frame as %r" % info.get("media_type"))
            if (info.get("width"), info.get("height")) != (640, 360):
                problems.append("the server measured %sx%s, and it is 640x360"
                                % (info.get("width"), info.get("height")))
            if problems:
                report.line("fail", name, "the frame itself was mishandled", problems)
                return
            report.line("warn", name, "the %s frame was accepted and measured (%dx%d, %d bytes), but there is no key"
                        % (client_type, info.get("width"), info.get("height"), len(frame)),
                        ["the server said: %s" % str(data.get("error"))[:140],
                         "the frame path is proven; only the model call is unproven without a key"])
            return
        report.line("fail", name, "POST /see -> HTTP %s (code=%s)" % (status, code),
                    [error_message(body),
                     "sent media_type=%s, %d bytes, a real %s" % (client_type, len(frame), actual)])
        return

    problems = []
    if data.get("decision") != "screen":
        problems.append("decision was %r, not 'screen'" % data.get("decision"))
    if data.get("nodes") or data.get("sources"):
        problems.append("a screen answer must light nothing: nodes=%s sources=%s"
                        % (data.get("nodes"), data.get("sources")))
    if info.get("media_type") != client_type:
        problems.append("the server recorded %r for a %s frame" % (info.get("media_type"), client_type))
    if (info.get("width"), info.get("height")) != (640, 360):
        problems.append("the server read the frame as %sx%s, and it is 640x360"
                        % (info.get("width"), info.get("height")))
    if not str(data.get("answer") or "").strip():
        problems.append("no answer text came back")
    if problems:
        report.line("fail", name, "the endpoint answered but its bookkeeping is wrong", problems)
        return

    after = ((as_json(get(base + "/health", timeout)[2]) or {}).get("sight") or {}).get("frames")
    notes = ["a real 640x360 %s, %d bytes, sent as media_type=%s" % (client_type, len(frame), client_type),
             "answer: %r" % str(data.get("answer"))[:120]]
    if isinstance(before, int) and isinstance(after, int) and after != before + 1:
        report.line("warn", name, "answered, but the frame counter went %s -> %s" % (before, after), notes)
        return
    report.line("pass", name, "one real frame in, one real answer out (frames %s -> %s)" % (before, after), notes)


# --------------------------------------------------------------------------- #
# 8. what the browser is served is what is on disk
# --------------------------------------------------------------------------- #
def check_served_matches_disk(report: Report, base: str, html: str, timeout: float) -> None:
    name = "8. the files the browser is served match the disk"
    if not html:
        report.line("warn", name, "no page was fetched, so nothing could be compared", ["see check 1"])
        return

    # Everything the page asks for from this server: the page, the graph, and any relative
    # .js/.css/.mjs it references. CDN URLs are not this server's business.
    wanted = [("/", os.path.join(VIEWER, "index.html")),
              ("/graph-data.js", os.path.join(VIEWER, "graph-data.js"))]
    for ref in re.findall(r"""(?:src|href)\s*=\s*["']([^"']+\.(?:js|css|mjs))["']""", html):
        if re.match(r"^[a-z]+:", ref) or ref.startswith("//"):
            continue
        rel = ref.split("?")[0].split("#")[0].lstrip("./").lstrip("/")
        wanted.append(("/" + rel, os.path.join(VIEWER, rel)))

    stale, missing, unreachable = [], [], []
    checked = 0
    for url, path in wanted:
        disk = read_file(path)
        status, _h, body, err = get(base + url, timeout)
        if status is None:
            unreachable.append("%s: %s" % (url, err))
            continue
        if status != 200:
            unreachable.append("%s: HTTP %s" % (url, status))
            continue
        checked += 1
        served = body.encode("utf-8", "replace")
        if disk is None:
            missing.append("%s (no file at %s)" % (url, os.path.relpath(path, ROOT)))
        elif served != disk:
            stale.append("%s: served %s, disk %s (disk edited %s)"
                         % (url, sha(served), sha(disk),
                            time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(path)))))
    problems = stale + missing + unreachable
    if problems:
        report.line("fail", name, "the browser is not getting what you edited",
                    problems[:6] + ["restart server.py, or reload the page - this is the "
                                    "'I fixed it but nothing changed' failure"])
        return
    report.line("pass", name, "%d served file(s) byte-identical to disk" % checked,
                ["/  ==  %s" % os.path.relpath(wanted[0][1], ROOT)])


# --------------------------------------------------------------------------- #
# 9. config.json is not reachable from the browser - the one that must shout
# --------------------------------------------------------------------------- #
def check_secrets(report: Report, base: str, config_path: str, timeout: float) -> None:
    name = "9. config.json is not reachable from the browser"
    if not os.path.isfile(config_path):
        report.line("warn", name, "there is no %s to protect" % config_path,
                    ["nothing can be checked until the server has a config"])
        return
    if os.path.abspath(config_path).startswith(os.path.abspath(VIEWER) + os.sep):
        report.line("fail", name, "config.json is INSIDE viewer/, which is served",
                    [config_path, "move it to the project root: that is the whole point of the split"])
        return

    cfg = read_config(config_path)
    key = (cfg.get("openai_api_key") or "").strip()
    watched = ["openai_api_key"] + ([key] if key_state(key) == "set" else [])

    paths = ["/config.json", "/../config.json", "/%2e%2e/config.json", "/..%2fconfig.json",
             "/%2e%2e%2fconfig.json", "/./../config.json", "/....//config.json",
             "/.git/config", "/../server.py", "/../build.py", "/../preflight.py",
             "/../notes/", "/../config.json.bak"]
    leaked, served_ok, answered = [], [], 0
    for path in paths:
        status, _h, body, _err = get(base + path, timeout)
        if status is None:
            continue                        # nothing answered: counted below, not a refusal
        answered += 1
        hits = [needle for needle in watched if needle and needle in body]
        if hits:
            leaked.append("%s -> HTTP %s and the body contains %s" % (path, status, ", ".join(hits)))
        elif status == 200:
            served_ok.append("%s -> HTTP 200" % path)
    for url, body in SEEN:                       # every byte fetched during this whole run
        for needle in watched:
            if needle and needle in body:
                leaked.append("the key (or its own field name) came back in the response to %s" % url)
                break
    if leaked:
        report.say("")
        report.say(report.paint("  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!", "31;1"))
        report.say(report.paint("  !!   THE API KEY IS REACHABLE FROM THE BROWSER                !!", "31;1"))
        report.say(report.paint("  !!   anyone who opens the page can read it and spend it       !!", "31;1"))
        report.say(report.paint("  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!", "31;1"))
        report.line("fail", name, "the key is leaking", leaked[:6] +
                    (["files that should never be served came back 200: %s" % served_ok[:3]] if served_ok else []) +
                    ["fix the path handling in server.py before anything else"])
        return
    if served_ok:
        report.line("fail", name, "files that are not the viewer came back 200: %s" % served_ok[:4],
                    ["nothing secret was in them this time, but they must not be served at all"])
        return
    if not answered:
        # A dead server refuses everything, including things it should serve. Passing here
        # would be the worst kind of green: nothing was tested at all.
        report.line("warn", name, "nothing answered, so nothing was tested",
                    ["see check 1 - the traversal probes never reached a server"])
        return
    report.line("pass", name, "%d traversal attempts refused by a server that answered, and the key never "
                "appeared in %d response(s)" % (answered, len(SEEN)))


# --------------------------------------------------------------------------- #
# 10. incident: "I restarted it" - except the process had not been restarted
# --------------------------------------------------------------------------- #
def check_restart(report: Report, health: dict) -> None:
    """incident 2026-09-23: server.py was edited and the running process kept answering
    with the old code. Every other check stayed green - the old code still worked - and the
    only clue was that behaviour did not change.
    """
    name = "10. the running server is not older than server.py"
    uptime = health.get("uptime_s")
    if not isinstance(uptime, (int, float)):
        report.line("warn", name, "the server did not report an uptime",
                    ["restart it - this check needs the current server.py"])
        return
    path = os.path.join(ROOT, "server.py")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        report.line("warn", name, "could not stat server.py", [])
        return
    started = time.time() - float(uptime)
    if mtime > started + 1:
        report.line("warn", name, "server.py was edited %.1f min AFTER this process started"
                    % ((mtime - started) / 60.0),
                    ["the process has been up %s and is running older code" % minutes(uptime),
                     "restart it:  kill the server and run python3 server.py"])
        return
    report.line("pass", name, "process up %s, and server.py has not changed since" % minutes(uptime))


# --------------------------------------------------------------------------- #
# 11. incident: the galaxy could not boot at all - no CDN and no offline copy
# --------------------------------------------------------------------------- #
def check_boot_dependencies(report: Report, timeout: float) -> None:
    """incident 2026-09-23: the browser checks could not get past the loading screen -
    jsDelivr is unreachable from this machine and viewer/vendor/ (the fallback the viewer
    ships) had not been staged. Every headless test passed while the page was dead in a
    real browser.
    """
    name = "11. the viewer can boot without a CDN"
    vendor = os.path.join(VIEWER, "vendor", "3d-force-graph.min.js")
    three = os.path.join(VIEWER, "vendor", "three.module.min.js")
    if os.path.isfile(vendor) and os.path.isfile(three):
        staged = (os.path.getsize(vendor) + os.path.getsize(three)) / 1048576.0
        report.line("pass", name, "the offline copy is staged (%0.1f MB in viewer/vendor/)" % staged)
        return
    url = "https://cdn.jsdelivr.net/npm/3d-force-graph@1.80.0/dist/3d-force-graph.min.js"
    status, _h, _b, err = http(url, "GET", timeout=min(timeout, 8))
    if status == 200:
        report.line("pass", name, "no local copy, but the CDN answered just now", [url])
        return
    report.line("fail", name, "the page would not boot here: no viewer/vendor/ copy and no CDN",
                ["%s -> %s" % (url, status or err), "run:  python3 tools/vendor.py"])


# --------------------------------------------------------------------------- #
# 12. incident: a note the brain cannot see (or a note that no longer exists)
# --------------------------------------------------------------------------- #
def check_notes_agree(report: Report, health: dict, base: str, timeout: float) -> None:
    """incident 2026-09-23: notes are read when the server starts. A markdown file dropped
    into the folder while it runs is invisible ("I added a note and it will not answer from
    it"), and a file deleted while it runs stays in the index. Both are silent.

    It reads the brain LIVE rather than from the boot snapshot everything else uses, and it
    sets preflight's own probe notes aside on BOTH sides of the comparison: this check runs
    after check 6 has filed a note, and a note filed during the run is exactly the kind of
    thing that made it report a disagreement that was preflight's own doing.
    """
    name = "12. the notes on disk and the notes the server knows agree"
    status, _h, body, _err = get(base + "/health", timeout)
    live = as_json(body)
    if isinstance(live, dict) and isinstance(live.get("notes"), int):
        health = live                       # the live count, not the one from before check 6
    notes_dir = health.get("notes_dir")
    server_count = health.get("notes")
    source = str(health.get("notes_source") or "")
    if not notes_dir or not isinstance(server_count, int):
        report.line("warn", name, "the server did not say where its notes come from", [])
        return
    if "note" not in source.lower():
        report.line("warn", name, "the server is reading %r, not the notes folder" % source,
                    ["so disk agreement cannot be checked in that mode"])
        return
    if not os.path.isdir(notes_dir):
        report.line("fail", name, "the notes folder it reported does not exist: %s" % notes_dir)
        return
    probe_prefix = "the-preflight-probe-"
    on_disk = 0
    for _dirpath, _dirs, files in os.walk(notes_dir):
        for name in files:
            if not name.lower().endswith(".md") or name.startswith("."):
                continue
            if name.lower().startswith(probe_prefix):
                continue                    # preflight's own, from a run that was interrupted
            on_disk += 1
    # Preflight's own probe notes are written and removed during a run: anything of that
    # shape still in the index is preflight's doing, not a mystery for the user to solve.
    titles = health.get("titles") or []
    probes = [t for t in titles if str(t).startswith(PROBE_TITLE_PREFIX)]
    counted = server_count - len(probes)
    notes = ["%d note(s) in the server's memory, %d .md file(s) in %s" % (server_count, on_disk, notes_dir)]
    if probes:
        notes.append("%d preflight probe note(s) from an earlier run are still in its memory (files removed)"
                     % len(probes))
    if counted == on_disk:
        report.line("pass", name, "the server knows exactly the %d note(s) on disk" % on_disk, notes)
        return
    if on_disk > counted:
        notes.append("%d file(s) on disk are NOT in the brain: it reads the folder at boot, so restart "
                     "server.py (filing through the ask bar indexes immediately)" % (on_disk - counted))
    else:
        notes.append("%d note(s) in the brain have no file: they were deleted after it started - "
                     "restart server.py" % (counted - on_disk))
    report.line("warn", name, "the brain and the folder disagree", notes)


# --------------------------------------------------------------------------- #
# 13. incident: "switch to opus 5" - a near miss must never become another model
# --------------------------------------------------------------------------- #
def check_swap(report: Report, base: str, config_path: str, health: dict, timeout: float,
               enabled: bool) -> None:
    """Added with the feature it checks, 2026-09-23.

    The failure this exists for: you say "opus 5", a loose matcher sees "opus", throws the
    version away, loads an older Opus, and cheerfully announces it did what you asked - so
    you spend an hour testing the wrong model. An honest error beats a helpful guess.

    So this check insists on all four: the swap takes, the swap is runtime only (config.json
    byte-identical), a version that is not in the set is REFUSED, and the refusal moves
    nothing. It puts the config brain back before it returns, whatever happened.
    """
    name = "13. a brain swap is runtime-only, and a near-miss is refused"
    if not enabled:
        report.line("warn", name, "not run (--no-swap)", [])
        return
    brains = health.get("brains") or []
    live, config_model = health.get("model"), health.get("config_model")
    if not brains or live is None or config_model is None:
        report.line("warn", name, "this server does not report its catalogue or its brains",
                    ["it is probably older than POST /model - restart it"])
        return
    before_bytes = read_file(config_path)
    if before_bytes is None:
        report.line("warn", name, "config.json could not be read, so it cannot be compared", [])
        return

    # Build a candidate id the way the server says it does: a family plus a version, with
    # the version put where the family's template says it goes. Then check the server really
    # accepts that exact id. (The catalogue carries templates, not ids: /health describes
    # what CAN be worn, and only POST /model decides what IS worn.)
    targets = []
    for b in brains:
        template = b.get("id") or ""
        if "{v}" not in template:
            continue
        for version in (b.get("versions") or []):
            candidate = template.replace("{v}", version)
            if candidate != live:
                targets.append((b.get("name") or "", version, candidate))
    if not targets:
        report.line("warn", name, "the catalogue has nothing other than the current brain", [])
        return
    # prefer a version with a dot in it: that is the one the label rule is really about
    family, version, target_id = next((t for t in targets if "." in t[1]), targets[0])
    family_versions = next((b.get("versions") or [] for b in brains if b.get("name") == family), [])

    notes = []
    back = {}
    try:
        # 1. the swap itself, and the label the little chip will carry. The spoken name is
        #    the family and the version, never the raw id: that is how a person would say it.
        status, _h, body, err = post(base + "/model",
                                     {"text": "switch to %s %s" % (family, version)}, timeout)
        data = as_json(body) or {}
        if status != 200 or not data.get("ok"):
            report.line("fail", name, "POST /model could not put %s in the chair (%s)"
                        % (target_id, data.get("code") or err or status),
                        [str(data.get("error") or data.get("answer") or body)[:200]])
            return
        if data.get("model") != target_id or not data.get("swapped"):
            report.line("fail", name, "the swap did not take: it reports %r" % data.get("model"),
                        [json.dumps(data)[:220]])
            return
        want_label = brain_label(target_id)
        if data.get("label") != want_label:
            report.line("fail", name, "the chip would read %r, and the rule says %r"
                        % (data.get("label"), want_label),
                        ["only a hyphen between two digits is a version dot"])
            return

        # 2. a near miss in the same family: a version that is not in the set
        miss = "%s 999" % family
        status2, _h2, body2, _e2 = post(base + "/model", {"text": "switch to %s" % miss}, timeout)
        refused = as_json(body2) or {}
        if status2 != 200 or refused.get("ok") is not False or not refused.get("refused"):
            report.line("fail", name, "THE NEAR MISS WAS NOT REFUSED: %r came back as %r"
                        % (miss, refused.get("model") or refused.get("code")),
                        ["a version that does not exist must never be rounded to one that does",
                         json.dumps(refused)[:220]])
            return
        if refused.get("model") != target_id:
            report.line("fail", name, "the refusal MOVED THE BRAIN to %r" % refused.get("model"),
                        ["a refusal must change nothing at all"])
            return
        told = str(refused.get("answer") or "")
        if family and family not in told:
            report.line("fail", name, "the refusal does not name the family it refused", [told[:200]])
            return
        notes.append("\"switch to %s %s\" put %s in the chair; \"%s\" was refused: %s"
                     % (family, version, target_id, miss, told[:130]))
        if family_versions and not any(v in told for v in family_versions):
            notes.append("and the refusal does not read back the versions it does have (%s)"
                         % ", ".join(family_versions))
    finally:
        # always put the chair back: this check must never leave a live server on a brain
        # that was not the person's decision
        status3, _h3, body3, _e3 = post(base + "/model",
                                        {"text": "go back to your normal brain"}, timeout)
        back = as_json(body3) or {}
        if status3 != 200 or back.get("model") != config_model:
            report.line("fail", name, "COULD NOT PUT THE CONFIG BRAIN BACK: the server is on %r"
                        % (back.get("model") or "?"),
                        ["restart server.py to get back to %s" % config_model])
            return

    if read_file(config_path) != before_bytes:
        report.line("fail", name, "THE SWAP WAS WRITTEN TO config.json",
                    ["a restart must always come back to the brain in that file, and now it cannot"])
        return
    if back.get("swapped"):
        report.line("fail", name, "the reset left a swap in place: %r" % back.get("model"), [])
        return
    report.line("pass", name, "%s worn and taken off; \"999\" refused without moving the chair"
                % target_id,
                notes + ["config.json is byte-identical after the whole thing"])


# 14. incident: a server that "watches" without watching, and a state with a name in it
# --------------------------------------------------------------------------- #
def focus_identity_leak(state) -> list:
    """Anything in this state that looks like it names an app or a place.

    The states carry his sentences too - a callout, a report card - but those are NESTED,
    and the rule only ever applies to the top-level scalars, which are booleans, counters
    and short enum words. A bundle id and a host name both carry a dot, a URL carries a
    slash, and nothing legitimate here is longer than a couple of words, so this is a
    structural check rather than a list of words to go looking for.
    """
    leaked = []
    if not isinstance(state, dict):
        return leaked
    for key, value in state.items():
        if isinstance(value, str) and (len(value) > 24 or any(ch in value for ch in "./:@\\")):
            leaked.append("%s=%r" % (key, value))
    return leaked



def check_focus(report: Report, base: str, health: dict, timeout: float, enabled: bool) -> None:
    """Added with the feature it checks, 2026-09-24.

    The failure this exists for is invisible from a unit test: the tick is a THREAD inside a
    server that lives for weeks, and the tempting way to write it - reading whatever an
    app-switch notification last delivered - stops delivering, reports the first app forever,
    and goes on cheerfully counting. Nothing errors. Nothing logs. The clock just stops
    meaning anything. So this check insists that the running server, right now, takes a real
    session from a real request, counts it down on its own with no browser open, asks the
    front app FRESH every tick, and hands back a state with no identity in it anywhere.

    It also refuses to touch a session that is already running - if you are mid-session when
    you run preflight, it says so and leaves you alone.
    """
    name = "14. a focus session ticks on the server, and the state carries no name"
    if not enabled:
        report.line("warn", name, "not run (--no-focus)", [])
        return
    block = (health or {}).get("focus")
    status, _h, body, err = get(base + "/focus", timeout)
    if status != 200 or not as_json(body):
        report.line("warn", name, "this server does not answer GET /focus (%s)"
                    % (err or status),
                    ["it is probably older than focus.py - restart it: kill the server and "
                     "run python3 server.py"])
        return
    state = as_json(body)
    if not block:
        report.line("warn", name, "GET /focus answers, but /health carries no focus block",
                    ["the server is older than the last edit; restart it"])
        return

    notes = []
    # 1. nothing that names you may travel in the state - and the state a RUNNING session
    #    hands back matters most, because that is when a reader has something to leak
    if focus_identity_leak(state):
        report.line("fail", name, "THE STATE CARRIES SOMETHING THAT NAMES YOU",
                    focus_identity_leak(state)[:4] +
                    ["the reader compares identities and throws them away; a name in this "
                     "state is a name on the page"])
        return

    # 2. a session that is already running is the person's, not preflight's
    if state.get("phase") in ("running", "paused"):
        report.line("warn", name, "a session is already running (id %s, %s left) - not touched"
                    % (state.get("id"), minutes(state.get("remaining_s") or 0)),
                    ["end it, or wait for it, and run preflight again to prove the tick here"])
        return

    # 3. the tick itself, with no browser anywhere near it
    started = post(base + "/focus", {"text": "thirty minutes on this"}, timeout)
    reply = as_json(started[2]) or {}
    if started[0] != 200 or not reply.get("ok"):
        code = reply.get("code") or started[0]
        hint = str(reply.get("answer") or reply.get("error") or "")[:160]
        if code in ("focus_blind", "focus_home"):
            report.line("warn", name,
                        "a session cannot be started on this machine right now (%s)" % code,
                        [hint, "the reader is a command (:focus.reader in /health, "
                               "--focus-reader on the command line): on anything that is not "
                               "a Mac it has to be pointed at one"])
            return
        report.line("fail", name, "POST /focus could not start a session (%s)" % code, [hint])
        return
    if focus_identity_leak(reply.get("focus")):
        report.line("fail", name, "THE STATE CARRIES SOMETHING THAT NAMES YOU (the reply to "
                    "starting a session)",
                    focus_identity_leak(reply.get("focus"))[:4] +
                    ["the reader compares identities and throws them away: a name that reaches "
                     "this reply is a name on the page, and in the log, and on the way back"])
        return
    session_id = (reply.get("focus") or {}).get("id")
    try:
        before = (reply.get("focus") or {})
        ticks_before = (before.get("timings") or {}).get("ticks", 0)
        runs_before = (before.get("timings") or {}).get("reader_runs", 0)
        left_before = before.get("remaining_s") or 0
        time.sleep(2.6)
        status2, _h2, body2, _e2 = get(base + "/focus", timeout)
        after = as_json(body2) or {}
        if focus_identity_leak(after):
            report.line("fail", name, "THE STATE CARRIES SOMETHING THAT NAMES YOU (while running)",
                        focus_identity_leak(after)[:4] +
                        ["this is the state the page polls once a second"])
            return
        timings = after.get("timings") or {}
        ticks = timings.get("ticks", 0) - ticks_before
        runs = timings.get("reader_runs", 0) - runs_before
        left_after = after.get("remaining_s") or 0
        if ticks < 2 or left_after >= left_before:
            report.line("fail", name, "THE CLOCK IS NOT MOVING: %d tick(s) in 2.6s, %.0fs still "
                        "on it (was %.0fs)" % (ticks, left_after, left_before),
                        ["the session lives on a thread in the server - a browser must not "
                         "be needed for it to run"])
            return
        if runs < ticks:
            report.line("fail", name, "%d tick(s) but only %d reader run(s): THE FRONT APP IS "
                        "NOT BEING QUERIED FRESH" % (ticks, runs),
                        ["a cached notification API reports the first app forever in a "
                         "long-lived server: every tick must ask again"])
            return
        if after.get("reader") != "live":
            report.line("warn", name, "the tick runs (%d in 2.6s) but the reader is not "
                        "answering (%s)" % (ticks, after.get("reader_why")),
                        ["a blind reader stops the clock rather than guessing - which is the "
                         "honest behaviour, and it is why this cannot be proven here"])
            return
        if abs(int(after.get("grace_ms") or 0) - int((block.get("grace_ms") or 0))) or \
                abs(float(after.get("tick_s") or 0) - float((block.get("tick_s") or 0))) > 0.01:
            report.line("fail", name, "the state's knobs disagree with /health's",
                        ["grace_ms %s/%s, tick_s %s/%s" % (after.get("grace_ms"),
                                                           block.get("grace_ms"),
                                                           after.get("tick_s"), block.get("tick_s"))])
            return
        notes.append("%d tick(s) in 2.6s with no browser open, %d fresh reader run(s), the "
                     "clock down %.0fs" % (ticks, runs, left_before - left_after))
        notes.append("a callout lands between TICK_S and TICK_S + GRACE_MS = %.0f..%.0fms "
                     "(python3 tools/focus-timings.py prints the field numbers)"
                     % (float(after.get("tick_s") or 0) * 1000.0,
                        float(after.get("tick_s") or 0) * 1000.0 + float(after.get("grace_ms") or 0)))
    finally:
        # never leave the person sitting in a session preflight started for itself
        stopped = as_json(post(base + "/focus", {"text": "end the session"}, timeout)[2]) or {}
        if stopped.get("ok") and (stopped.get("report") or {}).get("counted"):
            notes.append("note: the probe session was long enough to reach the ledger")
    notes.append("session id %s, opened and closed by this check" % session_id)
    report.line("pass", name, "a real session started, ticked, and closed with no browser "
                "attached", notes)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Preflight the live Alfred chain, end to end.")
    ap.add_argument("--url", default=os.environ.get("ALFRED_URL", DEFAULT_URL),
                    help="the running server (default: %s)" % DEFAULT_URL)
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="config.json to read (default: ./config.json)")
    ap.add_argument("--timeout", type=float, default=30.0, help="seconds per request (default: 30)")
    ap.add_argument("--keep-probe-note", action="store_true",
                    help="leave the /remember probe note on disk instead of removing it")
    ap.add_argument("--no-swap", action="store_true",
                    help="skip check 13 (it wears another brain for a moment, then puts the "
                         "config brain back)")
    ap.add_argument("--no-focus", action="store_true",
                    help="skip check 14 (it starts a focus session for a few seconds and "
                         "ends it again - a session already running is left alone)")
    ap.add_argument("--json", action="store_true", help="print one JSON object instead of the report")
    ap.add_argument("--no-color", action="store_true", help="no colour codes")
    args = ap.parse_args(argv)

    base = args.url.rstrip("/")
    report = Report(color=not args.no_color, quiet=args.json)
    started = time.time()

    report.say("\nAlfred preflight - the live chain, end to end")
    report.say("  server : %s" % base)
    report.say("  config : %s" % args.config)

    # Everything the checks need to know about the live brain, fetched once, before anything
    # in here has touched the system.
    status, _h, body, _err = get(base + "/health", args.timeout)
    health = as_json(body) or {}

    report.section("the chain, in the order it has to work")
    page = check_server(report, base, args.timeout)
    if health:
        report.info("live server: %s notes, model %s, key %s, up %s, notes at %s"
                    % (health.get("notes"), health.get("model"),
                       (health.get("key") or {}).get("state"),
                       minutes(health.get("uptime_s") or 0), health.get("notes_dir")))
    else:
        report.line("fail", "   /health answered", "no usable JSON from %s/health" % base,
                    ["every check that needs the brain's own state depends on this"])
    graph = check_graph(report, base, args.timeout)
    cfg_key = (read_config(args.config).get("openai_api_key") or "").strip()
    brain_configured = key_state(cfg_key) == "set"
    check_chat(report, base, graph.get("graph") or {}, brain_configured, args.timeout)
    key_info = check_key(report, args.config, health, args.timeout)
    check_model(report, args.config, health, key_info, args.timeout)
    check_remember(report, base, health, brain_configured, args.timeout, args.keep_probe_note)
    check_see(report, base, health, page.get("html", ""), brain_configured, args.timeout)
    check_served_matches_disk(report, base, page.get("html", ""), args.timeout)
    check_secrets(report, base, args.config, args.timeout)

    # Checks that exist because something actually broke here once. They run after the chain
    # itself, and they are the ones allowed to warn rather than shout.
    report.section("checks that exist because something actually broke")
    check_restart(report, health)
    check_boot_dependencies(report, args.timeout)
    check_notes_agree(report, health, base, args.timeout)
    check_swap(report, base, args.config, health, args.timeout, not args.no_swap)
    check_focus(report, base, health, args.timeout, not args.no_focus)

    elapsed = time.time() - started
    total = report.passes + report.fails + report.warns
    if args.json:
        print(json.dumps({
            "url": base, "config": args.config, "checks": report.records,
            "pass": report.passes, "fail": report.fails, "warn": report.warns,
            "seconds": round(elapsed, 1),
        }))
        return 1 if report.fails else 0

    report.section("summary")
    print("preflight: %d pass, %d fail, %d warn  (%d checks in %.1fs against %s)"
          % (report.passes, report.fails, report.warns, total, elapsed, base))
    if report.fails:
        print(report.paint("  RESULT: FAILED - fix the crosses above before calling anything done", "31"))
        return 1
    if report.warns:
        print(report.paint("  RESULT: the chain works; the warnings above are things this run could not prove",
                           "33"))
        return 0
    print(report.paint("  RESULT: CLEAN - every link of the live chain answered for itself", "32"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
