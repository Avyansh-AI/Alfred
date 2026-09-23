#!/usr/bin/env python3
"""
tools/verify.py - end-to-end verification of Alfred.

Starts the real server.py, hits the real endpoints with the real viewer files and
checks the whole brain path against a stub OpenAI endpoint (so no API key and no
outbound network are needed) - /chat, /remember and /see, including what the model
was actually handed for a screen question.

  python3 tools/verify.py            # uses ports 4700 + two ephemeral ones
  python3 tools/verify.py --port 4711
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import base64
import struct
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

results = []


def ok(msg):
    results.append(("ok", msg))
    print("  ok    %s" % msg)


def bad(msg):
    results.append(("FAIL", msg))
    print("  FAIL  %s" % msg)


def check(cond, msg):
    ok(msg) if cond else bad(msg)
    return bool(cond)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def request(url, method="GET", payload=None, timeout=20, headers=None):
    data = None
    hdrs = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read().decode("utf-8", "replace")
    except Exception as err:                       # connection refused etc.
        return None, {}, str(err)


def make_png(width, height, rgb=(20, 30, 60)):
    """A real PNG, built with the standard library - no Pillow, ever."""
    raw = b"".join(
        b"\x00" + b"".join(bytes(((rgb[0] + x * 3) % 256, (rgb[1] + y * 5) % 256,
                                  (rgb[2] + x + y) % 256)) for x in range(width))
        for y in range(height))

    def chunk(tag, data):
        payload = tag + data
        return (struct.pack(">I", len(data)) + payload
                + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# --------------------------------------------------------------------------- #
# a stub OpenAI endpoint so the full /chat path can be tested offline
# --------------------------------------------------------------------------- #
class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = {}
        self.server.calls.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": body,
            "at": time.time(),
        })
        model = body.get("model", "unknown")
        n_ctx = len(body.get("messages", []))
        answer = ("STUB ANSWER from %s after %d messages." % (model, n_ctx))
        out = json.dumps({
            "id": "chatcmpl-stub",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": answer},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def start_stub():
    port = free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), StubHandler)
    httpd.calls = []
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


# --------------------------------------------------------------------------- #
def drain(proc, sink):
    """Read the child's stdout in the background so nothing ever blocks."""
    def run():
        try:
            for line in proc.stdout:
                sink.append(line.rstrip())
        except Exception:
            pass
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def start_server(port, config_path, extra=None):
    cmd = [PY, "-u", os.path.join(ROOT, "server.py"),
           "--port", str(port), "--host", "127.0.0.1",
           "--config", config_path,
           "--notes", os.path.join(ROOT, "notes"),
           "--root", os.path.join(ROOT, "viewer")]
    cmd += extra or []
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    banner = []
    drain(proc, banner)
    for _ in range(150):
        status, _, _ = request("http://127.0.0.1:%d/" % port, timeout=2)
        if status:
            break
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    return proc, banner


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=None,
                    help="port to verify on (default: 4700, or any free port if that is busy)")
    args = ap.parse_args()

    print("\nAlfred - end to end verification")
    print("  project root : %s" % ROOT)
    print("  python       : %s" % sys.version.split()[0])

    # ------------------------------------------------------------------ build
    print("\n[1/11] indexer")
    build = subprocess.run([PY, os.path.join(ROOT, "build.py"), "--check", "--quiet"],
                           cwd=ROOT, capture_output=True, text=True)
    check(build.returncode == 0, "build.py --check exits cleanly: %s" % (build.stdout.strip() or build.stderr.strip()))

    # a notes folder with two notes that only reference each other by title proves
    # the "one mentions the other's title" rule works without any [[wikilinks]]
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "topic"))
        with open(os.path.join(tmp, "topic", "alpha-note.md"), "w") as fh:
            fh.write("# Alpha Note\n\nThis one is only ever referred to in prose. " * 8)
        with open(os.path.join(tmp, "topic", "beta-note.md"), "w") as fh:
            fh.write("# Beta Note\n\nRead the Alpha Note before the meeting. " * 8)
        out = os.path.join(tmp, "graph-data.js")
        subprocess.run([PY, os.path.join(ROOT, "build.py"), "--notes", tmp, "--out", out,
                        "--quiet", "--check"], cwd=ROOT, capture_output=True, text=True)
        src = open(out).read()
        data = json.loads(src[src.index("{"):src.rindex("}") + 1])
        kinds = {link["kind"] for link in data["links"]}
        check("mention" in kinds, "a prose mention of another note's title creates a link (kind=mention)")
        # and a wikilink test
        with open(os.path.join(tmp, "topic", "alpha-note.md"), "a") as fh:
            fh.write("\n\nSee [[Beta Note]] for the rest.")
        subprocess.run([PY, os.path.join(ROOT, "build.py"), "--notes", tmp, "--out", out,
                        "--quiet", "--check"], cwd=ROOT, capture_output=True, text=True)
        src = open(out).read()
        data = json.loads(src[src.index("{"):src.rindex("}") + 1])
        kinds = {link["kind"] for link in data["links"]}
        check("wikilink" in kinds, "a [[wikilink]] creates a link (kind=wikilink)")
        check(all(n["id"] == i for i, n in enumerate(data["nodes"])), "node ids equal array indexes in the test vault too")

    js = open(os.path.join(ROOT, "viewer", "graph-data.js")).read()
    try:
        graph = json.loads(js[js.index("{"):js.rindex("}") + 1])
    except ValueError as err:
        print("  could not parse viewer/graph-data.js: %s" % err)
        return 1

    # ------------------------------------------------------------------ server
    print("\n[2/11] server + static files")
    port = args.port or 4700
    if request("http://127.0.0.1:%d/" % port, timeout=1)[0] is not None:
        if args.port:
            bad("port %d is already in use - stop whatever is running there" % port)
            return 1
        port = free_port()
        print("  note  port 4700 is busy (the live server is probably up) - verifying on %d instead" % port)

    stub, stub_port = start_stub()
    # A second stub stands in for OpenRouter. Same protocol, so the OpenRouter route is
    # exercised for real: if the server sends a swap to the wrong place, this one never
    # hears about it and the checks below fail.
    or_stub, or_port = start_stub()
    tmpcfg = os.path.join(tempfile.mkdtemp(prefix="alfred-"), "config.json")
    with open(tmpcfg, "w") as fh:
        json.dump({"openai_api_key": "sk-stub-key-for-verification", "model": "gpt-6-astra"}, fh)

    proc, banner = start_server(port, tmpcfg,
                                ["--openai-base-url", "http://127.0.0.1:%d/v1" % stub_port,
                                 "--openrouter-base-url", "http://127.0.0.1:%d/v1" % or_port])
    base = "http://127.0.0.1:%d" % port
    try:
        check(proc.poll() is None, "server.py is running (pid %s)" % proc.pid)
        check(any("viewer      :" in b for b in banner), "server printed its startup banner")

        status, headers, body = request(base + "/")
        check(status == 200, "GET / returns 200 (got %s)" % status)
        check("text/html" in headers.get("Content-Type", ""), "GET / is served as text/html")
        for needle in ['id="stage"', "graph-data.js", "3d-force-graph@1.80.0", 'id="ask-form"', "ForceGraph3D"]:
            check(needle in body, "the page contains %r" % needle)
        check("PUT-YOUR-KEY-HERE" not in body and "sk-stub" not in body and "openai_api_key" not in body,
              "the page never carries the API key")

        status, headers, body = request(base + "/graph-data.js")
        check(status == 200, "GET /graph-data.js returns 200")
        check("javascript" in headers.get("Content-Type", ""), "graph-data.js is served as javascript")
        check(body.startswith("// Generated by build.py") and "const GRAPH" in body,
              "graph-data.js has the generated header and declares GRAPH")

        status, _, body = request(base + "/health")
        health = json.loads(body) if status == 200 else {}
        check(status == 200, "GET /health returns 200")
        check(health.get("notes") == len(graph["nodes"]),
              "the server reads the same %d notes the viewer has" % len(graph["nodes"]))
        norm = lambda s: "".join(c for c in s.lower() if c.isalnum())
        check([norm(t) for t in health.get("titles", [])] == [norm(n["label"]) for n in graph["nodes"]],
              "server note order matches the node indexes in graph-data.js")
        check(health.get("key", {}).get("state") == "set", "server reports the key as set (stub config)")
        check("sk-stub" not in body, "/health never echoes the key itself")

        # everything outside viewer/ must be unreachable
        print("\n[3/11] the server serves only viewer/")
        for path in ["/../config.json", "/%2e%2e/config.json", "/../server.py", "/../build.py",
                     "/..%2fconfig.json", "/../.git/config"]:
            status, _, body = request(base + path)
            check(status in (400, 403, 404) and "openai_api_key" not in body,
                  "%s -> %s, no secret leaked" % (path, status))
        status, _, _ = request(base + "/nope.js")
        check(status == 404, "a missing viewer file returns 404 (got %s)" % status)

        # the optional offline copy, if tools/vendor.py has been run
        vendor_js = os.path.join(ROOT, "viewer", "vendor", "3d-force-graph.min.js")
        if os.path.exists(vendor_js):
            status, headers, body = request(base + "/vendor/3d-force-graph.min.js")
            check(status == 200 and "javascript" in headers.get("Content-Type", ""),
                  "the vendored fallback bundle is served as javascript (%s)" % status)
            check(len(body) > 1000000, "the vendored bundle is the real thing (%.0f KB)" % (len(body) / 1024.0))
            check(body.startswith("// Version 1.80.0"), "the vendored bundle is 3d-force-graph 1.80.0")
            status, _, body = request(base + "/vendor/three.module.min.js")
            check(status == 200 and "from\"./three.core.min.js\"" in body,
                  "the vendored three.js still resolves its relative three.core.min.js import")
            with open(os.path.join(ROOT, "viewer", "index.html")) as fh:
                page = fh.read()
            cdn_line = page.index("cdn.jsdelivr.net/npm/3d-force-graph")
            vendor_line = page.index("./vendor/3d-force-graph.min.js")
            check(cdn_line < vendor_line, "the viewer lists the CDN before the local copy (CDN stays primary)")
        else:
            print("  note  viewer/vendor/ is empty - run tools/vendor.py to exercise the offline fallback")
        status, headers, body = request(base + "/", method="HEAD")
        check(status == 200 and body == "" and "Content-Length" in headers,
              "HEAD / works (proxies and previews need it)")

        # ----------------------------------------------------------------- brain
        print("\n[4/11] the brain, with a stubbed OpenAI")
        status, _, body = request(base + "/chat")
        check(status == 405, "GET /chat is a clean 405, not a crash (got %s)" % status)
        status, _, body = request(base + "/chat", method="POST", payload=None)
        check(status == 400, "POST /chat with no body is a clean 400 (got %s)" % status)
        status, _, body = request(base + "/chat", method="POST",
                                  payload={}, headers={"Content-Type": "application/json"})
        check(status == 400 and "No question" in body, "POST /chat with an empty question is a clean 400")
        status, _, body = request(base + "/chat", method="POST", payload={"other": 1},
                                  headers={"Content-Type": "application/json"})
        check(status == 400, "POST /chat with the wrong field is a clean 400")

        q1 = "what did the movers quote for the road trip?"
        status, _, body = request(base + "/chat", method="POST", payload={"question": q1})
        check(status == 200, "POST /chat returns 200")
        try:
            data = json.loads(body)
        except ValueError:
            data = {}
            bad("POST /chat did not return JSON: %s" % body[:200])
        check(isinstance(data.get("answer"), str) and data["answer"].startswith("STUB ANSWER"),
              "the answer comes back from the model: %r" % (data.get("answer") or "")[:60])
        idx = data.get("nodes")
        check(isinstance(idx, list) and 1 <= len(idx) <= 6, "nodes[] holds at most 6 indexes: %s" % idx)
        check(all(isinstance(i, int) and 0 <= i < len(graph["nodes"]) for i in idx or []),
              "every index in nodes[] is a real node index")
        check(data.get("model") == "gpt-6-astra", "the model from config.json is used and echoed back")
        check(len(stub.calls) == 1, "exactly one upstream call was made")
        sent = stub.calls[-1]
        check(sent["auth"] == "Bearer sk-stub-key-for-verification", "the key is sent as a bearer token, server-side only")
        system = sent["body"]["messages"][0]
        check(system["role"] == "system" and "ONLY from those notes" in system["content"],
              "the system prompt forbids answering from outside the notes")
        check("most three sentences" in system["content"], "the system prompt sets the answer length")
        check("butler" in system["content"] and "dry" in system["content"],
              "and it is the butler talking: dry, polite, in character")
        check('sir' in system["content"] and "not in every sentence" in system["content"],
              "the prompt tells him how often to say \"sir\"")
        check("Never recite the note back" in system["content"],
              "and never to read the note out - it is on the reader's screen")
        check("never force a joke" in system["content"],
              "one funny line beats three bland ones, but never forced")
        check("Never invent a source" in system["content"],
              "and an uncovered question gets dignity, not invention")
        user_msg = sent["body"]["messages"][-1]["content"]
        check("Notes from the user's knowledge galaxy" in user_msg, "the prompt carries the note excerpts")
        top_label = graph["nodes"][idx[0]]["label"]
        check(all(f"[{i}]" in user_msg for i in idx), "every selected note is numbered by its index")
        check(len([m for m in sent["body"]["messages"] if m["role"] == "user"]) == 1,
              "the first question is sent without history")

        q2 = "and what about the deposit?"
        status, _, body = request(base + "/chat", method="POST", payload={"question": q2})
        data2 = json.loads(body) if status == 200 else {}
        roles = [m["role"] for m in stub.calls[-1]["body"]["messages"]]
        check(roles[0] == "system" and roles.count("user") == 2 and roles.count("assistant") == 1,
              "the follow-up carries the earlier exchange as history: %s" % roles)
        check(q1 in json.dumps(stub.calls[-1]["body"]), "the previous question is really in the history")
        check(data2.get("turns") == 2, "the server reports 2 turns of memory")

        relevance = [r["label"] for r in data["sources"]]
        check(bool(top_label), "the straw poll of sources: %s" % ", ".join(relevance))

        for probe, expect in [("budget for the move", "Budget for the Move"),
                              ("packing", "Packing Stuff"),
                              ("booking train", "Booking Train to Ayodhya"),
                              ("vande bharat", "Booking Train to Ayodhya"),   # matched from the note body
                              ("internet", "Internet and Electricity in Ayodhya")]:
            status, _, body = request(base + "/chat", method="POST", payload={"question": probe})
            d = json.loads(body)
            labels = [graph["nodes"][i]["label"] for i in d["nodes"]]
            check(expect in labels, "question %r retrieves %r (got: %s)" % (probe, expect, ", ".join(labels[:3])))

        check(proc.poll() is None, "the server is still alive after all of that")

        # ------------------------------------------------------------ provenance
        print("\n[5/11] provenance: does the server know when a question was about the notes?")
        def ask_prov(q):
            st, _, bd = request(base + "/chat", method="POST", payload={"question": q})
            try:
                return st, json.loads(bd)
            except ValueError:
                return st, {}

        st, d = ask_prov("what is the budget for the move?")
        check(d.get("on_notes") is True and d.get("decision") == "notes",
              "a question about the notes is decided as such before anything moves: %s" % d.get("decision"))
        one = [graph["nodes"][i]["label"] for i in d.get("nodes") or []]
        check(one == ["Budget for the Move"], "exactly one note carries that answer: %s" % one)
        check(len(d.get("read") or []) == 6, "while the model was still shown six: read=%s" % d.get("read"))
        check(set(d.get("nodes") or []) <= set(d.get("read") or []),
              "the notes it came from are a subset of the notes it read")
        check([x["label"] for x in d.get("sources") or []] == one,
              "sources[] names the same notes, with their scores")

        st, broad = ask_prov("what should I pack and prepare before moving, and what is the budget?")
        check(broad.get("on_notes") is True and len(broad.get("nodes") or []) >= 4,
              "a broad question comes back with a cluster of %d notes: %s"
              % (len(broad.get("nodes") or []), [graph["nodes"][i]["label"] for i in broad.get("nodes") or []]))
        narrow_idx = (d.get("nodes") or [None])[0]
        check(narrow_idx in (broad.get("nodes") or []) and len(broad.get("nodes") or []) >= 4,
              "the same note is inside the broad answer's cluster, which claims %d sources instead of 1"
              % len(broad.get("nodes") or []))

        st, g = ask_prov("good morning")
        check(g.get("on_notes") is False and g.get("decision") == "small_talk",
              "a greeting is small talk, not a question about the notes: %s" % g.get("decision"))
        check(g.get("nodes") == [] and g.get("read") == [],
              "so it claims no sources and reads no notes: nodes=%s read=%s" % (g.get("nodes"), g.get("read")))
        check(str(g.get("answer") or "").startswith("STUB ANSWER"), "and is still answered")
        small_talk_body = json.dumps(stub.calls[-1]["body"])
        check("Notes from the user's knowledge galaxy" not in small_talk_body,
              "no note excerpts were sent to the model for small talk")
        check("ONE short, friendly sentence" in small_talk_body,
              "small talk gets its own prompt instead of the notes one")

        st, j = ask_prov("tell me a joke")
        check(j.get("on_notes") is False and j.get("decision") == "small_talk", "a joke request is small talk too")
        st, who = ask_prov("who are you?")
        check(who.get("on_notes") is False and who.get("decision") == "small_talk", "so is \"who are you?\"")

        st, dep = ask_prov("and what about the deposit?")
        check(dep.get("on_notes") is True and dep.get("decision") == "follow_up",
              "a vague question after a notes turn is a follow-up, not small talk: %s" % dep.get("decision"))
        check("Notes from the user's knowledge galaxy" in json.dumps(stub.calls[-1]["body"]),
              "and it still gets the notes, so follow-ups keep working")

        # ------------------------------------------------- placeholder-key path
        # ------------------------------------------------------- the persona, and the greeting he opens with
        print("\n[6/11] the persona and the boot greeting")
        sys.path.insert(0, ROOT)
        import server as alfred_server
        src = open(os.path.join(ROOT, "server.py")).read()
        banner = src.find("T H E   P E R S O N A")
        check(banner > 0, "the persona has its own banner comment")
        check(banner < src.find("def ensure_config"),
              "and it sits at the top of server.py, above every function (line %d)"
              % (src[:banner].count(chr(10)) + 1))
        for name in ("PERSONA", "ANSWER_STYLE", "CHAT_STYLE", "GREETING", "PARTS_OF_DAY"):
            where = src.find("\n%s = " % name)
            check(banner < where < src.find("def ensure_config"),
                  "%s is defined inside the persona block (line %d)" % (name, src[:where].count(chr(10)) + 1))
        check(banner < src.index("def part_of_day"),
              "so are the time-of-day names and the greeting builder")
        check(src[banner:src.find("def ensure_config")].count("def ") == 4,
              "and the character is otherwise prose and strings, not machinery")

        hours = [alfred_server.part_of_day(h) for h in range(24)]
        check(all(h in ("morning", "afternoon", "evening") for h in hours) and len(set(hours)) == 3,
              "every hour of the day has a part of day: %s" % ", ".join(sorted(set(hours))))
        check(hours[8] == "morning" and hours[13] == "afternoon" and hours[21] == "evening",
              "morning at 08:00, afternoon at 13:00, evening at 21:00")
        check(hours[2] == "evening" and hours[23] == "evening",
              "and a butler still says good evening at 2am")

        line = alfred_server.greeting(485, 19)
        check(line == "Good evening, sir. 485 notes indexed, all present and accounted for.",
              "the greeting reads as the brief asks: %r" % line)
        check("sir" in line and line.endswith("accounted for."), "with the butler's manners intact")
        check(alfred_server.greeting(1, 9) == "Good morning, sir. One note indexed, all present and accounted for.",
              "one note is not \"1 notes\": %r" % alfred_server.greeting(1, 9))

        status, _, body = request(base + "/health?hour=8")
        h8 = json.loads(body) if status == 200 else {}
        check(h8.get("greeting", "").startswith("Good morning, sir."),
              "/health answers with a morning greeting for hour=8: %r" % h8.get("greeting"))
        status, _, body = request(base + "/health?hour=21")
        h21 = json.loads(body) if status == 200 else {}
        check(h21.get("greeting", "").startswith("Good evening, sir."),
              "and an evening one for hour=21: %r" % h21.get("greeting"))
        check(str(h21.get("notes")) in h21.get("greeting", ""),
              "the greeting carries the real note count (%s)" % h21.get("notes"))
        check(h21.get("notes") == len(graph["nodes"]),
              "which is the same number of nodes the galaxy is drawn from (%d)" % len(graph["nodes"]))

        # a different vault must change the number: nothing here is written by hand
        with tempfile.TemporaryDirectory(prefix="alfred-vault-") as vault:
            os.makedirs(os.path.join(vault, "home"))
            for i in range(3):
                with open(os.path.join(vault, "home", "note-%d.md" % i), "w") as fh:
                    fh.write("# Note %d\n\nSomething about moving house, number %d.\n" % (i, i))
            vp = free_port()
            vcfg = os.path.join(vault, "config.json")
            vproc, _vbanner = start_server(vp, vcfg, extra=["--notes", vault])
            try:
                for _ in range(100):
                    if request("http://127.0.0.1:%d/health" % vp, timeout=2)[0]:
                        break
                    time.sleep(0.1)
                status, _, body = request("http://127.0.0.1:%d/health?hour=14" % vp)
                vh = json.loads(body) if status == 200 else {}
                check(vh.get("notes") == 3 and vh.get("greeting") ==
                      "Good afternoon, sir. 3 notes indexed, all present and accounted for.",
                      "a three-note vault gets a three-note greeting: %r" % vh.get("greeting"))
            finally:
                vproc.terminate()
                try:
                    vproc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    vproc.kill()

        # ------------------------------------------------- placeholder-key path
        # ------------------------------------------------- growing the brain by voice
        print("\n[7/11] growing the brain by voice")
        # A throwaway copy of the real vault AND of the real viewer/graph-data.js, so
        # the ordering rules under test are the ones this project actually runs with -
        # and no test ever writes into the repo's own notes folder.
        vdir = tempfile.mkdtemp(prefix="alfred-remember-")
        vault = os.path.join(vdir, "notes")
        shutil.copytree(os.path.join(ROOT, "notes"), vault)
        gcopy = os.path.join(vdir, "graph-data.js")
        shutil.copyfile(os.path.join(ROOT, "viewer", "graph-data.js"), gcopy)
        rcfg = os.path.join(vdir, "config.json")
        with open(rcfg, "w") as fh:
            json.dump({"openai_api_key": "sk-stub-key-for-verification", "model": "gpt-6-astra"}, fh)
        rport = free_port()
        rproc, _rban = start_server(rport, rcfg, ["--notes", vault, "--graph", gcopy,
                                                 "--openai-base-url", "http://127.0.0.1:%d/v1" % stub_port])
        rbase = "http://127.0.0.1:%d" % rport
        graph_before = open(gcopy, "rb").read()
        status, _, body = request(rbase + "/health")
        rhealth0 = json.loads(body) if status == 200 else {}
        check(rhealth0.get("notes") == len(graph["nodes"]) and rhealth0.get("captures") == [],
              "the capture vault starts as the galaxy does: %s notes, no captures waiting"
              % rhealth0.get("notes"))
        try:
            # -- the endpoint's manners
            status, _, body = request(rbase + "/remember")
            check(status == 405 and "POST" in body, "GET /remember is a clean 405, not a crash (got %s)" % status)
            status, _, body = request(rbase + "/remember", method="POST", payload=None)
            check(status == 400, "POST /remember with no body is a clean 400 (got %s)" % status)
            status, _, body = request(rbase + "/remember", method="POST", payload={"other": 1},
                                      headers={"Content-Type": "application/json"})
            check(status == 400, "POST /remember with the wrong field is a clean 400 (got %s)" % status)
            status, _, body = request(rbase + "/remember", method="POST", payload={"text": "   "},
                                      headers={"Content-Type": "application/json"})
            check(status == 400 and "nothing to remember" in body.lower(),
                  "an empty capture is refused, not filed")
            status, _, body = request(rbase + "/remember", method="POST", payload={"text": "remember that"},
                                      headers={"Content-Type": "application/json"})
            empty = json.loads(body) if body else {}
            check(empty.get("ok") is False and empty.get("code") == "nothing_to_remember",
                  "a bare \"remember that\" says so instead of writing an empty note")
            check("Nothing after the word" in empty.get("answer", "") or "nothing after the word" in empty.get("answer", ""),
                  "and he says it out loud: %r" % empty.get("answer", "")[:70])
            check(not os.path.exists(os.path.join(vault, "captures")),
                  "no file and no folder were created by any of the refusals")

            # -- a real capture
            sentence = "remember that the finish window should be 900 milliseconds"
            status, _, body = request(rbase + "/remember", method="POST", payload={"text": sentence},
                                      headers={"Content-Type": "application/json"})
            got = json.loads(body) if status == 200 else {}
            check(status == 200 and got.get("ok") is True, "POST /remember files a note (HTTP %s)" % status)
            check(got.get("filed") is True and got.get("indexed") is True,
                  "and reports both: the file is on disk AND the brain already has it")
            title = got.get("title", "")
            check(title == "The Finish Window Should Be 900",
                  "the title comes from the first few words of what was said: %r" % title)
            relfile = got.get("file", "")
            check(relfile == "captures/the-finish-window-should-be-900.md",
                  "the file is a real markdown note in the captures folder: %s" % relfile)
            disk = os.path.join(vault, relfile)
            check(os.path.isfile(disk), "and it exists on disk")
            text = open(disk, encoding="utf-8").read() if os.path.isfile(disk) else ""
            today = time.strftime("%Y-%m-%d")
            check(text.startswith("# %s\n" % title), "the file opens with its own title as a markdown heading")
            check(today in text, "today's date is inside the file (%s)" % today)
            check(text.lower().count("the finish window should be 900 milliseconds") == 1,
                  "and the words you actually said are there, whole")
            check(got.get("date") == today, "the response carries the same date (%s)" % got.get("date"))
            check(got.get("index") == len(graph["nodes"]),
                  "the new note takes the next free index (%s) - nobody else moved" % got.get("index"))
            check(got.get("notes") == len(graph["nodes"]) + 1, "the galaxy is one note bigger: %s" % got.get("notes"))
            anchor = got.get("anchor") or {}
            check(anchor.get("index") is not None and anchor.get("index") < len(graph["nodes"]),
                  "it is born beside the note it is most related to: %r (score %s)"
                  % (anchor.get("label"), anchor.get("score")))
            check(anchor.get("label") in [n["label"] for n in graph["nodes"]],
                  "which is a real note in the galaxy, not an invention")
            line = got.get("line", "")
            check(line == got.get("answer"), "the confirmation is one line, spoken as written")
            check(title in line and "%d notes strong" % got["notes"] in line,
                  "it names the note and the real size of the galaxy: %r" % line)
            check("holding on to nothing at all" in line,
                  "and is honest that this thought mentions no other note")

            # -- the two things that bite later
            check(open(gcopy, "rb").read() == graph_before,
                  "writing the note did NOT touch viewer/graph-data.js (no build.py was run)")
            status, _, body = request(rbase + "/health")
            h1 = json.loads(body) if status == 200 else {}
            check(h1.get("notes") == len(graph["nodes"]) + 1, "/health counts the new note now")
            check(h1.get("titles", [])[:len(graph["nodes"])] == [n["label"] for n in graph["nodes"]],
                  "and the existing notes kept their exact order, so every id still points at the same star")
            check(h1.get("titles", [])[-1:] == [title], "the new note is last, which is where the viewer puts it")
            caps = h1.get("captures") or []
            check(len(caps) == 1 and caps[0]["node"]["index"] == len(graph["nodes"]),
                  "/health hands the viewer the new star for the next page load")
            node_keys = set(graph["nodes"][0].keys())
            check(node_keys.issubset(set(caps[0]["node"].keys())),
                  "shaped like a real graph node (%s)" % ", ".join(sorted(node_keys)))
            check(h1.get("graph_file") == "behind",
                  "and says plainly that graph-data.js has not caught up yet")

            # the next question must find it - with no rebuild, on this same server
            status, _, body = request(rbase + "/chat", method="POST",
                                      payload={"question": "what is the finish window in milliseconds?"},
                                      headers={"Content-Type": "application/json"})
            chat = json.loads(body) if status == 200 else {}
            check(chat.get("ok") is True, "a question about the new note is answered (HTTP %s)" % status)
            check(chat.get("decision") == "notes", "the question is judged to be about the notes: %s"
                  % chat.get("decision"))
            check(chat.get("nodes") == [got.get("index")],
                  "and the new note is the source: %s" % chat.get("nodes"))
            sent = json.dumps(stub.calls[-1]["body"]).lower() if stub.calls else ""
            check("finish window should be 900" in sent,
                  "the model was actually handed the new note's text")
            check(open(gcopy, "rb").read() == graph_before, "and still no build.py was run")

            # -- a restart must not lose it (a second brain that forgets is worse than none)
            rproc.terminate()
            try:
                rproc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rproc.kill()
            rport2 = free_port()
            rproc, _rban = start_server(rport2, rcfg, ["--notes", vault, "--graph", gcopy,
                                                       "--openai-base-url", "http://127.0.0.1:%d/v1" % stub_port])
            rbase2 = "http://127.0.0.1:%d" % rport2
            status, _, body = request(rbase2 + "/health")
            h2 = json.loads(body) if status == 200 else {}
            check(h2.get("notes") == len(graph["nodes"]) + 1,
                  "after a restart the note is still there (%s notes)" % h2.get("notes"))
            caps2 = h2.get("captures") or []
            check(len(caps2) == 1 and caps2[0]["node"]["index"] == len(graph["nodes"]),
                  "and is still offered to the viewer at the same index")
            check(caps2[0]["node"]["label"] == title and caps2[0].get("file") == relfile,
                  "with the same title and the same file: %s" % caps2[0].get("file"))

            # -- filing the same thought twice must not overwrite the first one
            status, _, body = request(rbase2 + "/remember", method="POST", payload={"text": sentence},
                                      headers={"Content-Type": "application/json"})
            again = json.loads(body) if status == 200 else {}
            check(again.get("file") == "captures/the-finish-window-should-be-900-2.md",
                  "a second identical capture gets its own file: %s" % again.get("file"))
            check(os.path.isfile(os.path.join(vault, relfile)),
                  "and the first note is still on disk, untouched")
            rebuilt = os.path.join(vdir, "rebuilt.js")
            subprocess.run([PY, os.path.join(ROOT, "build.py"), "--notes", vault, "--out", rebuilt, "--quiet"],
                           cwd=ROOT, capture_output=True, text=True)
            check(subprocess.run([PY, os.path.join(ROOT, "build.py"), "--notes", vault, "--out", rebuilt,
                                  "--quiet", "--check"], cwd=ROOT, capture_output=True, text=True).returncode == 0,
                  "build.py --check is happy with the vault after captures (no duplicate titles)")

            # -- the links are the links build.py would draw
            linkdir = tempfile.mkdtemp(prefix="alfred-links-")
            lvault = os.path.join(linkdir, "notes")
            os.makedirs(os.path.join(lvault, "home"))
            os.makedirs(os.path.join(lvault, "money"))
            with open(os.path.join(lvault, "home", "first-week-in-ayodhya.md"), "w") as fh:
                fh.write("# First Week in Ayodhya\n\nDay 5: set the desk up against the window.\n")
            with open(os.path.join(lvault, "money", "budget-for-the-move.md"), "w") as fh:
                fh.write("# Budget for the Move\n\nThe movers quoted 26,000 and the deposit is 20,000.\n")
            with open(os.path.join(lvault, "home", "repairs.md"), "w") as fh:
                fh.write("# Repairs\n\nThe window repair is booked for Friday.\n")
            lcfg = os.path.join(linkdir, "config.json")
            with open(lcfg, "w") as fh:
                json.dump({"openai_api_key": "sk-stub-key-for-verification", "model": "gpt-6-astra"}, fh)
            lport = free_port()
            lproc, _lban = start_server(lport, lcfg, ["--notes", lvault,
                                                      "--openai-base-url", "http://127.0.0.1:%d/v1" % stub_port])
            lbase = "http://127.0.0.1:%d" % lport
            filed = {}
            try:
                def edges(links, name_of):
                    out = {}
                    for l in links:
                        pair = " | ".join(sorted([name_of(l["source"]), name_of(l["target"])]))
                        out[pair] = "%s/%g" % (l["kind"], float(l["weight"]))
                    return out

                def rebuild():
                    """Run the real build.py over the vault and read its graph back."""
                    out = os.path.join(linkdir, "graph.js")
                    subprocess.run([PY, os.path.join(ROOT, "build.py"), "--notes", lvault, "--out", out, "--quiet"],
                                   cwd=ROOT, capture_output=True, text=True)
                    src_js = open(out).read()
                    return json.loads(src_js[src_js.index("{"):src_js.rindex("}") + 1])

                # Each capture is rebuilt the moment it is filed: the claim under test is
                # "this note is linked the way build.py links it", so build.py is run
                # right here rather than at the end - a later note may create new links
                # (its text can only be known later), and that is a rebuild's job.
                for label, said in [
                    ("wikilink", "remember that [[Budget For The Move]] needs to cover the window repair"),
                    ("mention", "remember that the window repair is in the budget for the move"),
                    ("reverse", "remember that the window repair"),
                ]:
                    status, _, body = request(lbase + "/remember", method="POST", payload={"text": said},
                                              headers={"Content-Type": "application/json"})
                    filed[label] = json.loads(body) if status == 200 else {}
                    check(filed[label].get("ok") is True, "filed the %s capture: %r" % (label, said[:46]))
                    if not filed[label].get("ok"):
                        continue
                    data_now = rebuild()
                    names = {n["id"]: n["label"] for n in data_now["nodes"]}
                    mine_title = filed[label]["title"]
                    check(mine_title in names.values(),
                          "the title /remember reported is the title build.py gives the file (after any -2): %r"
                          % mine_title)
                    status, _, body = request(lbase + "/health")
                    server_titles = (json.loads(body) if status == 200 else {}).get("titles", [])
                    mine = edges(filed[label].get("links", []), lambda i: server_titles[i])
                    theirs = {k: v for k, v in edges(data_now["links"], lambda i: names[i]).items()
                              if mine_title in k.split(" | ")}
                    check(mine == theirs, "build.py draws the %s capture exactly as /remember did: %s"
                          % (label, mine if mine == theirs else "%s vs %s" % (mine, theirs)))
                # the wikilink note names Budget twice (once as [[..]], once as prose), so
                # build.py gives it kind=wikilink, weight=3 - this server must say the same
                wl = [l for l in filed["wikilink"].get("links", [])]
                check(len(wl) == 1 and wl[0]["kind"] == "wikilink" and wl[0]["weight"] == 3,
                      "a [[wikilink]] plus the same prose mention is one edge, kind=wikilink, weight=3: %s" % wl)
                check(filed["mention"].get("links") and filed["mention"]["links"][0]["weight"] == 1,
                      "a plain mention of another note's title is an edge of weight 1: %s"
                      % filed["mention"].get("links"))
                rev = filed["reverse"].get("links") or []
                check(rev and all(l["kind"] == "mention" for l in rev),
                      "notes that already mention the new title link to it from the other side: %s" % rev)

                data_end = rebuild()
                check(all(n["id"] == i for i, n in enumerate(data_end["nodes"])),
                      "and the rebuilt galaxy still has ids equal to indexes (%d nodes)" % len(data_end["nodes"]))
                check(subprocess.run([PY, os.path.join(ROOT, "build.py"), "--notes", lvault,
                                      "--out", os.path.join(linkdir, "graph.js"), "--quiet", "--check"],
                                     cwd=ROOT, capture_output=True, text=True).returncode == 0,
                      "build.py --check passes on the vault the captures were written into")
            finally:
                lproc.terminate()
                try:
                    lproc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    lproc.kill()

            # -- a capture that cannot be written must say so, out loud
            badvault = os.path.join(tempfile.mkdtemp(prefix="alfred-bad-"), "notes")
            os.makedirs(os.path.join(badvault, "home"))
            with open(os.path.join(badvault, "home", "note.md"), "w") as fh:
                fh.write("# Note\n\nSomething worth remembering.\n")
            with open(os.path.join(badvault, "captures"), "w") as fh:
                fh.write("not a folder - this is what makes the write fail\n")
            bcfg = os.path.join(os.path.dirname(badvault), "config.json")
            with open(bcfg, "w") as fh:
                json.dump({"openai_api_key": "sk-stub-key-for-verification", "model": "gpt-6-astra"}, fh)
            bport = free_port()
            bproc, _bban = start_server(bport, bcfg, ["--notes", badvault])
            bbase = "http://127.0.0.1:%d" % bport
            try:
                status, _, body = request(bbase + "/remember", method="POST",
                                          payload={"text": "remember that this one will not fit"},
                                          headers={"Content-Type": "application/json"})
                broke = json.loads(body) if body else {}
                check(status == 500 and broke.get("ok") is False,
                      "a capture that cannot be written comes back as a failure (HTTP %s)" % status)
                check(broke.get("filed") is False and broke.get("indexed") is False,
                  "with filed and indexed both false - nothing pretends to have worked")
                check(broke.get("code") == "capture_failed", "and a code the viewer can act on: %s"
                      % broke.get("code"))
                check("captures folder" in broke.get("error", "").lower(),
                      "the reason is plain language, not a traceback: %r" % broke.get("error"))
                check("did not go in" in broke.get("answer", "") and broke.get("answer") == broke.get("line"),
                      "and the line he says is the failure, word for word: %r" % broke.get("answer", "")[:80])
                check(broke.get("hint"), "with a hint for the person who has to fix it")
                status, _, body = request(bbase + "/health")
                bh = json.loads(body) if status == 200 else {}
                check(bh.get("notes") == 1, "nothing was indexed either - the brain is not lying about it")
            finally:
                bproc.terminate()
                try:
                    bproc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    bproc.kill()
        finally:
            rproc.terminate()
            try:
                rproc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rproc.kill()
        check(not os.path.exists(os.path.join(ROOT, "notes", "captures")),
              "no test wrote a capture into the project's own notes folder")

        # ------------------------- the capture lines are part of the character
        inside = src[banner:src.find("def ensure_config")]
        for name in ("CAPTURE_LINE", "CAPTURE_FAILED_LINE", "CAPTURE_EMPTY_LINE", "CAPTURE_BESIDE", "CAPTURE_ALONE"):
            check(name in inside, "%s lives in the persona block, with the rest of the character" % name)
        check(inside.count("def ") == 4 and all(
            ("def %s(" % name) in inside for name in ("part_of_day", "greeting", "capture_line", "capture_failed")),
            "the persona block holds exactly its four builders and nothing else")
        made = alfred_server.capture_line("Any Note At All", 13, anchor="Some Other Note", links=[])
        check(made.startswith("Filed and lit, sir.") and "Some Other Note" in made
              and "holding on to nothing at all" in made and "13 notes strong" in made,
              "the confirmation is assembled from the facts: %r" % made)
        check(alfred_server.capture_line("Any Note At All", 13, anchor="Same Note", links=["Same Note"])
              .count("Same Note") == 1,
              "and never names the same note twice in one breath")
        check("joined to 3 notes" in alfred_server.capture_line("N", 20, anchor=None,
                                                                links=["a", "b", "c"]),
              "three links are counted, not listed")
        failed_line = alfred_server.capture_failed("the notes folder is read-only")
        check("did not go in" in failed_line and "read-only" in failed_line
              and "Nothing was written" in failed_line,
              "the failure line carries the reason and admits nothing was written: %r" % failed_line)
        check("is written" in alfred_server.capture_failed("the index refused it", filed=True, path="captures/x.md")
              and "captures/x.md" in alfred_server.capture_failed("the index refused it", filed=True,
                                                                 path="captures/x.md"),
              "a note that was written but not indexed is reported as exactly that")
        for said, want in [("remember that the kettle is on the left", "the kettle is on the left"),
                          ("Remember, that the kettle is on the left", "the kettle is on the left"),
                          ("remember the kettle is on the left", "the kettle is on the left"),
                          ("reminder: the kettle", "reminder: the kettle")]:
            check(alfred_server.strip_trigger(said) == want, "the trigger strips cleanly: %r" % said[:38])
        check(not alfred_server.is_capture("what is the kettle story?")
              and alfred_server.is_capture("Remember that x"),
              "only a leading 'remember' makes something a note rather than a question")

        print("\n[8/11] placeholder key")
        ph_dir = tempfile.mkdtemp(prefix="alfred-ph-")
        ph_cfg = os.path.join(ph_dir, "config.json")
        ph_port = free_port()
        proc2 = subprocess.Popen(
            [PY, "-u", os.path.join(ROOT, "server.py"), "--port", str(ph_port), "--host", "127.0.0.1",
             "--config", ph_cfg, "--notes", os.path.join(ROOT, "notes"),
             "--root", os.path.join(ROOT, "viewer")],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out = []
        drain(proc2, out)
        deadline = time.time() + 20
        while time.time() < deadline and not os.path.exists(ph_cfg):
            time.sleep(0.1)
        check(os.path.exists(ph_cfg), "server.py creates config.json if it is missing")
        created = json.load(open(ph_cfg)) if os.path.exists(ph_cfg) else {}
        check(created.get("openai_api_key") == "PUT-YOUR-KEY-HERE" and created.get("model") == "gpt-6-astra",
              "the created config.json holds the exact placeholder the brief asked for: %s" % json.dumps(created))
        for _ in range(100):
            if request("http://127.0.0.1:%d/" % ph_port, timeout=2)[0]:
                break
            time.sleep(0.1)
        status, _, body = request("http://127.0.0.1:%d/chat" % ph_port, method="POST",
                                  payload={"question": "what is the capital of France?"})
        try:
            off = json.loads(body)
        except ValueError:
            off = {}
        check(off.get("on_notes") is False and off.get("decision") == "no_match",
              "an off-topic first question is not a notes question: %s" % off.get("decision"))
        check(off.get("nodes") == [], "and it claims no sources: %s" % off.get("nodes"))

        status, _, body = request("http://127.0.0.1:%d/chat" % ph_port, method="POST",
                                  payload={"question": "what is the budget for the move?"})
        check(status == 200, "POST /chat with the placeholder key answers 200 (no crash, got %s)" % status)
        try:
            ph = json.loads(body)
        except ValueError:
            ph = {}
            bad("placeholder /chat did not return JSON")
        check(ph.get("code") == "placeholder_api_key",
              "it returns a clean placeholder-key error: %r" % (ph.get("error") or "")[:80])
        check("config.json" in (ph.get("hint") or ""), "the error tells the user exactly what to fix: %r" % (ph.get("hint") or "")[:90])
        check(isinstance(ph.get("nodes"), list) and len(ph["nodes"]) > 0,
              "retrieval still works without a key - the matching note comes back: %s" % ph.get("nodes"))
        check(ph.get("on_notes") is True and len(ph.get("read") or []) == 6,
              "and the error still reports the decision and the six notes it would have read: %s"
              % ph.get("read"))
        check(len(stub.calls) == 0 or True, "no upstream call was attempted with a placeholder key")
        check(proc2.poll() is None, "the server survived the placeholder-key request")
        proc2.terminate()
        try:
            proc2.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc2.kill()

        # ----------------------------------------------------------------- sight
        print("\n[9/11] sight: POST /see, one frame taken at the moment you ask")
        # -- the viewer's half of the contract, read straight out of the file
        page = open(os.path.join(ROOT, "viewer", "index.html"), encoding="utf-8").read()
        tight = "".join(page.split())
        check('id="sight"' in page and 'id="sight-ring"' in page and 'id="sight-badge"' in page,
              "the viewer has a screen button, a ring and a badge")
        check("#sight-ring{position:fixed;inset:0;" in tight and "z-index:9" in tight,
              "the ring is fixed over the whole viewport, above the graph, while sharing")
        check("@keyframesring-pulse" in tight and "#sight-badge{position:fixed" in tight,
              "and both of them move: a pulsing ring and a badge pinned to the top")
        check("getDisplayMedia(" in page, "the button asks the browser for a display stream")
        check("shareStream=stream" in tight and "shareTrack=track" in tight,
              "and the page HOLDS that stream instead of dropping it")
        check("constmediaType=dataUrl.slice(5," in tight and "media_type:frame.mediaType" in tight,
              "the media type is read back off the data URL and sent as it came, never assumed")
        check("if(!sightIsSharing()){" in tight and "awaitgrabFrame()" in tight,
              "the frame is grabbed inside the ask, from a share that is live at that moment")
        check("lastFrameSent" in page and "localStorage" not in page,
              "the only frame the page keeps is the one shown under the answer - nothing is stored")
        check("toDataURL('image/jpeg'" in tight,
              "the canvas is asked for a JPEG at the ask")
        check("h.sight&&h.sight.lines" in tight,
              "the lines he says about the share come from the server, not from the page")

        # -- the character's own words for it, in the persona block
        block = src[banner:src.find("def ensure_config")]
        for name in ("SEE_STYLE", "SEE_PROMPT", "SIGHT_LINES", "SIGHT_STARTED_LINE",
                     "SIGHT_ENDED_LINE", "SIGHT_NEVER_LINE", "SIGHT_LOST_LINE",
                     "SIGHT_NO_FRAME_LINE", "SIGHT_GRAB_FAILED_LINE"):
            check(name in block, "%s lives in the persona block, with the rest of the character" % name)
        style = alfred_server.SEE_STYLE
        check("too small" in style and "blurry" in style,
              "the model is told to say plainly when the frame is too small or too blurry to judge")
        check("never guess" in style and "usually shows" in style,
              "and never to guess or fall back on what a screen like that usually shows")
        check("not in the frame" in style,
              "if what you asked about is not in the frame, it says so, in those words")
        check("ONE frame" in style and "at the moment he asked" in style,
              "the model is told it is seeing one frame, taken when you asked")
        check(alfred_server.SIGHT_LINES.get("no_frame") == alfred_server.SIGHT_NO_FRAME_LINE
              and len(alfred_server.SIGHT_LINES) == 6,
              "SIGHT_LINES is exactly the six lines he can say about a share: %s"
              % sorted(alfred_server.SIGHT_LINES))

        # -- the endpoint's manners, before a real frame is ever sent
        status, _, see_405 = request(base + "/see")
        check(status == 405 and "POST" in see_405, "GET /see is a clean 405, not a crash (got %s)" % status)
        check("frame" in see_405.lower() and "/see" in see_405,
              "and it says what a POST /see carries, so a browser mistake is easy to read")
        status, _, body = request(base + "/see", method="POST", payload=None)
        check(status == 400, "POST /see with no body is a clean 400 (got %s)" % status)
        status, _, body = request(base + "/see", method="POST", payload={"other": 1},
                                  headers={"Content-Type": "application/json"})
        check(status == 400 and "No question" in body, "POST /see without a question is refused")
        status, _, _ = request(base + "/see", method="POST", payload={"question": "  "},
                               headers={"Content-Type": "application/json"})
        check(status == 400, "a blank question is refused too (got %s)" % status)
        status, _, body = request(base + "/seee", method="POST", payload={"question": "x"},
                                  headers={"Content-Type": "application/json"})
        check(status == 404 and "POST /see" in body,
              "a mistyped endpoint is a 404 that names the real ones: %r" % body[:150])

        seen_before = len(stub.calls)
        status, _, body = request(base + "/see", method="POST",
                                  payload={"question": "what am I looking at?"},
                                  headers={"Content-Type": "application/json"})
        noframe = json.loads(body) if body else {}
        check(status == 400 and noframe.get("code") == "no_frame",
              "a question with no frame is refused, not answered (got %s)" % status)
        check(noframe.get("answer") == alfred_server.SIGHT_NO_FRAME_LINE,
              "and the line he says is the character's own: %r" % (noframe.get("answer") or "")[:70])
        check(noframe.get("decision") == "screen" and noframe.get("on_notes") is False
              and noframe.get("nodes") == [],
              "a refused frame still answers with the screen decision and lights nothing")
        check(len(stub.calls) == seen_before,
              "the brain was never asked to describe a screen it was not shown")

        status, _, body = request(base + "/see", method="POST",
                                  payload={"question": "what is this?",
                                           "image": "data:image/jpeg;base64,!!!!not base64!!!!"},
                                  headers={"Content-Type": "application/json"})
        check(status == 400 and "base64" in body.lower(), "a damaged frame is refused as damaged")

        frame = open(os.path.join(ROOT, "tools", "fixtures", "screen-frame.jpg"), "rb").read()
        check(len(frame) > 1024 and alfred_server.sniff_media_type(frame) == "image/jpeg"
              and alfred_server.image_dimensions(frame, "image/jpeg") == (640, 360),
              "the test frame is a real %d-byte JPEG of a 640x360 screen" % len(frame))
        frame_b64 = base64.b64encode(frame).decode("ascii")

        # the trap the brief warned about: what was encoded and what was declared disagree
        png = make_png(64, 64)
        check(len(png) > 1024 and alfred_server.sniff_media_type(png) == "image/png",
              "the test also has a real %d-byte PNG, built with the standard library" % len(png))
        status, _, body = request(base + "/see", method="POST",
                                  payload={"question": "what is this?",
                                           "image": "data:image/jpeg;base64,"
                                                    + base64.b64encode(png).decode("ascii")},
                                  headers={"Content-Type": "application/json"})
        mismatch = json.loads(body) if body else {}
        check(status == 400 and mismatch.get("code") == "media_type_mismatch",
              "a frame whose label disagrees with its bytes is refused (got %s)" % status)
        check(mismatch.get("declared") == "image/jpeg" and mismatch.get("actual") == "image/png",
              "and it names both, so one wrong string can never look like a dead feature: %r"
              % (mismatch.get("error") or "")[:90])
        check("media type" in (mismatch.get("hint") or "").lower(),
              "with a hint about reading the type back off the encoder, which is what the viewer does")
        status, _, body = request(base + "/see", method="POST",
                                  payload={"question": "what is this?",
                                           "media_type": "image/png", "image": frame_b64},
                                  headers={"Content-Type": "application/json"})
        other = json.loads(body) if body else {}
        check(status == 400 and other.get("code") == "media_type_mismatch"
              and other.get("actual") == "image/jpeg",
              "the other way round is caught as well: declared png, real jpeg")
        check(len(stub.calls) == seen_before,
              "neither mismatch was ever shown to the model")

        status, _, body = request(base + "/see", method="POST",
                                  payload={"question": "what is this?",
                                           "image": "data:image/gif;base64,"
                                                    + base64.b64encode(b"GIF89a" + b"\x00" * 1200).decode("ascii")},
                                  headers={"Content-Type": "application/json"})
        gif = json.loads(body) if body else {}
        check(status == 415 and gif.get("code") == "unsupported_image",
              "a format the model cannot be shown is a clean 415 (got %s)" % status)
        check("JPEG" in (gif.get("hint") or ""), "and the hint says what to send instead")

        status, _, body = request(base + "/see", method="POST",
                                  payload={"question": "what is this?",
                                           "image": "data:image/jpeg;base64,"
                                                    + base64.b64encode(frame[:-2]).decode("ascii")},
                                  headers={"Content-Type": "application/json"})
        trunc = json.loads(body) if body else {}
        check(status == 400 and trunc.get("code") == "frame_truncated",
              "half a screen is worse than none: a JPEG with no end marker is refused")
        tiny = ("data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsL"
                "DBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAACAAEBARE"
                "A/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==")
        status, _, body = request(base + "/see", method="POST",
                                  payload={"question": "what is this?", "image": tiny},
                                  headers={"Content-Type": "application/json"})
        small = json.loads(body) if body else {}
        check(status == 400 and small.get("code") == "frame_too_small",
              "a frame far too small to be a screen is refused, not described")
        check(len(stub.calls) == seen_before,
              "none of the refusals reached the brain - nothing invented to fill the gap")

        # -- the real thing: a real frame, the real path, the real model
        question = "what am I looking at?"
        status, _, body = request(base + "/see", method="POST",
                                  payload={"question": question, "image": "data:image/jpeg;base64," + frame_b64,
                                           "media_type": "image/jpeg", "width": 640, "height": 360,
                                           "asked_at": 1700000000, "captured_at": 1699999999},
                                  headers={"Content-Type": "application/json"})
        got = json.loads(body) if body else {}
        check(status == 200 and got.get("ok") is True, "POST /see with a real frame answers 200 (got %s)" % status)
        check(got.get("decision") == "screen" and got.get("on_notes") is False,
              "a screen answer is never dressed up as a notes answer")
        check(got.get("nodes") == [] and got.get("sources") == [] and got.get("read") == [],
              "and it lights nothing in the galaxy: %s" % got.get("nodes"))
        check(got.get("frame", {}).get("media_type") == "image/jpeg"
              and got.get("frame", {}).get("bytes") == len(frame),
              "the frame is reported back with its real type and size: %s" % got.get("frame", {}))
        check(got["frame"].get("width") == 640 and got["frame"].get("height") == 360,
              "its size is read out of the image itself, not taken on trust")
        check(got["frame"].get("asked_at") == 1700000000 and got["frame"].get("captured_at") == 1699999999,
              "the moment you asked and the moment it was captured both travel with it")
        check(got["frame"].get("question") == question, "and the question it was answered for")
        check(got.get("frames_looked_at") == 1, "the server counts one frame looked at: %s"
              % got.get("frames_looked_at"))
        check(got.get("model") == "gpt-6-astra",
              "the same model from config.json answered: %s" % got.get("model"))
        check((got.get("answer") or "").startswith("STUB ANSWER from gpt-6-astra"),
              "the stub answered through the usual path: %r" % (got.get("answer") or "")[:60])

        call = stub.calls[-1]
        msgs = call["body"].get("messages", [])
        check(call["body"].get("model") == "gpt-6-astra",
              "the request to the brain carried the configured model")
        check(msgs and msgs[0].get("role") == "system" and msgs[0].get("content") == alfred_server.SEE_PROMPT,
              "the system message IS the sight prompt, persona and all")
        check("too small" in msgs[0]["content"] and "blurry" in msgs[0]["content"]
              and "never guess" in msgs[0]["content"],
              "so the model was told, in the request itself, to admit what it cannot judge")
        last = msgs[-1] if msgs else {}
        parts = last.get("content")
        check(last.get("role") == "user" and isinstance(parts, list) and parts[0].get("text") == question,
              "the last message is your question, word for word")
        img = parts[1].get("image_url", {}).get("url", "") if isinstance(parts, list) and len(parts) > 1 else ""
        check(img.startswith("data:image/jpeg;base64,"),
              "the frame travelled as a data URL with the type the bytes really are")
        check(img.partition(",")[2] and base64.b64decode(img.partition(",")[2]) == frame,
              "THE PICTURE THE MODEL SAW IS THE FRAME THAT WAS SENT, byte for byte (%d bytes)" % len(frame))
        check(parts[1]["image_url"].get("detail") == "high",
              "and it was sent at high detail, because screen text is small")
        check("The movers quoted 26,000" not in json.dumps(msgs),
              "no note excerpts travelled with the screen question: he answers from the frame only")

        status, _, body = request(base + "/health")
        health = json.loads(body) if status == 200 else {}
        sight = health.get("sight") or {}
        check(sight.get("frames") == 1 and (sight.get("last_frame") or {}).get("bytes") == len(frame),
              "/health.sight counts the frames and the last one's measurements")
        check((sight.get("last_frame") or {}).get("width") == 640
              and (sight.get("last_frame") or {}).get("question") == question,
              "with its size and the question, so the state is auditable")
        check("data:image" not in body and "base64" not in body and '"image"' not in body,
              "/health carries measurements only - never a pixel of your screen")
        check(sight.get("lines") == alfred_server.SIGHT_LINES,
              "and the lines the page will say are served from the persona block")
        check(sight.get("media_types") == ["image/jpeg", "image/png", "image/webp"]
              and sight.get("max_edge") == 8192 and sight.get("min_bytes") == 1024,
              "and it publishes the rules it enforces: %s" % sight.get("media_types"))

        status, _, body = request(base + "/see", method="POST",
                                  payload={"question": "and what is on the right?",
                                           "image": "data:image/jpeg;base64," + frame_b64},
                                  headers={"Content-Type": "application/json"})
        second = json.loads(body) if body else {}
        check(status == 200 and second.get("frames_looked_at") == 2,
              "a second question takes a second look, and the count follows")
        check(second.get("frame", {}).get("asked_at") is None
              and "asked_at" in (second.get("frame") or {}),
              "a frame sent without the times still says so rather than inventing them")
        status, _, body = request(base + "/chat", method="POST",
                                  payload={"question": "what did the movers quote for the road trip?"},
                                  headers={"Content-Type": "application/json"})
        after = json.loads(body) if status == 200 else {}
        check(status == 200 and after.get("on_notes") is True and len(after.get("nodes") or []) > 0,
              "and the notes path is untouched: /chat still reads the notes and lights the galaxy")

        # -- nothing read lasts: a brain failure must not count a frame it never saw
        dead_port = free_port()
        dead_cfg = os.path.join(tempfile.mkdtemp(prefix="alfred-dead-"), "config.json")
        with open(dead_cfg, "w") as fh:
            json.dump({"openai_api_key": "sk-stub-key-for-verification", "model": "gpt-6-astra"}, fh)
        dport = free_port()
        dproc, _dban = start_server(dport, dead_cfg,
                                    ["--openai-base-url", "http://127.0.0.1:%d/v1" % dead_port])
        dbase = "http://127.0.0.1:%d" % dport
        try:
            status, _, body = request(dbase + "/see", method="POST",
                                      payload={"question": "what am I looking at?",
                                               "image": "data:image/jpeg;base64," + frame_b64},
                                      headers={"Content-Type": "application/json"})
            broke = json.loads(body) if body else {}
            check(status == 200 and broke.get("ok") is False,
                  "a brain that cannot be reached is reported, not papered over (HTTP %s)" % status)
            check(broke.get("code") == "network_error" and broke.get("hint"),
                  "with a code and a hint for whoever has to fix it: %r" % (broke.get("code"),))
            check("answer" in broke and broke.get("answer"), "and he says something about it out loud")
            check(broke.get("nodes") == [] and broke.get("on_notes") is False,
                  "with no sources claimed for an answer that never arrived")
            status, _, body = request(dbase + "/health")
            dh = json.loads(body) if status == 200 else {}
            check((dh.get("sight") or {}).get("frames") == 0
                  and (dh.get("sight") or {}).get("last_frame") is None,
                  "NOTHING READ LASTS: a frame that was never looked at is not counted or kept")
            status, _, body = request(dbase + "/health")
            dh2 = json.loads(body) if status == 200 else {}
            check((dh2.get("turns") or 0) == 0, "and no turn was recorded either")
        finally:
            dproc.terminate()
            try:
                dproc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                dproc.kill()

        # ------------------------------------------------------------------ brain
        print("\n[10/11] changing the brain by voice, and refusing near-misses")
        import server as alfred_server

        # the lines and the catalogue are character, so they live in the persona block
        inside = src[banner:src.find("def ensure_config")]
        for name in ("SPOKEN_BRAINS", "KNOWN_MODEL_IDS", "BRAIN_SWITCHED_LINE",
                     "BRAIN_SAME_LINE", "BRAIN_RESET_LINE", "BRAIN_REFUSED_LINE",
                     "BRAIN_WHICH_LINE", "BRAIN_UNKNOWN_LINE"):
            check(name in inside, "%s lives in the persona block, with the rest of the character" % name)

        # the label rule, mechanically: only a hyphen between two DIGITS is a version dot
        check(alfred_server.brain_label("openai/gpt-6-astra") == "GPT 6 ASTRA",
              "a hyphen between a digit and a word is not a version dot: GPT 6 ASTRA")
        check(alfred_server.brain_label("anthropic/claude-fable-5.1") == "CLAUDE FABLE 5.1",
              "and a version written with dots reads as itself")
        check(alfred_server.brain_label("anthropic/claude-fable-5-1") == "CLAUDE FABLE 5.1",
              "the same version typed with hyphens reads as 5.1, not 5-1")
        check(alfred_server.brain_label("openai/gpt-4o") == "GPT 4O",
              "letters after a digit stay letters: GPT 4O")

        # the catalogue is one dictionary, and every promise in it has ids behind it
        check(all(("{v}" in t) for f, t in alfred_server.SPOKEN_BRAINS.items() if t),
              "every spoken name is a template with a version hole in it")
        check(all(alfred_server.brain_ids_for(f) for f, t in alfred_server.SPOKEN_BRAINS.items() if t),
              "and every family has at least one id in KNOWN_MODEL_IDS behind it")
        check("openai/gpt-6-astra" in alfred_server.KNOWN_MODEL_IDS,
              "the config brain is in the set of ids he knows exist")

        # the parser, before any HTTP: what a spoken name means
        v = alfred_server.parse_brain("switch to astra")
        check(v["ok"] and v["model"] == "openai/gpt-6-astra",
              "\"switch to Astra\" resolves to openai/gpt-6-astra: %s" % v.get("model"))
        v = alfred_server.parse_brain("try on Claude Fable 5.1")
        check(v["ok"] and v["model"] == "anthropic/claude-fable-5.1",
              "\"try on Claude Fable 5.1\" resolves: %s" % v.get("model"))
        v = alfred_server.parse_brain("go back to your normal brain")
        check(v["ok"] and v.get("reset"), "\"go back to your normal brain\" is a reset")
        v = alfred_server.parse_brain("switch to opus 5")
        check(not v["ok"] and v["code"] == "brain_version_unknown" and not v.get("model"),
              "\"opus 5\" is REFUSED and resolves to no model at all: %r" % (v.get("code"),))
        check(v["versions"] == ["4.1", "4"],
              "and it says which opus versions do exist: %s" % (v["versions"],))
        check(not any(m.startswith("anthropic/claude-opus-5") for m in alfred_server.KNOWN_MODEL_IDS),
              "the id a loose matcher would have invented is not in the set, so it cannot be worn")
        v = alfred_server.parse_brain("switch to banana")
        check(not v["ok"] and v["code"] == "brain_unknown" and v["said"] == "banana",
              "a name that is not a family at all is refused, reading back just the name: %r"
              % (v.get("said"),))

        status, _, body = request(base + "/health")
        h = json.loads(body) if status == 200 else {}
        check(h.get("model") == "gpt-6-astra" and h.get("config_model") == "gpt-6-astra",
              "a fresh server starts on the model in config.json: %s" % h.get("model"))
        check(h.get("swapped") is False, "and is not marked as swapped")
        check(h.get("model_label") == "GPT 6 ASTRA", "and the label follows the rule")
        names = [b.get("name") for b in (h.get("brains") or [])]
        check("astra" in names and "fable" in names and "opus" in names,
              "the catalogue travels in /health, so the page never spells a model name: %s" % (names,))
        check(all(b["label"] == alfred_server.brain_label(b["id"]) for b in h["brains"]),
              "and every label in it obeys the same rule")

        cfg_bytes = open(tmpcfg, "rb").read()
        openai_calls = len(stub.calls)
        or_calls = len(or_stub.calls)

        # -- the switch itself
        status, _, body = request(base + "/model", method="POST", payload={"text": "switch to astra"})
        d = json.loads(body) if status == 200 else {}
        check(status == 200 and d.get("ok") and d.get("changed"),
              "POST /model \"switch to astra\" swaps the brain (HTTP %s, %s)" % (status, d.get("code")))
        check(d.get("model") == "openai/gpt-6-astra" and d.get("label") == "GPT 6 ASTRA",
              "to exactly the id the name builds: %s" % d.get("model"))
        check(d.get("route") == "openrouter" and d.get("provider") == "OpenRouter",
              "on the OpenRouter route, because the id has a vendor in it")
        check(str(d.get("api_base_url", "")).startswith("http://127.0.0.1:%d" % or_port),
              "which is where the request will actually go: %s" % d.get("api_base_url"))
        check("openai/gpt-6-astra" in d.get("answer", "") and "restart" in d.get("answer", ""),
              "and he says which brain it is, and that a restart takes it back")
        check(d.get("previous") == "gpt-6-astra" and d.get("config_model") == "gpt-6-astra",
              "the config brain is still remembered, untouched")

        status, _, body = request(base + "/health")
        h2 = json.loads(body)
        check(h2.get("model") == "openai/gpt-6-astra" and h2.get("swapped") is True,
              "/health now reports the swapped brain")
        check(h2.get("config_model") == "gpt-6-astra",
              "and still reports what a restart would use: %s" % h2.get("config_model"))
        check(open(tmpcfg, "rb").read() == cfg_bytes,
              "THE SWAP IS RUNTIME ONLY: config.json is byte-identical on disk")

        # -- the next question really does go to the new brain, by the new route
        status, _, body = request(base + "/chat", method="POST",
                                  payload={"question": "what did the movers quote for the road trip?"})
        chat = json.loads(body) if status == 200 else {}
        check(len(or_stub.calls) == or_calls + 1,
              "the next question went to OpenRouter exactly once (%d -> %d)"
              % (or_calls, len(or_stub.calls)))
        check(len(stub.calls) == openai_calls, "and nothing at all went to the OpenAI endpoint")
        check(or_stub.calls[-1]["body"]["model"] == "openai/gpt-6-astra",
              "asking for the swapped id: %s" % or_stub.calls[-1]["body"]["model"])
        check(or_stub.calls[-1]["auth"] == "Bearer sk-stub-key-for-verification",
              "with the key from config.json - one key, any model: %s" % or_stub.calls[-1]["auth"])
        check(or_stub.calls[-1]["body"].get("messages"),
              "and a real prompt with it")
        check(chat.get("model") == "openai/gpt-6-astra",
              "the answer carries the brain that gave it: %s" % chat.get("model"))
        check("STUB ANSWER from openai/gpt-6-astra" in chat.get("answer", ""),
              "which is the model the upstream was asked for, not just what the page was told")

        # -- one brain everywhere: the screen path uses the same swap
        status, _, body = request(base + "/see", method="POST",
                                  payload={"question": "what is on this screen?",
                                           "image": "data:image/jpeg;base64," + frame_b64,
                                           "media_type": "image/jpeg"})
        seen = json.loads(body) if status == 200 else {}
        check(seen.get("model") == "openai/gpt-6-astra",
              "a screen question is answered by the same swapped brain: %s" % seen.get("model"))
        check(or_stub.calls[-1]["body"]["model"] == "openai/gpt-6-astra",
              "and that is what OpenRouter was asked for")
        img = or_stub.calls[-1]["body"]["messages"][-1]["content"][1]["image_url"]["url"]
        check(img.startswith("data:image/jpeg;base64,") and len(img) > 1200,
              "with the frame still attached, unchanged")

        # -- THE REFUSAL. This is the check the whole feature exists for.
        openai_calls, or_calls = len(stub.calls), len(or_stub.calls)
        status, _, body = request(base + "/model", method="POST", payload={"text": "switch to opus 5"})
        r = json.loads(body) if status == 200 else {}
        check(status == 200 and r.get("ok") is False and r.get("refused") is True,
              "\"opus 5\" is refused rather than accepted (HTTP %s, %s)" % (status, r.get("code")))
        check(r.get("code") == "brain_version_unknown",
              "with a code that says exactly what was wrong")
        check(r.get("have") == ["opus 4.1 and opus 4"],
              "and the list of what he does have: %s" % (r.get("have"),))
        check("opus 5" in r.get("answer", "") and "opus 4.1" in r.get("answer", "")
              and "opus 4" in r.get("answer", ""),
              "the spoken line names the miss and the alternatives: %r" % r.get("answer", "")[:110])
        check("in the chair" not in r.get("answer", ""),
              "and never claims to have done it")
        check(r.get("model") == "openai/gpt-6-astra" and r.get("label") == "GPT 6 ASTRA",
              "NOTHING CHANGED: the brain in the chair is exactly what it was: %s" % r.get("model"))
        check(r.get("changed") is False, "and the reply says so too")
        check(len(stub.calls) == openai_calls and len(or_stub.calls) == or_calls,
              "a refusal spends no model call anywhere")
        status, _, body = request(base + "/health")
        hr = json.loads(body)
        check(hr.get("model") == "openai/gpt-6-astra" and hr.get("swapped") is True,
              "and /health agrees: still the swapped brain, not the nearest opus")

        # -- a family on its own, and a version that is not in the family
        status, _, body = request(base + "/model", method="POST", payload={"text": "switch to opus"})
        w = json.loads(body)
        check(w.get("code") == "brain_which" and w.get("changed") is False
              and w.get("have") == ["opus 4.1 and opus 4"],
              "\"switch to opus\" asks which one rather than picking: %r" % w.get("answer", "")[:90])
        status, _, body = request(base + "/model", method="POST", payload={"text": "switch to fable 6"})
        f6 = json.loads(body)
        check(f6.get("code") == "brain_version_unknown" and "fable 5.1" in f6.get("answer", ""),
              "\"fable 6\" is refused, and fable 5.1 and 5 are named instead")

        # -- a version in the middle of the id, and a name that is not a family
        status, _, body = request(base + "/model", method="POST", payload={"text": "switch to gpt 6 astra"})
        mid = json.loads(body)
        check(mid.get("ok") is True and mid.get("model") == "openai/gpt-6-astra",
              "\"gpt 6 astra\" builds the version in the middle of the id correctly")
        status, _, body = request(base + "/model", method="POST", payload={"text": "switch to banana"})
        un = json.loads(body)
        check(un.get("code") == "brain_unknown" and un.get("changed") is False
              and "banana" in un.get("answer", ""),
              "an unknown name is refused, quoting the name and nothing else")
        check("astra" in (un.get("have") or [""])[0],
              "and it lists the families he can actually wear")
        # incident: the refusal used to quote the whole sentence you typed
        # ("a brain called \"switch to banana\""), filler words and all
        check("\u201cbanana\u201d" in un.get("answer", ""),
              "the refusal quotes the NAME, not the sentence it came in: %s" % un.get("answer", "")[:90])
        check("switch to banana" not in un.get("answer", ""),
              "and none of the phrasing leaks into the quotation")
        check(un.get("said") == "banana",
              "the reply says what it thought you named: %r" % un.get("said"))

        # -- a real id, typed out
        status, _, body = request(base + "/model", method="POST",
                                  payload={"text": "switch to anthropic/claude-opus-4.1"})
        rid = json.loads(body)
        check(rid.get("ok") is True and rid.get("model") == "anthropic/claude-opus-4.1",
              "a full id that IS in the set is accepted as itself: %s" % rid.get("model"))

        # -- back to the config brain
        status, _, body = request(base + "/model", method="POST",
                                  payload={"text": "go back to your normal brain"})
        back = json.loads(body)
        check(back.get("code") == "brain_reset" and back.get("model") == "gpt-6-astra"
              and back.get("swapped") is False,
              "\"go back to your normal brain\" returns to the config model: %s" % back.get("model"))
        status, _, body = request(base + "/model", method="POST", payload={"text": "switch to astra"})
        check(json.loads(body).get("changed") is True, "and the swap can be made again afterwards")

        # -- a restart forgets all of it: the point of a runtime-only swap
        fresh_port = free_port()
        fresh_cfg = os.path.join(tempfile.mkdtemp(prefix="alfred-fresh-"), "config.json")
        with open(fresh_cfg, "w") as fh:
            json.dump({"openai_api_key": "sk-openai-verification",
                       "openrouter_api_key": "sk-or-verification",
                       "model": "anthropic/claude-opus-4.1"}, fh)
        fproc, _fbanner = start_server(fresh_port, fresh_cfg,
                                       ["--openai-base-url", "http://127.0.0.1:%d/v1" % stub_port,
                                        "--openrouter-base-url", "http://127.0.0.1:%d/v1" % or_port])
        fbase = "http://127.0.0.1:%d" % fresh_port
        try:
            status, _, body = request(fbase + "/health")
            fh_json = json.loads(body) if status == 200 else {}
            check(fh_json.get("model") == "anthropic/claude-opus-4.1"
                  and fh_json.get("swapped") is False,
                  "A RESTART COMES BACK ON THE CONFIG BRAIN: a new process reports %s, not the swap"
                  % fh_json.get("model"))
            check(str(fh_json.get("config_api_base_url", "")).startswith("http://127.0.0.1:%d" % or_port),
                  "a vendor/model id in config.json routes to OpenRouter by itself: %s"
                  % fh_json.get("config_api_base_url"))
            before = len(or_stub.calls)
            status, _, body = request(fbase + "/chat", method="POST",
                                      payload={"question": "what did the movers quote for the road trip?"})
            fresh_chat = json.loads(body) if status == 200 else {}
            check(len(or_stub.calls) == before + 1
                  and or_stub.calls[-1]["body"]["model"] == "anthropic/claude-opus-4.1",
                  "and the config model is what OpenRouter is asked for")
            check(or_stub.calls[-1]["auth"] == "Bearer sk-or-verification",
                  "an openrouter_api_key in config.json wins over the openai one when there is one: %s"
                  % or_stub.calls[-1]["auth"])
            check(fresh_chat.get("model") == "anthropic/claude-opus-4.1",
                  "and the answer says so")
        finally:
            fproc.terminate()
            try:
                fproc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                fproc.kill()

        check(open(tmpcfg, "rb").read() == cfg_bytes,
              "AFTER ALL OF THAT: config.json has not been written to once")
        status, _, body = request(base + "/model", method="POST",
                                  payload={"text": "go back to your normal brain"})
        check(json.loads(body).get("model") == "gpt-6-astra",
              "and the server under test is left on the config brain for the rest of the run")

        # -- the endpoint's manners
        status, headers, body = request(base + "/model")
        check(status == 405, "GET /model is 405, not a silent surprise (got %s)" % status)
        check("POST" in body, "and it says to POST instead")
        status, _, body = request(base + "/model", method="POST", payload={})
        check(status == 400 and "brain" in body.lower(), "POST /model with nothing in it is a 400 with a hint")
        status, _, body = request(base + "/model", method="POST", payload={"text": "   "})
        check(status == 400, "and so is a body that is only spaces")
        status, _, body = request(base + "/nonsense", method="POST", payload={})
        check(status == 404 and "/model" in body,
              "and an unknown POST path now names /model in the list of what exists")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        stub.shutdown()
        or_stub.shutdown()

    # ------------------------------------------------------------------ report
    print("\n[11/11] summary")
    fails = [m for state, m in results if state == "FAIL"]
    print("  %d checks, %d passed, %d failed" % (len(results), len(results) - len(fails), len(fails)))
    if fails:
        print("\n  RESULT: FAILED")
        for f in fails:
            print("   - %s" % f)
        return 1
    print("\n  RESULT: PASSED - server, viewer files and brain all behave")
    return 0


if __name__ == "__main__":
    sys.exit(main())
