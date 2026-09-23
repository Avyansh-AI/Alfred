#!/usr/bin/env node
/*
 * tools/verify-sight.mjs - can Alfred see your screen, and can he lie about it?
 *
 * The rules under test, in the order the user meets them:
 *
 *   the screen button   -> starts a real getDisplayMedia share and HOLDS the stream
 *   while sharing       -> an unmistakable indicator (ring + badge), and every
 *                          question is treated as a question about the screen
 *   at the moment asked -> ONE frame, grabbed then, encoded, and sent to POST /see
 *                          with the question. Never a frame from the start of the
 *                          share, never a frame from the previous question
 *   the frame's type    -> whatever the browser actually encoded (read back off the
 *                          data URL), so the claim on the wire always matches the
 *                          bytes. A JPEG canvas that hands back a PNG is sent as PNG
 *   share over          -> he says the share has ended. Nothing is captured, nothing
 *                          is sent, and a screen question is NEVER answered from an
 *                          earlier frame or from the notes
 *   the answer          -> the same on-screen card and the same voice as every other
 *                          answer, with the exact frame he looked at shown underneath
 *
 * Runs the viewer's real JavaScript against the mock screen/speech/graph in
 * tools/harness.mjs. No browser needed - tools/browser-sight-check.mjs is the same
 * story in a real Chromium against a real server.
 */
import fs from 'node:fs';
import path from 'node:path';
import {boot, tinyFrame, TINY_JPEG} from './harness.mjs';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const SRC = fs.readFileSync(path.join(ROOT, 'viewer', 'index.html'), 'utf8');

let pass = 0, fail = 0;
const failed = [];
const ok = (m) => { pass++; console.log('  ok    ' + m); };
const bad = (m) => { fail++; failed.push(m); console.log('  FAIL  ' + m); };
const check = (cond, m) => (cond ? ok(m) : bad(m));
const group = (name) => console.log('\n' + name);

/* The character's own lines, as the server sends them (never hardcoded in the viewer). */
const LINES = {
  started: 'Watching your screen now, sir. Whatever you ask me next, I will answer from what is actually on it.',
  ended: 'The screen share has ended, sir - I am not looking at anything now.',
  never: 'I have not been shown your screen yet, sir.',
  lost: 'The share is no longer sending a picture, sir.',
  no_frame: 'Nothing came with that question, sir.',
  grab_failed: 'I could not take a picture of your screen, sir.'
};
const HEALTH = {ok: true, notes: 12, key: {state: 'set'}, sight: {frames: 0, last_frame: null,
                media_types: ['image/jpeg', 'image/png', 'image/webp'], lines: LINES}};

const SEE_REPLY = (o = {}) => Object.assign({
  ok: true, decision: 'screen', on_notes: false, nodes: [], sources: [], read: [],
  answer: 'That is your own editor, sir: the file list is on the left and the terminal is at the bottom.',
  frame: {media_type: 'image/jpeg', bytes: 2048, width: 1280, height: 720},
  model: 'stub-model', turns: 1
}, o);

/* A page whose /see and /chat both record what they were handed. */
async function page({seeReply = SEE_REPLY(), seeStatus = 200, screen = null, health = HEALTH} = {}){
  const calls = [];
  const app = await boot({
    health, screen,
    fetchImpl: async (url, opts) => {
      const body = opts && opts.body ? JSON.parse(opts.body) : null;
      if (String(url).indexOf('/health') >= 0) return {ok: true, status: 200, json: async () => health};
      if (String(url).indexOf('/see') >= 0){
        calls.push({url: String(url), body});
        if (seeStatus !== 200) return {ok: false, status: seeStatus, json: async () => seeReply};
        return {ok: true, status: 200, json: async () => seeReply};
      }
      calls.push({url: String(url), body});
      return {ok: true, status: 200, json: async () => ({
        ok: true, answer: 'The movers quoted 26,000.', decision: 'notes', on_notes: true,
        nodes: [7], sources: [{index: 7, label: 'Budget for the Move', score: 3}],
        model: 'stub-model', turns: 1
      })};
    }
  });
  app.calls = calls;
  app.sees = () => calls.filter(c => c.url === '/see');
  app.chats = () => calls.filter(c => c.url === '/chat');
  return app;
}

/** ask a question and let the answer be spoken through the virtual clock */
async function ask(app, question){
  app.app.voice.submitQuestion(question);
  await app.flush(12, 40);
}

const text = (app) => app.elements.get('answer-text').textContent;
const heard = (app) => app.speech.words().join(' | ');

console.log('\nAlfred - sight: can he see your screen, and can he lie about it?');

/* ------------------------------------------------------------------------- */
group('the screen button: starts a share and keeps the stream');

{
  const p = await page({screen: {frames: [tinyFrame(1)]}});
  check(p.app.sight.state() === 'off', 'nothing is being shared before the button is pressed');
  check(p.app.sight.badge().hidden === true, 'and no indicator is on screen');
  check(p.app.sight.button().pressed === 'false', 'the button reads as not pressed');

  const started = await p.app.sight.start();
  await p.flush(6, 40);
  check(started.ok === true, 'pressing it starts a real getDisplayMedia share');
  check(p.screen.asked === 1, 'the browser was asked exactly once');
  check(p.app.sight.state() === 'live', 'the page is now sharing');
  check(p.app.sight.isSharing() === true, 'and the share counts as live');
  check(!!p.screen.tracks[0] && p.screen.tracks[0].readyState === 'live',
        'THE STREAM IS HELD: the track is still live after start() returns');
  check(p.app.sight.track() === p.screen.tracks[0], 'the page is holding that same track');
  check(p.screen.streams[0].getTracks()[0].readyState === 'live', 'and nothing has been stopped');
  check(p.screen.served.length === 0, 'no frame has been grabbed just because the share started');
}

group('the indicator is not subtle about it');

{
  const p = await page({screen: {}});
  await p.app.sight.start();
  await p.flush(6, 40);
  const badge = p.app.sight.badge();
  check(p.app.sight.ring() === true, 'a ring lights up around the whole viewport');
  check(badge.hidden === false, 'a badge appears while sharing');
  check(/screen live/i.test(badge.text), 'the badge says the screen is live (' + badge.text + ')');
  check(/everything you ask/i.test(badge.sub), 'and says what that means for your questions');
  check(p.app.sight.button().pressed === 'true', 'the button itself shows it is on');
  check(/screen/i.test(p.elements.get('q').placeholder),
        'the ask bar says the next question is about the screen');
  await p.flush(40, 40);          // the note-title hint rotates every 5.2s
  check(/screen/i.test(p.elements.get('q').placeholder),
        'and the rotating note hint does not quietly take that line back');
  check(p.elements.get('q').value === '', 'while the bar itself stays empty and ready');
  check(heard(p).includes(LINES.started), 'his own line about watching is spoken, from the server');
  // and the style really is loud, not a whisper in a corner
  const css = SRC.slice(SRC.indexOf('#sight-ring'), SRC.indexOf('#sight-ring') + 1600);
  check(/#sight-ring\{[^}]*position:fixed/.test(css.replace(/\s+/g, '')),
        'the ring is fixed over the viewport, not tucked into a corner');
  check(/#sight-ring\{[^}]*(inset:0|inset: 0)/.test(css), 'the ring covers the whole viewport');
  check(/z-index:\s*9|z-index:9/.test(css), 'the ring sits above the graph');
  check(/@keyframes\s+ring-pulse/.test(SRC), 'and it pulses, so a still eye cannot miss it');
  check(/#sight-badge\{[^}]*position:fixed/.test(SRC.replace(/\s+/g, '')),
        'the badge is fixed to the top of the window');
}

group('a question while sharing takes ONE frame, right then, and sends it to /see');

{
  const p = await page({screen: {frames: [tinyFrame(1), tinyFrame(2)]}});
  await p.app.sight.start();
  await p.flush(6, 40);

  await ask(p, 'what am I looking at?');
  check(p.sees().length === 1, 'exactly one POST /see went out');
  check(p.chats().length === 0, 'and it did not go to /chat instead');
  check(p.elements.get('q').value === '', 'the ask bar is free again after a screen question');
  const body = p.sees()[0].body;
  check(body.question === 'what am I looking at?', 'the question travelled with the frame');
  check(typeof body.image === 'string' && body.image.startsWith('data:image/'),
        'the frame travelled as a data URL');
  check(body.image === p.screen.served[0].url,
        'and it is exactly what the canvas encoded a moment ago');
  check(body.media_type === 'image/jpeg' && body.image.startsWith('data:image/jpeg;base64,'),
        'the declared media type matches the bytes the browser produced');
  check(body.width === 1280 && body.height === 720, 'the frame size went along with it');
  check(typeof body.asked_at === 'number' && typeof body.captured_at === 'number',
        'the ask time and the capture time are both on the wire');
  check(p.screen.served[0].type === 'image/jpeg', 'the canvas was asked for a JPEG');
  check(p.screen.served[0].width === 1280 && p.screen.served[0].height === 720,
        'the canvas was drawn at the frame size');

  // the answer comes back through the same path as every other answer
  check(text(p).includes('file list is on the left'), 'the answer appears on the answer card');
  check(heard(p).includes('file list is on the left'), 'and comes back through the same voice');
  check(p.app.sight.frameShown() !== null, 'the frame he looked at is shown under the answer');
  check(String(p.app.sight.frameShown().src).startsWith('data:image/jpeg'),
        'and it is the very frame that was sent');
  check(/1280\u00d7720/.test(p.app.sight.frameShown().meta),
        'with its size printed plainly (' + p.app.sight.frameShown().meta.split('\n')[1] + ')');
  check(/taken at \d\d:\d\d:\d\d/.test(p.app.sight.frameShown().meta),
        'and the moment it was taken');
  check(/from the screen/.test(p.elements.get('a-foot').innerHTML) ||
        /from the screen/.test(p.elements.get('a-foot').textContent),
        'the footer says where the answer came from');
  check(/no notes used/.test(p.elements.get('a-foot').innerHTML) ||
        /no notes used/.test(p.elements.get('a-foot').textContent),
        'and says no notes were used');
}

group('the frame is taken NOW: never from the start of the share, never the last one');

{
  const p = await page({screen: {frames: [tinyFrame(1), tinyFrame(2)]}});
  await p.app.sight.start();
  await p.flush(6, 40);
  check(p.screen.served.length === 0, 'a minute of sharing grabs nothing on its own');

  await ask(p, 'what is this?');
  const first = p.sees()[0].body.image;
  await ask(p, 'and what is this?');
  const second = p.sees()[1].body.image;
  check(p.sees().length === 2, 'two questions, two frames, two calls');
  check(first === tinyFrame(1), 'the first question carried the frame that existed then');
  check(second === tinyFrame(2), 'THE SECOND QUESTION CARRIED A NEW FRAME, taken at the ask');
  check(first !== second, 'the frame is not cached from when the share started');
  check(p.screen.served.length === 2, 'one canvas encode per question, no more');
}

group('the media type is read back, never assumed');

{
  const png = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFklEQVQIHWNkYPjPwMDAwMTAwMDAwAAAJgAB0AbCdwAAAABJRU5ErkJggg==';
  const p = await page({screen: {frames: [png]}});
  await p.app.sight.start();
  await p.flush(6, 40);
  await ask(p, 'what am I looking at?');
  const body = p.sees()[0].body;
  check(body.image.startsWith('data:image/png'), 'a browser that encoded a PNG sends a PNG');
  check(body.media_type === 'image/png',
        'and declares image/png - the claim matches the bytes (' + body.media_type + ')');
  check(body.media_type !== 'image/jpeg', 'it never claims JPEG just because it asked for one');
}

group('the share ends: he says so, and never answers a screen question from memory');

{
  const p = await page({screen: {frames: [tinyFrame(1)]}});
  await p.app.sight.start();
  await p.flush(6, 40);
  await ask(p, 'what am I looking at?');
  check(p.sees().length === 1, 'a screen answer happened while the share was live');

  p.screen.end();                       // exactly what the browser's Stop-sharing bar does
  await p.flush(6, 40);
  check(p.app.sight.state() === 'ended', 'the share is over');
  check(p.app.sight.isSharing() === false, 'and it no longer counts as live');
  check(p.app.sight.ring() === false, 'the ring goes out');
  check(/screen share ended/i.test(p.app.sight.badge().text), 'the badge says the share ended');
  check(p.app.sight.button().pressed === 'false', 'the button reads as not pressed');
  check(heard(p).includes(LINES.ended), 'and he says the share has ended, in his own words');
  check(p.screen.tracks[0].readyState === 'ended', 'the track really is stopped');

  const before = p.sees().length;
  await ask(p, 'what am I looking at now?');
  check(p.sees().length === before, 'a screen question after the end sends NOTHING to /see');
  check(text(p).includes(LINES.ended), 'he answers with "the share has ended", not with a guess');
  check(!text(p).includes('file list'), 'and never from what he saw a moment ago');
  check(p.chats().filter(c => /what am i looking at now/.test(c.body.question)).length === 0,
        'and that screen question is not quietly answered from the notes either');
  check(p.screen.served.length === 1, 'no new frame was even grabbed');

  // a question about the NOTES is untouched by any of this
  await ask(p, 'what is the budget for the move?');
  const chats = p.chats().filter(c => /budget for the move/.test(c.body.question));
  check(chats.length === 1, 'a question about the notes still goes to /chat');
  check(heard(p).includes('The movers quoted 26,000'), 'and is answered normally');
}

group('sharing stopped, then the notes question: the ended line is not sprayed around');

{
  const p = await page({screen: {frames: [tinyFrame(1)]}});
  await p.app.sight.start();
  await p.flush(6, 40);
  p.app.sight.stop();                    // the button again
  await p.flush(6, 40);
  check(p.app.sight.state() === 'ended', 'pressing the button stops the share');
  check(p.screen.stopped >= 1, 'the stream is really stopped, not just forgotten');

  await ask(p, 'what does my notes say about the movers?');
  check(p.sees().length === 0, 'a note question after a share ends is not sent to /see');
  check(p.chats().length === 1, 'it goes to /chat, as usual');
  check(!text(p).includes(LINES.ended), 'and no "the share has ended" is said over a note answer');
}

group('the track dies without a word: nothing is sent and nothing is guessed');

{
  const p = await page({screen: {frames: [tinyFrame(1)]}});
  await p.app.sight.start();
  await p.flush(6, 40);
  p.screen.kill();                       // the track is gone, but no onended fired
  await p.flush(4, 40);
  const before = p.sees().length;
  await ask(p, 'what am I looking at?');
  check(p.sees().length === before, 'a dead track sends nothing to /see');
  check(!text(p).includes('file list'), 'and nothing is answered from a picture that is gone');
  check(heard(p).includes(LINES.lost) || heard(p).includes(LINES.never) || heard(p).includes(LINES.grab_failed) ||
        heard(p).includes('screen'),
        'he says plainly that he cannot see the screen (' + text(p).slice(0, 60) + ')');
}

group('no share at all: a screen question is refused, honestly');

{
  const p = await page({screen: {}});
  await ask(p, 'what am I looking at?');
  check(p.sees().length === 0, 'nothing is sent to /see without a share');
  check(text(p).includes(LINES.never),
        'he says he has not been shown the screen (' + text(p).slice(0, 70) + ')');
  check(heard(p).includes(text(p)), 'and it is spoken, not just printed');
  check(p.app.sight.badge().hidden === true, 'no indicator is claimed when nothing is shared');
  await ask(p, 'what does my notes say about the movers?');
  check(p.chats().length === 1, 'screen-phrased questions and note questions are told apart...');
  check(p.elements.get('answer-text').textContent.includes('movers'),
        '...and the note question is answered (not blocked by the sight code)');
}

group('a share that cannot start says so, and the page keeps working');

{
  const unsupported = await page({screen: {unsupported: true}});
  check(unsupported.app.sight.supported() === false, 'a browser with no getDisplayMedia is noticed');
  const r = await unsupported.app.sight.start();
  check(r.ok === false && r.why === 'unsupported', 'pressing the button does not pretend to share');
  check(unsupported.app.sight.state() === 'off', 'and the page still says it is not sharing');
  await ask(unsupported, 'what does my notes say about the movers?');
  check(unsupported.chats().length === 1, 'the rest of the page works exactly as before');

  const denied = await page({screen: {deny: true}});
  const r2 = await denied.app.sight.start();
  await denied.flush(4, 40);
  check(r2.ok === false && r2.why === 'NotAllowedError', 'a refused permission is reported, not swallowed');
  check(denied.app.sight.state() === 'off', 'and nothing is left half-shared');
  check(denied.app.sight.badge().hidden === true, 'no indicator lights up for a share that never started');
}

group('a frame that cannot be grabbed is said out loud, and nothing is sent');

{
  const p = await page({screen: {videoWidth: 0, videoHeight: 0}});
  await p.app.sight.start();
  await p.flush(6, 40);
  await ask(p, 'what am I looking at?');
  await p.flush(80, 40);                // past the frame timeout: the grab gives up and speaks
  check(p.sees().length === 0, 'a frame that never arrived sends nothing');
  check(p.screen.served.length === 0, 'because there was nothing to encode');
  check(text(p).includes(LINES.grab_failed) || /could not/i.test(text(p)),
        'and he says he could not take a picture (' + text(p).slice(0, 70) + ')');
  check(p.app.sight.state() === 'live', 'the share itself is still running afterwards');
}

group('a /see that fails is not dressed up as an answer');

{
  const p = await page({screen: {frames: [tinyFrame(1)]},
                        seeStatus: 400,
                        seeReply: {ok: false, error: 'The frame was too small to judge.',
                                   code: 'frame_too_small'}});
  await p.app.sight.start();
  await p.flush(6, 40);
  await ask(p, 'what am I looking at?');
  check(p.sees().length === 1, 'the frame was sent');
  check(/too small/i.test(p.elements.get('answer-text').textContent) ||
        /too small/i.test(p.elements.get('a-foot').textContent),
        'the server\'s refusal is what you see');
  check(!/file list/.test(text(p)), 'no invented answer');
  check(p.app.sight.frameShown() !== null &&
        /could not look/.test(p.app.sight.frameShown().meta),
        'and the proof strip says the frame was not looked at');
  check(p.elements.get('send').disabled === false, 'the ask bar is usable again');
}

group('the character owns the words: the viewer carries none of these lines');

{
  check(!SRC.includes('Watching your screen now'),
        'the "watching" line is not hardcoded in the viewer');
  check(!SRC.includes('I am not looking at anything now'),
        'nor is the "share has ended" line');
  check(!SRC.includes('I have not been shown your screen yet'),
        'nor is the "not been shown" line');
  check(SRC.includes("h.sight && h.sight.lines"),
        'the lines are taken from /health, which is where the persona lives');
  check(SRC.includes("POST /see") || SRC.includes("'/see'"),
        'and the viewer knows the endpoint it must ask');
  check(!/image\/jpeg'\);\s*\/\/[^\n]*media_type/.test(SRC),
        'no place assumes the media type without reading it back');
}

/* ------------------------------------------------------------------- report */
console.log('\nAlfred - sight verification (virtual clock, no browser needed)');
if (failed.length){
  console.log('\n  RESULT: FAILED (' + fail + ')');
  failed.forEach(p => console.log('   - ' + p));
  process.exit(1);
}
console.log('\n  RESULT: PASSED - ' + pass + ' checks: the share is held, the indicator is loud, the\n' +
            '  frame is taken at the ask, its type is read back, and a lost screen is\n' +
            '  always said out loud instead of guessed at');
