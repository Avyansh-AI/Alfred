#!/usr/bin/env node
/*
 * tools/browser-provenance-check.mjs - "the galaxy proves where the answer came from",
 * shown end to end in a real browser.
 *
 * This one runs the REAL server.py against the REAL notes: retrieval, scoring and the
 * "was this question about my notes?" decision are the actual code, not a fixture.
 * Only the model is stubbed (there is no API key here), and only the speech engine is
 * spied on (a headless browser has no speakers).
 *
 * It demonstrates the three cases, and photographs each one:
 *
 *   1. one note     - "what is the budget for the move?"  -> the camera flies to that
 *                     note, lights it and its neighbours, opens its panel, and does
 *                     all of it while the answer is still being spoken
 *   2. four or more - "what should I pack and prepare before moving, and what is the
 *                     budget?" -> nothing flies; the whole cluster lights up instead
 *   3. small talk   - "good morning" -> the galaxy does not move at all
 *
 *   node tools/browser-provenance-check.mjs
 *   node tools/browser-provenance-check.mjs --out tools/screenshots --keep
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import net from 'node:net';
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
// One canned answer, deliberately NOT copied from the note: the check proves the page
// speaks the answer and never the note, and a canned answer makes that unambiguous.
const ANSWER = 'The movers quoted 26,000 including insurance for the TV and the fridge, ' +
               'and the train tickets for three came to 4,200.';
// Small talk gets a small-talk answer, so the screenshots read honestly.
const CHAT_ANSWER = 'Good morning! Ask me anything about your notes.';
const prompts = [];
const stub = http.createServer((req, res) => {
  let body = '';
  req.on('data', (c) => { body += c; });
  req.on('end', () => {
    let parsed = {};
    try { parsed = JSON.parse(body); } catch (err) { /* not JSON: still answer */ }
    prompts.push(parsed);
    const smallTalk = JSON.stringify(parsed.messages || []).includes('ONE short, friendly sentence');
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify({
      id: 'chatcmpl-stub', object: 'chat.completion', model: parsed.model || 'stub-model',
      choices: [{index: 0, message: {role: 'assistant', content: smallTalk ? CHAT_ANSWER : ANSWER},
                 finish_reason: 'stop'}],
      usage: {prompt_tokens: 10, completion_tokens: 5, total_tokens: 15}
    }));
  });
});
await new Promise((resolve) => stub.listen(0, '127.0.0.1', resolve));
const STUB_PORT = stub.address().port;

/* ------------------------------------------------------------- the real server */
const PORT = await freePort();
const cfg = path.join(os.tmpdir(), 'alfred-prov-' + process.pid + '.json');
fs.writeFileSync(cfg, JSON.stringify({openai_api_key: 'sk-stub-key-for-verification', model: 'stub-model'}));
const server = spawn(PY, ['-u', path.join(ROOT, 'server.py'), '--port', String(PORT), '--host', '127.0.0.1',
                          '--config', cfg, '--notes', path.join(ROOT, 'notes'), '--root', path.join(ROOT, 'viewer'),
                          '--openai-base-url', 'http://127.0.0.1:' + STUB_PORT + '/v1'],
                     {cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe']});
let serverLog = '';
server.stdout.on('data', (d) => { serverLog += d; });
server.stderr.on('data', (d) => { serverLog += d; });

// Even when this script dies mid-check (a failed assertion, an uncaught error), the
// server and the stub must not be left behind holding ports.
process.on('exit', () => {
  try { server.kill('SIGKILL'); } catch (err) { /* already gone */ }
  try { stub.close(); } catch (err) { /* already gone */ }
  try { fs.unlinkSync(cfg); } catch (err) { /* already gone */ }
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
  try { fs.unlinkSync(cfg); } catch (err) { /* gone */ }
};

if (!ready){
  console.error('\nbrowser-provenance-check: the server never came up on port ' + PORT);
  console.error(serverLog.slice(-1200));
  await cleanup();
  process.exit(2);
}

fs.mkdirSync(OUT, {recursive: true});

/* ------------------------------------------------------------- what we inject */
// A speech engine that takes its time: the page's own promise chain runs for real, and
// "while the answer is still being spoken" becomes an observable window.
const INJECT = () => {
  window.__speech = {texts: [], state: 'idle', startedAt: 0, endedAt: 0};
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
    }, 1800);
    return realSpeak(u);
  };
  synth.cancel = () => {};
  synth.getVoices = () => [{name: 'Daniel', lang: 'en-GB'}, {name: 'Samantha', lang: 'en-US'}];
};

console.log('\nAlfred - provenance check in a real browser');
console.log('  server : real server.py on port ' + PORT + ' (real notes, real retrieval)');
console.log('  model  : stubbed OpenAI on port ' + STUB_PORT);
console.log('  viewer : ' + URL_);

const launched = await launchBrowser({args: ['--window-size=1500,900']});
if (launched.error === 'no-puppeteer'){ noPuppeteerMessage(); await cleanup(); process.exit(2); }
if (launched.error){
  console.error('browser-provenance-check: could not launch a browser: ' + launched.message);
  await cleanup();
  process.exit(2);
}
const { browser } = launched;
console.log('  browser: ' + await browser.version());

const page = await browser.newPage();
await page.setViewport({width: 1500, height: 900, deviceScaleFactor: 1});
await page.evaluateOnNewDocument(INJECT);
const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push(String(e)));

async function open(){
  await page.goto(URL_, {waitUntil: 'domcontentloaded', timeout: 45000});
  await page.waitForFunction(() => window.__alfred && window.__alfred.selfTest().ok, {timeout: 45000, polling: 400});
  await sleep(900);                       // let the layout settle before photographing
}

/* Type the question into the real ask bar and press Enter. */
async function askByTyping(question){
  const before = await page.evaluate(() => {
    const c = window.__alfred.graph.camera().position;
    return {x: c.x, y: c.y, z: c.z};
  });
  await page.click('#q');
  await page.type('#q', question, {delay: 4});
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => {
    const el = document.getElementById('answer-text');
    return el && el.textContent.trim().length > 5 && !el.classList.contains('thinking');
  }, {timeout: 30000, polling: 100});
  await sleep(250);
  return {camera: before};          // same shape as state(), so moved() can compare them
}

const state = () => page.evaluate(() => {
  const g = window.__alfred;
  const c = g.graph.camera().position;
  const fly = g.lastFlyTo();
  return {
    camera: {x: c.x, y: c.y, z: c.z},
    flyTo: fly ? {x: fly.pos.x, y: fly.pos.y, z: fly.pos.z, node: fly.node} : null,
    lit: g.provenance.lit(),
    litLinks: g.provenance.litLinks().length,
    panelOpen: g.provenance.panelOpen(),
    panelLabel: g.provenance.panelLabel(),
    // the live plan holds graph nodes (which carry THREE objects): send a plain shape
    plan: (() => {
      const p = g.provenance.last();
      if (!p) return null;
      return {kind: p.kind, why: p.why || '',
              nodes: (p.nodes || []).map(n => n.index),
              top: p.node ? p.node.index : null};
    })(),
    answer: document.getElementById('answer-text').textContent,
    footer: document.getElementById('a-foot').textContent,
    status: g.voice.status(),
    spoken: (window.__speech ? window.__speech.texts : []).filter(t => t && t.trim()),
    speechState: window.__speech ? window.__speech.state : 'none',
    // the open note's own text, so we can prove the page never reads it out
    excerpt: (() => {
      const label = document.getElementById('panel-label').textContent;
      const n = (g.data.nodes || []).find(x => x.label === label);
      return n && n.excerpt ? n.excerpt : '';
    })()
  };
});
const moved = (a, b) => Math.hypot(a.camera.x - b.camera.x, a.camera.y - b.camera.y, a.camera.z - b.camera.z);
const neat = (p) => '[x ' + p.camera.x.toFixed(0) + ', y ' + p.camera.y.toFixed(0) + ', z ' + p.camera.z.toFixed(0) + ']';

/* ======================================================= 1. one note: it flies */
console.log('\n1. "what is the budget for the move?"  ->  one note, so the camera flies to it');
{
  await open();
  const before = await askByTyping('what is the budget for the move?');

  // the panel opens (and the flight starts) while the answer is still being spoken
  const duringSpeech = await page.waitForFunction(() => {
    const open = document.getElementById('panel').classList.contains('open');
    const speaking = window.__speech && window.__speech.state === 'speaking';
    return open && speaking && !!window.__alfred.lastFlyTo();
  }, {timeout: 15000, polling: 60}).then(() => true).catch(() => false);
  check(duringSpeech, 'the note is opened and the camera is already flying WHILE the answer is still being spoken');

  // let the flight finish
  await page.waitForFunction(() => {
    const g = window.__alfred;
    const dest = g.lastFlyTo && g.lastFlyTo();
    if (!dest) return false;
    const cam = g.graph.camera().position;
    return Math.hypot(cam.x - dest.pos.x, cam.y - dest.pos.y, cam.z - dest.pos.z) < 2;
  }, {timeout: 15000, polling: 150}).catch(() => {});
  await sleep(900);

  const after = await state();
  check(!!after.plan && after.plan.kind === 'single', 'the galaxy judged this a single-source answer');
  check(moved(before, after) > 1, 'the camera moved: ' + neat(before) + ' -> ' + neat(after) +
        ' (' + Math.round(moved(before, after)) + ' units)');
  check(after.panelOpen, 'that note\'s side panel is open');
  check(!!after.panelLabel, 'and it names the note the answer came from: "' + after.panelLabel + '"');
  check(after.lit.length > 1, 'the note and its ' + (after.lit.length - 1) + ' neighbours are lit: [' + after.lit.join(', ') + ']');
  check(/Budget for the Move/i.test(after.panelLabel), 'which is the note that actually holds the budget');
  check(after.footer.includes('from 1 note'), 'the answer says "from 1 note": "' + after.footer + '"');
  check(after.spoken.length === 1 && after.spoken[0] === ANSWER, 'the answer was spoken, once, and it is the answer');
  check(after.excerpt.length > 40 && !after.spoken.join(' ').includes(after.excerpt.slice(0, 40)),
        'the note itself is never read aloud (it starts "' + after.excerpt.slice(0, 28).trim() + '...")');
  await page.screenshot({path: path.join(OUT, 'provenance-fly.jpg'), type: 'jpeg', quality: 86});
  ok('screenshot: ' + rel(path.join(OUT, 'provenance-fly.jpg')));
}

/* ================================================ 2. four or more: the cluster */
console.log('\n2. "what should I pack and prepare before moving, and what is the budget?"  ->  a cluster');
{
  await open();
  const before = await askByTyping('what should I pack and prepare before moving, and what is the budget?');
  const after = await state();

  check(!!after.plan && after.plan.kind === 'cluster', 'the galaxy judged this a cluster answer from ' +
        (after.plan ? after.plan.nodes.length : 0) + ' notes');
  check(after.plan && after.plan.nodes.length >= 4, 'four or more notes, so: no flight');
  check(after.flyTo === null, 'no camera flight was started at all');
  check(moved(before, after) < 1, 'the camera did not move: ' + neat(after) + ' (unchanged)');
  check(!after.panelOpen, 'no panel opened - a panel can only name one note');
  check(after.lit.length >= 4, 'the whole cluster is lit instead: [' + after.lit.join(', ') + ']');
  check(after.litLinks > 0, 'with the links between those notes lit (' + after.litLinks + ')');
  check(after.footer.includes('from ' + (after.plan ? after.plan.nodes.length : 0) + ' notes'),
        'the answer says how many notes it came from: "' + after.footer + '"');
  await page.screenshot({path: path.join(OUT, 'provenance-cluster.jpg'), type: 'jpeg', quality: 86});
  ok('screenshot: ' + rel(path.join(OUT, 'provenance-cluster.jpg')));
}

/* ================================================== 3. small talk: hold still */
console.log('\n3. "good morning"  ->  not a question about the notes, so nothing moves');
{
  await open();
  const before = await askByTyping('good morning');
  const after = await state();

  check(!!after.plan && after.plan.kind === 'still', 'the galaxy judged this small talk: "' +
        (after.plan ? after.plan.why : '') + '"');
  check(after.flyTo === null, 'no camera flight');
  check(moved(before, after) < 1, 'the camera is exactly where it was: ' + neat(after));
  check(after.lit.length === 0, 'nothing was lit up');
  check(after.litLinks === 0, 'no links were lit either');
  check(!after.panelOpen, 'and no panel was opened');
  check(after.answer.trim() === CHAT_ANSWER, 'the answer still arrives, and it is small talk: "' +
        after.answer.trim() + '"');
  check(after.spoken.length === 1 && after.spoken[0] === CHAT_ANSWER, 'and is still spoken, once');
  check(after.footer.includes('no notes used'), 'the answer says no notes were used: "' + after.footer + '"');
  await page.screenshot({path: path.join(OUT, 'provenance-still.jpg'), type: 'jpeg', quality: 86});
  ok('screenshot: ' + rel(path.join(OUT, 'provenance-still.jpg')));
}

/* ============================================ what the model was actually sent */
console.log('\nwhat the model was sent, question by question');
{
  const noteAsking = prompts.filter(p => JSON.stringify(p).includes("Notes from the user's knowledge galaxy"));
  const chatty = prompts.filter(p => JSON.stringify(p).includes('ONE short, friendly sentence'));
  check(prompts.length === 3, 'three questions reached the model (' + prompts.length + ')');
  check(noteAsking.length === 2, 'the two real questions carried note excerpts (' + noteAsking.length + ')');
  check(chatty.length === 1, 'the greeting carried no excerpts and used the small-talk prompt instead');
  check(!JSON.stringify(chatty[0] || {}).includes("Notes from the user's knowledge galaxy"),
        'nothing in the greeting\'s prompt could have come from a note');
}

check(pageErrors.length === 0, 'no uncaught page errors' +
      (pageErrors.length ? ' -> ' + pageErrors.slice(0, 2).join(' | ') : ''));

await browser.close();
if (!KEEP) await cleanup();

const fails = results.filter(([s]) => s === 'FAIL');
console.log('\n  %d checks, %d passed, %d failed', results.length, results.length - fails.length, fails.length);
if (fails.length){
  console.log('\n  RESULT: FAILED');
  fails.forEach(([, m]) => console.log('   - ' + m));
  process.exit(1);
}
console.log('\n  RESULT: PASSED - one note flies, four or more light the cluster, and small talk moves nothing');
