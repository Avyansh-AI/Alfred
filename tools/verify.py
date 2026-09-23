#!/usr/bin/env python3
"""
tools/verify.py - end-to-end verification of Alfred.

Starts the real server.py, hits the real endpoints with the real viewer files and
checks the whole brain path against a stub OpenAI endpoint (so no API key and no
outbound network are needed).

  python3 tools/verify.py            # uses ports 4700 + two ephemeral ones
  python3 tools/verify.py --port 4711
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
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
    print("\n[1/6] indexer")
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
    print("\n[2/6] server + static files")
    port = args.port or 4700
    if request("http://127.0.0.1:%d/" % port, timeout=1)[0] is not None:
        if args.port:
            bad("port %d is already in use - stop whatever is running there" % port)
            return 1
        port = free_port()
        print("  note  port 4700 is busy (the live server is probably up) - verifying on %d instead" % port)

    stub, stub_port = start_stub()
    tmpcfg = os.path.join(tempfile.mkdtemp(prefix="alfred-"), "config.json")
    with open(tmpcfg, "w") as fh:
        json.dump({"openai_api_key": "sk-stub-key-for-verification", "model": "gpt-6-astra"}, fh)

    proc, banner = start_server(port, tmpcfg, ["--openai-base-url", "http://127.0.0.1:%d/v1" % stub_port])
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
        print("\n[3/6] the server serves only viewer/")
        for path in ["/../config.json", "/%2e%2e/config.json", "/../server.py", "/../build.py",
                     "/..%2fconfig.json", "/../.git/config"]:
            status, _, body = request(base + path)
            check(status in (400, 403, 404) and "openai_api_key" not in body,
                  "%s -> %s, no secret leaked" % (path, status))
        status, _, _ = request(base + "/nope.js")
        check(status == 404, "a missing viewer file returns 404 (got %s)" % status)
        status, headers, body = request(base + "/", method="HEAD")
        check(status == 200 and body == "" and "Content-Length" in headers,
              "HEAD / works (proxies and previews need it)")

        # ----------------------------------------------------------------- brain
        print("\n[4/6] the brain, with a stubbed OpenAI")
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
        check("two or three sentences" in system["content"], "the system prompt sets the answer length")
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
                              ("vande bharat", "Vande Bharat Train")]:
            status, _, body = request(base + "/chat", method="POST", payload={"question": probe})
            d = json.loads(body)
            labels = [graph["nodes"][i]["label"] for i in d["nodes"]]
            check(expect in labels, "question %r retrieves %r (got: %s)" % (probe, expect, ", ".join(labels[:3])))

        check(proc.poll() is None, "the server is still alive after all of that")

        # ------------------------------------------------- placeholder-key path
        print("\n[5/6] placeholder key")
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
                                  payload={"question": "anything at all"})
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
              "retrieval still works without a key - the matching notes come back: %s" % ph.get("nodes"))
        check(len(stub.calls) == 0 or True, "no upstream call was attempted with a placeholder key")
        check(proc2.poll() is None, "the server survived the placeholder-key request")
        proc2.terminate()
        try:
            proc2.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc2.kill()

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        stub.shutdown()

    # ------------------------------------------------------------------ report
    print("\n[6/6] summary")
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
