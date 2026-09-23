# Alfred

A 3D knowledge galaxy built from your markdown notes, with a brain you can talk to.

Every `.md` file becomes a glowing star. Notes that mention each other are joined by faint
light. Click a star and the camera flies to it, lights up its neighbours and opens the note.
Type a question in the bar at the bottom and it is answered by a dry, impeccably polite British
butler, from your own notes - never from the internet, never from imagination. The galaxy shows you which notes it used: it flies to
the note when there is one, lights the whole cluster when there are several, and stays still when
you were only saying good morning. It answers out loud too, and you can just hold a conversation
with it using the microphone button.

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

## The character

Alfred answers as a dry, impeccably polite British butler with a razor wit, and all of the
character lives in **one commented block at the top of `server.py`** - the banner that starts
"THE PERSONA". Nothing below that block knows what he sounds like, so the character can be
rewritten without hunting through the file:

```python
# ============================== T H E   P E R S O N A ==============================
PERSONA      = "You are Alfred, a dry, impeccably polite British butler ..."
ANSWER_STYLE = "- Open with ONE short, dry line of wit, then give him the facts. ..."
CHAT_STYLE   = "- This is not a question about his notes: it is a greeting ..."
SYSTEM_PROMPT = PERSONA + ...      # questions about the notes
CHAT_PROMPT   = PERSONA + ...      # small talk, jokes, anything else
GREETING      = "Good {part_of_day}, sir. {count} notes indexed, all present and accounted for."
PARTS_OF_DAY  = ((5, 12, "morning"), (12, 17, "afternoon"), (17, 23, "evening"), (23, 5, "evening"))
def greeting(count, hour=None): ...
# ============================== end of the persona =================================
```

The rules in the prompts: say "sir" once in a while, never in every sentence; **one** short dry
line of wit and then the facts; never recite a note back (he wrote it, it is on his screen); one
genuinely funny line beats three bland ones, and if nothing funny is available, be brief rather
than force it; when the notes do not cover something, say so plainly and with dignity - never
invent a source, never pad, never dress up a related note as the answer.

Small talk never reaches the camera: the server decides that a greeting or a joke is not a
question about the notes (see [Where an answer came from](#where-an-answer-came-from)) and the
viewer holds the galaxy still.

### The greeting

On load he greets you with the real note count and the time of day:

> Good morning, sir. 12 notes indexed, all present and accounted for.

The number comes from the notes the server actually holds (`len(state.notes)`), never from a
string written by hand - `verify.py` serves a three-note vault and checks he says three, and
checks that the count matches the nodes the galaxy is drawn from. The salutation comes from
*your* clock: the page sends its local hour with the `/health` request, and the server composes
the line. It is spoken like anything else, which means before the first click it waits quietly
and is heard the moment the page is touched - and in a `?mute=1` tab it is shown and never
spoken.

### Five things to say to him

Ask these in order and you will have seen the whole character. The third is the one it cannot
answer; the fourth is the one where the wit has to do the work.

| say this | what to expect |
| --- | --- |
| "What is the budget for the move, and where did the 74,700 come from?" | **one note**: the camera flies to *Budget for the Move*, its neighbours light, the panel opens with the figures. A dry line about the 55,000 you told everyone, then the numbers. |
| "What should I get done in the last week before moving?" | **six notes**: no flight, the whole cluster lights, and he gives you the shape of the week instead of reciting the checklist at you. |
| "Be honest with me: am I over budget?" | **one note**, flown to, and the most butler-like answer in the vault: the ledger, the sofa on OLX, and a verdict delivered politely. |
| "Good morning, Alfred. Any advice for today?" | **small talk**: nothing moves, nothing lights, no panel - one short line back, in character. |
| "Did I remember to cancel the newspaper?" | **nothing at all**: the notes do not match, so no notes are sent, nothing is claimed and the galaxy holds still. He says plainly that he has nothing on that, sir. |

## Talk to it

Two browser APIs, both of them already in the page. Nothing is installed, nothing is paid for,
and no audio leaves your machine.

* **Speaking** - every answer is read out with `speechSynthesis`. A British English voice is
  preferred (`en-GB`, then `en-UK`, then any English voice, then by known names such as Daniel,
  Serena or Kate), and a voice is only ever chosen if the browser actually offers one.
* **Listening** - the microphone button uses `webkitSpeechRecognition`. Say something, and the
  transcript goes through exactly the same `/chat` path as a typed question: same retrieval,
  same history, same lighting-up of notes.

### The pause problem, and `FINISH_MS`

Speech recognition finalises a phrase the moment you stop talking - including the small pause in
the middle of a sentence. Dispatch on that first final result and "what did the movers quote for
the road trip" arrives as two questions: *what did the movers* and *quote for the road trip*.

So the viewer does not send the first final result. It holds it in a buffer and waits:

```js
const FINISH_MS = 900;   // one constant, at the top of viewer/index.html
```

If more speech arrives before the timer fires, the new text is **appended** to what is already
buffered and the timer **restarts**. Only the pause that genuinely ends the thought sends
anything - the whole combined sentence, as one question.

Short interrupt words (`stop`, `wait`, `hold on`, `cancel`, `quiet`, `never mind`, `shut up`)
skip the buffer entirely and fire the instant they are heard, so you can cut Alfred off
mid-answer.

### `?mute=1`

Open <http://127.0.0.1:4700/?mute=1> and that tab never speaks - not the answer, not the
"ready" chime, not even the silent primer used to unlock audio. The check lives *inside* the one
function that speaks, as its first act, so nothing in the page can route around it. This is the
flag to use when you have Alfred open in a second window and only want to read.

### The status line

Under the ask bar it always says which of you the machine thinks is talking - the exact strings
are `idle · ask me something`, `listening · you're talking`, `thinking · reading your
notes`, `speaking · alfred is talking`, `muted · this tab never speaks`,
`stopped · not listening`, plus a detail line for things like the fragment currently being
held in the buffer.

Browsers refuse to play audio until the page has been touched, so the first click anywhere
unlocks speech with a genuinely silent utterance. Before that click, nothing is played - and
after it, the first answer speaks reliably.

### Try the voice by hand

1. Open <http://127.0.0.1:4700> and **click anywhere once** (this unlocks audio; nothing has
   been spoken yet, and that is deliberate).
2. Type a question such as *"what is the budget for the move?"* and press Enter. Watch the
   status line go `thinking · reading your notes` and then `speaking · alfred is
   talking`, and **hear** the answer in a British voice.
3. Click the microphone (it turns on, and the status line says `listening · you're talking`),
   then say *"what did the movers quote for the road trip"* - and **pause for about a second
   in the middle**, after *movers*. Nothing is sent while it waits: the detail line shows the
   fragment being held, `heard "what did the movers" · 900ms pause to add more`. Finish the
   sentence within that window and **one** question reaches the brain - the whole sentence, not
   two halves - and one spoken answer comes back.
4. While Alfred is speaking, say *"stop"*. It stops immediately instead of waiting out the
   buffer, and the status line reads `stopped · not listening`.
5. Open <http://127.0.0.1:4700/?mute=1> in a second tab. The status line says `muted · this
   tab never speaks` from the moment it loads. Ask anything: the answer appears on screen, the
   status line stays off `speaking`, and you hear nothing - even with the other tab quiet.
6. Ask a second question by voice without touching anything: when an answer finishes, the
   microphone reopens by itself, so you can keep talking.

Two things worth knowing: Chrome and Edge are the only browsers with
`webkitSpeechRecognition` (the page says so plainly and keeps working without it, microphone
button included - it just tells you it cannot listen), and recognition sends your voice to the
browser vendor's speech service, which is why it works without a key of your own.

## Where an answer came from

`/chat` reports which notes the answer came from, and the galaxy shows it rather than
claiming it:

* **One to three notes** - the camera flies to the best-scoring one, lights it and its direct
  neighbours, and opens its side panel. This happens while the answer is still being spoken, so
  you hear the answer *and* see the note it came from at the same time.
* **Four or more** - nothing flies. The whole cluster lights up instead, with the links between
  those notes. Flying to one arbitrary note out of six would be a lie about where the answer
  came from, so the viewer refuses to pick one.
* **No notes** - a greeting, a joke, or an off-topic question moves nothing at all. "Good
  morning" leaves the camera exactly where it was.

The answer box says the same thing in words: `from 1 note · read 6`, `from 6 notes · read 6`, or
`no notes used`. The difference matters - the model is shown the best six notes whether they
help or not, and only the ones that clear `SUPPORT_RATIO` of the best score are reported as
sources.

### The three cases, in pictures

`tools/browser-provenance-check.mjs` runs the real server against the real notes with only the
model stubbed, asks the three questions in a real browser and photographs each one:

| question | what happens | shot |
| --- | --- | --- |
| "what is the budget for the move?" | one source: the camera flies to *Budget for the Move*, lights it and its 7 neighbours, opens its panel | `tools/screenshots/provenance-fly.jpg` |
| "what should I pack and prepare before moving, and what is the budget?" | six sources: no flight, the whole cluster lights, panel closed | `tools/screenshots/provenance-cluster.jpg` |
| "good morning" | no sources: the camera does not move, nothing lights, the answer still arrives and is spoken | `tools/screenshots/provenance-still.jpg` |

### Deciding before moving (the part that separates a demo from a toy)

`server.py` decides whether the question was about the notes at all **before** the viewer is
allowed to decide anything about the camera, and it sends that decision with the answer:

```python
on_notes, decision = about_my_notes(question, picked, last_on_notes)   # "notes" | "small_talk" | "follow_up" | "no_match"
support = support_notes(picked) if on_notes else []                    # what the answer came from
```

A question only counts as small talk when it *also* fails to match the notes, so "thanks, what
is the budget?" is still a question about the notes. Nothing in `viewer/index.html` chooses a
camera move on any other basis:

```js
const plan = answerSource(data);      // decide what the answer came from - no side effects
showAnswerSource(plan);               // ...and only here may the camera or the lights move
```

`answerSource()` is a pure function of the reply: it never touches the camera, and a still plan
does nothing at all. Small talk arrives with no sources and `on_notes: false`, so it falls out
of the decision before any camera code can see it. A reply that arrives with an error is not an
answer either, so it holds the galaxy still as well.

Which notes count as sources is `SUPPORT_RATIO` (40%) and `RELEVANCE_FLOOR` (2.0) in
`server.py`; how many are still "one note" for the camera is `PROVENANCE_FLY_MAX = 3` in
`viewer/index.html`. The spoken answer is only ever the answer - `SPEAK_MAX_CHARS = 480` trims a
very long one at a sentence end, and the full text stays on screen.

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
  in the middle of the *visible* area - measured at 605px against a visible centre of 605px.

One thing the provenance work corrected rather than caught: the README used to say an answer
"lights those notes up in the galaxy". It never did - the response's node list was only ever
rendered as chips under the answer. It lights them now, and only after the decision above says
which notes the answer really came from.

`tools/browser-voice-check.mjs` does the same for the voice layer, with a spy on
`speechSynthesis` and a fake microphone. It caught two things:

* **A rejected voice object killed the answer.** Setting `utterance.voice` to the voice the
  browser had handed over threw `TypeError: Failed to convert value to 'SpeechSynthesisVoice'`
  on that platform. The throw escaped `speak()`, the status line stuck on `thinking`, and the
  answer was never spoken. Choosing a voice is now best-effort: if the assignment is refused,
  the page falls back to the `en-GB` language tag and speaks anyway. Losing the accent is fine;
  losing the answer is not.
* **The mute notice vanished on the first status write.** The `?mute=1` notice was set once at
  load and then overwritten by the next status update. The flag is now re-applied on every
  status write, so a muted tab says so the whole session.

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
| `tools/browser.mjs` | the shared "find and launch a browser" helper both browser checks use |
| `tools/harness.mjs` | the headless page harness: DOM, speech and recognition mocks, virtual clock |
| `tools/verify-voice.mjs` | the voice logic under test: buffers, interrupts, mute, voice choice, status |
| `tools/verify-provenance.mjs` | the provenance logic under test: fly-to-source, cluster, still, speech length |
| `server.py` - "THE PERSONA" | the whole character, in one commented block at the top of the file |
| `tools/browser-voice-check.mjs` | drives the voice layer in a real browser with a speech spy and a fake mic |
| `tools/browser-provenance-check.mjs` | the real server, the real notes, three questions, three photographs |
| `tools/screenshots/` | screenshots produced by those checks (`galaxy.jpg`, `galaxy-focused.jpg`, `ask.jpg`, `voice.jpg`, `voice-muted.jpg`, `greeting.jpg`, `provenance-*.jpg`) |
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
* `tools/verify-voice.mjs` - the voice layer headlessly, on a virtual clock: that `FINISH_MS`
  is defined exactly once and really is 900, that a fragment is held at 899ms and sent at 901ms,
  that a second fragment restarts the window and the two are joined into one question, that
  `stop` bypasses the buffer, that a `?mute=1` tab never touches the speech engine at all, that
  the British voice is preferred and a missing voice list does not break anything, and that a
  browser which never fires `onend` cannot wedge the UI.
* `tools/verify.py` also checks the persona itself: that the block sits above every function in
  `server.py`, that the prompts carry the rules the tests depend on, that every hour of the day
  maps to a part of day, and that the greeting's number is the real note count - including on a
  three-note vault, so a hardcoded number would fail.
* `tools/verify-voice.mjs` covers the greeting in the page: shown at load, not a word spoken
  before the first click, spoken on the click, replaced by the first answer, and silent in a
  `?mute=1` tab.
* `tools/verify-provenance.mjs` - what the galaxy does with the notes an answer came from: one
  note flies (and the flight is a real camera tween), three still fly to the best one, four or
  six light the cluster with no camera call at all, small talk and errors move nothing, the
  top source is always the first index the server sent, the decision has no side effects until
  `showAnswerSource()` is called, the note is never read aloud, and a long answer is trimmed at
  a sentence end while the full text stays on screen.
* `tools/browser-check.mjs` - drives the actual page in a real browser: 41 checks covering the
  drawn frame (pixel statistics), click-to-fly, panel contents, camera framing, the ask bar,
  the idle drift, keyboard shortcuts and the offline fallback. Needs Chrome; skip it with
  `SKIP_BROWSER=1 ./tools/verify.sh`.
* `tools/browser-provenance-check.mjs` - starts the real `server.py` against the real notes and a
  stubbed model, then asks the three questions above in a real browser and checks what the
  galaxy did: camera units moved, which nodes are lit, the panel contents, what was spoken, and
  what the model was actually sent (the greeting's prompt contains no note excerpts at all).
* `tools/browser-voice-check.mjs` - the same page with the microphone faked and every call to
  the speech engine recorded: a typed question is spoken back, the British voice is picked, the
  mic opens a real recognition session in `en-IN`, a mid-sentence pause sends one combined
  question after 900ms, `stop` fires immediately, a muted tab makes zero calls to the engine,
  and neither tab raises an uncaught error.

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
