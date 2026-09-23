#!/usr/bin/env node
/*
 * tools/verify-capture.mjs - "I can grow the brain by voice", in the page's own logic.
 *
 * Runs the real viewer/index.html in the headless harness (virtual DOM, virtual clock,
 * mocked CDN bundle) and drives the capture path exactly as the ask bar does:
 *
 *   "remember that ..."  ->  POST /remember   (never /chat)
 *                        ->  a new star, born at the note it is most related to
 *                        ->  a brief glow, and only then the camera flight
 *                        ->  one line of confirmation, spoken
 *
 * and the failure path, which must never be quiet:
 *
 *   a write that fails    ->  shown AND spoken, no star, camera does not move
 *
 *   node tools/verify-capture.mjs
 */
import { boot, GRAPH_DATA, defaultCapture } from './harness.mjs';

/* ------------------------------------------------------------------ reporting */
const results = [];
const ok = (m) => { results.push(['ok', m]); console.log('  ok    ' + m); };
const bad = (m) => { results.push(['FAIL', m]); console.log('  FAIL  ' + m); };
const check = (c, m) => (c ? ok(m) : bad(m));

console.log('\nAlfred - growing the brain by voice (viewer logic)');

const LABEL = 'The Finish Window Should Be 900';
const LINE = 'Filed and lit, sir. \u201c' + LABEL + '\u201d is in the galaxy now, born beside Budget for the Move, ' +
             'joined to Budget for the Move, and the galaxy is 13 notes strong.';
const FILE = 'captures/the-finish-window-should-be-900.md';
const ANCHOR = 7;                      // Budget for the Move in the shipped graph

/* the payload a capture comes back with: one link, so "joined to" is real too */
function capturePayload(over = {}){
  const i = GRAPH_DATA.nodes.length;
  return Object.assign({
    ok: true, captured: true, filed: true, indexed: true, answer: LINE, line: LINE,
    title: LABEL, date: '2026-09-23', file: FILE, index: i, notes: i + 1,
    node: {id: i, index: i, label: LABEL, group: 'captures',
           excerpt: 'The finish window should be 900 milliseconds.',
           path: FILE, words: 15, chars: 103, degree: 1, wikilinks: [], mentions: ['Budget for the Move']},
    anchor: {index: ANCHOR, label: 'Budget for the Move', score: 3.5},
    links: [{source: ANCHOR, target: i, kind: 'mention', weight: 1}],
    link_labels: ['Budget for the Move'], graph_file: 'behind', model: 'stub-model', turns: 0
  }, over);
}

/* the /chat answer the follow-up question gets: the new note is the source */
const FOLLOW_UP = {
  ok: true, answer: 'Nine hundred milliseconds, sir, and not a moment longer.',
  on_notes: true, decision: 'notes', nodes: [GRAPH_DATA.nodes.length],
  sources: [{index: GRAPH_DATA.nodes.length, label: LABEL, score: 6.5}],
  read: [GRAPH_DATA.nodes.length], model: 'stub-model', turns: 1
};

function makeFetch({capture = capturePayload(), failWith = null, chat = FOLLOW_UP} = {}){
  const calls = [];
  const impl = async (url, opts) => {
    calls.push({url: String(url), body: opts && opts.body ? JSON.parse(opts.body) : null});
    if (String(url).indexOf('/health') >= 0)
      return {ok: true, status: 200, json: async () => ({ok: true, notes: GRAPH_DATA.nodes.length, key: {state: 'set'}})};
    if (String(url).indexOf('/remember') >= 0){
      if (failWith) return {ok: false, status: 500, json: async () => failWith};
      return {ok: true, status: 200, json: async () => capture};
    }
    return {ok: true, status: 200, json: async () => chat};
  };
  impl.calls = calls;
  return impl;
}

/* Submit like the ask bar does, then walk the clock forward: the birth glow is driven
   by animation frames, so time has to move for it to finish - and for the camera
   flight that comes after it. */
const ask = async (app, text, ms = 900) => {
  app.elements.get('q').value = text;
  await app.sandbox.window.document.getElementById('ask-form')._ev.submit({preventDefault(){}});
  await app.flush(6, 20);                       // the request, the response, the birth
  await app.clock.advanceAsync(ms, 16);         // the glow, and then the flight
  await app.flush(4, 20);
};

/* =================================================== 1. typed: /remember, not /chat */
{
  const fetchImpl = makeFetch();
  const app = await boot({fetchImpl});

  check(app.app.capture.PULSE_MS === 1100, 'the glow has one named length: ' + app.app.capture.PULSE_MS + ' ms');
  check(app.app.capture.trigger.source.indexOf('remember') >= 0 && app.app.capture.trigger.test('  Remember that x'),
        'the trigger is a leading "remember", typed in any case');

  const before = app.app.capture.nodes().length;
  await ask(app, 'remember that the finish window should be 900 milliseconds');

  const remember = fetchImpl.calls.filter(c => c.url === '/remember');
  const chat = fetchImpl.calls.filter(c => c.url === '/chat');
  check(remember.length === 1 && chat.length === 0,
        '"remember that ..." goes to /remember and never to /chat (' + remember.length + ' vs ' + chat.length + ')');
  check(remember[0] && remember[0].body && remember[0].body.text ===
        'remember that the finish window should be 900 milliseconds',
        'the whole sentence is posted, so the server can strip the trigger itself');
  check(fetchImpl.calls.filter(c => c.url.indexOf('/chat') >= 0).length === 0,
        'the brain was not asked a question it would have had to invent an answer for');

  const nodes = app.app.capture.nodes();
  check(nodes.length === before + 1, 'the galaxy gained a node without a reload (' + before + ' -> ' + nodes.length + ')');
  const born = nodes[nodes.length - 1];
  check(born.label === LABEL && born.group === 'captures',
        'it carries the server\'s own title and folder: ' + born.label + ' (' + born.group + ')');
  check(born.index === GRAPH_DATA.nodes.length, 'and takes the index the server gave it (' + born.index + ')');
  check(!!app.app.data.nodes.find(n => n.label === LABEL), 'it is in the same node array the graph was built from');
  check(app.app.capture.stats().indexOf(nodes.length + ' notes') === 0,
        'the stats line counts it: "' + app.app.capture.stats() + '"');
  check(app.app.capture.links().some(l => l.target === born.id && l.source === ANCHOR && l.kind === 'mention'),
        'and it is linked, the way build.py links: ' + JSON.stringify(app.app.capture.links().slice(-1)));

  const bornRow = app.app.capture.nodes().find(n => n.label === LABEL);
  const anchorNow = app.app.capture.nodes().find(n => n.index === ANCHOR);
  check(Math.hypot(bornRow.x - anchorNow.x, bornRow.y - anchorNow.y, bornRow.z - anchorNow.z) < 0.001,
        'it is born at the exact position of the note it is most related to');
  check(anchorNow.degree === (GRAPH_DATA.nodes[ANCHOR].degree || 0) + 1,
        'and the note it joins now has one more link (degree ' + anchorNow.degree + ')');

  const birth = app.app.capture.lastBirth();
  check(!!birth && birth.anchor === ANCHOR && birth.file === FILE,
        'the birth records the note it was born beside and the file it came from');
  check(birth && birth.flewAt != null, 'the camera did fly to it');
  check(birth && birth.flewAt >= birth.pulseEndedAt,
        'and only AFTER the glow finished (pulse ended at ' + Math.round(birth.pulseEndedAt) +
        ' ms, flight began at ' + Math.round(birth.flewAt) + ' ms)');
  check(birth && birth.pulseEndedAt >= birth.pulseMs - 16,
        'the glow ran for its ' + app.app.capture.PULSE_MS + ' ms of real animation frames');
  const bornNode = app.app.capture.nodes().find(n => n.label === LABEL);
  check(birth.pinned() === true,
        'it is held exactly where it was born while it glows, instead of drifting off');
  check(Math.hypot(bornNode.x - anchorNow.x, bornNode.y - anchorNow.y, bornNode.z - anchorNow.z) < 0.001,
        'still on its parent note after the glow is over');
  await app.clock.advanceAsync(app.app.CFG.flyMs + 60, 16);      // the whole flight
  check(!birth.pinned(), 'and let go once the camera has arrived, so the layout can settle it');
  check(birth && Math.round(birth.pulseEndedAt) >= app.app.capture.PULSE_MS - 40,
        'the glow really did last its ' + app.app.capture.PULSE_MS + ' ms');
  const fly = app.app.lastFlyTo();
  check(!!fly && Math.abs(fly.node.x - bornRow.x) < 0.001 && Math.abs(fly.node.z - bornRow.z) < 0.001,
        'the flight was aimed at the new star, not at anything else');

  const panelText = app.elements.get('panel-label').textContent;
  check(app.app.provenance.panelOpen() && panelText === LABEL,
        'its side panel is open, as if it had always been there: ' + panelText);
  const answer = app.elements.get('answer-text').textContent;
  check(answer === LINE, 'his confirmation is the server\'s line, word for word');
  check(app.speech.words().filter(t => t === LINE).length === 1,
        'and it is spoken once, out loud');
  check(app.app.capture.foot().indexOf(FILE) >= 0, 'the footer says where the file went: "' +
        app.app.capture.foot() + '"');
  check(app.app.capture.foot().indexOf('build.py') >= 0,
        'and that graph-data.js catches up on the next build.py run');

  check(app.elements.get('q').value === '',
        'the ask bar is empty again after a capture, ready for the next thing you say');

  /* the question that follows must land on the new star */
  const flyBefore = app.app.lastFlyTo();
  await ask(app, 'what is the finish window in milliseconds?');
  const flyAfter = app.app.lastFlyTo();
  check(flyAfter !== flyBefore && Math.abs(flyAfter.node.x - bornRow.x) < 0.001,
        'the very next question flies to the note that was just filed');
  check(app.app.data.nodes.find(n => n.label === LABEL) !== undefined &&
        app.app.provenance.lit().indexOf(bornRow.id) >= 0,
        'and lights it up as a source (' + JSON.stringify(app.app.provenance.lit()) + ')');
}

/* ============================================ 2. spoken: the same path, by voice */
{
  const fetchImpl = makeFetch();
  const app = await boot({fetchImpl});
  app.app.voice._hooks.final('remember that the kettle belongs by the window');
  await app.flush(30, 40);
  app.app.voice._hooks.finish();
  await app.flush(40, 40);
  check(fetchImpl.calls.filter(c => c.url === '/remember').length === 1,
        'a spoken sentence is filed the same way a typed one is');
  check(app.app.capture.nodes().length === GRAPH_DATA.nodes.length + 1,
        'and the star is born for the spoken capture too');
}

/* ==================================================== 3. a capture that cannot land */
{
  const failure = {
    ok: false, captured: false, filed: false, indexed: false, code: 'capture_failed',
    error: 'there is a file sitting where the captures folder should be (file exists)',
    answer: 'It did not go in, sir - there is a file sitting where the captures folder should be ' +
            '(file exists). Nothing was written, and I would rather tell you than let you think ' +
            'that thought was safe.',
    line: '', hint: 'Nothing was indexed, so /chat cannot answer from it either.'
  };
  failure.line = failure.answer;
  const fetchImpl = makeFetch({failWith: failure});
  const app = await boot({fetchImpl});
  const before = app.app.capture.nodes().length;
  const flyBefore = app.app.lastFlyTo();
  await ask(app, 'remember that this one will not fit');

  check(app.app.capture.nodes().length === before, 'a failed capture adds no star (' + before + ')');
  check(app.app.lastFlyTo() === flyBefore, 'and the camera does not move');
  const answer = app.elements.get('answer-text').textContent;
  check(answer === failure.answer, 'the failure is on screen, word for word');
  check(app.elements.get('answer-text').className.indexOf('failed') >= 0,
        'and styled as a failure, not as an answer');
  check(app.speech.words().indexOf(failure.answer) >= 0,
        'IT IS SPOKEN OUT LOUD - a failed capture is never silent');
  check(app.app.capture.foot() === failure.hint, 'with the hint in the footer: "' + app.app.capture.foot() + '"');
  check(app.app.capture.lastBirth() === null, 'nothing was recorded as born');
  check(app.elements.get('q').disabled === false, 'and the ask bar works again afterwards');

  /* the "remember that" with nothing after it, refused by the server */
  const emptyImpl = makeFetch({failWith: {
    ok: false, captured: false, filed: false, indexed: false, code: 'nothing_to_remember',
    error: 'There was nothing after the word remember.',
    answer: 'Remember what exactly, sir? There was nothing after the word \u201cremember\u201d for me to write down.',
    line: 'Remember what exactly, sir? There was nothing after the word \u201cremember\u201d for me to write down.'
  }});
  const app2 = await boot({fetchImpl: emptyImpl});
  await ask(app2, 'remember that');
  check(app2.elements.get('answer-text').textContent.indexOf('Remember what exactly') === 0,
        'an empty "remember that" gets a spoken refusal, not an empty note');
  check(app2.app.capture.nodes().length === GRAPH_DATA.nodes.length, 'and nothing is added to the galaxy');
}

/* =============================================== 4. ?mute=1 still files the note */
{
  const fetchImpl = makeFetch();
  const app = await boot({fetchImpl, search: '?mute=1'});
  await ask(app, 'remember that the finish window should be 900 milliseconds');
  check(app.app.capture.nodes().length === GRAPH_DATA.nodes.length + 1,
        'a muted tab still writes the note and grows the galaxy');
  check(app.speech.words().length === 0, 'it just never says a word about it');
}

/* ================================= 5. notes filed since the last build.py come back */
{
  const entry = capturePayload();
  const fetchImpl = async (url) => {
    if (String(url).indexOf('/health') >= 0)
      return {ok: true, status: 200, json: async () => ({
        ok: true, notes: GRAPH_DATA.nodes.length + 1, key: {state: 'set'},
        greeting: 'Good morning, sir. 13 notes indexed, all present and accounted for.',
        captures: [entry], graph_file: 'behind'
      })};
    return {ok: true, status: 200, json: async () => FOLLOW_UP};
  };
  const app = await boot({fetchImpl});
  const nodes = app.app.capture.nodes();
  check(nodes.length === GRAPH_DATA.nodes.length + 1,
        'a reload brings back a note the graph file has not caught up with (' + nodes.length + ' nodes)');
  check(nodes.some(n => n.label === LABEL), 'with its title and its links intact');
  check(app.app.capture.lastBirth() === null, 'quietly: no glow, no camera flight at boot');
  check(app.app.lastFlyTo() === null, 'and the camera is left where the galaxy put it');
}

/* ================================================== 6. it is still a question, too */
{
  const fetchImpl = makeFetch();
  const app = await boot({fetchImpl});
  for (const q of ['what is the finish window?', 'remind me about the window', 'my notes about the window']){
    check(!app.app.capture.isRemember(q), 'not a capture: "' + q + '"');
  }
  await ask(app, 'what is the finish window?');
  check(fetchImpl.calls.filter(c => c.url === '/chat').length === 1 &&
        fetchImpl.calls.filter(c => c.url === '/remember').length === 0,
        'an ordinary question still goes to the brain');
  check(app.app.capture.nodes().length === GRAPH_DATA.nodes.length, 'and adds nothing to the galaxy');
  check(app.app.capture.thoughtOf('  Remember, that the kettle is on  ') === 'the kettle is on',
        'the trigger is stripped from the thought, not left in the note');
  check(defaultCapture({notes: 12}).line.indexOf('13 notes strong') > 0,
        'the harness default payload is honest about the count');
}

/* ------------------------------------------------------------------- result */
const fails = results.filter(([s]) => s === 'FAIL');
console.log('\n  %d checks, %d passed, %d failed', results.length, results.length - fails.length, fails.length);
if (fails.length){
  console.log('\n  RESULT: FAILED');
  fails.forEach(([, m]) => console.log('   - ' + m));
  process.exit(1);
}
console.log('\n  RESULT: PASSED - a note filed by voice or typing lands in the galaxy, glows, ' +
            'flies, and never fails silently');
