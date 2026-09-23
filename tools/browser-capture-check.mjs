#!/usr/bin/env node
/*
 * tools/browser-capture-check.mjs - "I grew the brain by voice", in a real browser.
 *
 * This is the test to run by hand, automated: the REAL server.py writes a REAL file
 * into a throwaway copy of the notes folder, the REAL page adds the star to the REAL
 * running galaxy, and the follow-up question is answered from the note that was just
 * filed. Only the model is stubbed (there is no API key here) and only the speech
 * engine is spied on (a headless browser has no speakers).
 *
 *   1. type  "remember that the finish window should be 900 milliseconds"
 *      -> the file lands in notes/captures/, the star is born beside the note it is
 *         most related to, it glows, and the camera goes to it
 *   2. ask   "what is the finish window in milliseconds?"
 *      -> answered from the note that was written a second earlier, camera and all,
 *         with no build.py and no reload
 *   3. a capture that cannot be written -> said out loud, in a second server whose
 *      notes folder has something sitting where the captures folder should be
 *
 *   node tools/browser-capture-check.mjs
 *   node tools/browser-capture-check.mjs --out tools/screenshots --keep
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
const getJSON = async (url) => (await fetch(url)).json();
const postJSON = async (url, payload) => {
  const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                              body: JSON.stringify(payload)});
  let body = {};
  try { body = await r.json(); } catch (err) { /* not JSON */ }
  return {status: r.status, body};
};

/* ------------------------------------------------------------------ the model */
// The stub answers from whatever it was sent, so the answer to the follow-up question
// can only exist if the note really was in the prompt.
const stub = http.createServer((req, res) => {
  let body = '';
  req.on('data', (c) => { body += c; });
  req.on('end', () => {
    let parsed = {};
    try { parsed = JSON.parse(body); } catch (err) { /* not JSON: answer anyway */ }
    const sent = JSON.stringify(parsed.messages || []);
    const answer = sent.includes('900 milliseconds')
      ? 'Nine hundred milliseconds, sir: the window is generous by design.'
      : 'I have nothing on that, sir.';
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify({
      id: 'chatcmpl-stub', object: 'chat.completion', model: parsed.model || 'stub-model',
      choices: [{index: 0, message: {role: 'assistant', content: answer}, finish_reason: 'stop'}],
      usage: {prompt_tokens: 10, completion_tokens: 5, total_tokens: 15}
    }));
  });
});
await new Promise((resolve) => stub.listen(0, '127.0.0.1', resolve));
const STUB_PORT = stub.address().port;

/* -------------------------------------------- a throwaway copy of the real vault */
const workdir = fs.mkdtempSync(path.join(os.tmpdir(), 'alfred-capture-'));
const vault = path.join(workdir, 'notes');
fs.cpSync(path.join(ROOT, 'notes'), vault, {recursive: true});
const graphCopy = path.join(workdir, 'graph-data.js');
fs.copyFileSync(path.join(ROOT, 'viewer', 'graph-data.js'), graphCopy);
const cfg = path.join(workdir, 'config.json');
fs.writeFileSync(cfg, JSON.stringify({openai_api_key: 'sk-stub-key-for-verification', model: 'stub-model'}));

const startServer = async (port, notes, graph) => {
  const proc = spawn(PY, ['-u', path.join(ROOT, 'server.py'), '--port', String(port), '--host', '127.0.0.1',
                          '--config', cfg, '--notes', notes, '--graph', graph, '--root', path.join(ROOT, 'viewer'),
                          '--openai-base-url', 'http://127.0.0.1:' + STUB_PORT + '/v1'],
                     {cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe']});
  let log = '';
  proc.stdout.on('data', (d) => { log += d; });
  proc.stderr.on('data', (d) => { log += d; });
  for (let i = 0; i < 120; i++){
    try { if ((await fetch(`http://127.0.0.1:${port}/health`)).ok) return {proc, log: () => log}; } catch (err) {}
    await sleep(150);
  }
  throw new Error('server did not come up on ' + port + '\n' + log.slice(-800));
};

const serverPort = await freePort();
const server = await startServer(serverPort, vault, graphCopy);
const URL_ = `http://127.0.0.1:${serverPort}/`;

// a second server whose notes folder is sabotaged: a FILE where captures/ should be
const badDir = fs.mkdtempSync(path.join(os.tmpdir(), 'alfred-nocapture-'));
const badVault = path.join(badDir, 'notes');
fs.mkdirSync(path.join(badVault, 'home'), {recursive: true});
fs.writeFileSync(path.join(badVault, 'home', 'note.md'), '# Note\n\nSomething worth remembering.\n');
fs.writeFileSync(path.join(badVault, 'captures'), 'not a folder - this is what makes the write fail\n');
const badPort = await freePort();
const badServer = await startServer(badPort, badVault, path.join(badDir, 'no-graph-here.js'));

const cleanup = () => {
  try { server.proc.kill('SIGTERM'); } catch (err) {}
  try { badServer.proc.kill('SIGTERM'); } catch (err) {}
  try { stub.close(); } catch (err) {}
  if (!KEEP){
    try { fs.rmSync(workdir, {recursive: true, force: true}); } catch (err) {}
    try { fs.rmSync(badDir, {recursive: true, force: true}); } catch (err) {}
  }
};
process.on('exit', cleanup);

/* ------------------------------------------------------------- what we inject */
const INJECT = () => {
  window.__speech = {texts: [], state: 'idle'};
  const synth = window.speechSynthesis;
  if (!synth) return;
  const realSpeak = synth.speak.bind(synth);
  synth.speak = (u) => {
    window.__speech.texts.push(u && u.text ? u.text : '');
    window.__speech.state = 'speaking';
    try { if (u.onstart) u.onstart(); } catch (e) {}
    setTimeout(() => {
      window.__speech.state = 'done';
      try { if (u.onend) u.onend(); } catch (e) {}
    }, 1500);
    return realSpeak(u);
  };
  synth.cancel = () => {};
  synth.getVoices = () => [{name: 'Daniel', lang: 'en-GB'}];
};

fs.mkdirSync(OUT, {recursive: true});
console.log('\nAlfred - growing the brain by voice, in a real browser');
console.log('  server : real server.py on port ' + serverPort + ' (real notes copy, real files)');
console.log('  model  : stubbed OpenAI on port ' + STUB_PORT);
console.log('  vault  : ' + vault);

const launched = await launchBrowser({args: ['--window-size=1500,900']});
if (launched.error === 'no-puppeteer'){ noPuppeteerMessage(); cleanup(); process.exit(2); }
if (launched.error){
  console.error('browser-capture-check: could not launch a browser: ' + launched.message);
  cleanup();
  process.exit(2);
}
const { browser } = launched;
console.log('  browser: ' + await browser.version());

const page = await browser.newPage();
await page.setViewport({width: 1500, height: 900, deviceScaleFactor: 1});
await page.evaluateOnNewDocument(INJECT);
const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push(String(e)));

/* Type into the ask bar and press Enter, then look at the screen while it happens. */
const typeAndSend = async (text, waitMs = 260) => {
  await page.$eval('#q', (el) => { el.value = ''; });
  await page.click('#q');
  await page.type('#q', text, {delay: 3});
  await page.keyboard.press('Enter');
  await sleep(waitMs);
};

const state = () => page.evaluate(() => {
  const g = window.__alfred;
  const c = g.graph.camera().position;
  const fly = g.lastFlyTo();
  const birth = g.capture.lastBirth();
  return {
    camera: {x: c.x, y: c.y, z: c.z},
    flyTo: fly ? {x: fly.pos.x, y: fly.pos.y, z: fly.pos.z, node: fly.node} : null,
    nodes: g.capture.nodes(),
    links: g.capture.links(),
    birth: birth ? {id: birth.id, label: birth.label, anchor: birth.anchor, at: birth.at,
                     pulseMs: birth.pulseMs, pulseEndedAt: birth.pulseEndedAt, flewAt: birth.flewAt,
                     file: birth.file} : null,
    panelOpen: g.provenance.panelOpen(),
    panelLabel: g.provenance.panelLabel(),
    lit: g.provenance.lit(),
    stats: g.capture.stats(),
    answer: document.getElementById('answer-text').textContent,
    answerClass: document.getElementById('answer-text').className,
    footer: document.getElementById('a-foot').textContent,
    spoken: (window.__speech ? window.__speech.texts : []).filter(t => t && t.trim())
  };
});
const moved = (a, b) => Math.hypot(a.camera.x - b.camera.x, a.camera.y - b.camera.y, a.camera.z - b.camera.z);

async function open(url){
  await page.goto(url, {waitUntil: 'domcontentloaded', timeout: 45000});
  await page.waitForFunction(() => window.__alfred && window.__alfred.selfTest().ok, {timeout: 45000, polling: 400});
  await sleep(800);
}

/* =========================================================================
   1. the test you would do by hand: say it, and watch a star arrive
   ========================================================================= */
console.log('\n1. "remember that the finish window should be 900 milliseconds"');
{
  await open(URL_);
  const before = await state();
  check(before.nodes.length === 12, 'the galaxy starts with the 12 real notes');
  check(!before.nodes.some(n => /Finish Window/.test(n.label)), 'and nothing called "The Finish Window" yet');

  const sentence = 'remember that the finish window should be 900 milliseconds';
  await page.click('#q');
  await page.type('#q', sentence, {delay: 3});
  await page.keyboard.press('Enter');

  // Sampled the instant the birth is recorded, in ONE frame: the anchor's live
  // position and the new star's live position, read together. This is the claim
  // "born at the position of its most related existing node", measured where it
  // happens rather than after the force layout has had a second to move it.
  const birthHandle = await page.waitForFunction(() => {
    const g = window.__alfred;
    const s = g.capture.lastSpawn();
    if (!s) return null;
    const nodes = g.capture.nodes();
    const born = nodes.find(n => n.id === s.id);
    const anchor = s.anchorId == null ? born : nodes.find(n => n.id === s.anchorId);
    if (!born || !anchor) return null;
    return {id: s.id, label: s.label, anchorId: s.anchorId, anchorLabel: s.anchorLabel,
            // the star's own position and its parent's, read together in this frame
            anchorPos: {x: anchor.x, y: anchor.y, z: anchor.z},
            bornPos: {x: born.x, y: born.y, z: born.z},
            spawnPos: s.spawnPos, distance: s.distance, at: s.at, links: s.links};
  }, {timeout: 30000, polling: 'raf'});
  const atBirth = await birthHandle.jsonValue();
  // the birth, caught mid-glow: the halo is driven by real frames
  await sleep(300);
  await page.screenshot({path: path.join(OUT, 'capture-born.jpg'), type: 'jpeg', quality: 88});
  ok('screenshot: ' + rel(path.join(OUT, 'capture-born.jpg')) + '  (the new star, glowing)');

  await page.waitForFunction(() => {
    const b = window.__alfred.capture.lastBirth();
    return b && b.flewAt != null;
  }, {timeout: 30000, polling: 50});
  await sleep(2200);                     // the camera flight, then the pin released
  await page.screenshot({path: path.join(OUT, 'capture-flown.jpg'), type: 'jpeg', quality: 88});
  ok('screenshot: ' + rel(path.join(OUT, 'capture-flown.jpg')) + '  (the camera has gone to it)');

  const after = await state();
  const born = after.nodes.find(n => /Finish Window/.test(n.label));
  check(after.nodes.length === before.nodes.length + 1,
        'the running galaxy gained a star without a reload (' + before.nodes.length + ' -> ' + after.nodes.length + ')');
  check(!!born, 'its title comes from the first few words: "' + (born ? born.label : '') + '"');
  check(after.birth && after.birth.label === (born || {}).label, 'the page recorded the birth');

  check(atBirth.anchorId != null && atBirth.anchorLabel,
        'it was born beside the note it is most related to: "' + atBirth.anchorLabel + '"');
  check(atBirth.distance < 0.5, 'and at that note\'s own position: ' +
        Math.hypot(atBirth.bornPos.x - atBirth.anchorPos.x, atBirth.bornPos.y - atBirth.anchorPos.y,
                   atBirth.bornPos.z - atBirth.anchorPos.z).toFixed(4) + ' units apart');
  check(atBirth.anchorPos && Math.hypot(atBirth.anchorPos.x, atBirth.anchorPos.y, atBirth.anchorPos.z) > 1,
        'which is a real place in the galaxy, not the origin: ' +
        '(' + [atBirth.anchorPos.x, atBirth.anchorPos.y, atBirth.anchorPos.z].map(v => Math.round(v)).join(', ') + ')');
  check(after.birth.pulseMs === 1100, 'the glow has one named length: ' + after.birth.pulseMs + ' ms');
  check(after.birth.pulseEndedAt >= after.birth.pulseMs - 60,
        'it glowed for its full ' + after.birth.pulseMs + ' ms before anything moved');
  check(after.birth.flewAt >= after.birth.pulseEndedAt,
        'the camera flight started only after the glow (glow ended ' +
        Math.round(after.birth.pulseEndedAt) + ' ms, flight ' + Math.round(after.birth.flewAt) + ' ms)');
  check(!!after.flyTo && Math.abs(after.flyTo.node.x - atBirth.bornPos.x) < 0.5 &&
        Math.abs(after.flyTo.node.z - atBirth.bornPos.z) < 0.5,
        'and the flight was aimed at the new star');
  check(moved(before, after) > 20, 'the camera really moved: ' + Math.round(moved(before, after)) + ' units');
  check(after.panelOpen && after.panelLabel === born.label,
        'the new note opened in the side panel: ' + after.panelLabel);
  check(after.stats.indexOf(after.nodes.length + ' notes') === 0, 'the stats line counts it: ' + after.stats);

  // the file on disk: a real note in the real captures folder
  const relFile = after.birth.file;
  const onDisk = path.join(vault, relFile);
  check(fs.existsSync(onDisk), 'a real markdown file is on disk: notes/' + relFile);
  const text = fs.existsSync(onDisk) ? fs.readFileSync(onDisk, 'utf8') : '';
  const today = new Date().toISOString().slice(0, 10);
  check(text.startsWith('# ' + born.label), 'it opens with the title as a heading');
  check(text.includes(today), 'it carries today\'s date (' + today + ')');
  check(text.toLowerCase().includes('the finish window should be 900 milliseconds'),
        'and the words that were said');
  check(!fs.existsSync(path.join(ROOT, 'notes', 'captures')),
        'nothing was written into the project\'s own notes folder');

  // what the server says about its own galaxy
  const health = await getJSON(`http://127.0.0.1:${serverPort}/health`);
  check(health.notes === 13, '/health counts 13 notes now');
  check(health.titles[12] === born.label, 'the new note is index 12, last, exactly where the viewer put it');
  check(health.graph_file === 'behind' && (health.captures || []).length === 1,
        'and the server tells the viewer about it, because graph-data.js has not caught up');
  check(fs.readFileSync(graphCopy, 'utf8') === fs.readFileSync(path.join(ROOT, 'viewer', 'graph-data.js'), 'utf8'),
        'graph-data.js on disk is untouched: writing a note is not the same as re-indexing the graph file');

  const spoken = after.spoken.filter(t => t.indexOf('Filed and lit') === 0);
  check(spoken.length === 1, 'he said one line out loud, in character: "' + (spoken[0] || '') + '"');
  check(spoken[0] === after.answer, 'and it is exactly what is on screen');
}

/* =========================================================================
   2. the very next question, answered from the note that was just filed
   ========================================================================= */
console.log('\n2. "what is the finish window in milliseconds?"  ->  answered from the new note');
{
  const before = await state();
  check(before.nodes.filter(n => /Finish Window/.test(n.label)).length === 1,
        'one question later, there is still exactly one such note (the bar was empty: "' +
        before.answer.slice(0, 40) + '")');
  await typeAndSend('what is the finish window in milliseconds?', 400);
  let answered = true;
  try {
    await page.waitForFunction(() => {
      const t = document.getElementById('answer-text').textContent;
      return t.indexOf('Nine hundred') === 0;
    }, {timeout: 25000, polling: 100});
  } catch (err){
    answered = false;
    const seen = await page.$eval('#answer-text', (el) => el.textContent);
    bad('the follow-up question was not answered from the new note (screen said: "' +
        String(seen).slice(0, 120) + '")');
  }
  if (answered) ok('the follow-up question was answered from the note filed a moment earlier');
  await sleep(1600);
  await page.screenshot({path: path.join(OUT, 'capture-retrieved.jpg'), type: 'jpeg', quality: 88});
  ok('screenshot: ' + rel(path.join(OUT, 'capture-retrieved.jpg')) + '  (the answer, and where it came from)');

  const after = await state();
  const chips = await page.$$eval('#answer-sources .src', els => els.map(e => e.textContent.trim()));
  check(after.answer.indexOf('Nine hundred milliseconds') === 0,
        'the model answered from the note that was written a moment ago: "' + after.answer + '"');
  check(chips.length === 1 && chips[0].indexOf('Finish Window') >= 0,
        'the source chip names the new note: ' + JSON.stringify(chips));
  check(after.flyTo && after.flyTo.node && Math.abs(after.flyTo.node.x - before.nodes.find(n => /Finish Window/.test(n.label)).x) < 0.5,
        'and the camera flew to it as the source of this answer');
  check(after.lit.length >= 1, 'the galaxy lit it up (' + after.lit.length + ' node(s) lit)');
  check(before.stats.indexOf('13 notes') === 0 && after.stats.indexOf('13 notes') === 0,
        'no reload happened between the capture and the question');

  const health = await getJSON(`http://127.0.0.1:${serverPort}/health`);
  check(health.questions_asked >= 1, 'the question reached the same server that filed the note');
}

/* =========================================================================
   3. a capture that cannot be written is said out loud, not swallowed
   ========================================================================= */
console.log('\n3. a capture that cannot be written  ->  said out loud');
{
  await open(`http://127.0.0.1:${badPort}/`);
  const before = await state();
  const badHealth = await getJSON(`http://127.0.0.1:${badPort}/health`);
  check(badHealth.notes === 1 && (badHealth.captures || []).length === 0,
        'the sabotaged vault has exactly one note and nothing waiting to be indexed');
  check(fs.statSync(path.join(badVault, 'captures')).isFile(),
        'and a FILE sits where the captures folder should be - the write has to fail');
  await typeAndSend('remember that this one will not fit', 900);

  const after = await state();
  check(after.nodes.length === before.nodes.length, 'no star was invented for a note that does not exist (' +
        before.nodes.length + ' nodes before and after)');
  check(after.answerClass.indexOf('failed') >= 0, 'the screen shows a failure, not an answer');
  check(/did not go in/i.test(after.answer), 'and says plainly that it did not go in: "' +
        after.answer.split('.')[0] + '."');
  check(after.spoken.some(t => /did not go in/i.test(t)),
        'IT IS SPOKEN - a failed capture is never silent');
  check(moved(before, after) < 1, 'and the camera stayed exactly where it was');
  check(after.footer.length > 0, 'with a hint for fixing it: "' + after.footer + '"');
  const health = await getJSON(`http://127.0.0.1:${badPort}/health`);
  check(health.notes === 1, 'the server did not index anything either');
  await page.screenshot({path: path.join(OUT, 'capture-failed.jpg'), type: 'jpeg', quality: 88});
  ok('screenshot: ' + rel(path.join(OUT, 'capture-failed.jpg')));
}

/* =========================================================================
   4. a capture with real links, linked the way build.py links
   ========================================================================= */
console.log('\n4. a capture that mentions another note  ->  a real edge, as build.py draws it');
{
  await open(URL_);                       // back to the real vault, on the real server
  const beforeReload = await state();
  check(beforeReload.nodes.length === 13, 'the real galaxy still holds the note filed in step 1');
  const {body} = await postJSON(`http://127.0.0.1:${serverPort}/remember`,
                                {text: 'remember that the RML physiotherapy visit belongs with the Budget for the Move'});
  check(body.ok === true && (body.links || []).length === 1 &&
        (body.link_labels || []).join(' ').toLowerCase().indexOf('budget') >= 0,
        'a capture naming another note\'s title comes back linked to it: ' +
        JSON.stringify(body.links) + ' -> ' + JSON.stringify(body.link_labels));
  check(/joined to/.test(body.line) || /born beside/.test(body.line),
        'and the confirmation says where it landed: "' + body.line + '"');
  const src = fs.readFileSync(graphCopy, 'utf8');
  check(!src.includes(body.title), 'the graph file still has not been rewritten (that is build.py\'s job)');
  const health = await getJSON(`http://127.0.0.1:${serverPort}/health`);
  check(health.notes === 14 && (health.captures || []).length === 2,
        'both captures are waiting for the next build.py run (14 notes, ' +
        (health.captures || []).length + ' waiting)');

  // a reload must not lose either of them: the page is told about them by /health
  await page.reload({waitUntil: 'domcontentloaded'});
  await page.waitForFunction(() => window.__alfred && window.__alfred.selfTest().ok, {timeout: 45000, polling: 400});
  await page.waitForFunction(() => window.__alfred.capture.nodes().length >= 14, {timeout: 15000, polling: 200});
  await sleep(400);
  const after = await state();
  check(after.nodes.length === 14, 'a reload brings both captures back (' + after.nodes.length + ' nodes)');
  check(after.nodes.some(n => n.label === body.title), 'including the one just filed: "' + body.title + '"');
  check(after.birth === null, 'quietly - no glow and no camera flight for notes that are already there');
}

check(pageErrors.length === 0, 'no uncaught page errors' +
      (pageErrors.length ? ' -> ' + pageErrors.slice(0, 2).join(' | ') : ''));

await browser.close();
cleanup();

const fails = results.filter(([s]) => s === 'FAIL');
console.log('\n  %d checks, %d passed, %d failed', results.length, results.length - fails.length, fails.length);
if (fails.length){
  console.log('\n  RESULT: FAILED');
  fails.forEach(([, m]) => console.log('   - ' + m));
  process.exit(1);
}
console.log('\n  RESULT: PASSED - a note filed by voice becomes a real file, a real star, ' +
            'and the source of the next answer');
