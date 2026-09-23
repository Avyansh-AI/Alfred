#!/usr/bin/env python3
"""
server.py - serves the viewer and gives the galaxy a brain.

  * serves ONLY the viewer/ folder (nothing else on disk is reachable)
  * POST /chat : scores every note against your question, sends the best six
    to the OpenAI API and returns {"answer": "...", "nodes": [indexes]}
  * keeps a short conversation history server-side so follow-ups work

Python 3 standard library only.

    python3 server.py                 # http://127.0.0.1:4700
    python3 server.py --notes ~/vault
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------- #
# paths / defaults
# --------------------------------------------------------------------------- #
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.join(HERE, "viewer")
DEFAULT_NOTES = os.path.join(HERE, "notes")
DEFAULT_CONFIG = os.path.join(HERE, "config.json")
DEFAULT_GRAPH = os.path.join(HERE, "viewer", "graph-data.js")
DEFAULT_BASE_URL = "https://api.openai.com/v1"
PLACEHOLDER_KEY = "PUT-YOUR-KEY-HERE"
PLACEHOLDER_MODEL = "gpt-6-astra"

TOP_N = 6                      # notes handed to the model
CONTEXT_CHARS = 1500           # per note, when building the prompt
HISTORY_TURNS = 3              # user+assistant pairs kept for follow-ups

# When did the answer actually come from the notes? Two numbers decide it.
#
# RELEVANCE_FLOOR: below this score nothing in the notes really matches the question,
#   so it was not a question about the notes at all ("good morning", a joke, an
#   off-topic ask). Such a turn is answered from conversation alone and the viewer is
#   told on_notes: false, which is what keeps the camera still.
# SUPPORT_RATIO: among the notes that WERE handed to the model, only those scoring at
#   least this fraction of the best one are reported as sources. The model reads the
#   best six notes whether they help or not; a note dragged in by one shared word did
#   not put anything into the answer, and the galaxy should not claim it did.
RELEVANCE_FLOOR = 2.0
SUPPORT_RATIO = 0.4
MAX_BODY = 64 * 1024
REQUEST_TIMEOUT = 60

SYSTEM_PROMPT = (
    "You are Alfred, the brain of a personal knowledge galaxy built from one person's "
    "markdown notes. You are given numbered excerpts from their notes, and you answer "
    "ONLY from those notes. Answer in two or three sentences, in plain prose, no bullet "
    "points and no preamble. Use the notes' own facts, names, numbers and dates. If the "
    "notes do not cover what was asked, say so plainly - for example \"Your notes do not "
    "cover that\" - and then say what they do cover if anything nearby is relevant. Never "
    "invent details, and never use outside knowledge. Follow-up questions refer back to "
    "the conversation so far."
)

# Used instead of SYSTEM_PROMPT when the question was not about the notes. No note
# excerpts are sent at all in that case - there is nothing in them to answer with.
CHAT_PROMPT = (
    "You are Alfred, the brain of a personal knowledge galaxy built from one person's "
    "markdown notes. The user has said something that is not a question about their "
    "notes - a greeting, thanks, a joke, small talk, or a question about you. Reply in "
    "ONE short, friendly sentence. Do not invent facts about their notes, their life or "
    "anything else, and do not pretend to know things you have not been told. If they "
    "ask for a joke, tell one short joke."
)

# Phrases that are small talk rather than questions about the notes. They only take
# effect when the notes also fail to match (see about_my_notes) - "thanks, what is the
# budget?" is a question about the notes that happens to start with thanks.
SMALL_TALK_PATTERNS = (
    r"^\s*(hi|hey|hello|yo|sup|namaste|greetings)\b",
    r"\b(good (morning|afternoon|evening|night)|how are you|how are things|how's it going|how is it going|what's up)\b",
    r"\b(thanks|thank you|thx|cheers|nice one|well done|good job|much appreciated)\b",
    r"\b(bye|goodbye|see you|good night|sleep well)\b",
    r"\b(tell me a joke|joke|make me laugh|riddle|sing (me )?(a|something))\b",
    r"\b(who are you|what are you|what can you do|what do you do|your name|are you (a )?(robot|ai|human|real))\b",
    r"\b(i am (fine|good|ok|okay)|i'm (fine|good|ok|okay)|not bad|all good)\b",
)

STOPWORDS = {
    "a", "about", "after", "again", "against", "all", "am", "an", "and", "any", "are",
    "as", "at", "be", "because", "been", "before", "being", "below", "between", "both",
    "but", "by", "can", "did", "do", "does", "doing", "don", "down", "during", "each",
    "few", "for", "from", "further", "had", "has", "have", "having", "he", "her", "here",
    "hers", "him", "his", "how", "i", "if", "in", "into", "is", "it", "its", "just",
    "me", "more", "most", "my", "no", "nor", "not", "now", "of", "off", "on", "once",
    "only", "or", "other", "our", "out", "over", "own", "s", "same", "she", "should",
    "so", "some", "such", "t", "than", "that", "the", "their", "them", "then", "there",
    "these", "they", "this", "those", "through", "to", "too", "under", "until", "up",
    "very", "was", "we", "were", "what", "when", "where", "which", "while", "who",
    "whom", "why", "will", "with", "you", "your", "tell", "know", "note", "notes",
    "according", "mentioned", "say", "says", "there", "got", "get", "also", "much",
}


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def ensure_config(path: str, create: bool = True) -> dict:
    """Read config.json, creating it with the placeholder key if it is missing."""
    if not os.path.exists(path) and create:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"openai_api_key": PLACEHOLDER_KEY, "model": PLACEHOLDER_MODEL}, fh, indent=2)
            fh.write("\n")
        print("server.py: created %s - paste your OpenAI key into it." % path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        cfg = {}
    except (OSError, ValueError) as err:
        print("server.py: config.json could not be read (%s) - treating the key as missing." % err, file=sys.stderr)
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("openai_api_key", PLACEHOLDER_KEY)
    cfg.setdefault("model", PLACEHOLDER_MODEL)
    return cfg


def key_state(key: str) -> str:
    """'placeholder' | 'missing' | 'set' - never returns or logs the key itself."""
    k = (key or "").strip()
    if not k:
        return "missing"
    if k.upper() in {"PUT-YOUR-KEY-HERE", "PUT_YOUR_KEY_HERE", "YOUR-KEY-HERE", "YOUR_API_KEY",
                     "SK-YOUR-KEY", "CHANGEME", "<YOUR-KEY>", "SK-..."}:
        return "placeholder"
    if re.search(r"put[-_ ]?your[-_ ]?key|paste[-_ ]?your|your[-_ ]?api[-_ ]?key", k, re.I):
        return "placeholder"
    return "set"


# --------------------------------------------------------------------------- #
# notes
# --------------------------------------------------------------------------- #
def strip_markdown(text: str) -> str:
    t = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, flags=re.S)
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"\[\[([^\]|#]*)(?:[#|][^\]]*)?\]\]", r"\1", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"[*_`~]{1,3}", "", t)
    return re.sub(r"\s+", " ", t).strip()


SMALL_WORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into",
    "nor", "of", "on", "onto", "or", "over", "per", "the", "to", "up", "via",
    "vs", "with",
}


def title_from_filename(path: str) -> str:
    """Must stay identical to build.py:title_from_path so node indexes line up."""
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"^\d+[\s._-]+", "", stem)
    stem = re.sub(r"[-_]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    if not stem:
        return os.path.basename(path)
    words = stem.split(" ")
    out = []
    for i, w in enumerate(words):
        if w.isupper() and len(w) > 1:
            out.append(w)
        elif i not in (0, len(words) - 1) and w.lower() in SMALL_WORDS:
            out.append(w.lower())
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


def read_notes_dir(notes_dir: str) -> list:
    notes = []
    if not notes_dir or not os.path.isdir(notes_dir):
        return notes
    for dirpath, dirnames, filenames in os.walk(notes_dir):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in {"node_modules", "__pycache__"})
        for name in sorted(filenames):
            if not name.lower().endswith(".md") or name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    raw = fh.read()
            except OSError:
                continue
            rel = os.path.relpath(full, notes_dir).replace(os.sep, "/")
            parent = os.path.dirname(rel)
            notes.append({
                "title": title_from_filename(full),
                "group": parent.split("/")[0] if parent else os.path.basename(os.path.abspath(notes_dir)),
                "path": rel,
                "body": strip_markdown(raw),
            })
    return notes


def read_graph_data(path: str) -> list:
    """Fallback: rebuild the note list from the generated viewer/graph-data.js."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
    except OSError:
        return []
    m = re.search(r"const GRAPH\s*=\s*(\{.*?\});\s*(?:\n|$)", src, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return []
    out = []
    for n in data.get("nodes", []):
        out.append({
            "title": n.get("label") or "",
            "group": n.get("group") or "notes",
            "path": n.get("path") or "",
            "body": n.get("excerpt") or "",
        })
    return out


# --------------------------------------------------------------------------- #
# retrieval
# --------------------------------------------------------------------------- #
def tokens(text: str) -> list:
    return [w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if w not in STOPWORDS and len(w) > 1]


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (text or "").lower())).strip()


def rank_notes(question: str, notes: list, top_n: int = TOP_N) -> list:
    """Keyword overlap, weighted towards the title, best matches first."""
    q_tokens = tokens(question)
    q_set = set(q_tokens)
    q_norm = norm(question)
    ranked = []
    for i, n in enumerate(notes):
        title_tokens = tokens(n["title"])
        title_set = set(title_tokens)
        title_norm = norm(n["title"])
        body_counts = Counter(tokens(n["body"]))
        score = 0.0
        hits = []
        for t in q_set:
            if t in title_set:
                score += 3.0
                hits.append(t)
            elif t in body_counts:
                score += 1.0 + min(body_counts[t], 3) * 0.15
                hits.append(t)
        # the question quoting a whole title ("the budget for the move") is a strong signal
        if title_norm and len(title_norm) > 3 and title_norm in q_norm:
            score += 4.0
        # a multi-word run of the question appearing in the note's first characters
        if len(q_tokens) > 2:
            for size in (3, 2):
                for s in range(len(q_tokens) - size + 1):
                    phrase = " ".join(q_tokens[s:s + size])
                    if phrase and phrase in norm(n["body"][:600]):
                        score += 0.8 * size
        if n["group"] and norm(n["group"]) in q_set:
            score += 1.5
        ranked.append((score, -i, i, hits))
    ranked.sort(reverse=True)
    out = []
    for score, _neg, idx, hits in ranked:
        if score <= 0 or len(out) >= top_n:
            break
        out.append({"index": idx, "score": round(score, 3), "hits": sorted(set(hits))[:8]})
    return out


def support_notes(picked: list) -> list:
    """The picked notes that actually carry the answer, best first.

    A note the model merely glanced at is not a source. Empty list means: the answer
    was not built from the notes (or none of them matched), so nothing should be lit
    and the camera has no reason to move.
    """
    if not picked:
        return []
    best = picked[0]["score"]
    if best < RELEVANCE_FLOOR:
        return []
    floor = max(RELEVANCE_FLOOR, best * SUPPORT_RATIO)
    return [p for p in picked if p["score"] >= floor]


def looks_like_small_talk(question: str) -> bool:
    q = norm(question)
    return any(re.search(pattern, q) for pattern in SMALL_TALK_PATTERNS)


def about_my_notes(question: str, picked: list, last_on_notes: bool):
    """Was this question about the notes? Decided BEFORE anything moves.

    Returns (on_notes, decision) with decision one of:
      "notes"      - the notes match the question; answer from them
      "small_talk" - a greeting, a joke, a question about Alfred; no notes are sent
      "follow_up"  - a vague question after an earlier notes question ("and the
                     deposit?") - still answered from the notes and the history
      "no_match"   - nothing matched and the conversation has not been about the
                     notes, so there is nothing honest to retrieve
    """
    top = picked[0]["score"] if picked else 0.0
    small_talk = looks_like_small_talk(question)
    if top >= RELEVANCE_FLOOR:
        return True, "notes"
    if small_talk:
        return False, "small_talk"
    if last_on_notes:
        return True, "follow_up"
    return False, "no_match"


def build_messages(question: str, notes: list, picked: list, history: list) -> list:
    blocks = []
    for p in picked:
        n = notes[p["index"]]
        body = n["body"][:CONTEXT_CHARS]
        blocks.append("[%d] %s  (folder: %s)\n%s" % (p["index"], n["title"], n["group"], body))
    context = "\n\n---\n\n".join(blocks) if blocks else "(no matching notes were found)"
    user = (
        "Notes from the user's knowledge galaxy:\n\n%s\n\n"
        "Question: %s\n\n"
        "Answer in two or three sentences using only the notes above." % (context, question)
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}] + history + [{"role": "user", "content": user}]


def build_chat_messages(question: str, history: list) -> list:
    """Small talk: same history, no note excerpts, and a prompt that says so."""
    user = ("The user said: %s\n\n"
            "Reply in one short, friendly sentence." % question)
    return [{"role": "system", "content": CHAT_PROMPT}] + history + [{"role": "user", "content": user}]


# --------------------------------------------------------------------------- #
# OpenAI call
# --------------------------------------------------------------------------- #
class BrainError(Exception):
    def __init__(self, message, code="brain_error", hint="", status=HTTPStatus.OK):
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint
        self.status = status


def call_openai(cfg: dict, messages: list, base_url: str) -> str:
    key = (cfg.get("openai_api_key") or "").strip()
    model = (cfg.get("model") or PLACEHOLDER_MODEL).strip()

    state = key_state(key)
    if state == "placeholder":
        raise BrainError(
            "The brain has no API key yet - config.json still holds the placeholder.",
            code="placeholder_api_key",
            hint="Open config.json in the project root, replace PUT-YOUR-KEY-HERE with your real "
                 "OpenAI key, then restart server.py. The key is only ever read by the server; "
                 "it is never sent to the browser.",
        )
    if state == "missing":
        raise BrainError(
            "config.json has no openai_api_key.",
            code="missing_api_key",
            hint='Add {"openai_api_key": "sk-...", "model": "%s"} to config.json and restart.' % PLACEHOLDER_MODEL,
        )

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 400,
    }
    url = base_url.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer %s" % key,
            "Content-Type": "application/json",
            "User-Agent": "alfred-knowledge-galaxy/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")
        try:
            msg = json.loads(detail).get("error", {}).get("message") or detail[:300]
        except ValueError:
            msg = detail[:300] or "no detail"
        if err.code in (401, 403):
            raise BrainError("OpenAI rejected the API key (%d): %s" % (err.code, msg),
                             code="api_key_rejected",
                             hint="Check that config.json holds a valid, unexpired key.")
        if err.code == 404:
            raise BrainError('The model "%s" was not found (404): %s' % (model, msg),
                             code="model_not_found",
                             hint="Set a model your key can access in config.json (for example gpt-4o-mini) "
                                  "and restart server.py.")
        if err.code == 429:
            raise BrainError("OpenAI rate-limited the request (429): %s" % msg,
                             code="rate_limited", hint="Wait a moment, or check your quota/billing.")
        raise BrainError("OpenAI returned HTTP %d: %s" % (err.code, msg),
                         code="upstream_error", hint="Model: %s" % model)
    except urllib.error.URLError as err:
        raise BrainError("Could not reach %s (%s)." % (url, err.reason),
                         code="network_error",
                         hint="The server needs outbound HTTPS access to the OpenAI API.")
    except (TimeoutError, socket.timeout):
        raise BrainError("The OpenAI request timed out after %ds." % REQUEST_TIMEOUT,
                         code="timeout", hint="Try a shorter question, or retry.")
    except OSError as err:
        raise BrainError("Network failure talking to OpenAI: %s" % err,
                         code="network_error", hint="Check the sandbox/server's outbound access.")

    try:
        data = json.loads(body)
    except ValueError:
        raise BrainError("OpenAI returned a response that was not JSON.", code="bad_upstream_response")
    if isinstance(data.get("error"), dict):
        raise BrainError(data["error"].get("message") or "OpenAI returned an error.",
                         code="upstream_error")
    choices = data.get("choices") or []
    if not choices:
        raise BrainError("OpenAI returned no choices.", code="empty_completion")
    message = choices[0].get("message") or {}
    answer = (message.get("content") or "").strip()
    if not answer:
        raise BrainError("OpenAI returned an empty answer.", code="empty_completion")
    return answer


# --------------------------------------------------------------------------- #
# server
# --------------------------------------------------------------------------- #
class State:
    def __init__(self, args):
        self.args = args
        self.config_path = os.path.abspath(args.config)
        self.cfg = ensure_config(self.config_path, create=not args.no_create_config)
        self.notes_dir = os.path.abspath(os.path.expanduser(args.notes or DEFAULT_NOTES))
        self.graph_path = os.path.abspath(args.graph or DEFAULT_GRAPH)
        self.root = os.path.abspath(args.root or DEFAULT_ROOT)
        self.base_url = args.openai_base_url or os.environ.get("ALFRED_OPENAI_BASE_URL") or DEFAULT_BASE_URL
        self.history = []                     # [{"role": .., "content": ..}, ..]
        self.last_on_notes = False            # has this conversation been about the notes yet?
        self.lock = threading.Lock()
        self.started = time.time()
        self.questions = 0
        self.notes = read_notes_dir(self.notes_dir)
        self.source = "notes folder"
        if not self.notes:
            self.notes = read_graph_data(self.graph_path)
            self.source = "viewer/graph-data.js" if self.notes else "none"

    @property
    def model(self):
        return (self.cfg.get("model") or PLACEHOLDER_MODEL).strip()

    def health(self):
        groups = sorted({n["group"] for n in self.notes})
        return {
            "ok": True,
            "notes": len(self.notes),
            "notes_source": self.source,
            "notes_dir": self.notes_dir,
            "groups": groups,
            "model": self.model,
            "key": {"state": key_state(self.cfg.get("openai_api_key")), "path": self.config_path},
            "turns": len(self.history) // 2,
            "questions_asked": self.questions,
            "titles": [n["title"] for n in self.notes],
            "uptime_s": round(time.time() - self.started, 1),
            "root": self.root,
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "alfred/1.0"
    protocol_version = "HTTP/1.1"
    state: State = None                      # injected below

    # -- plumbing ---------------------------------------------------------- #
    def log_message(self, fmt, *args):
        sys.stderr.write("  %s  %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _send(self, code, body: bytes, ctype="text/plain; charset=utf-8", extra=None, head_only=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, code, payload, head_only=False):
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", {"Cache-Control": "no-store"}, head_only)

    def _error(self, code, message, hint=""):
        self._json(code, {"ok": False, "error": message, "hint": hint})

    # -- routing ----------------------------------------------------------- #
    def do_GET(self):
        self._route(head_only=False)

    def do_HEAD(self):
        self._route(head_only=True)

    def _route(self, head_only=False):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/health", "/healthz", "/api/health"):
            return self._json(HTTPStatus.OK, self.state.health(), head_only)
        if path == "/chat":
            return self._error(HTTPStatus.METHOD_NOT_ALLOWED,
                               "GET /chat is not supported - POST a JSON body with your question.",
                               'Example: curl -X POST -d \'{"question":"..."}\' '
                               'http://127.0.0.1:%d/chat' % self.server.server_address[1])
        return self._static(path, head_only)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/chat":
            return self._chat()
        self._error(HTTPStatus.NOT_FOUND, "No such endpoint: %s" % path,
                    "Only POST /chat exists on this server.")

    def do_OPTIONS(self):
        self._send(HTTPStatus.NO_CONTENT, b"", extra={"Allow": "GET, HEAD, POST, OPTIONS"})

    # -- static files: viewer/ only --------------------------------------- #
    def _static(self, path, head_only=False):
        root = self.state.root
        decoded = urllib.parse.unquote(path)
        if "\x00" in decoded or "\\" in decoded:
            return self._error(HTTPStatus.BAD_REQUEST, "Bad path.")
        parts = [p for p in decoded.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            return self._error(HTTPStatus.FORBIDDEN, "Path traversal is not allowed.")
        # validate each segment on its own: the separator must not be part of the
        # allowed character class, or subfolders inside viewer/ become unreachable
        if not all(re.fullmatch(r"[A-Za-z0-9._\- ]+", p) for p in parts):
            return self._error(HTTPStatus.BAD_REQUEST, "Unsupported characters in path.")
        rel = "/".join(parts) or "index.html"
        target = os.path.abspath(os.path.join(root, rel))
        # belt and braces: the resolved path must still sit inside viewer/
        if not (target == root or target.startswith(root + os.sep)):
            return self._error(HTTPStatus.FORBIDDEN, "That file is outside the viewer folder.")
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if os.path.basename(target).lower() in {"config.json", ".env", "server.py", "build.py"}:
            return self._error(HTTPStatus.FORBIDDEN, "Refusing to serve that file.",
                               "Secrets and source live outside viewer/ and are never served.")
        if not os.path.isfile(target):
            return self._error(HTTPStatus.NOT_FOUND, "Not found: /%s" % rel,
                               "This server only serves the viewer/ folder.")
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".mjs": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".ico": "image/x-icon",
            ".woff2": "font/woff2",
            ".txt": "text/plain; charset=utf-8",
            ".map": "application/json; charset=utf-8",
        }.get(os.path.splitext(target)[1].lower(), "application/octet-stream")
        try:
            with open(target, "rb") as fh:
                body = fh.read()
        except OSError as err:
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not read %s: %s" % (rel, err))
        cache = "no-store" if ctype.startswith(("text/html", "application/javascript")) else "public, max-age=3600"
        return self._send(HTTPStatus.OK, body, ctype, {"Cache-Control": cache}, head_only)

    # -- the brain --------------------------------------------------------- #
    def _chat(self):
        state = self.state
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Question is too large.")
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return self._error(HTTPStatus.BAD_REQUEST,
                               "Request body must be JSON.", 'Send {"question": "..."}.')
        if not isinstance(payload, dict):
            return self._error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        question = (payload.get("question") or payload.get("q") or "").strip()
        if not question:
            return self._error(HTTPStatus.BAD_REQUEST, "No question was provided.",
                               'Send {"question": "..."}.')
        question = question[:1000]

        picked = rank_notes(question, state.notes)
        with state.lock:
            history = list(state.history)
            last_on_notes = state.last_on_notes

        # Two decisions, in this order, before anything is allowed to move.
        # 1. was this a question about the notes at all?
        on_notes, decision = about_my_notes(question, picked, last_on_notes)
        # 2. if so, which of the notes the model is shown actually carry the answer?
        support = support_notes(picked) if on_notes else []
        read = [p["index"] for p in picked] if on_notes else []
        nodes = [p["index"] for p in support]
        sources = [{"index": p["index"], "label": state.notes[p["index"]]["title"], "score": p["score"]}
                   for p in support]

        if on_notes:
            messages = build_messages(question, state.notes, picked, history)
        else:
            messages = build_chat_messages(question, history)

        state.questions += 1
        try:
            answer = call_openai(state.cfg, messages, state.base_url)
        except BrainError as err:
            # Retrieval still worked, so the sources come back - but there is no answer
            # to justify them, and the viewer keeps the galaxy still unless ok is true.
            return self._json(err.status, {
                "ok": False,
                "error": err.message,
                "code": err.code,
                "hint": err.hint,
                "answer": err.message,
                "on_notes": on_notes,
                "decision": decision,
                "nodes": nodes,
                "sources": sources,
                "read": read,
                "model": state.model,
                "turns": len(history) // 2,
            })
        except Exception as err:                                  # never crash the server
            return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "ok": False, "error": "Unexpected brain failure: %s" % err,
                "code": "internal_error", "on_notes": on_notes, "decision": decision,
                "nodes": nodes, "sources": sources, "read": read,
            })

        with state.lock:
            state.history.append({"role": "user", "content": question})
            state.history.append({"role": "assistant", "content": answer})
            state.history = state.history[-(HISTORY_TURNS * 2):]
            # Sticky on purpose: once the conversation has been about the notes, a vague
            # question ("and the deposit?") is a follow-up rather than small talk, and it
            # still gets the notes. Closing the door again would break follow-ups.
            if on_notes:
                state.last_on_notes = True

        return self._json(HTTPStatus.OK, {
            "ok": True,
            "answer": answer,
            "on_notes": on_notes,
            "decision": decision,
            "nodes": nodes,           # the notes the answer came from (1-3: fly, 4+: the cluster)
            "sources": sources,       # the same, with labels and scores
            "read": read,             # what the model was shown, whether it used it or not
            "model": state.model,
            "turns": len(state.history) // 2,
        })


def main(argv=None):
    ap = argparse.ArgumentParser(description="Serve the knowledge galaxy and its brain")
    ap.add_argument("--port", type=int, default=4700)
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: 0.0.0.0)")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="folder to serve (default: viewer/)")
    ap.add_argument("--notes", default=DEFAULT_NOTES, help="notes folder to read for /chat")
    ap.add_argument("--graph", default=DEFAULT_GRAPH, help="generated graph-data.js (fallback source)")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="config.json in the project root")
    ap.add_argument("--openai-base-url", default=None, help="override the OpenAI API base URL")
    ap.add_argument("--no-create-config", action="store_true", help="never create config.json")
    args = ap.parse_args(argv)

    state = State(args)
    Handler.state = state

    if not os.path.isdir(state.root):
        print("server.py: viewer folder missing: %s" % state.root, file=sys.stderr)
        print("           run 'python3 build.py' first, then start the server.", file=sys.stderr)
        return 2

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as err:
        print("server.py: cannot bind %s:%d (%s)" % (args.host, args.port, err), file=sys.stderr)
        print("           something else is already using that port:  server.py --port 4701", file=sys.stderr)
        return 2
    httpd.daemon_threads = True

    ks = key_state(state.cfg.get("openai_api_key"))
    print("")
    print("  Alfred - knowledge galaxy")
    print("  ---------------------------------------------------------------")
    print("  viewer      : http://127.0.0.1:%d" % args.port)
    print("  serving     : %s" % state.root)
    print("  notes       : %d notes from %s (%s)" % (len(state.notes), state.source, state.notes_dir))
    print("  model       : %s" % state.model)
    print("  api key     : %s  (%s)" % (ks, state.config_path))
    if ks != "set":
        print("                -> /chat answers with a clean 'paste your key' error until this is set")
    print("  endpoints   : GET /  GET /health  POST /chat")
    print("  ---------------------------------------------------------------")
    print("  ctrl-c to stop")
    print("")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nserver.py: stopped.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
