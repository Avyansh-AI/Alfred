#!/usr/bin/env node
/*
 * tools/browser-brain-check.mjs - changing the brain, in a real browser, against the
 * real server, with the OpenRouter route forced to prove itself.
 *
 * The trick that makes this check worth having: the server's OpenAI base URL points at
 * a port where NOTHING is listening. The only way any answer can come back at all is
 * through the OpenRouter route, so "the swap really took effect" is not a claim about
 * a label on screen - it is the only possible explanation for the answer arriving.
 *
 *   1. the chip   -> the config brain, on screen, from the server's own label
 *   2. "switch to fable 5.1" -> the chip changes, "until restart" appears, the line is
 *      shown and spoken
 *   3. ask a question -> it is answered by the SWAPPED model, by the OpenRouter route,
 *      and the answer footer names the brain that answered
 *   4. "switch to opus 5" -> the refusal: shown, spoken, styled as a refusal, chip
 *      unchanged, and no model call spent on it
 *   5. "go back to your normal brain" -> the config brain, back in the chair
 *   6. restart the server -> the page comes up on the config brain again, because a
 *      swap is runtime only and config.json was never written to
 *
 *   node tools/browser-brain-check.mjs
 *   node tools/browser-brain-check.mjs --out tools/screenshots --keep
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

/* ------------------------------------------------- stands in for OpenRouter */
/* It records every call: the id it was asked for, and the key it was given. That is
 * how "the swap reached the wire" is checked, rather than believed. */
const requests = [];
const stub = http.createServer((req, res) => {
  let body = '';
  req.on('data', (c) => { body += c; });
  req.on('end', () => {
    let parsed = {};
    try { parsed = JSON.parse(body); } catch (err) { /* answer anyway */ }
    const messages = parsed.messages || [];
    const last = messages[messages.length - 1] || {};
    const asked = Array.isArray(last.content)
      ? ((last.content.find((p) => p && p.type === 'text') || {}).text || '')
      : String(last.content || '');
    requests.push({model: parsed.model, auth: req.headers.authorization,
                   title: req.headers['x-title'], path: req.url, at: Date.now()});
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify({
      id: 'chatcmpl-stub', object: 'chat.completion', model: parsed.model || 'stub-model',
      choices: [{index: 0, finish_reason: 'stop', message: {role: 'assistant',
        content: 'The movers quoted 26,000 including insurance, sir - said ' +
                 parsed.model + ' to the question "' + asked + '".'}}],
      usage: {prompt_tokens: 10, completion_tokens: 5, total_tokens: 15}
    }));
  });
});
await new Promise((resolve) => stub.listen(0, '127.0.0.1', resolve));
const STUB_PORT = stub.address().port;

/* --------------------------------------------------------------- the server */
const PORT = await freePort();
const DEAD_PORT = await freePort();          // nothing listens here: the OpenAI route
const cfg = path.join(os.tmpdir(), 'alfred-brain-' + process.pid + '.json');
const CFG_TEXT = JSON.stringify({openai_api_key: 'sk-browser-brain-check',
                                 model: 'gpt-6-astra'}, null, 2) + '\n';
fs.writeFileSync(cfg, CFG_TEXT);
let server = null, serverLog = '';
function startServer(){
  server = spawn(PY, ['-u', path.join(ROOT, 'server.py'), '--port', String(PORT), '--host', '127.0.0.1',
                      '--config', cfg, '--notes', path.join(ROOT, 'notes'), '--root', path.join(ROOT, 'viewer'),
                      '--openai-base-url', 'http://127.0.0.1:' + DEAD_PORT + '/v1',
                      '--openrouter-base-url', 'http://127.0.0.1:' + STUB_PORT + '/v1'],
                 {cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe']});
  serverLog = '';
  server.stdout.on('data', (d) => { serverLog += d; });
  server.stderr.on('data', (d) => { serverLog += d; });
}
startServer();

const cleanup = async () => {
  try { if (server) server.kill('SIGTERM'); } catch (err) { /* gone */ }
  try { stub.close(); } catch (err) { /* gone */ }
  try { if (!KEEP) fs.unlinkSync(cfg); } catch (err) { /* gone */ }
};
process.on('exit', () => { try { if (server) server.kill('SIGKILL'); } catch (err) {} });

async function waitForHealth(timeoutMs = 15000){
  for (let i = 0; i < Math.ceil(timeoutMs / 150); i++){
    try {
      const r = await fetch('http://127.0.0.1:' + PORT + '/health');
      if (r.ok) return await r.json();
    } catch (err) { /* not up yet */ }
    await sleep(150);
  }
  return null;
}
let health = await waitForHealth();
if (!health){
  console.error('\nbrowser-brain-check: the server never came up on port ' + PORT);
  console.error(serverLog.slice(-1200));
  await cleanup();
  process.exit(2);
}
fs.mkdirSync(OUT, {recursive: true});

/* ---------------------------------------------------------- what we inject */
const INJECT = () => {
  window.__speech = {texts: []};
  const synth = window.speechSynthesis;
  if (!synth) return;
  const realSpeak = synth.speak.bind(synth);
  synth.speak = (u) => {
    window.__speech.texts.push(u && u.text ? u.text : '');
    try { if (u.onstart) u.onstart(); } catch (e) {}
    setTimeout(() => { try { if (u.onend) u.onend(); } catch (e) {} }, 600);
    return realSpeak(u);
  };
  synth.cancel = () => {};
  synth.getVoices = () => [{name: 'Daniel', lang: 'en-GB'}];
};

console.log('\nAlfred - changing the brain in a real browser');
console.log('  server : real server.py on port ' + PORT);
console.log('  openai : ' + DEAD_PORT + '  (nothing listens there - the route is dead on purpose)');
console.log('  openrouter: stubbed on port ' + STUB_PORT + '  (stands in for the real thing)');

const launched = await launchBrowser({args: ['--window-size=1500,900']});
if (launched.error === 'no-puppeteer'){ noPuppeteerMessage(); await cleanup(); process.exit(2); }
if (launched.error){
  console.error('browser-brain-check: could not launch a browser: ' + launched.message);
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
page.on('console', (m) => {
  if (m.type() !== 'error') return;
  const text = m.text();
  if (/ERR_CONNECTION_CLOSED|ERR_NAME_NOT_RESOLVED|Failed to load resource/.test(text)) return;
  pageErrors.push('console: ' + text);
});

const URL_ = 'http://127.0.0.1:' + PORT + '/';
async function open(){
  await page.goto(URL_, {waitUntil: 'domcontentloaded', timeout: 45000});
  await page.waitForFunction(() => window.__alfred && window.__alfred.selfTest().ok, {timeout: 45000, polling: 400});
  await sleep(700);
}
const state = () => page.evaluate(() => {
  const g = window.__alfred;
  const chip = g.brain.chip();
  return {
    chip, catalog: g.brain.catalog(),
    answer: document.getElementById('answer-text').textContent,
    answerClass: document.getElementById('answer-text').className,
    model: document.getElementById('a-model').textContent,
    footer: document.getElementById('a-foot').textContent,
    spoken: (window.__speech.texts || []).slice(),
    q: document.getElementById('q').value
  };
});

/* say something and wait for a NEW answer: the line already on screen belongs to the
   previous turn, so this one has arrived when that line has changed */
async function say(text){
  const before = await page.$eval('#answer-text', (el) => el.textContent);
  await page.click('#q');
  await page.type('#q', text, {delay: 3});
  await page.keyboard.press('Enter');
  await page.waitForFunction((was) => {
    const el = document.getElementById('answer-text');
    return el && el.textContent !== was && el.textContent.trim().length > 5 &&
           !el.classList.contains('thinking');
  }, {timeout: 30000, polling: 100}, before);
  await sleep(500);
}

/* =========================================================================
   1. the chip: the config brain, and every word of it from the server
   ========================================================================= */
console.log('\n1. opening the page  ->  the chip names the brain');
await open();
{
  const s = await state();
  check(s.chip.label === 'GPT 6 ASTRA', 'the chip reads the server\'s label: ' + s.chip.label);
  check(s.chip.swapped === false, 'nothing is swapped at boot');
  check(s.chip.untilRestart === false, 'so there is no "until restart" on it');
  check(s.chip.title.indexOf('gpt-6-astra') >= 0,
        'and the tooltip carries the real id: ' + s.chip.title);
  check(s.catalog.some(b => b.name === 'opus' && b.versions.indexOf('4.1') >= 0),
        'the catalogue reached the page: ' + s.catalog.map(b => b.name).join(', '));
}

/* =========================================================================
   2. switch to a real model
   ========================================================================= */
console.log('\n2. "switch to fable 5.1"  ->  a different brain in the chair');
{
  const before = requests.length;
  await say('switch to fable 5.1');
  const s = await state();
  check(s.chip.label === 'CLAUDE FABLE 5.1', 'the chip changed: ' + s.chip.label);
  check(s.chip.swapped === true, 'and is marked as a runtime swap');
  check(s.chip.untilRestart === true, 'with "until restart" on it, in plain sight');
  check(/CLAUDE FABLE 5\.1 is in the chair/.test(s.answer), 'he says so on screen: ' +
        s.answer.slice(0, 90));
  check(s.spoken.join(' ').indexOf('CLAUDE FABLE 5.1 is in the chair') >= 0,
        'and out loud');
  check(s.footer.indexOf('swapped until the next restart') >= 0,
        'the footer says how long it lasts: ' + s.footer);
  check(s.model.indexOf('anthropic/claude-fable-5.1') >= 0,
        'and the real id is on the card: ' + s.model);
  check(requests.length === before, 'changing the brain cost no model call');
  check(s.q === '', 'the ask bar is free again');
  await page.screenshot({path: path.join(OUT, 'brain-switched.jpg'), type: 'jpeg', quality: 88});
  ok('screenshot: ' + rel(path.join(OUT, 'brain-switched.jpg')) + '  (the chip on the new brain)');
}

/* =========================================================================
   3. the next question really is answered by the new brain, on the new route
   ========================================================================= */
console.log('\n3. asking a question  ->  answered by the swapped brain, over OpenRouter');
{
  const before = requests.length;
  await say('what did the movers quote for the road trip?');
  const s = await state();
  check(requests.length === before + 1, 'the model was asked exactly once');
  const sent = requests[requests.length - 1];
  check(sent.model === 'anthropic/claude-fable-5.1',
        'AND THE WIRE SAYS SO: OpenRouter was asked for ' + sent.model);
  check(sent.path.indexOf('/chat/completions') >= 0,
        'on the chat completions path: ' + sent.path);
  check(sent.auth === 'Bearer sk-browser-brain-check',
        'carrying the key from config.json - one key, any model');
  check(sent.title === 'Alfred - knowledge galaxy', 'and naming the app to OpenRouter');
  check(/26,000/.test(s.answer), 'the answer arrived, so the OpenRouter route is live: ' +
        s.answer.slice(0, 80));
  check(s.model.indexOf('anthropic/claude-fable-5.1') >= 0,
        'and the card names the brain that answered: ' + s.model);
  await page.screenshot({path: path.join(OUT, 'brain-answering.jpg'), type: 'jpeg', quality: 88});
  ok('screenshot: ' + rel(path.join(OUT, 'brain-answering.jpg')) + '  (an answer from the new brain)');
}

/* =========================================================================
   4. the refusal: a version that does not exist
   ========================================================================= */
console.log('\n4. "switch to opus 5"  ->  refused, out loud, nothing changed');
{
  const before = requests.length;
  await say('switch to opus 5');
  const s = await state();
  check(/^There is no opus 5/.test(s.answer), 'the refusal is on screen: ' + s.answer.slice(0, 90));
  check(/opus 4\.1 and opus 4/.test(s.answer), 'and it names what he does have');
  check(s.spoken.join(' ').indexOf('There is no opus 5') >= 0, 'and it is spoken');
  check(s.answerClass.indexOf('refused') >= 0,
        'the card is styled as a refusal: ' + s.answerClass);
  check(s.footer.indexOf('NOT CHANGED') >= 0, 'the footer says NOT CHANGED: ' + s.footer);
  check(s.chip.label === 'CLAUDE FABLE 5.1', 'THE CHIP DID NOT MOVE: ' + s.chip.label);
  check(s.chip.swapped === true, 'the swap that was in the chair is still in the chair');
  check(requests.length === before, 'and a refusal spent no model call anywhere');
  const h = await (await fetch('http://127.0.0.1:' + PORT + '/health')).json();
  check(h.model === 'anthropic/claude-fable-5.1' && h.swapped === true,
        'the server agrees: still ' + h.model);
  await page.screenshot({path: path.join(OUT, 'brain-refused.jpg'), type: 'jpeg', quality: 88});
  ok('screenshot: ' + rel(path.join(OUT, 'brain-refused.jpg')) + '  (the refusal, not a guess)');
}

/* =========================================================================
   5. a family on its own, and back to the config brain
   ========================================================================= */
console.log('\n5. "switch to opus"  ->  asked which one, then "normal brain"  ->  the config brain');
{
  await say('switch to opus');
  const s = await state();
  check(/^Which opus/.test(s.answer), 'a bare family is a question, not a pick: ' + s.answer.slice(0, 80));
  check(s.chip.label === 'CLAUDE FABLE 5.1', 'and the chip still has not moved');
  await say('go back to your normal brain');
  const back = await state();
  check(back.chip.label === 'GPT 6 ASTRA', 'back on the config brain: ' + back.chip.label);
  check(back.chip.swapped === false, 'and marked as not swapped again');
  check(back.chip.untilRestart === false, 'so "until restart" is gone');
  check(/Back on GPT 6 ASTRA/.test(back.answer), 'and he says so');
}

/* =========================================================================
   6. a restart comes back on the config brain
   ========================================================================= */
console.log('\n6. restarting the server  ->  the config brain, and config.json untouched');
{
  await say('switch to fable 5.1');
  const swapped = await state();
  check(swapped.chip.label === 'CLAUDE FABLE 5.1', 'the page is on the swapped brain before the restart');
  check(fs.readFileSync(cfg, 'utf8') === CFG_TEXT,
        'and config.json on disk is still exactly as it was written');

  try { server.kill('SIGTERM'); } catch (err) { /* gone */ }
  await sleep(600);
  startServer();
  const h = await waitForHealth();
  check(!!h, 'the restarted server came up');
  check(h && h.model === 'gpt-6-astra', 'A RESTART FORGETS THE SWAP: it reports ' + (h || {}).model);
  check(h && h.swapped === false, 'and says it is not swapped');
  check(h && h.config_model === 'gpt-6-astra', 'because config.json is what it reads: ' + (h || {}).config_model);

  await open();                                 // the same page, reloaded
  const s = await state();
  check(s.chip.label === 'GPT 6 ASTRA', 'and the page shows the config brain again: ' + s.chip.label);
  check(s.chip.swapped === false, 'with no swap marker on the chip');
}

check(pageErrors.length === 0,
      'no uncaught page errors' + (pageErrors.length ? ': ' + pageErrors.slice(0, 2).join(' | ') : ''));

await page.close();
await browser.close();
await cleanup();

console.log('\nAlfred - changing the brain (real browser, real server)');
const fails = results.filter(r => r[0] === 'FAIL');
if (fails.length){
  console.log('\n  RESULT: FAILED (' + fails.length + ' of ' + results.length + ')');
  fails.forEach(f => console.log('   - ' + f[1]));
  process.exit(1);
}
console.log('\n  ' + results.length + ' checks, ' + results.length + ' passed, 0 failed');
console.log('\n  RESULT: PASSED - the swap reaches the wire over OpenRouter, a version that does\n' +
            '  not exist is refused instead of guessed at, and a restart goes back to the\n' +
            '  brain in config.json');
