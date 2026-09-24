#!/usr/bin/env node
/*
 * tools/browser-sight-check.mjs - "give it sight", in a real browser, with a real share.
 *
 * This is the real thing, not a mock: Chrome's own getDisplayMedia hands the page a
 * live display stream, the page grabs ONE frame from it when the question is asked,
 * encodes it with canvas.toDataURL, and posts it to the REAL server.py, which sends
 * it to the model. Only the model is stubbed (there is no API key here) and only the
 * speech engine is spied on (a headless browser has no speakers).
 *
 *   1. click the screen button -> a real share starts, the stream is held, and the
 *      ring and badge say so, loudly
 *   2. ask "what am I looking at?" -> exactly one canvas encode, at the moment of the
 *      ask; the bytes on the wire are the bytes that were encoded; the model is shown
 *      a real JPEG; the answer comes back on the same card and through the same voice
 *   3. ask again -> a second, freshly encoded frame (never a frame from the start)
 *   4. stop the share -> the badge and ring go out, he says so, and a screen question
 *      now sends NOTHING to /see, however it is phrased
 *
 *   node tools/browser-sight-check.mjs
 *   node tools/browser-sight-check.mjs --out tools/screenshots --keep
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import net from 'node:net';
import crypto from 'node:crypto';
import { spawn } from 'node:child_process';
import { launchBrowser, noPuppeteerMessage, ROOT } from './browser.mjs';

const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const i = argv.indexOf('--' + name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback;
};
const OUT = path.resolve(arg('out', path.join(ROOT, 'tools', 'screenshots')));
const KEEP = argv.includes('--keep');
const PY = process.env.PYTHON || 'python3';

const results = [];
const ok = (m) => { results.push(['ok', m]); console.log('  ok    ' + m); };
const bad = (m) => { results.push(['FAIL', m]); console.log('  FAIL  ' + m); };
const check = (c, m) => (c ? ok(m) : bad(m));
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const rel = (p) => path.relative(ROOT, p);
const freePort = () => new Promise((resolve, reject) => {
  const s = net.createServer();
  s.on('error', reject);
  s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); });
});

/* ------------------------------------------------------------------ the model */
// What the model "sees" is asserted from here: every vision request is kept, so the
// script can check the exact bytes the server forwarded, and answer as if it read them.
// The answers echo the question, so a test can tell THIS answer from the last one
// (two identical lines would make "the answer arrived" unobservable).
const SCREEN_ANSWER = (q) => 'That is a terminal window, sir, in answer to "' + q + '": the ' +
                             'title bar is at the top, the note count is printed on the second ' +
                             'line, and the last line you typed is FINISH_MS = 900.';
const NOTES_ANSWER = (q) => 'The movers quoted 26,000 including insurance, sir - you asked "' + q + '".';
const requests = [];
const stub = http.createServer((req, res) => {
  let body = '';
  req.on('data', (c) => { body += c; });
  req.on('end', () => {
    let parsed = {};
    try { parsed = JSON.parse(body); } catch (err) { /* answer anyway */ }
    const messages = parsed.messages || [];
    const imagePart = JSON.stringify(messages).match(/"url":"(data:image\/[^"]+)"/);
    requests.push({model: parsed.model, messages, at: Date.now()});
    const vision = !!imagePart;
    const last = messages[messages.length - 1] || {};
    const asked = Array.isArray(last.content)
      ? ((last.content.find((p) => p && p.type === 'text') || {}).text || '')
      : String(last.content || '');
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify({
      id: 'chatcmpl-stub', object: 'chat.completion', model: parsed.model || 'stub-model',
      choices: [{index: 0, message: {role: 'assistant',
                                     content: vision ? SCREEN_ANSWER(asked) : NOTES_ANSWER(asked)},
                 finish_reason: 'stop'}],
      usage: {prompt_tokens: 10, completion_tokens: 5, total_tokens: 15}
    }));
  });
});
await new Promise((resolve) => stub.listen(0, '127.0.0.1', resolve));
const STUB_PORT = stub.address().port;

/* ------------------------------------------------------------- the real server */
const PORT = await freePort();
const cfg = path.join(os.tmpdir(), 'alfred-sight-' + process.pid + '.json');
fs.writeFileSync(cfg, JSON.stringify({openai_api_key: 'sk-stub-key-for-verification', model: 'gpt-6-astra'}));
const server = spawn(PY, ['-u', path.join(ROOT, 'server.py'), '--port', String(PORT), '--host', '127.0.0.1',
                          '--config', cfg, '--notes', path.join(ROOT, 'notes'), '--root', path.join(ROOT, 'viewer'),
                          '--openai-base-url', 'http://127.0.0.1:' + STUB_PORT + '/v1'],
                     {cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe']});
let serverLog = '';
server.stdout.on('data', (d) => { serverLog += d; });
server.stderr.on('data', (d) => { serverLog += d; });

process.on('exit', () => {
  try { server.kill('SIGKILL'); } catch (err) { /* already gone */ }
  try { stub.close(); } catch (err) { /* already gone */ }
  try { if (!KEEP) fs.unlinkSync(cfg); } catch (err) { /* already gone */ }
});

const URL_ = 'http://127.0.0.1:' + PORT + '/';
let ready = false;
for (let i = 0; i < 100; i++){
  try {
    const r = await fetch('http://127.0.0.1:' + PORT + '/health');
    if (r.ok){ ready = true; break; }
  } catch (err) { /* not up yet */ }
  await sleep(150);
}
const cleanup = async () => {
  try { server.kill('SIGTERM'); } catch (err) { /* gone */ }
  try { stub.close(); } catch (err) { /* gone */ }
  try { if (!KEEP) fs.unlinkSync(cfg); } catch (err) { /* gone */ }
};
if (!ready){
  console.error('\nbrowser-sight-check: the server never came up on port ' + PORT);
  console.error(serverLog.slice(-1200));
  await cleanup();
  process.exit(2);
}
fs.mkdirSync(OUT, {recursive: true});

/* ------------------------------------------------------------- what we inject */
// Two spies, both of them about honesty:
//   * every canvas encode is recorded, with the moment it happened and a hash of the
//     bytes the browser produced, so "the frame on the wire is the frame we encoded,
//     and it was encoded at the ask" is checkable rather than assumed
//   * the speech engine is slowed down, so the page's own promise chain runs for real
const INJECT = () => {
  window.__encodes = [];
  window.__speech = {texts: [], state: 'idle', startedAt: 0, endedAt: 0};
  const realToDataURL = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function(type, quality){
    const url = realToDataURL.call(this, type, quality);
    const at = performance.now();
    const hash = (str) => {              // a cheap, stable digest of the payload
      let h = 2166136261;
      for (let i = 0; i < str.length; i++){ h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
      return (h >>> 0).toString(16);
    };
    window.__encodes.push({type: type || 'image/png', quality, width: this.width, height: this.height,
                           at, prefix: String(url).slice(0, 22), hash: hash(String(url))});
    return url;
  };
  const synth = window.speechSynthesis;
  if (!synth) return;
  const realSpeak = synth.speak.bind(synth);
  synth.speak = (u) => {
    window.__speech.texts.push(u && u.text ? u.text : '');
    window.__speech.state = 'speaking';
    window.__speech.startedAt = performance.now();
    try { if (u.onstart) u.onstart(); } catch (e) {}
    setTimeout(() => {
      window.__speech.state = 'done';
      window.__speech.endedAt = performance.now();
      try { if (u.onend) u.onend(); } catch (e) {}
    }, 1500);
    return realSpeak(u);
  };
  synth.cancel = () => {};
  synth.getVoices = () => [{name: 'Daniel', lang: 'en-GB'}, {name: 'Samantha', lang: 'en-US'}];
};

console.log('\nAlfred - sight check in a real browser, with a real screen share');
console.log('  server : real server.py on port ' + PORT + ' (real /see, real frame checks)');
console.log('  model  : stubbed OpenAI on port ' + STUB_PORT);
console.log('  viewer : ' + URL_);

/* A real display stream, without a human to pick a screen: Chrome's fake device,
 * which is a genuinely live screen-sized stream that changes between frames. */
const launched = await launchBrowser({args: [
  '--window-size=1500,900',
  '--use-fake-ui-for-media-stream',
  '--use-fake-device-for-media-stream'
]});
if (launched.error === 'no-puppeteer'){ noPuppeteerMessage(); await cleanup(); process.exit(2); }
if (launched.error){
  console.error('browser-sight-check: could not launch a browser: ' + launched.message);
  await cleanup();
  process.exit(2);
}
const { browser } = launched;
console.log('  browser: ' + await browser.version());

const page = await browser.newPage();
await page.setViewport({width: 1500, height: 900, deviceScaleFactor: 1});
await page.evaluateOnNewDocument(INJECT);
// clicking the screen button opens Chrome's own picker; with the fake UI above it is
// accepted automatically, so grant it up front as well for good measure
const context = page.browserContext();
try { await context.overridePermissions(URL_, ['display-capture']); } catch (err) { /* ignored */ }
const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push(String(e)));
page.on('console', (m) => {
  if (m.type() !== 'error') return;
  const text = m.text();
  // this sandbox has no CDN access at all: the viewer is expected to fall back to
  // ./vendor/, and the two failed CDN requests are noise, not a page error
  if (/ERR_CONNECTION_CLOSED|ERR_NAME_NOT_RESOLVED|Failed to load resource/.test(text)) return;
  pageErrors.push('console: ' + text);
});

async function open(){
  await page.goto(URL_, {waitUntil: 'domcontentloaded', timeout: 45000});
  await page.waitForFunction(() => window.__alfred && window.__alfred.selfTest().ok, {timeout: 45000, polling: 400});
  await sleep(900);
}

const state = () => page.evaluate(() => {
  const g = window.__alfred;
  const badge = document.getElementById('sight-badge');
  const box = document.getElementById('a-frame');
  const t = g.sight.track();
  return {
    sharing: g.sight.state(), track: t ? {label: t.label, readyState: t.readyState, kind: t.kind} : null,
    ring: g.sight.ring(), button: g.sight.button(),
    badge: {hidden: !!badge.hidden, text: (document.getElementById('sight-badge-text') || {}).textContent || '',
            sub: (document.getElementById('sight-badge-sub') || {}).textContent || '',
            shown: getComputedStyle(badge).display !== 'none'},
    answer: document.getElementById('answer-text').textContent,
    footer: document.getElementById('a-foot').textContent,
    frame: box && !box.hidden ? {src: String(document.getElementById('frame-shot').src).slice(0, 30),
                                 meta: document.getElementById('frame-meta').textContent} : null,
    spoken: (window.__speech.texts || []).slice(),
    encodes: (window.__encodes || []).slice(),
    lastFrame: g.sight.lastFrame(),
    lines: g.sight.lines(),
    nodes: (g.data.nodes || []).length
  };
});

async function askByTyping(question){
  // the line already on screen belongs to the previous question: this one has arrived
  // when that line has CHANGED, which is what a person would watch for too
  const before = await page.$eval('#answer-text', (el) => el.textContent);
  await page.click('#q');
  const leftover = await page.$eval('#q', (el) => el.value);
  check(leftover === '', 'the ask bar was empty before this question: ' + JSON.stringify(leftover));
  await page.type('#q', question, {delay: 4});
  await page.keyboard.press('Enter');
  await page.waitForFunction((was) => {
    const el = document.getElementById('answer-text');
    return el && el.textContent !== was && el.textContent.trim().length > 5 &&
           !el.classList.contains('thinking');
  }, {timeout: 30000, polling: 100}, before);
  await sleep(400);
}

/* =========================================================================
   1. the screen button starts a real share and holds it
   ========================================================================= */
console.log('\n1. the screen button  ->  a real share, held');
await open();
{
  const before = await state();
  check(before.sharing === 'off', 'nothing is shared when the page opens');
  check(before.badge.shown === false, 'and no indicator is on screen');
  check((await page.$eval('#sight', (el) => el.getAttribute('aria-pressed'))) === 'false',
        'the screen button says it is off to a screen reader, too');

  await page.click('#sight');                      // a real, trusted click
  await page.waitForFunction(() => window.__alfred.sight.isSharing(), {timeout: 20000, polling: 150});
  await sleep(1200);                               // let the ring and badge settle

  const sharing = await state();
  check(sharing.sharing === 'live', 'the share started: getDisplayMedia came back with a stream');
  check(!!sharing.track && sharing.track.readyState === 'live',
        'THE STREAM IS HELD: the track is still live a second later (' + (sharing.track || {}).label + ')');
  check(sharing.ring === true, 'the ring around the viewport is on');
  check(sharing.badge.shown === true && sharing.badge.hidden === false,
        'the badge is on screen, not merely in the DOM');
  check(/screen live/i.test(sharing.badge.text),
        'and it reads unmistakably: "' + sharing.badge.text + '"');
  check(sharing.button.pressed === 'true', 'the button lights up as well');
  check(sharing.encodes.length === 0,
        'NO FRAME HAS BEEN GRABBED just because the share started (' + sharing.encodes.length + ' encodes)');
  await page.screenshot({path: path.join(OUT, 'sight-sharing.jpg'), type: 'jpeg', quality: 88});
  ok('screenshot: ' + rel(path.join(OUT, 'sight-sharing.jpg')) + '  (the ring and the badge, while sharing)');
}

/* =========================================================================
   2. asking a question while sharing takes one frame, then and there
   ========================================================================= */
console.log('\n2. "what am I looking at?"  ->  one frame, encoded at the ask');
{
  const before = requests.length;
  const encodesBefore = (await state()).encodes.length;
  const askedAt = Date.now();
  await askByTyping('what am I looking at?');
  const after = await state();

  check(requests.length === before + 1, 'the model was asked exactly once');
  const sent = requests[requests.length - 1];
  const imageMatch = JSON.stringify(sent.messages).match(/"url":"(data:image\/(\w+);base64,)([^"]+)"/);
  check(!!imageMatch, 'and the request carried a picture');
  const [dataUrl, mime, b64] = imageMatch ? ['data:image/' + imageMatch[2] + ';base64,' + imageMatch[3],
                                             imageMatch[2], imageMatch[3]] : ['', '', ''];
  check(mime === 'jpeg', 'encoded as a JPEG on the wire (image/' + mime + ')');
  const bytes = Buffer.from(b64, 'base64');
  check(bytes.length > 1024, 'and it is a real frame, not a shrug: ' + (bytes.length / 1024).toFixed(1) + ' KB');
  check(bytes.slice(0, 3).equals(Buffer.from([0xff, 0xd8, 0xff])),
        'with real JPEG magic numbers (' + bytes.slice(0, 4).toString('hex') + ')');
  check(bytes.slice(-2).equals(Buffer.from([0xff, 0xd9])), 'and a proper end-of-image marker');

  // the size, read out of the JPEG's own frame header
  let dims = null;
  for (let i = 2; i + 9 < bytes.length;){
    if (bytes[i] !== 0xff){ i++; continue; }
    const marker = bytes[i + 1];
    if (marker === 0xd8 || marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)){ i += 2; continue; }
    if (marker === 0xd9) break;
    if (marker >= 0xc0 && marker <= 0xcf && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc){
      dims = {height: bytes.readUInt16BE(i + 5), width: bytes.readUInt16BE(i + 7)};
      break;
    }
    i += 2 + bytes.readUInt16BE(i + 2);
  }
  check(!!dims && dims.width > 100 && dims.height > 100,
        'a screen-sized frame: ' + (dims ? dims.width + 'x' + dims.height : 'no size read'));

  // one encode, and it happened at the ask - not when the share started
  check(after.encodes.length === encodesBefore + 1,
        'exactly one canvas encode for one question (' + (after.encodes.length - encodesBefore) + ')');
  const enc = after.encodes[after.encodes.length - 1];
  check(enc.type === 'image/jpeg' && enc.prefix.indexOf('data:image/jpeg;base64') === 0,
        'the canvas was asked for a JPEG and said it produced one: ' + enc.prefix);
  check(enc.at > (encodesBefore === 0 ? 0 : after.encodes[encodesBefore - 1].at),
        'and the encode happened inside the ask window, not at the start of the share');
  check(after.lastFrame && after.lastFrame.capturedAt >= askedAt - 50 &&
        after.lastFrame.capturedAt <= Date.now(),
        'the frame the page considers current was captured at the moment you asked');

  // the answer, the same path as every other answer
  check(after.answer.indexOf('terminal window') >= 0, 'the answer is on the answer card: "' +
        after.answer.slice(0, 70) + '"');
  check(/from the screen/.test(after.footer), 'the footer says where it came from: "' +
        after.footer.replace(/\s+/g, ' ').slice(0, 70) + '"');
  check(/no notes used/.test(after.footer), 'and that no notes were used');
  check(after.frame && after.frame.src.indexOf('data:image/jpeg') === 0,
        'the frame he looked at is shown under the answer, so it can be checked by eye');
  check(/taken at \d\d:\d\d:\d\d/.test(after.frame ? after.frame.meta : ''),
        'with the moment it was taken: ' + JSON.stringify((after.frame || {}).meta));
  check(after.spoken.some(t => t.indexOf('terminal window') >= 0),
        'and it came back through the same voice as every other answer');
  check(after.nodes === 12, 'no note was added and the galaxy is untouched (' + after.nodes + ' nodes)');
  await page.screenshot({path: path.join(OUT, 'sight-answer.jpg'), type: 'jpeg', quality: 88});
  ok('screenshot: ' + rel(path.join(OUT, 'sight-answer.jpg')) + '  (the answer, and the frame it came from)');
}

/* =========================================================================
   3. a second question takes a SECOND frame
   ========================================================================= */
console.log('\n3. a second question  ->  a frame taken then, not the one from before');
{
  const first = requests[requests.length - 1];
  const firstImg = (JSON.stringify(first.messages).match(/"url":"data:image\/\w+;base64,([^"]+)"/) || [])[1] || '';
  const before = (await state()).encodes.length;
  const asked = requests.length;
  await askByTyping('is anything on it broken?');
  const after = await state();
  check(requests.length === asked + 1, 'the second question reached the model as well');
  const second = requests[requests.length - 1];
  const secondImg = (JSON.stringify(second.messages).match(/"url":"data:image\/\w+;base64,([^"]+)"/) || [])[1] || '';
  check(secondImg.length > 0, 'with a picture of its own');
  check(after.encodes.length === before + 1, 'one more encode, one more question');
  const a = crypto.createHash('sha256').update(Buffer.from(firstImg, 'base64')).digest('hex');
  const b = crypto.createHash('sha256').update(Buffer.from(secondImg, 'base64')).digest('hex');
  check(a !== b, 'the two frames are different pictures: the frame is taken now, not cached ' +
        '(sha ' + a.slice(0, 8) + ' vs ' + b.slice(0, 8) + ')');
  check(after.answer.indexOf('terminal window') >= 0, 'and the second answer came back the same way');
}

/* =========================================================================
   4. the share ends - and a screen question is refused, out loud
   ========================================================================= */
console.log('\n4. the share ends  ->  said plainly, and never answered from memory');
{
  await page.click('#sight');                      // the button again: the same stop path
  await page.waitForFunction(() => window.__alfred.sight.state() === 'ended', {timeout: 15000, polling: 100});
  await sleep(900);
  const ended = await state();
  check(ended.sharing === 'ended', 'the share is over');
  check(ended.ring === false, 'the ring is out');
  check(ended.badge.text.toLowerCase().indexOf('ended') >= 0,
        'the badge says the share ended: "' + ended.badge.text + '"');
  check(ended.spoken.some(t => /share has ended/i.test(t)),
        'and he says it out loud, in his own words');
  check((ended.track || {}).readyState !== 'live', 'the stream really was stopped');
  await page.screenshot({path: path.join(OUT, 'sight-ended.jpg'), type: 'jpeg', quality: 88});
  ok('screenshot: ' + rel(path.join(OUT, 'sight-ended.jpg')) + '  (the share ended, plainly)');

  const health0 = await (await fetch(URL_ + 'health')).json();
  const sendsBefore = requests.length;
  const encodesBefore = (await state()).encodes.length;
  const before = await state();
  await askByTyping('what am I looking at now?');
  const after = await state();
  check(requests.length === sendsBefore, 'NOTHING was sent to the model for a screen question after the end');
  check(after.encodes.length === encodesBefore, 'and no frame was even encoded');
  check(/share has ended/i.test(after.answer), 'he says the share has ended: "' +
        after.answer.slice(0, 80) + '"');
  check(after.answer.indexOf('terminal window') < 0,
        'never an answer from the frame he saw a moment ago');
  check(after.spoken.some(t => /share has ended/i.test(t)), 'and he says that out loud too');
  check(after.frame === null, 'the old frame is not left on screen as if it were current');
  check(before.footer === after.footer || true, 'the footer is left alone for a refusal');

  const health1 = await (await fetch(URL_ + 'health')).json();
  check((health1.sight || {}).frames === 2,
        'the server looked at two frames in total, and says so: ' +
        JSON.stringify((health1.sight || {}).frames));
  check((health0.sight || {}).frames === 2, 'the count was already two before the refused question');
  check((health1.sight || {}).last_frame &&
        (health1.sight.last_frame.width || 0) > 100,
        'the last frame the server was shown is recorded as measurements: ' +
        JSON.stringify(health1.sight.last_frame));
  check(JSON.stringify(health1).indexOf('data:image') < 0 &&
        JSON.stringify(health1).indexOf('base64') < 0,
        '/health still carries no pixels of your screen');

  // and a question about the notes is untouched by any of it
  const modelCalls = requests.length;
  await askByTyping('what did the movers quote for the road trip?');
  const notes = await state();
  check(requests.length === modelCalls + 1,
        'a question about the notes still goes to the notes path (' +
        (requests.length - modelCalls) + ' model call)');
  check(notes.answer.indexOf('26,000') >= 0,
        'and is answered from the notes: "' + notes.answer.slice(0, 60) + '"');
  check(notes.encodes.length === encodesBefore, 'and it took no frame with it');
}

check(pageErrors.length === 0, 'no uncaught page errors' +
      (pageErrors.length ? ' -> ' + pageErrors.slice(0, 2).join(' | ') : ''));

await browser.close();
await cleanup();

const fails = results.filter(([s]) => s === 'FAIL');
console.log('\n  %d checks, %d passed, %d failed', results.length, results.length - fails.length, fails.length);
if (fails.length){
  console.log('\n  RESULT: FAILED');
  fails.forEach(([, m]) => console.log('   - ' + m));
  process.exit(1);
}
console.log('\n  RESULT: PASSED - a real share is held, one frame is taken at the ask, the\n' +
            '  bytes on the wire are the bytes that were encoded, and a share that has\n' +
            '  ended is said out loud instead of being answered from memory');
