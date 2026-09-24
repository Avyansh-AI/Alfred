#!/usr/bin/env python3
"""
build.py - the indexer.

Scans every .md file under your notes folder and writes viewer/graph-data.js:

    const GRAPH = {nodes: [...], links: [...]};

Each node gets:
  id      = its index in the nodes array (later prompts look nodes up by index)
  label   = derived from the filename
  group   = its parent folder name
  excerpt = roughly 700 characters of the note

Two notes are linked when one mentions the other's title in its body,
or when they share a [[wikilink]].

Python 3 standard library only. No dependencies.

    python3 build.py                        # index ./notes
    python3 build.py --notes ~/vault --check
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter

EXCERPT_CHARS = 700
DEFAULT_NOTES = "notes"
DEFAULT_OUT = os.path.join("viewer", "graph-data.js")
SKIP_DIRS = {".git", ".obsidian", ".trash", "node_modules", "__pycache__", ".venv", "venv"}

# words kept lowercase in the middle of a title ("Route and Halts")
SMALL_WORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into",
    "nor", "of", "on", "onto", "or", "over", "per", "the", "to", "up", "via",
    "vs", "with",
}


# --------------------------------------------------------------------------- #
# text helpers
# --------------------------------------------------------------------------- #
def title_from_path(path: str) -> str:
    """Filename -> human label. '01-moving_to-ayodhya.md' -> 'Moving to Ayodhya'."""
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"^\d+[\s._-]+", "", stem)          # drop a leading "01-"
    stem = re.sub(r"[-_]+", " ", stem)                 # dashes/underscores -> spaces
    stem = re.sub(r"\s+", " ", stem).strip()
    if not stem:
        return os.path.basename(path)
    words = stem.split(" ")
    out = []
    for i, w in enumerate(words):
        if w.isupper() and len(w) > 1:                 # keep acronyms like UPPCL
            out.append(w)
        elif i not in (0, len(words) - 1) and w.lower() in SMALL_WORDS:
            out.append(w.lower())
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


def normalise(s: str) -> str:
    """Lowercase, strip accents/punctuation, collapse whitespace. Used for matching."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """Markdown -> a single readable paragraph of roughly `limit` characters."""
    t = text
    t = re.sub(r"^---\s*\n.*?\n---\s*\n", "", t, flags=re.S)   # YAML frontmatter
    t = re.sub(r"```.*?```", " ", t, flags=re.S)               # fenced code
    t = re.sub(r"`([^`]*)`", r"\1", t)                         # inline code
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)                # images
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)             # links -> text
    t = re.sub(r"\[\[([^\]|#]*)(?:[#|][^\]]*)?\]\]", r"\1", t) # [[wikilink]] -> text
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)        # heading marks
    t = re.sub(r"^\s{0,3}>\s?", "", t, flags=re.M)             # blockquotes
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)             # bullets
    t = re.sub(r"^\s*\d+[.)]\s+", "", t, flags=re.M)           # numbered lists
    t = re.sub(r"[*_~]{1,3}", "", t)                           # emphasis marks
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:-") + "\u2026"


def sentence_key(text_lower: str, key: str) -> bool:
    """True if `key` (already normalised, lowercase) appears as a whole word run.
    `text_lower` must already be lowercased."""
    if not key:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])"
    return re.search(pattern, text_lower) is not None


# --------------------------------------------------------------------------- #
# scanning
# --------------------------------------------------------------------------- #
def iter_markdown(root: str) -> list:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if name.lower().endswith(".md") and not name.startswith("."):
                found.append(os.path.join(dirpath, name))
    return found


def read_text(path: str) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as fh:
                return fh.read()
        except UnicodeDecodeError:
            continue
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def collect_notes(root: str) -> list:
    """Return note dicts: path, rel, label, group, raw, excerpt, wikilinks, mentions."""
    notes = []
    for path in iter_markdown(root):
        raw = read_text(path)
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        parent = os.path.dirname(rel)
        group = (parent.split("/")[0] if parent else os.path.basename(os.path.abspath(root)))
        label = title_from_path(path)
        body = excerpt(raw, 10 ** 9)                 # cleaned full text, for matching
        wikilinks = [w.strip() for w in re.findall(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]", raw)]
        notes.append(
            {
                "path": path,
                "rel": rel,
                "label": label,
                "group": group or "notes",
                "raw": raw,
                "clean": body,
                "clean_lower": body.lower(),          # mention matching is case-insensitive
                "chars": len(raw),
                "words": len(body.split()),
                "excerpt": excerpt(raw, EXCERPT_CHARS),
                "wikilinks": [w for w in dict.fromkeys(wikilinks)],
                "mentions": [],
            }
        )
    return notes


def build_links(notes: list) -> tuple:
    """Links from (a) wikilinks and (b) one note mentioning another's title."""
    by_key = {}
    for i, n in enumerate(notes):
        for key in {normalise(n["label"]), normalise(os.path.splitext(os.path.basename(n["path"]))[0])}:
            if key:
                by_key.setdefault(key, []).append(i)

    pair_kind = {}      # (i, j) with i < j -> "wikilink" | "mention"
    pair_weight = Counter()
    dangling = []

    for i, n in enumerate(notes):
        # (a) explicit [[wikilinks]]
        for w in n["wikilinks"]:
            key = normalise(w)
            targets = [j for j in by_key.get(key, []) if j != i]
            if not targets:
                dangling.append((n["label"], w))
            for j in targets:
                pair_kind[(min(i, j), max(i, j))] = "wikilink"
                pair_weight[(min(i, j), max(i, j))] += 2

        # (b) plain mention of another note's title
        for j, other in enumerate(notes):
            if i == j:
                continue
            if sentence_key(n["clean_lower"], normalise(other["label"])):
                n["mentions"].append(other["label"])
                k = (min(i, j), max(i, j))
                pair_kind.setdefault(k, "mention")
                pair_weight[k] += 1

    links = []
    for (a, b), kind in sorted(pair_kind.items()):
        links.append(
            {
                "source": a,
                "target": b,
                "kind": kind,
                "weight": round(pair_weight[(a, b)], 2),
            }
        )
    return links, dangling


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
def render_js(nodes: list, links: list, notes_dir: str) -> str:
    header = (
        "// Generated by build.py - do not edit by hand.\n"
        "// Rebuild with:  python3 build.py --notes %s\n"
        "// notes: %d   links: %d   generated: %s\n"
        % (notes_dir, len(nodes), len(links), time.strftime("%Y-%m-%d %H:%M:%S"))
    )
    body = json.dumps({"nodes": nodes, "links": links}, indent=2, ensure_ascii=False)
    tail = (
        "\n\n// `const GRAPH` is the source of truth; the window assignment is a safety net\n"
        "// for environments that only look at globals.\n"
        "if (typeof window !== \"undefined\") window.GRAPH = GRAPH;\n"
    )
    js = header + "const GRAPH = " + body + ";\n" + tail
    # node ids must equal their array index - the brain looks nodes up that way
    for i, n in enumerate(nodes):
        assert n["id"] == i, "node id must equal its index in the nodes array"
    return js


def check_data(nodes: list, links: list) -> list:
    """Hard validation. Returns a list of problem strings ([] == healthy)."""
    problems = []
    for i, n in enumerate(nodes):
        if n.get("id") != i:
            problems.append("node %d has id %r (must equal index)" % (i, n.get("id")))
        if not n.get("label"):
            problems.append("node %d has no label" % i)
        if not n.get("group"):
            problems.append("node %d has no group" % i)
        if not n.get("excerpt"):
            problems.append("node %d has no excerpt" % i)
    labels = Counter(n["label"].lower() for n in nodes)
    for label, count in labels.items():
        if count > 1:
            problems.append("duplicate note title %r appears %d times" % (label, count))
    for k, link in enumerate(links):
        for end in ("source", "target"):
            v = link.get(end)
            if not isinstance(v, int) or not (0 <= v < len(nodes)):
                problems.append("link %d %s=%r is not a valid node index" % (k, end, v))
        if link.get("source") == link.get("target"):
            problems.append("link %d points at itself" % k)
    return problems


def main(argv=None) -> int:
    global EXCERPT_CHARS

    ap = argparse.ArgumentParser(description="Index markdown notes into viewer/graph-data.js")
    ap.add_argument("--notes", default=DEFAULT_NOTES, help="notes folder (default: ./notes)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output file (default: viewer/graph-data.js)")
    ap.add_argument("--excerpt", type=int, default=EXCERPT_CHARS, help="excerpt length (default: 700)")
    ap.add_argument("--check", action="store_true", help="validate the result and exit non-zero on problems")
    ap.add_argument("--quiet", action="store_true", help="only print the one-line summary")
    ap.add_argument("--summary-json", default=None, help="also write a JSON stats file here")
    args = ap.parse_args(argv)

    EXCERPT_CHARS = args.excerpt

    notes_dir = os.path.abspath(os.path.expanduser(args.notes))
    if not os.path.isdir(notes_dir):
        print("build.py: notes folder not found: %s" % notes_dir, file=sys.stderr)
        print("         create it, or point the indexer somewhere else:  build.py --notes ~/vault", file=sys.stderr)
        return 2

    raw_notes = collect_notes(notes_dir)
    if not raw_notes:
        print("build.py: no .md files found under %s" % notes_dir, file=sys.stderr)
        return 2

    links, dangling = build_links(raw_notes)

    nodes = []
    for i, n in enumerate(raw_notes):
        nodes.append(
            {
                "id": i,
                "index": i,
                "label": n["label"],
                "group": n["group"],
                "excerpt": n["excerpt"],
                "path": n["rel"],
                "words": n["words"],
                "chars": n["chars"],
                "degree": 0,
                "wikilinks": n["wikilinks"],
                "mentions": n["mentions"],
            }
        )
    for link in links:
        nodes[link["source"]]["degree"] += 1
        nodes[link["target"]]["degree"] += 1

    problems = check_data(nodes, links)

    out_path = os.path.abspath(os.path.expanduser(args.out))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(render_js(nodes, links, os.path.relpath(notes_dir, os.getcwd()) if notes_dir.startswith(os.getcwd()) else notes_dir))

    groups = Counter(n["group"] for n in nodes)
    orphans = [n["label"] for n in nodes if n["degree"] == 0]

    if not args.quiet:
        print("Indexed %d notes from %s" % (len(nodes), notes_dir))
        print("  links        : %d (%d wikilink, %d mention)"
              % (len(links),
                 sum(1 for l in links if l["kind"] == "wikilink"),
                 sum(1 for l in links if l["kind"] == "mention")))
        print("  groups       : %s" % ", ".join("%s (%d)" % (g, c) for g, c in sorted(groups.items())))
        print("  excerpt      : %d chars each" % args.excerpt)
        print("  unmatched    : %s" % (", ".join(orphans) if orphans else "none"))
        if dangling:
            print("  dead links   : %s" % ", ".join("[[%s]] in %s" % (w, n) for n, w in dangling[:8]))
        print("  wrote        : %s (%.1f KB)" % (os.path.relpath(out_path, os.getcwd()), os.path.getsize(out_path) / 1024.0))

    if problems:
        print("  PROBLEMS     : %d" % len(problems))
        for p in problems[:10]:
            print("    - %s" % p)
        return 1

    if args.summary_json:
        with open(args.summary_json, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "notes_dir": notes_dir,
                    "out": out_path,
                    "nodes": len(nodes),
                    "links": len(links),
                    "groups": dict(groups),
                    "orphans": orphans,
                    "dead_links": ["[[%s]] in %s" % (w, n) for n, w in dangling],
                    "titles": [n["label"] for n in nodes],
                },
                fh,
                indent=2,
            )

    if args.check:
        print("build.py: OK - %d nodes, %d links, ids match indexes" % (len(nodes), len(links)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
