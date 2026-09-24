#!/usr/bin/env node
/*
 * tools/verify-focus.mjs - focus sessions, in the page.
 *
 * The session is the server's: this suite drives the page against a scripted /focus, on a
 * virtual clock, and checks the parts a person actually meets.
 *
 *   the card       -> pinned, only when there is a session, the countdown from the
 *                     SERVER's seconds, filled from the server's numbers, and tinted on
 *                     drift. Every word on it is a category, a state or a count
 *   the routing    -> "thirty minutes on this" and "pause" and "end the session" go to
 *                     /focus, even while a screen share is live; a question that merely
 *                     mentions minutes, work or focus stays a question
 *   the callouts   -> whatever the server has decided to say is SPOKEN, once, in order,
 *                     and never twice for the same seq (a reload must not nag again)
 *   the report     -> spoken and on the card, and the footer says whether it counted
 *   the button     -> starts a session when idle, ends it when one is running
 *   no identity    -> the card, the answer and the footer are checked for the things a
 *                     leaking app or host would look like
 *
 * Runs the viewer's real JavaScript against the mocks in tools/harness.mjs.
 * node tools/verify-focus.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import {boot, tinyFrame} from './harness.mjs';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const SRC = fs.readFileSync(path.join(ROOT, 'viewer', 'index.html'), 'utf8');

let pass = 0, fail = 0;
const failed = [];
const ok = (m) => { pass++; console.log('  ok    ' + m); };
const bad = (m) => { fail++; failed.push(m); console.log('  FAIL  ' + m); };
const check = (cond, m) => (cond ? ok(m) : bad(m));
const group = (name) => console.log('\n' + name);

/* ------------------------------------------------------------------ the states */
const IDLE = {ok: true, phase: 'idle', id: 0, locked: false, tab_locked: false, on_target: true,
              reason: 'none', drifting: false, tier: 0, excused: false, snoozed: false,
              snooze_s_left: 0, reader: 'live', reader_why: 'ok', planned_s: 0, remaining_s: 0,
              elapsed_s: 0, on_s: 0, off_s: 0, off_open_s: 0, refunded_s: 0, noise_s: 0,
              drifts: 0, refunds: 0, ignores: 0, clean_pct: 100, nag_s: 30, grace_ms: 800,
              tick_s: 1, streak: 0, best_streak: 0, sessions: 0, clean_sessions: 0,
              callouts: 0, since: 0, speak: [], report: null, timings: {ticks: 0, reader_runs: 0}};

const RUNNING = (o = {}) => Object.assign({}, IDLE, {
  phase: 'running', id: 1, locked: true, planned_s: 1800, remaining_s: 1800, elapsed_s: 0,
  on_s: 0, tab_locked: true,
  lines: {}, timings: {ticks: 7, reader_runs: 8, detect_ms: 1000, callout_ms: 1000}
}, o);

const DRIFT = (o = {}) => RUNNING(Object.assign({
  on_target: false, reason: 'tab', drifting: true, tier: 1, remaining_s: 1793, elapsed_s: 7,
  on_s: 4, off_open_s: 3, drifts: 1, clean_pct: 57
}, o));

const VOICE = (n, text, tier = 1) => ({state: 'x', seq: n, text, tier, at_s: 0});

/* The server's two calls: GET /focus and POST /focus. A scripted GET queue lets a test
   walk the page through a session a second at a time. The harness api is returned as-is,
   with a `calls` log of our own on top of it. */
async function page({states = [IDLE], post = null, health = {ok: true, notes: 12, key: {state: 'set'}},
                     screen = null, search = ''} = {}){
  const calls = [];
  // A queue of states, handed out one per GET and then pinned to the last one. A test
  // that needs to be deterministic calls api.pin(state) first, so a background timer
  // tick cannot walk the script on while the test is looking elsewhere.
  let queue = states.slice();
  let pinned = states[states.length - 1];
  const api = await boot({
    health, screen, search, fetchImpl: async (url, opts) => {
      const u = String(url);
      const body = opts && opts.body ? JSON.parse(opts.body) : null;
      calls.push({url: u, body, method: (opts && opts.method) || 'GET'});
      if (u.indexOf('/health') >= 0) return {ok: true, status: 200, json: async () => health};
      if (u.indexOf('/focus') === 0 && (!opts || (opts.method || 'GET') === 'GET')){
        const state = queue.length ? queue.shift() : pinned;
        pinned = state;
        return {ok: true, status: 200, json: async () => state};
      }
      if (u.indexOf('/focus') === 0 && opts && opts.method === 'POST'){
        const reply = post ? post(body) : {ok: true, code: 'focus_state', answer: '',
                                           focus: states[states.length - 1]};
        return {ok: !!reply.ok, status: reply.status || (reply.ok ? 200 : 400),
                json: async () => reply};
      }
      return {ok: true, status: 200, json: async () => ({ok: true, answer: 'The movers quoted 26,000, sir.',
                                                         decision: 'notes', on_notes: true,
                                                         nodes: [], sources: [], model: 'stub-model'})};
    }
  });
  api.calls = calls;
  api.pin = (state) => { queue = [state]; pinned = state; return state; };
  return api;
}

const shot = () => new Promise(r => setImmediate(r));

/* =========================================================================
   the card
   ========================================================================= */
group('the card: a view of the server\'s session, and nothing else');
{
  const p = await page({states: [IDLE]});
  const before = p.app.focus.card();
  check(before.hidden === true, 'nothing on screen when there is no session');
  check(p.app.focus.button_face().pressed === 'false', 'and the FOCUS button says so');

  const p2 = await page({states: [RUNNING({remaining_s: 1560, on_s: 240, planned_s: 1800})]});
  await p2.app.focus.join(); await shot();
  const card = p2.app.focus.card();
  check(card.hidden === false, 'a session on the server puts the card on screen');
  check(card.clock === '26:00', 'the countdown comes from the server\'s seconds: ' + card.clock);
  check(card.fill !== '' && parseFloat(card.fill) > 12 && parseFloat(card.fill) < 14,
        'the bar is filled from the server\'s numbers (' + card.fill + ', 240s into 1800s)');
  check(/on target/.test(card.state), 'and the state line reads on target: ' + JSON.stringify(card.state));
  check(p2.app.focus.button_face().pressed === 'true', 'the button is pressed while a session runs');
  check(/30s/.test(card.meta), 'the meta line carries the counters: ' + card.meta);
}
{
  // the countdown is the server's clock: the same state twice does not tick locally
  const p = await page({states: [RUNNING({remaining_s: 1799})]});
  await p.app.focus.join(); await shot();
  const a = p.app.focus.card().clock;
  await p.clock.advance(5000); await shot();
  const b = p.app.focus.card().clock;
  check(a === b, 'the card does not invent a countdown of its own: ' + a + ' after five seconds');
}
{
  const p = await page({states: [DRIFT()]});
  await p.app.focus.join(); await shot();
  const card = p.app.focus.card();
  check(card.className.indexOf('drift') >= 0, 'drifting tints the card: ' + card.className);
  check(card.tier === 'tier 1', 'and says which tier it is: ' + card.tier);
  check(/another tab/.test(card.state), 'the state line names the KIND, not the site: ' + card.state);
  check(/3s off/.test(card.meta), 'with how long it has been off target: ' + card.meta);
}
{
  const p = await page({states: [DRIFT({tier: 3, reason: 'app', off_open_s: 77, drifts: 3})]});
  await p.app.focus.join(); await shot();
  const card = p.app.focus.card();
  check(/t3/.test(card.className), 'tier 3 is marked as its own thing: ' + card.className);
  check(/another app/.test(card.state), 'and an app drift reads "another app": ' + card.state);
}
{
  const p = await page({states: [RUNNING({reader: 'blind', reader_why: 'failed', on_s: 0})]});
  await p.app.focus.join(); await shot();
  const card = p.app.focus.card();
  check(card.className.indexOf('blind') >= 0, 'a blind reader marks the card');
  check(/cannot see/.test(card.state), 'and says so plainly: ' + card.state);
  check(/nothing is being counted/.test(card.meta),
        'and promises that nothing is being counted while it cannot see: ' + card.meta);
}
{
  const p = await page({states: [RUNNING({reason: 'home', on_target: true})]});
  await p.app.focus.join(); await shot();
  check(/home base/.test(p.app.focus.card().state),
        'his own tab reads as home base, not as a drift: ' + p.app.focus.card().state);
}

/* =========================================================================
   the routing
   ========================================================================= */
group('routing: commands about the session go to /focus, questions stay questions');
{
  const p = await page({states: [RUNNING()]});
  await p.app.focus.join(); await shot();
  const COMMANDS = ['thirty minutes on this', 'focus for 30 minutes', 'pause', 'resume',
                    'end the session', "I'm done", "it's okay, I'm doing research",
                    'give me fifteen seconds', 'call me out every thirty seconds',
                    'extend by ten minutes'];
  const QUESTIONS = ['what did my notes say about the budget?', 'how many minutes was the meeting?',
                     'what should I get done in the last week before moving?',
                     'can you focus on the budget note?', 'did I write anything about work?',
                     'switch to astra', 'remember that the finish window is 900 milliseconds',
                     'what is on my screen?', 'hello there', 'stop'];
  for (const text of COMMANDS){
    check(p.app.focus.isCommand(text, 'running') === true, 'a command: ' + JSON.stringify(text));
  }
  for (const text of QUESTIONS){
    check(p.app.focus.isCommand(text, 'running') === false, 'a question: ' + JSON.stringify(text));
  }
}
{
  // routing: the command really goes to /focus, and never to /chat
  const p = await page({states: [RUNNING()],
    post: (body) => ({ok: true, code: 'focus_paused', answer: 'Paused, sir. The clock stops with you.',
                      focus: RUNNING({phase: 'paused'})})});
  await p.app.focus.join(); await shot();
  p.calls.length = 0;
  p.app.voice.submitQuestion('pause');
  await p.flush(3, 40);
  const asked = p.calls.filter(c => c.url.indexOf('/chat') >= 0);
  const focused = p.calls.filter(c => c.url.indexOf('/focus') >= 0 && c.method === 'POST');
  check(focused.length === 1, 'a focus command reaches POST /focus exactly once');
  check(asked.length === 0, 'and never /chat: the notes are not involved');
  check(focused[0].body.text === 'pause', 'with the words you actually said: ' + focused[0].body.text);
  check(/Paused/.test(p.elements.get('answer-text').textContent),
        'and his answer is on the card: ' + p.elements.get('answer-text').textContent);
}
{
  // a question that merely mentions focus still goes to the notes
  const p = await page({states: [RUNNING()]});
  await p.app.focus.join(); await shot();
  p.calls.length = 0;
  p.app.voice.submitQuestion('can you focus on the budget note?');
  await p.flush(3, 40);
  check(p.calls.some(c => c.url.indexOf('/chat') >= 0), 'a question about focus still reaches /chat');
  check(!p.calls.some(c => c.url.indexOf('/focus') >= 0 && c.method === 'POST'),
        'and does not touch the session');
}
{
  // a focus command outranks a live screen share: the session is still drivable by voice
  const p = await page({states: [RUNNING()], screen: {frames: [tinyFrame(1)]}});
  await p.app.focus.join(); await shot();
  await p.app.sight.start(); await shot();
  p.calls.length = 0;
  p.app.voice.submitQuestion('end the session');
  await p.flush(3, 40);
  check(p.calls.some(c => c.url.indexOf('/chat') >= 0) === false &&
        p.calls.some(c => c.url.indexOf('/see') >= 0) === false,
        'ending a session during a live share does not go to the screen or the notes');
  check(p.calls.some(c => c.url.indexOf('/focus') >= 0), 'it goes to /focus');
  check(p.app.sight.isSharing() === true, 'and the share is untouched');
}

/* =========================================================================
   the callouts
   ========================================================================= */
group('the callouts: spoken once, in order, and never twice');
{
  const words = (app) => app.voice.lastSpoken();
  const p = await page({states: [
    DRIFT({speak: [VOICE(1, 'That is not where the work is, sir.')], since: 1}),
    DRIFT({speak: [], since: 1}),
    DRIFT({speak: [VOICE(2, 'Sir. The clock is running and you are not on it.', 2)], tier: 2, since: 2})
  ]});
  p.pin(DRIFT({speak: [VOICE(1, 'That is not where the work is, sir.')], since: 1}));
  await p.app.focus.join(); await shot();
  check(words(p.app) === 'That is not where the work is, sir.',
        'the first callout is spoken: ' + JSON.stringify(words(p.app)));
  check(p.app.focus.spokenSeq() === 1,
        'and remembered by seq (' + p.app.focus.spokenSeq() + '), so the next poll asks for more');
  // a second poll with nothing new in it: the same line must not be said again
  p.pin(DRIFT({speak: [], since: 1}));
  await p.app.focus.poll(); await shot();
  check(words(p.app) === 'That is not where the work is, sir.',
        'the same callout is not repeated when the server sends nothing new');
  p.pin(DRIFT({speak: [VOICE(2, 'Sir. The clock is running and you are not on it.', 2)], tier: 2, since: 2}));
  await p.app.focus.poll(); await shot();
  check(words(p.app) === 'Sir. The clock is running and you are not on it.',
        'the next tier arrives when the server sends it: ' + JSON.stringify(words(p.app)));
  check(p.app.focus.spokenSeq() === 2, 'and the seq moves on with it');
  // the poll is on a timer, not on a hand-crank: one second, by the page's own constant
  check(p.clock.pending().some(ms => ms > 0 && ms <= 1000),
        'the page polls the session on a timer of its own: ' + JSON.stringify(p.clock.pending().slice(0, 3)));
  check(p.app.focus.POLL_MS === 1000, 'and that timer is one second: ' + p.app.focus.POLL_MS);
  const before = p.calls.filter(c => c.url.indexOf('/focus') >= 0).length;
  await p.flush(4, 400);
  check(p.calls.filter(c => c.url.indexOf('/focus') >= 0).length > before,
        'the timer really fires: the page asked again without being told to');
}
{
  // a page that opens mid-drift does not nag about a callout it was not there for
  const p = await page({states: [DRIFT({speak: [], since: 9})]});
  await p.app.focus.join(); await shot();
  check(!p.app.voice.lastSpoken(), 'joining a session says nothing about old callouts');
  check(p.app.focus.spokenSeq() === 9, 'it marks them as already dealt with: ' + p.app.focus.spokenSeq());
}
{
  // a muted tab: the card still shows the drift, and the speech engine is never called
  const p = await page({states: [DRIFT({speak: [VOICE(1, 'That is not where the work is, sir.')], since: 1})],
                       search: '?mute=1'});
  await p.app.focus.join(); await shot();
  check(!p.app.voice.lastSpoken(), 'a ?mute=1 tab says nothing');
  check(p.app.focus.card().className.indexOf('drift') >= 0, 'the drift is still visible in a muted tab');
  check(p.app.focus.spokenSeq() === 1, 'and the callout is still marked as dealt with');
}

/* =========================================================================
   the report card
   ========================================================================= */
group('the report card: spoken, on-screen, and honest about the ledger');
{
  const report = {
    ok: true, phase: 'ended', id: 1, planned_s: 1800, remaining_s: 0, elapsed_s: 1800,
    on_s: 1680, off_s: 120, clean_pct: 93, drifts: 2, refunds: 1, streak: 3, best_streak: 3,
    sessions: 4, clean_sessions: 3, callouts: 2, since: 1, speak: [], tier: 0, excused: false,
    snoozed: false, reader: 'live', reader_why: 'ok', locked: false, tab_locked: false,
    on_target: true, reason: 'none', drifting: false, nag_s: 30, grace_ms: 800, tick_s: 1,
    report: {on: 28, planned: 30, pct: 93, drifts: 2, refunds: 1, streak: 3, clean: true,
             counted: true, seconds: 1800,
             text: 'Session closed, sir. 28 of 30 minutes on target - 93 percent clean, 2 drifts ' +
                   'on the tally, 1 refund. That is 3 clean in a row.'}
  };
  const p = await page({states: [report]});
  await p.app.focus.join(); await shot();
  check(p.app.focus.card().phase === 'session closed', 'the card says the session is closed');
  check(p.app.focus.card().clock === '30:00',
        'a closed card shows the time the session took, not a countdown still running: ' +
        p.app.focus.card().clock);
  check(/93% clean/.test(p.app.focus.card().state), 'and reads the clean percentage');
  const shown = p.elements.get('answer-text').textContent;
  check(/28 of 30 minutes on target/.test(shown), 'the report card is on the answer card: ' + shown);
  check(p.elements.get('a-foot').textContent.indexOf('streak 3') >= 0,
        'the footer carries the streak: ' + p.elements.get('a-foot').textContent);
  await p.clock.advance(20000); await shot();
  check(p.app.focus.card().hidden === true, 'and the card puts itself away afterwards');

  const short = Object.assign({}, report, {elapsed_s: 12, on_s: 12, clean_pct: 100,
    report: {on: 0.2, planned: 30, pct: 100, drifts: 0, refunds: 0, streak: 0, clean: true,
             counted: false, seconds: 12,
             text: 'That was 12 seconds, sir - a poke, not a session. Nothing has gone in the ' +
                   'ledger, and no streak was risked.'}});
  const q = await page({states: [short]});
  await q.app.focus.join(); await shot();
  check(/not in the ledger/.test(q.elements.get('a-foot').textContent),
        'a session too short to count says so on the footer: ' +
        q.elements.get('a-foot').textContent);

  // The page opened while the LAST report was still on the server: a linger is armed for it,
  // and then a new session starts. The linger must not reach in and put the new card away -
  // the bug this pins down was a card that froze mid-session, eight seconds in.
  const r = await page({states: [report]});                  // the page opens on the old report
  await r.app.focus.join(); await shot();
  check(/session closed/.test(r.app.focus.card().phase),
        'a page opened on a finished session shows it: ' + r.app.focus.card().phase);
  check(r.app.focus.card().hidden === false, 'and the card is up for it');
  r.pin(RUNNING({remaining_s: 1740, on_s: 60}));             // the next session starts: FOCUS
  await r.app.focus.poll(); await shot();
  check(r.app.focus.card().hidden === false && r.app.focus.card().clock === '29:00',
        'the new session takes the card over: ' + r.app.focus.card().clock);
  // The report the page opened on armed a linger. Nothing polls by hand from here: the
  // interval is what must still be running when the linger's moment comes.
  await r.clock.advance(20000); await shot();
  check(r.app.focus.card().hidden === false,
        'and the old report\'s linger does NOT reach in and put it away twelve seconds later');
  // The sharp end of this: if that linger had stopped the poll, the card would be frozen.
  // Pin a newer state, walk the clock, and it must move - a stopped poll cannot paint it.
  r.pin(RUNNING({remaining_s: 1620, on_s: 180}));
  await r.clock.advance(3000); await shot();
  check(r.app.focus.card().clock === '27:00',
        'and the poll is still running - the card keeps up with the server: ' +
        r.app.focus.card().clock);
}

/* =========================================================================
   the button
   ========================================================================= */
group('the FOCUS button: start it, and end it');
{
  const p = await page({states: [IDLE], post: (body) => (body.action === 'start'
    ? {ok: true, code: 'focus_started', answer: '30 minutes, sir. I have my eye on this one.',
       focus: RUNNING()}
    : {ok: true, code: 'focus_ended', answer: 'Session closed, sir.', focus: RUNNING({phase: 'ended'})})});
  const pressed = p.app.focus.button();
  await p.flush(3, 40);
  await pressed;
  const started = p.calls.filter(c => c.method === 'POST' && c.url.indexOf('/focus') >= 0);
  check(started.length === 1, 'the button posts once');
  check(started[0].body.action === 'start', 'with the start action: ' + JSON.stringify(started[0].body));
  check(started[0].body.minutes === 30, 'and thirty minutes by default');
  check(p.app.focus.card().hidden === false, 'the card appears');
  check(/30 minutes/.test(p.elements.get('answer-text').textContent), 'and he says so');
}
{
  const p = await page({states: [RUNNING()], post: () => ({ok: true, code: 'focus_ended',
    answer: 'Session closed, sir. 20 of 30 minutes.', focus: RUNNING({phase: 'ended'})})});
  await p.app.focus.join(); await shot();
  const pressed2 = p.app.focus.button(); await p.flush(3, 40); await pressed2;
  const posted = p.calls.filter(c => c.method === 'POST' && c.url.indexOf('/focus') >= 0);
  check(posted.length === 1 && /end the session/.test(posted[0].body.text || ''),
        'with a session running the same button ends it: ' + JSON.stringify(posted[0] && posted[0].body));
}
{
  // a refusal is a refusal: the card does not pretend the session started
  const p = await page({states: [IDLE], post: () => ({ok: false, status: 200, code: 'focus_home',
    answer: 'You are looking at me, sir, and I can only lock what is in front of you.',
    focus: IDLE})});
  const pressed2 = p.app.focus.button(); await p.flush(3, 40); await pressed2;
  check(p.app.focus.card().hidden === true, 'a refused start leaves the card off');
  check(p.app.focus.button_face().pressed === 'false', 'and the button unpressed');
  check(/can only lock what is in front of you/.test(p.elements.get('answer-text').textContent),
        'and he explains why: ' + p.elements.get('answer-text').textContent.slice(0, 60));
  check(/NOTHING CHANGED/.test(p.elements.get('a-foot').textContent),
        'with NOTHING CHANGED in the footer: ' + p.elements.get('a-foot').textContent);
}

/* =========================================================================
   no identity, anywhere on screen
   ========================================================================= */
group('nothing about where you are can appear on screen');
{
  const SECRET_APP = 'com.tinyspeck.slackmacgap';
  const SECRET_HOST = 'work.example.com';
  const p = await page({states: [DRIFT({note: 'drift', speak: [VOICE(1, 'That is not where the work is, sir.')]})]});
  await p.app.focus.join(); await shot();
  const surfaces = [
    JSON.stringify(p.app.focus.card()),
    JSON.stringify(p.app.focus.state()),
    p.elements.get('answer-text').textContent,
    p.elements.get('a-foot').textContent,
    p.elements.get('focus-meta').textContent,
    p.elements.get('focus-state').textContent,
    p.app.voice.lastSpoken() || ''
  ].join(' | ');
  check(surfaces.indexOf(SECRET_APP) < 0, 'no bundle identifier anywhere on the page');
  check(surfaces.indexOf(SECRET_HOST) < 0, 'no host anywhere on the page');
  check(/another tab/.test(surfaces), 'only the category is shown, and it is there: a real check, ' +
        'not a vacuous one');
}
{
  // the client asks for exactly what it needs, and nothing that could carry an identity
  const p = await page({states: [RUNNING()]});
  await p.app.focus.join(); await shot();
  const health = p.calls.find(c => c.url.indexOf('/health') >= 0);
  check(!!health, 'the page asks /health at boot: ' + (health && health.url));
  check(p.calls.every(c => c.url.indexOf('/focus') < 0 || /\/focus(\?since=\d+)?$/.test(c.url)),
        'and only ever asks /focus for the session and the callouts it has not heard');
}

/* ------------------------------------------------------------------------- */
console.log('\nAlfred - focus sessions (virtual clock, no browser needed)');
if (fail){
  console.log('\n  RESULT: FAILED (' + fail + ' of ' + (pass + fail) + ')');
  failed.forEach(f => console.log('   - ' + f));
  process.exit(1);
}
console.log('\n  RESULT: PASSED - ' + pass + ' checks: the card is a view of the server\'s\n' +
            '  session, commands route to /focus, callouts are spoken once each, the report\n' +
            '  card is honest about the ledger, and no app or site can appear on screen');
