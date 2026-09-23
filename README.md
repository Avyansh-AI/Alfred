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

## What the real browser check caught

`tools/browser-check.mjs` opens the page in Chrome, waits for WebGL frames, clicks a star, asks a
question and screenshots the result. It found three things that headless logic tests could not:

* **The auto-frame stole the camera.** On a slow machine the force layout is still cooling when
  you click a star, and `onEngineStop` then fired and yanked the camera back to the framed view.
  The auto-frame now defers whenever you have focused something.
* **The idle drift fought the fly-to.** OrbitControls' `autoRotate` and the camera tween both write
  `camera.position`; with the drift running, a click looked like it did nothing. The drift now
  stands down for the duration of a flight.
* **The focused note hid behind the side panel.** The camera now pans so the note you flew to sits
  in the middle of the *visible* area - measured at 602px against a visible centre of 605px.

## Files

| path | what it is |
| --- | --- |
| `build.py` | the indexer. Writes `viewer/graph-data.js` as `const GRAPH = {nodes, links}` |
| `server.py` | stdlib HTTP server on port 4700. Serves `viewer/` **only**, plus `GET /health` and `POST /chat` |
| `viewer/index.html` | the whole viewer: 3d-force-graph from a CDN, starfield, HUD, side panel, ask bar |
| `viewer/graph-data.js` | generated - rebuilt by `build.py`, never edit by hand |
| `config.json` | your key and model (git-ignored, created automatically if missing) |
| `notes/` | a 12-note sample vault to show the thing off |
| `tools/verify.sh` | one command that re-checks the indexer, the viewer, the brain and the browser |
| `tools/browser-check.mjs` | opens the real page in Chrome, clicks a star, asks a question, screenshots it |
| `tools/screenshots/` | screenshots produced by that check (`galaxy.jpg`, `galaxy-focused.jpg`, `ask.jpg`) |
| `tools/vendor.py` | optional: keeps a local copy of the CDN files in `viewer/vendor/` |

## If your network blocks CDNs

The viewer prefers the CDN and only falls back when it cannot be reached (jsDelivr, then
unpkg, then a local copy). If a CDN is blocked on your network, make the local copy once:

```bash
python3 tools/vendor.py      # downloads from registry.npmjs.org, not from the CDN
python3 server.py            # restart to pick it up
```

That writes `viewer/vendor/` - the graph engine plus the exact three.js revision, with a
`manifest.json` holding versions and SHA-256 hashes. It is git-ignored (third-party build
artefacts, not source) and safe to delete; the CDN stays the primary source either way. The
download comes from the npm registry because a network that blocks CDNs usually still allows
npm - which is exactly the situation this project was verified in.

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
* `tools/browser-check.mjs` - drives the actual page in a real browser: 40 checks covering the
  drawn frame (pixel statistics), click-to-fly, panel contents, camera framing, the ask bar,
  the idle drift, keyboard shortcuts and the offline fallback. Needs Chrome; skip it with
  `SKIP_BROWSER=1 ./tools/verify.sh`.

In the browser, `__alfred.selfTest()` in the console reports whether the renderer is actually
drawing (it also drives the "live" badge in the HUD).

Two things the browser console may tell you, both harmless and both expected:

* `Multiple instances of Three.js being imported` - the page imports three.js so the bundle can
  reuse that exact instance; the bundle also carries its own copy, which stays unused.
* `script not available: <url>` - one CDN was unreachable and the next source was tried.

## The CDN, and why three.js is pinned

`viewer/index.html` loads `3d-force-graph@1.80.0` from jsDelivr. That bundle is built against
three.js **r183** and reuses `window.THREE` when it is already defined, so the page loads
exactly `three@0.183.0` first - `three.module.min.js`, which imports `./three.core.min.js`
relatively, so no import map is needed. The exact URLs and the version that ships them were
checked against the live jsDelivr. If the CDN is unreachable the page falls back to unpkg and
then to `./vendor/`, and if all of them fail it names the sources it tried instead of leaving
you with a black rectangle.
