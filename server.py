#!/usr/bin/env python3
"""
server.py - serves the viewer and gives the galaxy a brain.

  * serves ONLY the viewer/ folder (nothing else on disk is reachable)
  * POST /chat : scores every note against your question, sends the best six
    to the OpenAI API and returns {"answer": "...", "nodes": [indexes]}
  * POST /remember : "remember that ..." writes a real markdown note into
    notes/captures/, indexes it immediately and reports where to put the new star
  * POST /see : a question plus ONE frame of the user's screen, captured by the
    browser at the moment he asked, answered from the picture by the same model
  * keeps a short conversation history server-side so follow-ups work

Python 3 standard library only.

    python3 server.py                 # http://127.0.0.1:4700
    python3 server.py --notes ~/vault
"""

from __future__ import annotations

import argparse
import base64
import binascii
import errno
import json
import os
import re
import socket
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# =========================================================================== #
#                                                                             #
#   T H E   P E R S O N A   -   everything about the character lives here     #
#                                                                             #
#   Rewrite the character inside this block and nowhere else. Nothing below    #
#   it knows what Alfred sounds like: the two prompts, the greeting and the    #
#   names he gives the times of day are all built from the pieces here.        #
#                                                                             #
#   The rules the code depends on, whether you keep this butler or replace     #
#   him entirely:                                                              #
#     * SYSTEM_PROMPT must keep the phrase "ONLY from those notes" and must    #
#       still cap the answer at "three sentences" - the tests check both, and  #
#       the second is what keeps a spoken answer short.                        #
#     * CHAT_PROMPT must keep "ONE short, friendly sentence" - the tests use   #
#       it to tell small talk apart from a notes answer at the model.          #
#     * GREETING keeps {part_of_day} and {count}; {count} is filled in with    #
#       the number of notes actually indexed, never by hand.                   #
#                                                                             #
# =========================================================================== #

# Who he is. The model reads this on every single answer.
PERSONA = (
    "You are Alfred, a dry, impeccably polite British butler who has looked after this "
    "person's affairs for years. You are fond of him, and you would never say so. Your "
    "wit is dry: never whimsical, never cute, and never at the expense of being right."
)

# How he answers a question about the notes.
ANSWER_STYLE = (
    "How you answer:\n"
    "- Address him as \"sir\" when it fits - once in a while, not in every sentence. "
    "Used sparingly it is charming; used constantly it grates.\n"
    "- Open with ONE short, dry line of wit, then give him the facts. One genuinely "
    "funny line beats three bland ones. If nothing funny is within reach, be brief "
    "instead - never force a joke.\n"
    "- Never recite the note back to him. He wrote it and it is on his screen: give him "
    "the answer, not the document.\n"
    "- At most three sentences. Usually two.\n"
    "- Answer ONLY from those notes. They are your only source of fact.\n"
    "- If the notes do not cover what he asked, say so plainly and with dignity - "
    "\"I have nothing on that, sir\" - and then say what nearby thing they do cover, if "
    "anything does. Never invent a source, never pad, and never dress up a related note "
    "as the answer to a different question.\n"
    "- Follow-up questions refer back to the conversation so far."
)

# How he answers everything else: greetings, thanks, jokes, questions about you, and
# anything else that is not a question about the notes. No note excerpts are sent with
# this one and the server reports on_notes: false, so the viewer holds the galaxy
# perfectly still - small talk must never drag the camera around the graph.
CHAT_STYLE = (
    "How you answer:\n"
    "- This is not a question about his notes: it is a greeting, thanks, a joke, small "
    "talk, or a question about you. Reply with ONE short, friendly sentence, in "
    "character. Dry wit welcome.\n"
    "- Do not invent facts about his notes, his life, or anything else, and do not "
    "pretend to know things you have not been told. If he asks about his affairs and "
    "you have no notes for it, say so plainly and with dignity.\n"
    "- If he asks for a joke, tell one short one."
)

SYSTEM_PROMPT = PERSONA + "\n\nYou are given numbered excerpts from his notes.\n" + ANSWER_STYLE
CHAT_PROMPT = PERSONA + "\n\n" + CHAT_STYLE

# ---- the boot greeting ----------------------------------------------------- #
# "Good evening, sir. 485 notes indexed, all present and accounted for."
#
# The count is never written by hand: greeting() is called with len(state.notes), the
# number of notes the galaxy is actually holding, and verify.py checks that this is the
# same number the viewer draws as nodes.
GREETING = "Good {part_of_day}, sir. {count} notes indexed, all present and accounted for."
GREETING_ONE = "Good {part_of_day}, sir. One note indexed, all present and accounted for."

# What he calls each part of the day, and when. A butler does not wish you good night at
# midnight; he wishes you good evening and means it, so the small hours stay "evening".
# The windows cover all 24 hours exactly once (the last one wraps past midnight).
PARTS_OF_DAY = (
    (5, 12, "morning"),
    (12, 17, "afternoon"),
    (17, 23, "evening"),
    (23, 5, "evening"),
)


def part_of_day(hour):
    """'morning' | 'afternoon' | 'evening' for a 0-23 hour."""
    hour = int(hour) % 24
    for start, end, name in PARTS_OF_DAY:
        if start < end:
            if start <= hour < end:
                return name
        elif hour >= start or hour < end:           # the window that wraps midnight
            return name
    return "evening"


def greeting(count, hour=None):
    """The line he opens with, built from the real note count."""
    hour = time.localtime().tm_hour if hour is None else hour
    template = GREETING_ONE if int(count) == 1 else GREETING
    return template.format(part_of_day=part_of_day(hour), count=int(count))


# ---- when something is filed ------------------------------------------------ #
# The one line he says when a thought you have just given him goes into the notes.
# It is spoken out loud and shown on screen word for word, so it is character, not
# machinery - edit it here. {title} is the note's own title, {detail} says where it
# was born and what it is holding on to, {count} is the real size of the galaxy,
# and {reason} is the plain-language reason a file could not be written.
CAPTURE_LINE = ("Filed and lit, sir. \u201c{title}\u201d is in the galaxy now, {detail}, "
                "and the galaxy is {count} notes strong.")
CAPTURE_BESIDE = "born beside {anchor}"
CAPTURE_JOINED = "joined to {label}"
CAPTURE_JOINED_MANY = "joined to {count} notes"
CAPTURE_ALONE = "holding on to nothing at all"

# Nothing was said after "remember that" - there is nothing to write down.
CAPTURE_EMPTY_LINE = ("Remember what exactly, sir? There was nothing after the word "
                      "\u201cremember\u201d for me to write down, so nothing was filed.")

# The write itself failed. This one is never swallowed: he says it out loud.
CAPTURE_FAILED_LINE = ("It did not go in, sir - {reason}. Nothing was written, and I would "
                       "rather tell you than let you think that thought was safe.")

# The file is on disk but the brain did not take it, which is just as serious.
CAPTURE_UNINDEXED_LINE = ("The thought is written to {file}, sir, but it is not in the index - "
                          "{reason}. I will not pretend you can search it yet.")


def capture_line(title, count, anchor=None, links=()):
    """The one spoken confirmation, assembled from the facts of the capture."""
    bits = []
    if anchor:
        bits.append(CAPTURE_BESIDE.format(anchor=anchor))
    labels = [l for l in links if l]
    if len(labels) == 1:
        # naming the same note twice in one breath ("born beside X, joined to X")
        # is exactly the padding this character is not supposed to do
        if labels[0] != anchor:
            bits.append(CAPTURE_JOINED.format(label=labels[0]))
    elif labels:
        bits.append(CAPTURE_JOINED_MANY.format(count=len(labels)))
    if not labels:
        bits.append(CAPTURE_ALONE)
    return CAPTURE_LINE.format(title=title, detail=", ".join(bits), count=int(count))


def capture_failed(reason, filed=False, path=""):
    """The line for a failure. `filed` says whether the file made it to disk."""
    if filed:
        return CAPTURE_UNINDEXED_LINE.format(file=path, reason=reason)
    return CAPTURE_FAILED_LINE.format(reason=reason)


# ---- when he is shown a screen ---------------------------------------------- #
# The instruction for a screen question. The one thing this must never allow is a
# guess dressed up as a look, so the rules about small, blurry and missing are as
# explicit as the rules about being specific.
SEE_STYLE = (
    "You are being shown ONE frame of the user's screen, captured at the moment he asked, "
    "and the question he asked about it. Answer about what is actually in that frame and "
    "nothing else.\n"
    "Be specific: names, numbers, labels, headings, what is where, what looks wrong.\n"
    "If the frame is too small, too blurry, too dark or too cropped to judge what he asked "
    "about, say so plainly and say what you would need to see it better - never guess at what "
    "it might say, and never fall back on what a screen like that usually shows.\n"
    "If the thing he asked about is not in the frame, say that it is not in the frame.\n"
    "If he asks about his notes while you are looking at his screen, say plainly that his notes "
    "are not in front of you and that stopping the screen share will bring them back.\n"
    "One short dry line and then the facts, in the same voice as ever, and no more than three "
    "sentences."
)
SEE_PROMPT = (PERSONA + "\n\nYou are shown one frame of his screen, taken the moment he asks, "
              "and the question he asked about it.\n" + SEE_STYLE)

# What he says about the share itself. These are served to the page by /health, so the
# character stays in this block even though the buttons that trigger them live in the viewer.
SIGHT_STARTED_LINE = ("Watching your screen now, sir. Whatever you ask me next, I will answer "
                      "from what is actually on it.")
SIGHT_ENDED_LINE = ("The screen share has ended, sir - I am not looking at anything now. "
                    "The screen button will start it again.")
SIGHT_NEVER_LINE = ("I have not been shown your screen yet, sir. The screen button in the ask bar "
                    "is how you point me at something.")
SIGHT_LOST_LINE = ("The share is still listed but no live picture is coming through it, sir - "
                   "start it again and I will look properly.")
SIGHT_NO_FRAME_LINE = ("Nothing came with that question for me to look at, sir, and I will not "
                       "describe a screen I cannot see.")
SIGHT_GRAB_FAILED_LINE = ("I could not take a picture of your screen just then, sir. Nothing was "
                          "sent to be looked at, and I would rather say so than guess.")

SIGHT_LINES = {
    "started": SIGHT_STARTED_LINE,
    "ended": SIGHT_ENDED_LINE,
    "never": SIGHT_NEVER_LINE,
    "lost": SIGHT_LOST_LINE,
    "no_frame": SIGHT_NO_FRAME_LINE,
    "grab_failed": SIGHT_GRAB_FAILED_LINE,
}


# =========================================================================== #
#   end of the persona - below here is machinery, not character               #
# =========================================================================== #

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


# --------------------------------------------------------------------------- #
# captures - "remember that ..." writes a real note and indexes it right now
#
# Two things this section is careful about, because they bite later:
#   1. Writing a file is not the same as indexing it. The note goes into the live
#      list in this process the moment it is written, so the very next question can
#      find it - no build.py, no restart. build.py only makes it part of the
#      generated graph-data.js, which is why /health reports it as a "capture" for
#      as long as the graph file has not caught up.
#   2. A capture never fails silently. Every failure path returns ok: false and a
#      line Alfred says out loud (see CAPTURE_FAILED_LINE in the persona block).
# --------------------------------------------------------------------------- #
class CaptureError(Exception):
    """A capture that did not land. Carries whether the file itself got written."""

    def __init__(self, reason, filed=False, path=""):
        Exception.__init__(self, reason)
        self.reason = reason
        self.filed = filed
        self.path = path


def plain_error(err) -> str:
    """One short, plain-language phrase for a filesystem failure."""
    code = getattr(err, "errno", None)
    said = (getattr(err, "strerror", None) or str(err) or "").lower()
    if code in (errno.EACCES, errno.EPERM):
        return "the notes folder will not let me write to it (%s)" % said
    if code == errno.EROFS:
        return "the notes folder is read-only"
    if code == errno.ENOSPC:
        return "there is no space left on the disk"
    if code in (errno.ENOTDIR, errno.EISDIR, errno.EEXIST):
        return "there is a file sitting where the captures folder should be (%s)" % said
    if code == errno.ENOENT:
        return "the notes folder is not there any more"
    return "the write failed (%s)" % said


CAPTURE_DIR = "captures"           # a folder inside the notes folder
CAPTURE_TRIGGER = r"^\s*remember\b[:,]?\s*(?:that\b[:,]?\s*)?"
CAPTURE_TITLE_WORDS = 6            # the title comes from the first few words
CAPTURE_TAIL_WORDS = ("is", "are", "was", "were", "be", "been", "being", "am", "will", "shall",
                      "should", "must", "can", "could", "would", "may", "might", "do", "does",
                      "did", "have", "has", "had", "goes", "belongs")   # never a title's last word
CAPTURE_MAX_CHARS = 4000           # a runaway transcription is not a note


def note_key(text: str) -> str:
    """Lowercase, accents and punctuation stripped - build.py:normalise, verbatim.

    Must stay identical to build.py:normalise or the links this server adds to a
    capture would differ from the ones the next build.py run generates.
    """
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def sentence_key(text_lower: str, key: str) -> bool:
    """True if `key` appears as a whole word run. build.py:sentence_key, verbatim."""
    if not key:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])"
    return re.search(pattern, text_lower) is not None


def strip_trigger(text: str) -> str:
    """'remember that X' -> 'X'. Anything not starting with the trigger is returned as is."""
    return re.sub(CAPTURE_TRIGGER, "", text or "", count=1, flags=re.I).strip()


def is_capture(text: str) -> bool:
    return bool(re.match(CAPTURE_TRIGGER, text or "", flags=re.I))


def capture_slug(text: str) -> str:
    """A filename from the first few words. build.py turns this back into the title.

    Words that were SHOUTED are kept as they were typed, because build.py's
    title_from_path keeps acronyms uppercase - so "the RML hospital visit" becomes
    notes/captures/the-RML-hospital-visit.md and reads back as "The RML Hospital
    Visit", not "The Rml Hospital Visit".
    """
    words = re.findall(r"[A-Za-z0-9']+", text or "")[:CAPTURE_TITLE_WORDS]
    slug = "-".join(w if (w.isupper() and len(w) > 1) else w.lower() for w in words)
    # a title should not end on a dangling preposition, conjunction or auxiliary
    # ("the window repair is in the budget for the move" -> The Window Repair)
    tail = sorted(SMALL_WORDS | set(CAPTURE_TAIL_WORDS))
    slug = re.sub(r"(?:-(?:%s))+$" % "|".join(tail), "", slug)
    slug = slug.strip("-")[:70].strip("-")
    return slug or "capture"


def capture_markdown(title: str, text: str, when: str) -> str:
    """The file that lands in the notes folder: a title, the date, and your words."""
    body = (text or "").strip()
    if body:
        body = body[0].upper() + body[1:]
        if body[-1] not in ".!?":
            body += "."
    return "# %s\n\nCaptured %s.\n\n%s\n" % (title, when, body)


def capture_target(notes_dir: str, slug: str) -> tuple:
    """(folder, filename) for a new capture, never overwriting an existing note."""
    folder = os.path.join(notes_dir, CAPTURE_DIR)
    name = slug + ".md"
    n = 2
    while os.path.exists(os.path.join(folder, name)):
        name = "%s-%d.md" % (slug, n)
        n += 1
        if n > 500:
            return folder, "%s-%d.md" % (slug, int(time.time()))
    return folder, name


def read_one_note(notes_dir: str, rel: str) -> dict:
    """The note as the rest of the brain would read it after a restart, or None."""
    for n in read_notes_dir(notes_dir):
        if n["path"] == rel:
            return n
    return None


def capture_links(note: dict, raw: str, notes: list) -> tuple:
    """The links build.py would draw for this note, plus the labels they join.

    Same two rules as build.py:build_links:
      (a) an explicit [[wikilink]] to another note's title or filename, weight 2
      (b) a plain mention of another note's title in the text, weight 1
    and the reverse of (b): an existing note whose text already mentions the new
    title. Nothing else - a brand new note that mentions nothing gets no edges and
    is left standing on its own, because inventing a link is inventing a fact.
    """
    keyed = {}
    for i, other in enumerate(notes):
        for key in {note_key(other["title"]),
                    note_key(os.path.splitext(os.path.basename(other["path"]))[0])}:
            if key:
                keyed.setdefault(key, []).append(i)
    pairs = {}
    weight = Counter()
    labels = []
    new_index = note.get("index", len(notes) - 1)                 # `notes` already ends with this note

    for wikilink in re.findall(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]", raw or ""):
        for j in keyed.get(note_key(wikilink.strip()), []):
            if j == new_index:
                continue
            pairs[(min(new_index, j), max(new_index, j))] = "wikilink"
            weight[(min(new_index, j), max(new_index, j))] += 2

    clean_lower = note["body"].lower()
    for j, other in enumerate(notes):
        if j == new_index:
            continue
        if sentence_key(clean_lower, note_key(other["title"])):
            k = (min(new_index, j), max(new_index, j))
            pairs.setdefault(k, "mention")
            weight[k] += 1
        elif sentence_key(other["body"].lower(), note_key(note["title"])):
            k = (min(new_index, j), max(new_index, j))
            pairs.setdefault(k, "mention")
            weight[k] += 1

    out = []
    for (a, b), kind in sorted(pairs.items()):
        out.append({"source": a, "target": b, "kind": kind, "weight": round(weight[(a, b)], 2)})
        other = notes[b] if a == new_index else notes[a]
        labels.append(other["title"])
    return out, labels


def capture_anchor(text: str, notes: list) -> dict:
    """The existing note the new one is most related to - where it is born.

    The same keyword scoring /chat retrieves with, over the notes that already
    exist. No match at all means no anchor, and the viewer drops the new star at
    the middle of the galaxy rather than pretending it has a home.
    """
    if len(notes) < 2:
        return None
    pool = notes[:-1]                  # the note being filed is the last one
    picked = rank_notes(text, pool, top_n=1)
    # A number is a weak word. "900 milliseconds" matching a train fare of 1,900 is a
    # coincidence of digits, not a relationship, so if digits are the only reason for
    # the best match, ask again about the words - and keep the digits if that finds
    # nothing at all. Either way this is the same scorer /chat retrieves with.
    if picked and picked[0]["hits"] and all(h.isdigit() for h in picked[0]["hits"]):
        words_only = re.sub(r"\b\d+\b", " ", text or "").strip()
        better = rank_notes(words_only, pool, top_n=1) if words_only else []
        if better:
            picked = better
    if not picked:
        return None
    best = picked[0]
    return {"index": best["index"], "label": notes[best["index"]]["title"],
            "score": best["score"]}


def capture_entry(note: dict, notes: list, raw: str, text: str, file_rel: str) -> dict:
    """Everything the viewer needs to add one star to the running galaxy."""
    links, labels = capture_links(note, raw, notes)
    return {
        "file": file_rel,
        "node": {
            "id": note["index"], "index": note["index"], "label": note["title"],
            "group": note["group"], "excerpt": note["body"][:700], "path": note["path"],
            "words": len(note["body"].split()), "chars": len(raw), "degree": len(links),
            "wikilinks": [w.strip() for w in re.findall(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]", raw or "")],
            "mentions": labels,
        },
        "anchor": capture_anchor(text, notes),
        "links": links,
    }


# --------------------------------------------------------------------------- #
# sight - the frame the viewer sends with POST /see
#
# The frame is captured in the browser at the moment a question is asked, encoded
# as JPEG and sent inline. Nothing here stores a picture: the server keeps the
# measurements of the last frame (type, size, dimensions, when) and nothing else,
# so there is no earlier frame for a later question to be answered from.
# --------------------------------------------------------------------------- #
SEE_MAX_BODY = 12 * 1024 * 1024        # the whole request body
SEE_MIN_IMAGE_BYTES = 1024             # smaller than this is not a screen, it is a stub
SEE_MAX_EDGE = 8192                    # a frame bigger than this is not a screen either
SEE_MEDIA_TYPES = (
    ("image/jpeg", "jpeg"),
    ("image/png", "png"),
    ("image/webp", "webp"),
)


def sniff_media_type(data: bytes) -> str:
    """What the bytes actually are, by magic number. Never what the sender claimed."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:2] == b"BM":
        return "image/bmp"
    return ""


def image_dimensions(data: bytes, media_type: str) -> tuple:
    """(width, height) read out of the file itself, or (None, None)."""
    try:
        if media_type == "image/png":
            return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))
        if media_type == "image/jpeg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                if marker == 0xD9:
                    break
                seg = int.from_bytes(data[i + 2:i + 4], "big")
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height = int.from_bytes(data[i + 5:i + 7], "big")
                    width = int.from_bytes(data[i + 7:i + 9], "big")
                    return (width, height)
                i += 2 + seg
        if media_type == "image/webp":
            chunk = data[12:16]
            if chunk == b"VP8X":
                width = 1 + int.from_bytes(data[24:27], "little")
                height = 1 + int.from_bytes(data[27:30], "little")
                return (width, height)
            if chunk == b"VP8 ":
                width = int.from_bytes(data[26:28], "little") & 0x3FFF
                height = int.from_bytes(data[28:30], "little") & 0x3FFF
                return (width, height)
            if chunk == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    except (IndexError, ValueError):
        pass
    return (None, None)


def strip_data_url(text: str) -> tuple:
    """Split 'data:image/jpeg;base64,....' into (declared_type, base64_payload)."""
    text = (text or "").strip()
    if text[:5].lower() != "data:":
        return "", text
    head, _, tail = text.partition(",")
    return head[5:].split(";")[0].strip().lower(), tail.strip()


def order_notes_like_graph(notes: list, graph_path: str) -> tuple:
    """Notes in the order the galaxy is drawn in, plus the ones the graph lacks.

    A node's id IS its position in viewer/graph-data.js, so the server's list has to
    agree with it or an answer would light up the wrong star. Notes the graph file
    does not know about yet (a capture since the last build.py run) are appended in
    path order - the same place the viewer puts them.
    """
    try:
        with open(graph_path, "r", encoding="utf-8") as fh:
            m = re.search(r"const GRAPH\s*=\s*(\{.*?\});\s*(?:\n|$)", fh.read(), re.S)
        known = [n.get("path") or "" for n in json.loads(m.group(1)).get("nodes", [])] if m else []
    except (OSError, ValueError, AttributeError):
        known = []
    if not known:
        return notes, []
    by_path = {n["path"]: n for n in notes}
    ordered = [by_path[p] for p in known if p in by_path]
    seen = {id(n) for n in ordered}
    extra = sorted((n for n in notes if id(n) not in seen), key=lambda n: n["path"])
    return ordered + extra, extra


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


def build_see_messages(question: str, image: bytes, media_type: str, history: list) -> list:
    """The question plus the frame, in the shape the vision endpoint expects."""
    data_url = "data:%s;base64,%s" % (media_type, base64.b64encode(image).decode("ascii"))
    user = {
        "role": "user",
        "content": [
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
        ],
    }
    return [{"role": "system", "content": SEE_PROMPT}] + history + [user]


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
        self.captures = []                    # notes the graph file does not know yet
        self.frames = 0                       # frames looked at through POST /see
        self.last_frame = None                # measurements of the last one, never the pixels
        self.last_on_notes = False            # has this conversation been about the notes yet?
        self.lock = threading.Lock()
        self.started = time.time()
        self.questions = 0
        self.notes = read_notes_dir(self.notes_dir)
        self.source = "notes folder"
        if not self.notes:
            self.notes = read_graph_data(self.graph_path)
            self.source = "viewer/graph-data.js" if self.notes else "none"
        # The viewer draws its nodes in the order build.py wrote them, and a node's
        # id IS its position - so the brain's list is put in that same order, with
        # anything the graph file has not caught up with yet appended at the end.
        self.notes, self.unbuilt = order_notes_like_graph(self.notes, self.graph_path)
        for i, n in enumerate(self.notes):
            n["index"] = i    # the only index that matters: position in this list
        # Notes filed since the last build.py run. They are already searchable (they
        # are in self.notes above); these entries are how the viewer learns about
        # them on boot, so reloading the page never loses a star.
        for n in self.unbuilt:
            try:
                with open(os.path.join(self.notes_dir, n["path"]), "r",
                          encoding="utf-8", errors="replace") as fh:
                    raw = fh.read()
                self.captures.append(capture_entry(n, self.notes, raw, n["body"], n["path"]))
            except OSError:
                continue

    @property
    def model(self):
        return (self.cfg.get("model") or PLACEHOLDER_MODEL).strip()

    def health(self, hour=None):
        groups = sorted({n["group"] for n in self.notes})
        with self.lock:
            captures = [dict(e) for e in self.captures]
        return {
            "ok": True,
            "notes": len(self.notes),
            # the boot greeting, with the real note count in it. The viewer asks for
            # ?hour=<its own local hour> so the salutation matches the reader's clock.
            "greeting": greeting(len(self.notes), hour),
            "notes_source": self.source,
            "notes_dir": self.notes_dir,
            "groups": groups,
            "model": self.model,
            # Which brain this process is actually talking to, and which key file it
            # read: preflight.py checks the LIVE chain, so it has to be told what the
            # live chain is rather than guess. A URL and a path, never the key itself.
            "api_base_url": self.base_url,
            "key": {"state": key_state(self.cfg.get("openai_api_key")), "path": self.config_path},
            "turns": len(self.history) // 2,
            "questions_asked": self.questions,
            "titles": [n["title"] for n in self.notes],
            # Notes that exist in the brain but not yet in viewer/graph-data.js:
            # captures taken since the last build.py run. The viewer adds these on
            # boot so a reload never loses a thought that was filed. A build.py run
            # empties this list, because the graph file then knows them itself.
            "captures": captures,
            "graph_file": "behind" if captures else "current",
            # Sight: the character's own lines for the share (so the page carries none of
            # the personality), how many frames have been looked at, and the measurements of
            # the last one. Deliberately not the picture itself - there is no stored frame in
            # this server for a later question to be answered from.
            "sight": {
                "frames": self.frames,
                "last_frame": dict(self.last_frame) if self.last_frame else None,
                "lines": dict(SIGHT_LINES),
                "media_types": [t for t, _ in SEE_MEDIA_TYPES],
                "max_edge": SEE_MAX_EDGE,
                "min_bytes": SEE_MIN_IMAGE_BYTES,
            },
            "uptime_s": round(time.time() - self.started, 1),
            "root": self.root,
        }

    # -- captures ---------------------------------------------------------- #
    def capture(self, text: str) -> dict:
        """Write a real note, index it here and now, and describe the new star.

        Returns a payload that is always honest about what happened:
          ok        - the file is on disk AND the brain can search it already
          filed     - the file made it to disk
          indexed   - the notes list in this process now contains it
        Nothing in here is allowed to fail quietly.
        """
        when = time.strftime("%Y-%m-%d")
        slug = capture_slug(text)
        folder, name = capture_target(self.notes_dir, slug)
        rel = "%s/%s" % (CAPTURE_DIR, name)
        # the title comes from the name the file actually gets - including the "-2"
        # that keeps a second identical thought from overwriting the first. build.py
        # titles a note from its filename, so this is the only way the live star and
        # the rebuilt one can carry the same label.
        title = title_from_filename(name)

        # 1. the write. Into a hidden temp file first, then renamed, so a reader
        #    can never catch a half-written note.
        try:
            os.makedirs(folder, exist_ok=True)
            tmp = os.path.join(folder, ".%s.tmp" % name)
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(capture_markdown(title, text, when))
            os.replace(tmp, os.path.join(folder, name))
        except OSError as err:
            try:
                if os.path.exists(locals().get("tmp", "")):
                    os.remove(tmp)
            except OSError:
                pass
            raise CaptureError(plain_error(err), filed=False)
        except Exception as err:                    # a surprise is still not silence
            raise CaptureError("the write failed (%s)" % err.__class__.__name__, filed=False)

        # 2. indexing it - read the file back through the same reader the rest of
        #    the brain uses, so the new note is identical to one found at boot.
        try:
            with self.lock:
                notes = list(self.notes)
                note = read_one_note(self.notes_dir, rel)
                if note is None:
                    note = {"title": title, "group": CAPTURE_DIR, "path": rel,
                            "body": strip_markdown(capture_markdown(title, text, when))}
                note["index"] = len(notes)
                notes.append(note)
                raw = capture_markdown(title, text, when)
                entry = capture_entry(note, notes, raw, text, rel)
                self.notes = notes
                self.captures = [e for e in self.captures if e["file"] != rel] + [entry]
        except Exception as err:
            raise CaptureError("the index would not take it (%s)" % err.__class__.__name__,
                               filed=True, path=rel)

        entry["title"] = title
        entry["date"] = when
        entry["notes"] = len(notes)
        entry["anchor_label"] = (entry["anchor"] or {}).get("label")
        entry["link_labels"] = [l for l in entry["node"]["mentions"]]
        return entry


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
            hour = None
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("hour", [""])[0].isdigit():
                hour = int(query["hour"][0]) % 24
            return self._json(HTTPStatus.OK, self.state.health(hour), head_only)
        if path == "/chat":
            return self._error(HTTPStatus.METHOD_NOT_ALLOWED,
                               "GET /chat is not supported - POST a JSON body with your question.",
                               'Example: curl -X POST -d \'{"question":"..."}\' '
                               'http://127.0.0.1:%d/chat' % self.server.server_address[1])
        if path == "/remember":
            return self._error(HTTPStatus.METHOD_NOT_ALLOWED,
                               "GET /remember is not supported - POST a JSON body instead.",
                               'Example: curl -X POST -d \'{"text":"remember that ..."}\' '
                               'http://127.0.0.1:%d/remember' % self.server.server_address[1])
        if path == "/see":
            return self._error(HTTPStatus.METHOD_NOT_ALLOWED,
                               "GET /see is not supported - POST a JSON body with the question "
                               "and the frame.",
                               'The frame is taken in the browser (viewer/index.html, submitScreen) '
                               'and posted as {"question": "...", "image": "data:image/jpeg;base64,..."} '
                               'to http://127.0.0.1:%d/see' % self.server.server_address[1])
        return self._static(path, head_only)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/chat":
            return self._chat()
        if path == "/remember":
            return self._remember()
        if path == "/see":
            return self._see()
        self._error(HTTPStatus.NOT_FOUND, "No such endpoint: %s" % path,
                    "Only POST /chat, POST /remember and POST /see exist on this server.")

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

    # -- growing the brain -------------------------------------------------- #
    def _remember(self):
        """POST /remember - write the note, index it now, say what happened."""
        state = self.state
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "That is too long to file as one note.")
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return self._error(HTTPStatus.BAD_REQUEST, "Request body must be JSON.",
                               'Send {"text": "remember that ..."}.')
        if not isinstance(payload, dict):
            return self._error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        text = (payload.get("text") or payload.get("question") or payload.get("note") or "").strip()
        if not text:
            return self._error(HTTPStatus.BAD_REQUEST, "There was nothing to remember.",
                               'Send {"text": "remember that ..."}.')
        text = text[:CAPTURE_MAX_CHARS]
        # the trigger is stripped here as well as in the viewer: this endpoint accepts
        # the whole sentence ("remember that the kettle is on the left") and files
        # only the thought behind it
        thought = strip_trigger(text) if is_capture(text) else text
        if not thought:
            return self._json(HTTPStatus.BAD_REQUEST, {
                "ok": False, "captured": False, "filed": False, "indexed": False,
                "code": "nothing_to_remember", "error": "There was nothing after the word remember.",
                "answer": CAPTURE_EMPTY_LINE, "line": CAPTURE_EMPTY_LINE,
                "notes": len(state.notes), "turns": len(state.history) // 2,
            })
        try:
            entry = state.capture(thought)
        except CaptureError as err:
            line = capture_failed(err.reason, filed=err.filed, path=err.path)
            return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "ok": False, "captured": False, "filed": err.filed, "indexed": False,
                "code": "capture_failed", "error": err.reason, "answer": line, "line": line,
                "file": err.path,
                "hint": "Nothing was indexed, so /chat cannot answer from it either. Check that "
                        "the notes folder is writable, then try again.",
                "notes": len(state.notes), "turns": len(state.history) // 2,
            })
        line = capture_line(entry["title"], entry["notes"],
                            anchor=entry["anchor_label"], links=entry["link_labels"])
        return self._json(HTTPStatus.OK, {
            "ok": True, "captured": True, "filed": True, "indexed": True,
            "answer": line, "line": line,
            "title": entry["title"], "date": entry["date"], "file": entry["file"],
            "index": entry["node"]["index"], "node": entry["node"],
            "anchor": entry["anchor"], "links": entry["links"],
            "link_labels": entry["link_labels"], "notes": entry["notes"],
            "graph_file": "behind",       # the new note is not in graph-data.js yet
            "model": state.model, "turns": len(state.history) // 2,
        })

    # -- sight -------------------------------------------------------------- #
    def _see(self):
        """POST /see - a question plus ONE frame of the screen, captured at ask time.

        The client is responsible for taking the frame when the question is asked; this
        endpoint never holds a picture from one request to the next, so a question can
        only ever be answered from the frame that arrived with it.
        """
        state = self.state
        length = int(self.headers.get("Content-Length") or 0)
        if length > SEE_MAX_BODY:
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                               "That frame is too large to send (%.1f MB)." % (length / 1048576.0),
                               "Take the frame at a smaller size: the viewer caps the longest "
                               "edge at %d pixels." % SEE_MAX_EDGE)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return self._error(HTTPStatus.BAD_REQUEST, "Request body must be JSON.",
                               'Send {"question": "...", "image": "data:image/jpeg;base64,..."}.')
        if not isinstance(payload, dict):
            return self._error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")

        question = (payload.get("question") or payload.get("q") or "").strip()
        if not question:
            return self._error(HTTPStatus.BAD_REQUEST, "No question was provided.",
                               'Send {"question": "...", "image": "data:image/jpeg;base64,..."}.')
        question = question[:1000]

        # The frame. Accepted either as a self-describing data URL or as base64 plus a
        # declared media_type; whichever way, the claim is checked against the bytes.
        declared = (payload.get("media_type") or "").strip().lower()
        blob = payload.get("image") or payload.get("frame") or payload.get("image_base64") or ""
        if not isinstance(blob, str) or not blob.strip():
            return self._json(HTTPStatus.BAD_REQUEST, {
                "ok": False, "code": "no_frame", "answer": SIGHT_NO_FRAME_LINE,
                "error": "No frame was sent with the question.",
                "hint": "The frame is captured in the browser when you ask; a request without "
                        "one is a question about a screen that was never shown.",
                "decision": "screen", "on_notes": False, "nodes": [], "sources": [],
                "model": state.model, "turns": len(state.history) // 2,
            })
        prefix_type, b64 = strip_data_url(blob)
        if prefix_type:
            declared = prefix_type
        if len(b64) > SEE_MAX_BODY:
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                               "That frame is too large to send (%.1f MB decoded)."
                               % (len(b64) * 0.75 / 1048576.0))
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            return self._error(HTTPStatus.BAD_REQUEST,
                               "That frame is not valid base64 - it arrived damaged.",
                               "The viewer encodes the frame with canvas.toDataURL('image/jpeg') "
                               "and posts the payload unchanged; a corrupted body means it was "
                               "altered in transit.")

        actual = sniff_media_type(data)
        # THE trap this feature is built to survive: an encoder and a media type that do
        # not agree. Say exactly what was sent and exactly what arrived.
        if declared and actual and declared != actual:
            return self._json(HTTPStatus.BAD_REQUEST, {
                "ok": False, "code": "media_type_mismatch",
                "error": "The frame was declared %s but the bytes are %s." % (declared, actual),
                "answer": SIGHT_NO_FRAME_LINE,
                "hint": "Send the media type of what was actually encoded. The viewer reads it "
                        "back off the data URL (canvas.toDataURL returns what the browser really "
                        "produced) instead of assuming.",
                "declared": declared, "actual": actual, "bytes": len(data),
                "decision": "screen", "on_notes": False, "nodes": [], "sources": [],
                "model": state.model, "turns": len(state.history) // 2,
            })
        if actual not in [t for t, _ in SEE_MEDIA_TYPES]:
            return self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {
                "ok": False, "code": "unsupported_image",
                "error": "That frame is %s (%s), which the model cannot be shown."
                          % (actual or "not an image at all",
                             " ".join("%02x" % b for b in data[:4])),
                "answer": SIGHT_NO_FRAME_LINE,
                "hint": "Send JPEG, PNG or WebP. The viewer asks the canvas for image/jpeg.",
                "declared": declared, "actual": actual, "bytes": len(data),
                "decision": "screen", "on_notes": False, "nodes": [], "sources": [],
                "model": state.model, "turns": len(state.history) // 2,
            })
        if len(data) < SEE_MIN_IMAGE_BYTES:
            return self._json(HTTPStatus.BAD_REQUEST, {
                "ok": False, "code": "frame_too_small",
                "error": "That frame is %d bytes, too small to be a screen." % len(data),
                "answer": SIGHT_NO_FRAME_LINE,
                "hint": "A real capture of a screen is far bigger than this. An empty or "
                        "blank canvas usually means the share ended before the frame was taken.",
                "bytes": len(data), "decision": "screen", "on_notes": False,
                "nodes": [], "sources": [], "model": state.model,
                "turns": len(state.history) // 2,
            })
        if actual == "image/jpeg" and not data.endswith(b"\xff\xd9"):
            return self._json(HTTPStatus.BAD_REQUEST, {
                "ok": False, "code": "frame_truncated",
                "error": "That JPEG is truncated - it has no end-of-image marker.",
                "answer": SIGHT_NO_FRAME_LINE,
                "hint": "The frame was cut short in transit. Nothing was shown to the model, "
                        "because half a screen is worse than none.",
                "bytes": len(data), "decision": "screen", "on_notes": False,
                "nodes": [], "sources": [], "model": state.model,
                "turns": len(state.history) // 2,
            })
        width, height = image_dimensions(data, actual)
        if width and height and (width > SEE_MAX_EDGE or height > SEE_MAX_EDGE):
            return self._json(HTTPStatus.BAD_REQUEST, {
                "ok": False, "code": "frame_too_large",
                "error": "That frame is %dx%d, larger than this server accepts." % (width, height),
                "answer": SIGHT_NO_FRAME_LINE,
                "hint": "The viewer caps the longest edge at %d pixels before encoding."
                        % SEE_MAX_EDGE,
                "width": width, "height": height, "bytes": len(data),
                "decision": "screen", "on_notes": False, "nodes": [], "sources": [],
                "model": state.model, "turns": len(state.history) // 2,
            })

        frame = {
            "media_type": actual,
            "bytes": len(data),
            "width": width,
            "height": height,
            "question": question,
            "asked_at": payload.get("asked_at"),
            "captured_at": payload.get("captured_at"),
            "at": time.time(),
        }
        with state.lock:
            history = list(state.history)
        messages = build_see_messages(question, data, actual, history)

        state.questions += 1
        try:
            answer = call_openai(state.cfg, messages, state.base_url)
        except BrainError as err:
            # Nothing is recorded as looked at: no frame was read, so the galaxy, the
            # frame counter and the history all stay exactly as they were.
            return self._json(err.status, {
                "ok": False, "error": err.message, "code": err.code, "hint": err.hint,
                "answer": err.message, "decision": "screen", "on_notes": False,
                "nodes": [], "sources": [], "read": [], "frame": frame,
                "model": state.model, "turns": len(history) // 2,
            })
        except Exception as err:                                  # never crash the server
            return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "ok": False, "error": "Unexpected brain failure: %s" % err,
                "code": "internal_error", "decision": "screen", "on_notes": False,
                "nodes": [], "sources": [], "read": [], "frame": frame,
            })

        with state.lock:
            state.history.append({"role": "user", "content": question})
            state.history.append({"role": "assistant", "content": answer})
            state.history = state.history[-(HISTORY_TURNS * 2):]
            state.frames += 1
            state.last_frame = frame         # measurements only, never the picture

        return self._json(HTTPStatus.OK, {
            "ok": True,
            "answer": answer,
            "decision": "screen",            # never "notes": a screen answer lights nothing
            "on_notes": False,
            "nodes": [],                     # so the galaxy holds still, as it must
            "sources": [],
            "read": [],
            "frame": frame,
            "frames_looked_at": state.frames,
            "model": state.model,
            "turns": len(state.history) // 2,
        })

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
    print("  endpoints   : GET /  GET /health  POST /chat  POST /remember  POST /see")
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
