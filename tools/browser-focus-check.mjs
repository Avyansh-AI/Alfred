#!/usr/bin/env node
/*
 * tools/browser-focus-check.mjs - focus sessions, in a real browser, against the real
 * server, with a scripted reader in place of the Mac.
 *
 * The live loop is the acceptance test for this feature, so it belongs in the repo rather
 * than in somebody's shell history: switch away, HEAR the callout, switch back, end the
 * session, HEAR the report. Everything here is real except the front-app reader, which is a
 * two-line shell script reading a file (that is the documented extension point, and it is
 * how the feature is verified on anything that is not a Mac).
 *
 *   1. FOCUS          -> the card appears, 30:00, on target, and the session is the server's
 *   2. wander off     -> the card tints, says "drifting", and he SPEAKS within three seconds
 *                        (timed, because "within three seconds" is the requirement)
 *   3. a deep link    -> moving around INSIDE the work site is not a drift (host, not URL)
 *   4. RELOAD         -> the tab rejoins the same session: same id, same clock, still running
 *   5. snooze         -> quiet, still counting
 *   6. home base      -> the galaxy's own tab is never a drift
 *   7. end            -> the report card, on the answer card, in the server's words
 *   8. the ledger     -> written to disk, aggregates only, with no name in it
 *   9. no identities  -> nothing on screen, in the page, or in the JSON mentions the app,
 *                        the host or the path the reader reported
 *
 *   node tools/browser-focus-check.mjs
 *   node tools/browser-focus-check.mjs --out tools/screenshots --keep
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import {spawn} from 'node:child_process';
import {launchBrowser, noPuppeteerMessage, ROOT} from './browser.mjs';

const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const i = argv.indexOf('--' + name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback;
};
/* the speech log has to be still for this long before the stopwatch is armed - the start
 * announcement arrives in pieces, and a piece that lands after the snapshot looks exactly
 * like a callout that beat the grace */
const QUIET_MS = 500;
const OUT = path.resolve(arg('out', path.join(ROOT, 'tools', 'screenshots')));
const KEEP = argv.includes('--keep');
const PY = process.env.PYTHON || 'python3';

/* the one number a person actually feels on the first drift: from the write that moved us to
 * the moment the line reached the speech engine. It is NOT the grace, and the check no longer
 * pretends it is - see the floor assertions below. */
let firstCalloutMs = null;
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

/* ------------------------------------------------------ the scripted reader
 * The documented contract: line 1 is the frontmost app, line 2 the active tab's URL, a
 * non-zero exit or a blank answer is a failed read. This one reads a file, so "switch to
 * another site" is a write - and the file is the only place an identity lives. */
const WORKDIR = fs.mkdtempSync(path.join(os.tmpdir(), 'alfred-focus-browser-'));
const FRONT = path.join(WORKDIR, 'front.txt');
const READER = path.join(WORKDIR, 'reader.sh');
const LEDGER = path.join(WORKDIR, 'focus-ledger.json');
fs.writeFileSync(READER, '#!/bin/sh\ncat "' + FRONT + '"\n');
fs.chmodSync(READER, 0o755);
const WORK_SITE = 'https://work.example.com/board/42';
const DEEP_LINK = 'https://work.example.com/board/42/card/9911?tab=history';
const OTHER_SITE = 'https://news.example.com/top';
const write = (app, url = '') => fs.writeFileSync(FRONT, app + '\n' + url + '\n');

/* --------------------------------------------------------------- the server */
const PORT = await freePort();
const cfg = path.join(WORKDIR, 'config.json');
fs.writeFileSync(cfg, JSON.stringify({openai_api_key: 'sk-browser-focus-check',
                                      model: 'gpt-6-astra'}, null, 2) + '\n');
let server = null, serverLog = '';
function startServer(){
  server = spawn(PY, ['-u', path.join(ROOT, 'server.py'), '--port', String(PORT),
                      '--host', '127.0.0.1', '--config', cfg,
                      '--notes', path.join(ROOT, 'notes'), '--root', path.join(ROOT, 'viewer'),
                      '--focus-reader', READER, '--focus-ledger', LEDGER,
                      '--focus-home-host', '127.0.0.1'],
                 {cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe']});
  serverLog = '';
  server.stdout.on('data', (d) => { serverLog += d; });
  server.stderr.on('data', (d) => { serverLog += d; });
}
const cleanup = async () => {
  try { if (server) server.kill('SIGTERM'); } catch (err) { /* gone */ }
  if (!KEEP){
    try { fs.rmSync(WORKDIR, {recursive: true, force: true}); } catch (err) { /* gone */ }
  } else {
    console.log('  kept: ' + WORKDIR);
  }
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

write('com.google.Chrome', WORK_SITE);          // the work site is where we start
startServer();
const health = await waitForHealth();
if (!health){
  console.error('\nbrowser-focus-check: the server never came up on port ' + PORT);
  console.error(serverLog.slice(-1200));
  await cleanup();
  process.exit(2);
}
fs.mkdirSync(OUT, {recursive: true});

const post = async (payload) => {
  const r = await fetch('http://127.0.0.1:' + PORT + '/focus', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)});
  let body = {};
  try { body = await r.json(); } catch (err) { /* reported below */ }
  return {status: r.status, body};
};
const state = async () => (await fetch('http://127.0.0.1:' + PORT + '/focus')).json();

/* ---------------------------------------------------------- what we inject
 * A spy on the speech engine: "he says something" has to be checkable, and the callout
 * must be SPOKEN, not merely written on the card. */
const INJECT = () => {
  window.__speech = {texts: []};
  const synth = window.speechSynthesis;
  if (!synth) return;
  const realSpeak = synth.speak.bind(synth);
  synth.speak = (u) => {
    window.__speech.texts.push(u && u.text ? u.text : '');
    try { if (u.onstart) u.onstart(); } catch (e) {}
    setTimeout(() => { try { if (u.onend) u.onend(); } catch (e) {} }, 250);
    return realSpeak(u);
  };
  synth.cancel = () => {};
  synth.getVoices = () => [{name: 'Daniel', lang: 'en-GB'}];
};

console.log('\nAlfred - focus sessions in a real browser');
console.log('  server : real server.py on port ' + PORT);
console.log('  reader : ' + rel(READER) + '  (a shell script reading ' + rel(FRONT) + ')');
console.log('  ledger : ' + rel(LEDGER));

const launched = await launchBrowser({args: ['--window-size=1500,900']});
if (launched.error === 'no-puppeteer'){ noPuppeteerMessage(); await cleanup(); process.exit(2); }
if (launched.error){
  console.error('browser-focus-check: could not launch a browser: ' + launched.message);
  await cleanup();
  process.exit(2);
}
const {browser} = launched;
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
  await page.waitForFunction(() => window.__alfred && window.__alfred.selfTest().ok,
                             {timeout: 45000, polling: 400});
  await sleep(600);
}
const card = () => page.evaluate(() => window.__alfred.focusSession.card());
const spoke = () => page.evaluate(() => window.__speech.texts.slice());
const visibleText = () => page.evaluate(() => document.body.innerText);
/* everything the page holds about the session - the on-screen text, the card's own state
 * object, and the raw JSON the server hands it. An identity anywhere in there is a leak. */
const sessionBlob = () => page.evaluate(() => JSON.stringify({
  state: window.__alfred.focusSession.state(),
  card: window.__alfred.focusSession.card(),
  body: document.body.innerText
}));
/* wait for the speech engine to have something new, and report it */
async function waitForSpeech(fromIndex, limitMs = 3000){
  const started = Date.now();
  while (Date.now() - started < limitMs){
    const said = await spoke();
    if (said.length > fromIndex) return {said, ms: Date.now() - started};
    await sleep(60);
  }
  return {said: await spoke(), ms: Date.now() - started, timedOut: true};
}

/* wait until the speech log has gone quiet: a fixed sleep is a guess, and a guess arms the
 * stopwatch while the start announcement is still in the air, which then gets timed as if it
 * were the callout (it did, twice: 722ms and 623ms, both before the grace was up). */
async function waitForQuiet(stableMs = QUIET_MS, limitMs = 6000){
  const started = Date.now();
  let seen = (await spoke()).length, since = Date.now();
  while (Date.now() - started < limitMs){
    await sleep(80);
    const n = (await spoke()).length;
    if (n !== seen){ seen = n; since = Date.now(); continue; }
    if (Date.now() - since >= stableMs) break;
  }
  return {lines: seen, waitedMs: Date.now() - started};
}

/* wait for a predicate on the CARD, and report how long it took */
async function waitForCard(label, test, limitMs = 8000){
  const started = Date.now();
  let c = await card();
  while (Date.now() - started < limitMs){
    if (test(c)) return {card: c, ms: Date.now() - started};
    await sleep(120);
    c = await card();
  }
  return {card: c, ms: Date.now() - started, timedOut: true};
}

await open();
{
  const boot = await card();
  check(boot.hidden === true, 'the card is not on screen before anything starts');
}

/* ------------------------------------------------------------- 1. FOCUS ----- */
await page.click('#focus-btn');
{
  const started = await waitForCard('focus', (c) => c.phase === 'focus' && !c.hidden);
  check(!started.timedOut, 'the FOCUS button starts a session');
  check(started.card.clock === '30:00',
        'the card reads the full half hour from the server: ' + started.card.clock);
  check(/on target/.test(started.card.state), 'and says on target: ' + started.card.state);
  const live = await state();
  check(live.phase === 'running' && live.tab_locked === true,
        'the server holds a running session, locked to the app AND the site');
  check(await page.$eval('#focus-btn', (el) => el.getAttribute('aria-pressed')) === 'true',
        'and the button says it is running');
  await page.screenshot({path: path.join(OUT, 'focus-running.jpg'), quality: 80, type: 'jpeg'});
}

/* --------------------------------------------------- 2. wander off, and he speaks
 * The requirement is in seconds, so it is measured in milliseconds - from the moment the
 * front file says we moved, to the moment the speech engine has the callout.
 *
 * The callout is identified by the SERVER, not by looking for a new line in the speech log.
 * Both of the wrong numbers this check ever produced came from that guess: 722ms and 623ms,
 * and once more 715ms in a full ./tools/verify.sh run, all "callouts" that beat the grace
 * they cannot beat - because they were pieces of the start announcement arriving late, and a
 * late piece of an announcement looks exactly like a very fast callout. The server queues the
 * line it wants spoken with the tier that chose it (the `speak` field of /focus), so the check
 * waits for THAT line: the text on the screen is the text the server queued, or it is not the
 * callout. The wait for the log to go quiet stays - it keeps the announcement out of the
 * window at all - but nothing is decided by it any more. */
{
  const opening = await waitForSpeech(0, 4000);
  check(!opening.timedOut && /minutes/.test(opening.said.join(' ')),
        'the start is spoken before anything is timed: "' + opening.said.join(' | ') + '"');
  const quiet = await waitForQuiet();
  const saidBefore = await spoke();
  const cursor = (await state()).callouts || 0;
  console.log('        the log is quiet before the stopwatch: '
    + quiet.waitedMs + 'ms, ' + saidBefore.length + ' line(s) already in the air, '
    + cursor + ' callout(s) so far');
  const left = Date.now();
  write('com.google.Chrome', OTHER_SITE);
  let calloutMs = null, drifted = null, queued = null;
  /* every line the page speaks after the write, with the moment this check first saw it: when
   * a callout appears to beat the grace, this timeline is the evidence rather than a theory */
  const timeline = [];
  let counted = saidBefore.length;
  const noteLines = async () => {
    const said = await spoke();
    if (said.length > counted){
      for (const line of said.slice(counted)) timeline.push((Date.now() - left) + 'ms  ' + line);
      counted = said.length;
    }
    return said;
  };
  while (Date.now() - left < 4000){
    const c = await card();
    if (c.className.includes('drift')) drifted = c;
    const live = await state();
    const fresh = (live.speak || []).filter((s) => Number(s.tier) >= 1 && s.seq > cursor);
    if (fresh.length){
      queued = fresh[fresh.length - 1];                 // the server's own line, and its tier
      const said = await noteLines();
      if (said.includes(queued.text)){
        calloutMs = Date.now() - left;
        firstCalloutMs = calloutMs;
        break;
      }
    } else {
      await noteLines();
    }
    await sleep(80);
  }
  if (timeline.length) console.log('        after the write, line by line:\n          '
    + timeline.join('\n          '));
  check(drifted !== null, 'a switch to another site tints the card');
  check(calloutMs !== null && calloutMs <= 3000,
        'HE SPEAKS WITHIN THREE SECONDS of the drift (' + calloutMs + 'ms)');
  const liveGrace = await state();
  /* What the grace actually guarantees, and all it can: he never speaks about an excursion
   * that is COUNTED as younger than GRACE_MS. It is not a stopwatch started when you wandered
   * - the reader is asked once a second, so the tick that first sees a drift charges the whole
   * window that led to it, and that window is usually longer than the grace already. Asserting
   * a wall-clock floor of GRACE_MS from the moment the front file changed was WRONG: it failed
   * at 468ms against a server behaving exactly as designed. The tested promise is the counted
   * one, in the server's own numbers. */
  check(Number(liveGrace.timings.detect_ms) >= liveGrace.grace_ms,
        'and the drift was counted with the grace already behind it: detect_ms='
        + liveGrace.timings.detect_ms + ' >= ' + liveGrace.grace_ms + 'ms');
  const line = queued ? queued.text : '';
  check(line.length > 10, 'and what he says is a real line: "' + line + '"');
  // the same floor, in the server's own numbers: callout_ms is how old the excursion was when
  // he spoke, so a callout younger than the grace would show up here whatever the wire did
  check(Number(liveGrace.timings.callout_ms) >= liveGrace.grace_ms,
        'and when he spoke the excursion was still counted at or above the grace: callout_ms='
        + liveGrace.timings.callout_ms + ' >= ' + liveGrace.grace_ms + 'ms');
  console.log('        from the write to the voice: ' + calloutMs + 'ms - the tick that first'
    + ' saw the drift charged its whole window, so the wait is the tick phase (0-'
    + Math.round(liveGrace.tick_s * 1000) + 'ms) plus the page poll');
  const after = await card();
  check(/drifting/.test(after.state), 'the card says which kind of drift: ' + after.state);
  check(/tier 1/.test(after.tier), 'tier 1, because the excursion is young: ' + after.tier);
  check(/1 drift/.test(after.meta), 'and the counters are on the card: ' + after.meta);
  const live = await state();
  check(live.timings.reader_runs >= live.timings.ticks,
        'every tick asked the reader afresh (' + live.timings.reader_runs + ' runs / '
        + live.timings.ticks + ' ticks)');
  await page.screenshot({path: path.join(OUT, 'focus-drift.jpg'), quality: 80, type: 'jpeg'});
}

/* -------------------------------------------- 3. a deep link inside the work site
 * The work site is a single-page app: its path changes with every click, and its host does
 * not. Moving around inside it is work, and this is the check that says so. */
{
  const before = await state();
  write('com.google.Chrome', DEEP_LINK);
  await sleep(2600);
  const now = await state();
  const c = await card();
  check(now.drifts === before.drifts,
        'clicking deeper into the work site is NOT a drift (drifts stayed at ' + now.drifts + ')');
  check(now.on_target === true, 'the session is back on target');
  check(/on target/.test(c.state), 'and the card says so: ' + c.state);
}

/* -------------------------------------------------------- 4. RELOAD, mid-session
 * The session is the server's, so a reloaded tab rejoins it: same id, same clock, still
 * running - and the callout it missed is not replayed at it. */
{
  const before = await state();
  const t0 = Date.now();
  // sample the server's own clock across the reload: if seconds go missing, this says where
  await open();                                  // a full reload of the page
  await sleep(900);
  // The wall clock has to be read AT THE SAME MOMENT as the server's answer. Measuring it
  // later - after a page.evaluate, which under software rendering can take seconds while the
  // galaxy draws - compares a clock reading from three seconds ago with a wall clock from
  // now, and blames the server for the difference. (It did: this check reported "5 ticks in
  // 8.1s" and a clock that "lost" three seconds, when the timeline said the clock was
  // exactly one second per tick throughout. The server was right; the stopwatch was wrong.)
  const t1 = Date.now();
  const after = await state();
  const c = await card();
  check(after.id === before.id, 'a reloaded tab rejoins the SAME session (id ' + after.id + ')');
  check(after.phase === 'running' && c.hidden === false,
        'the session is still running and the card is back on screen');
  // "Not restarted" is a statement about the clock running down, not a fixed tolerance: a
  // reload costs whatever it costs (a browser under software rendering is not quick). What
  // must hold is that the seconds that passed on the wall are the seconds the server took
  // off - and that the clock is well below where it started, not back at the half hour.
  const wallS = (t1 - t0) / 1000;
  const expected = before.remaining_s - wallS;
  const ticksS = (after.timings.ticks - before.timings.ticks);
  const elapsedS = after.elapsed_s - before.elapsed_s;
  console.log('        reload diagnostics: ticks %d -> %d, elapsed %s -> %s (%ss counted of %ss'
              + ' wall), lag worst %sms, reader worst %sms',
              before.timings.ticks, after.timings.ticks, before.elapsed_s.toFixed(1),
              after.elapsed_s.toFixed(1), elapsedS.toFixed(1), wallS.toFixed(1),
              after.timings.tick_lag_max_ms, after.timings.reader_ms_max);
  check(Math.abs(after.remaining_s - expected) < 2.5,
        'the clock kept running through the reload: ' + before.remaining_s.toFixed(0) + 's -> '
        + after.remaining_s.toFixed(0) + 's in ' + wallS.toFixed(1) + 's of wall clock');
  check(after.remaining_s < before.remaining_s - 0.5,
        'it counted DOWN from where it was, and did not start again at '
        + after.planned_s.toFixed(0) + 's');
  // A fresh page starts with an empty speech log, so this is a statement about the reload:
  // nothing the old tab already heard is said again - and there is nothing within
  // CALLOUT_REPLAY_S of this moment for the join to hand it either.
  const saidAfter = await spoke();
  check(saidAfter.length === 0,
        'and the tab reloaded into silence: nothing was replayed at it ('
        + saidAfter.length + ' line(s))');
}

/* ------------------------------------------------------------- 5. the snooze
 * Quiet, but still counting: the point of a snooze is that you asked for it. */
{
  const r = await post({text: 'give me fifteen seconds'});
  check(r.body.code === 'focus_snoozed', 'the snooze is accepted: ' + r.body.code);
  const saidBefore = (await spoke()).length;
  write('com.google.Chrome', OTHER_SITE);
  await sleep(2600);
  const saidAfter = (await spoke()).length;
  const c = await card();
  const live = await state();
  check(saidAfter === saidBefore, 'nothing is said while it is snoozed');
  check(/snoozed/.test(c.state), 'the card says it is snoozed: ' + c.state);
  check(live.drifting === true, 'while the drift is still being counted underneath');
  check(live.snoozed === true, 'and the state agrees');
}

/* ----------------------------------------------------------- 6. home base
 * The galaxy's own tab: never a drift, however long you stand in it. */
{
  const before = await state();
  write('com.google.Chrome', 'http://127.0.0.1:' + PORT + '/');
  await sleep(2800);
  const live = await state();
  const c = await card();
  check(live.reason === 'home' && live.on_target === true,
        'the galaxy\'s own tab is home base, not a drift: ' + live.reason);
  check(/home base/.test(c.state), 'and the card says so: ' + c.state);
  check(live.drifts >= before.drifts, 'the drifts that happened are still on the tally');
  check(await page.$eval('#focus-btn', (el) => el.getAttribute('aria-pressed')) === 'true',
        'and the session is still the one the page is watching');

  // The timer's own honesty, and the reason this project prints numbers instead of trusting
  // a feeling: stand still on target and see whether the seconds he counted are the seconds
  // that passed. (They were not, once: a busy machine made the tick loop late, and the old
  // "never count more than 5s in one tick" cap swallowed 3.4s of a 9.4s stretch - the clock
  // quietly under-counted time the person really spent working. A gap that long is now
  // either counted in full or not counted at all, and this is what says so.)
  const wallT0 = Date.now(), elapsedT0 = (await state()).elapsed_s;
  await sleep(6000);
  const wallT1 = Date.now(), elapsedT1 = (await state()).elapsed_s;
  const wallS = (wallT1 - wallT0) / 1000, countedS = elapsedT1 - elapsedT0;
  check(Math.abs(countedS - wallS) < 2.0,
        'the clock counts the WALL CLOCK, not its own ticks: ' + countedS.toFixed(1)
        + 's counted in ' + wallS.toFixed(1) + 's of real time');
}

/* ------------------------------------------------------- 7. end it, by voice */
{
  await page.type('#q', 'end the session', {delay: 3});
  await page.keyboard.press('Enter');
  const ended = await waitForCard('ended', (c) => c.phase === 'session closed', 9000);
  check(!ended.timedOut, 'the session ends from the ask bar, in his words');
  const answer = await page.$eval('#answer-text', (el) => el.textContent);
  check(/Session closed|poke, not a session/.test(answer),
        'the report card is on the answer card: "' + answer + '"');
  const foot = await page.$eval('#a-foot', (el) => el.textContent);
  check(/ended/.test(foot), 'and the footer says the session is over: ' + foot);
  const said = await spoke();
  check(said.length > 0 && said[said.length - 1].length > 10,
        'the report is SPOKEN, not only written: "' + said[said.length - 1] + '"');
  const live = await state();
  check(live.phase === 'ended', 'the server has no running session');
  check(ended.card.clock !== '', 'the closed card shows a time rather than an empty clock: '
        + ended.card.clock);
  await page.screenshot({path: path.join(OUT, 'focus-report.jpg'), quality: 80, type: 'jpeg'});
}

/* --------------------------------------------------- 8. the ledger, on disk
 * Aggregates only - and a poke is not written down at all, which is the honest branch. */
{
  const exists = fs.existsSync(LEDGER);
  const ledger = exists ? JSON.parse(fs.readFileSync(LEDGER, 'utf8')) : null;
  const keys = ledger ? Object.keys(ledger).sort() : [];
  const live = await state();
  const counted = !!(live.report && live.report.counted);
  /* Whether a run this short is written down depends on how long the machine took - it is a
   * poke under LEDGER_MIN_SESSION_S and a session over it - so this does not branch on the
   * clock any more: it checks the thing that would actually be wrong, which is the report and
   * the disk disagreeing about it. (The branch also used to change the NUMBER of checks the
   * suite reports, so a green run and a green run did not look alike.) */
  check(counted === !!ledger,
        'the report and the ledger agree about whether it counted: report=' + counted
        + ', file=' + !!ledger);
  check(ledger ? ledger.sessions >= 1 : live.report.counted === false,
        ledger ? 'and it counted the session, in ' + keys.length + ' aggregate field(s)'
               : 'a session this short is a poke and the report says so: "'
                 + live.report.text + '"');
  check(ledger ? keys.every((k) => typeof ledger[k] === 'number')
               : !exists,
        ledger ? 'and every field is a number, no strings: ' + keys.join(', ')
               : 'and nothing was written down at all');
}

/* -------------------------------------------- 9. no identity, anywhere on screen */
{
  const secrets = ['com.google.Chrome', 'work.example.com', 'news.example.com',
                   'board/42', '/top', '9911'];
  const blob = await sessionBlob();
  const leaked = secrets.filter((s) => blob.includes(s));
  check(leaked.length === 0,
        'nothing on screen, in the card or in the state names the app, the host or the path'
        + (leaked.length ? ' (LEAKED: ' + leaked.join(', ') + ')' : ''));
  const log = serverLog;
  check(secrets.filter((s) => log.includes(s)).length === 0,
        'and the server log carries none of it either');
  const shown = await visibleText();
  check(/focus|session closed/i.test(shown), 'the session is on screen for a person to read');
  check(pageErrors.length === 0,
        'no uncaught page errors' + (pageErrors.length ? ': ' + pageErrors.slice(0, 2).join(' | ') : ''));
}

/* -------------------------------------- the field numbers, for the next person
 * Any future argument about GRACE_MS or TICK_S starts here, with the numbers from a real
 * run on a real machine - which is the only way this project moves a knob. */
{
  const live = await state();
  const t = live.timings || {};
  console.log('\n  the field numbers, from this run:');
  console.log('    knobs        : tick %ss, grace %sms, nag %ss', live.tick_s, live.grace_ms,
              live.nag_s);
  console.log('    the tick     : %s tick(s), %sms avg / %sms worst, lag %sms last / %sms worst',
              t.ticks, t.tick_ms_avg, t.tick_ms_max, t.tick_lag_ms, t.tick_lag_max_ms);
  console.log('    the reader   : %s run(s), %s fail(s), %sms avg / %sms worst',
              t.reader_runs, t.reader_fails, t.reader_ms_avg, t.reader_ms_max);
  console.log('    the callout  : counted at %sms of off-target time (the floor is GRACE_MS ='
              + ' %sms, and the tick that first sees a drift charges its whole window TICK_S ='
              + ' %sms), called out at %sms; from the write it reached the voice in %sms - the'
              + ' wait a person feels is the tick phase plus the page poll',
              t.detect_ms, live.grace_ms, Math.round(live.tick_s * 1000),
              t.callout_ms, firstCalloutMs === null ? '?' : firstCalloutMs);
  console.log('    (python3 tools/focus-timings.py prints these from a running server)');
}

await page.close();
await browser.close();
await cleanup();

const fails = results.filter(([s]) => s === 'FAIL');
console.log('\nAlfred - focus sessions (real browser, real server, scripted reader)');
console.log('  %d checks, %d passed, %d failed', results.length, results.length - fails.length,
            fails.length);
if (fails.length){
  console.log('\n  RESULT: FAILED');
  fails.forEach(([, m]) => console.log('   - ' + m));
  process.exit(1);
}
console.log('\n  RESULT: PASSED - a real session starts, is called out inside three seconds,\n' +
            '  survives a reload, and ends with a spoken report that names nothing');
