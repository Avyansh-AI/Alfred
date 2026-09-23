# Alfred

A 3D knowledge galaxy built from your markdown notes, with a brain you can talk to.

Every `.md` file becomes a glowing star. Notes that mention each other are joined by faint
light. Click a star and the camera flies to it, lights up its neighbours and opens the note.
Type a question in the bar at the bottom and the answer is drawn from your own notes - never
from the internet, never from imagination.

No npm. No build step. No framework. Python 3 standard library plus one CDN script.

---

## Quickstart

```bash
python3 build.py      # index the notes  ->  viewer/graph-data.js
python3 server.py     # serve the viewer ->  http://127.0.0.1:4700
```

Then open <http://127.0.0.1:4700>.

## Give the brain a key

`config.json` sits in the project root (never inside `viewer/`, so the browser can never reach
it). It is already there, waiting:

```json
{
  "openai_api_key": "PUT-YOUR-KEY-HERE",
  "model": "gpt-6-astra"
}
```

Replace the placeholder with your real key, set `model` to a model your key can access, and
restart `server.py`. Until you do, the page still loads and the galaxy still works - asking a
question returns a clear "the brain has no API key yet" message instead of failing.

The key is read by the server only. It is never written into the page, never sent to the
browser, and never logged.

## Ask it things

The input bar at the bottom posts to `POST /chat`. The server

1. scores every note against your question by keyword overlap (title matches weigh 3x,
   body matches 1x, and a question that quotes a whole title gets a big bonus),
2. sends the best 6 notes to the model with a system prompt that says *answer only from these
   notes, in two or three sentences, and say so plainly if they don't cover it*,
3. returns `{"answer": "...", "nodes": [indexes used]}` and lights those notes up in the galaxy,
4. keeps the last 3 exchanges in server-side history so follow-ups ("and what about the deposit?")
   work.

## Point it at your own notes

By default it indexes `./notes`. Any folder of markdown works:

```bash
python3 build.py --notes ~/vault          # writes viewer/graph-data.js
python3 server.py --notes ~/vault         # /chat reads the same folder
```

The label comes from the filename, the group is the parent folder, and each node stores a
~700-character excerpt. `[[wikilinks]]` and plain prose mentions of another note's title both
create links.

## Files

| path | what it is |
| --- | --- |
| `build.py` | the indexer. Writes `viewer/graph-data.js` as `const GRAPH = {nodes, links}` |
| `server.py` | stdlib HTTP server on port 4700. Serves `viewer/` **only**, plus `GET /health` and `POST /chat` |
| `viewer/index.html` | the whole viewer: 3d-force-graph from a CDN, starfield, HUD, side panel, ask bar |
| `viewer/graph-data.js` | generated - rebuilt by `build.py`, never edit by hand |
| `config.json` | your key and model (git-ignored, created automatically if missing) |
| `notes/` | a 12-note sample vault to show the thing off |
| `tools/verify.sh` | one command that re-checks the indexer, the viewer and the brain |

## Verify it yourself

```bash
./tools/verify.sh
```

* `build.py --check` - node ids equal array indexes, every link endpoint is a real node.
* `tools/verify.mjs` - runs the viewer's real JavaScript headlessly against a mock of the
  CDN bundle whose every accessor is validated against the real 3d-force-graph@1.80.0 API.
* `tools/verify.py` - starts the real server, checks static serving and path-traversal
  refusals, runs the full `/chat` path against a stub OpenAI endpoint, and confirms a
  placeholder key produces a clean error rather than a crash.

In the browser, `__alfred.selfTest()` in the console reports whether the renderer is actually
drawing (it also drives the "live" badge in the HUD).

## The CDN, and why three.js is pinned

`viewer/index.html` loads `3d-force-graph@1.80.0` from jsDelivr. That bundle is built against
three.js **r183** and reuses `window.THREE` when it is already defined, so the page loads
exactly `three@0.183.0` first. If that CDN is unreachable the page falls back to unpkg, and if
both fail it tells you in plain language what went wrong instead of showing a black rectangle.
