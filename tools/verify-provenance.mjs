#!/usr/bin/env node
/*
 * tools/verify-provenance.mjs - does the galaxy prove where an answer came from?
 *
 * The rules under test:
 *
 *   one to three supporting notes  -> fly to the top one, light it and its direct
 *                                     neighbours, open its side panel, while the
 *                                     answer is still being spoken
 *   four or more                   -> do not fly anywhere: light the whole cluster
 *                                     (flying to one of six would be a lie about where
 *                                     the answer came from)
 *   no notes / small talk          -> the galaxy does not move at all
 *
 * and, running through all of it: the decision ("was this question about my notes?")
 * is taken BEFORE any camera code can see it, the note is never read aloud, and the
 * spoken answer stays short.
 *
 * Runs the viewer's real JavaScript against the mock graph/speech/recognition in
 * tools/harness.mjs. No browser needed - tools/browser-provenance-check.mjs is the
 * same story in a real Chromium.
 */
import fs from 'node:fs';
import path from 'node:path';
import { boot } from './harness.mjs';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const SRC = fs.readFileSync(path.join(ROOT, 'viewer', 'index.html'), 'utf8');

let pass = 0, fail = 0;
const failed = [];
const ok = (m) => { pass++; console.log('  ok    ' + m); };
const bad = (m) => { fail++; failed.push(m); console.log('  FAIL  ' + m); };
const check = (cond, m) => (cond ? ok(m) : bad(m));
const group = (name) => console.log('\n' + name);

/* A page whose /chat replies with exactly what the test says it did. */
async function page(reply, bootOptions = {}){
  const asked = [];
  const app = await boot({
    ...bootOptions,
    fetchImpl: async (url, opts) => {
      if (String(url).indexOf('/health') >= 0)
        return {ok: true, status: 200, json: async () => ({ok: true, notes: 12, key: {state: 'set'}})};
      asked.push(opts && opts.body ? JSON.parse(opts.body).question : null);
      return {ok: true, status: 200, json: async () => reply};
    }
  });
  app.asked = asked;
  return app;
}

async function ask(app, question = 'what is the budget for the move?'){
  // fire and forget, then run the virtual clock: submitQuestion() waits for the
  // utterance to end, and the utterance only ends when the clock moves.
  app.app.voice.submitQuestion(question);
  await app.flush(10, 40);
  return app.app.provenance.last();
}

/* the nodes a real answer would light: the note plus everything joined to it */
function neighboursOf(app, id){
  const G = app.sandbox.window.GRAPH;
  const set = new Set([id]);
  G.links.forEach(l => {
    if (l.source === id) set.add(l.target);
    if (l.target === id) set.add(l.source);
  });
  return Array.from(set).sort((a, b) => a - b);
}
const labelOf = (app, id) => app.sandbox.window.GRAPH.nodes[id].label;
const excerptOf = (app, id) => app.sandbox.window.GRAPH.nodes[id].excerpt || '';
const flew = (app) => app.app.lastFlyTo() !== null || app.graph.recorded.calls.includes('cameraPosition');

const notes = (indexes, scores) => indexes.map((i, k) => ({index: i, label: 'NOTE ' + i, score: (scores || [])[k] || 1}));
const payload = (o = {}) => Object.assign({
  ok: true,
  answer: 'The movers quoted 26,000 including insurance for the TV and the fridge.',
  on_notes: true, decision: 'notes',
  nodes: [3], sources: notes([3]), read: [3, 0, 7, 2, 4, 8],
  model: 'stub-model', turns: 1
}, o);

console.log('\nAlfred - provenance: does the galaxy prove where an answer came from?');

/* ------------------------------------------------------------------------- */
group('one note: the camera flies to it, lights its neighbours and opens it');
{
  const p = await page(payload());
  const plan = await ask(p);

  check(plan && plan.kind === 'single', 'the answer is judged to have come from a single note');
  check(plan && plan.node && plan.node.index === 3, 'and that note is the top source (#3)');
  check(flew(p), 'the camera flew to it');
  check(p.app.lastFlyTo() && p.app.lastFlyTo().node && p.graph.recorded.calls.includes('cameraPosition'),
        'the flight is a real cameraPosition tween, not just a highlight');
  check(p.app.provenance.panelOpen(), 'its side panel opened');
  check(p.app.provenance.panelLabel() === labelOf(p, 3),
        'the panel shows the note the answer came from: "' + p.app.provenance.panelLabel() + '"');

  const expected = neighboursOf(p, 3);
  const lit = p.app.provenance.lit();
  check(lit.join(',') === expected.join(','),
        'the note and its ' + (expected.length - 1) + ' direct neighbours are lit: [' + lit.join(', ') + ']');
  check(p.app.provenance.litLinks().length > 0, 'the links from the note to those neighbours are lit too');

  const heard = p.speech.words();
  check(heard.length === 1 && heard[0] === payload().answer,
        'exactly one thing is spoken, and it is the answer');
  check(!heard.join(' ').includes(excerptOf(p, 3).slice(0, 40)),
        'the note itself is never read aloud');
  check(p.elements.get('a-foot').textContent.includes('from 1 note'),
        'the answer says what it came from: "' + p.elements.get('a-foot').textContent + '"');
  check(p.elements.get('a-foot').textContent.includes('read 6'),
        'and how many notes it was shown, so "read 6, used 1" is visible');
}

/* ------------------------------------------------------------------------- */
group('two or three notes: still the top one, not a cluster');
{
  const p = await page(payload({nodes: [7, 3, 2], sources: notes([7, 3, 2])}));
  const plan = await ask(p);
  check(plan.kind === 'single', 'three notes is still a single-source answer');
  check(plan.node.index === 7, 'and the camera goes to the FIRST (best-scoring) one, #7');
  check(p.app.provenance.panelLabel() === labelOf(p, 7), 'the panel opens on #7: "' + labelOf(p, 7) + '"');
  const lit = p.app.provenance.lit();
  check(lit.join(',') === neighboursOf(p, 7).join(','),
        'the lighting is #7 and its own neighbours, not the other two sources');
}

/* ------------------------------------------------------------------------- */
group('four or more notes: light the cluster, move nothing');
{
  const four = [10, 7, 3, 4];
  const p = await page(payload({nodes: four, sources: notes(four), read: four.concat([8, 0])}));
  const plan = await ask(p);

  check(plan.kind === 'cluster', 'four notes is judged a cluster');
  check(plan.nodes.length === 4, 'and all four are in the plan');
  check(!flew(p) && p.app.lastFlyTo() === null, 'the camera did not fly anywhere');
  check(!p.graph.recorded.calls.includes('cameraPosition'), 'no camera tween was even started');
  check(!p.app.provenance.panelOpen(), 'the panel stays closed - it can only name one note');
  const lit = p.app.provenance.lit();
  check(lit.join(',') === [3, 4, 7, 10].join(','),
        'the whole cluster is lit instead: [' + lit.join(', ') + ']');
  check(p.elements.get('a-foot').textContent.includes('from 4 notes'),
        'and the answer says so: "' + p.elements.get('a-foot').textContent + '"');
  const heard = p.speech.words();
  check(heard.length === 1 && heard[0] === payload().answer, 'the answer is still spoken, once');

  const six = [10, 7, 3, 4, 8, 0];
  const p6 = await page(payload({nodes: six, sources: notes(six)}));
  await ask(p6);
  check(p6.app.provenance.last().kind === 'cluster', 'six notes is a cluster too');
  check(p6.app.provenance.lit().join(',') === [0, 3, 4, 7, 8, 10].join(','), 'all six are lit');
  check(!flew(p6), 'and the camera is still exactly where it was');
}

/* ------------------------------------------------------------------------- */
group('small talk holds the graph perfectly still');
{
  const p = await page(payload({
    answer: 'Good morning! What would you like to look into?',
    on_notes: false, decision: 'small_talk', nodes: [], sources: [], read: []
  }));
  const plan = await ask(p, 'good morning');

  check(plan.kind === 'still', 'a greeting is not a question about the notes');
  check(plan.why === 'small talk', 'and the reason is kept: "' + plan.why + '"');
  check(!flew(p), 'the camera did not move');
  check(p.app.provenance.lit().length === 0, 'nothing was lit');
  check(p.app.provenance.litLinks().length === 0, 'no link was lit either');
  check(!p.app.provenance.panelOpen(), 'no panel opened');
  check(p.elements.get('answer-text').textContent.startsWith('Good morning!'), 'the answer still appears on screen');
  check(p.speech.words().join(' ').includes('Good morning!'), 'and is still spoken');
  check(p.elements.get('a-foot').textContent.includes('no notes used'),
        'the answer states that no notes were used: "' + p.elements.get('a-foot').textContent + '"');
}

/* ------------------------------------------------------------------------- */
group('the server\'s decision wins over the list it sends');
{
  // a reply that contradicts itself: on_notes false but nodes present
  const p = await page(payload({on_notes: false, decision: 'no_match', nodes: [3, 7]}));
  const plan = await ask(p, 'what is the capital of France?');
  check(plan.kind === 'still', 'on_notes:false means still, whatever the node list says');
  check(!flew(p) && p.app.provenance.lit().length === 0, 'the camera and the lights stay untouched');
  check(p.elements.get('answer-text').textContent.length > 0, 'the answer is still shown and spoken');
  check(p.speech.words().length === 1, 'exactly one utterance: the model\'s answer');
}

/* ------------------------------------------------------------------------- */
group('an error is never dressed up as an answer');
{
  const p = await page({
    ok: false, error: 'The brain has no API key yet - config.json still holds the placeholder.',
    code: 'placeholder_api_key', answer: 'The brain has no API key yet - config.json still holds the placeholder.',
    hint: 'Open config.json and paste your real key.', on_notes: true, decision: 'notes',
    nodes: [3], sources: notes([3]), read: [3, 0]
  });
  const plan = await ask(p);
  check(plan.kind === 'still', 'no answer means no provenance claim');
  check(plan.why === 'no answer', 'and the plan says why: "' + plan.why + '"');
  check(!flew(p), 'the camera did not move for a message that is not an answer');
  check(p.app.provenance.lit().length === 0, 'and no note was lit up as a source');
  check(p.elements.get('answer-text').textContent.includes('no API key'), 'the error is shown on screen');
  check(p.speech.words().length === 1, 'and spoken');
}

/* ------------------------------------------------------------------------- */
group('the spoken answer is the answer, never the note');
{
  const p = await page(payload());
  await ask(p);

  const short = p.app.provenance.spokenText('The movers quoted 26,000.');
  check(short.trimmed === false && short.text === 'The movers quoted 26,000.', 'a short answer is read as it is');

  // deliberately longer than SPEAK_MAX_CHARS, so the ear gets the short version
  const long = 'The movers quoted 26,000 including insurance for the TV and the fridge. ' +
    'Vande Bharat tickets for three come to 4,200, and the flat deposit is 20,000 with the first month at 9,500. ' +
    'The geyser, stabiliser and a small almirah add 12,000, and miscellaneous tips are another 3,000. ' +
    'That lands at about 74,700, which is above the 55,000 told to everyone, so the sofa and the dining table go on OLX. ' +
    'The old flat deposit should return 14,000 within a month of handing over the keys, which is not counted as ' +
    'income until it lands, and the rule for the next two months is no new furniture beyond the almirah.';
  check(long.length > 480, 'the test answer is longer than the spoken budget (' + long.length + ' chars)');
  const trimmed = p.app.provenance.spokenText(long);
  check(trimmed.trimmed === true, 'a long answer is trimmed for the ear');
  check(trimmed.text.length <= p.app.provenance.SPEAK_MAX_CHARS,
        'the spoken version stays within ' + p.app.provenance.SPEAK_MAX_CHARS + ' characters');
  check(/[.!?]$/.test(trimmed.text), 'and it stops at the end of a sentence, never mid-sentence: "…' +
        trimmed.text.slice(-42) + '"');
  check(long.startsWith(trimmed.text.slice(0, 40)), 'it is the beginning of the answer, not a summary invented by us');

  const monster = 'This note ' + 'runs on '.repeat(80) + 'and never ends.';
  const whole = p.app.provenance.spokenText(monster);
  check(whole.trimmed === false, 'a single monster sentence is read out rather than cut in half');

  // neverEnds: the utterance is still "being spoken" when we look, so the status line
  // can be read at the moment it matters
  const p2 = await page(payload({answer: long}), {neverEnds: true});
  await ask(p2);
  check(p2.elements.get('answer-text').textContent === long, 'the FULL answer is on screen');
  check(p2.speech.words()[0].length < long.length, 'while only the short version is spoken');
  check(p2.app.voice.status() === 'speaking' && p2.app.voice.detail().includes('short version'),
        'and the status line says so while it talks: "' + p2.app.voice.detail() + '"');
}

/* ------------------------------------------------------------------------- */
group('deciding and acting are separate (the decision happens first)');
{
  const p = await page(payload({nodes: [7, 3], sources: notes([7, 3])}));
  const data = payload({nodes: [7, 3], sources: notes([7, 3])});

  const plan = p.app.provenance.answerSource(data);
  check(plan.kind === 'single' && plan.node.index === 7, 'answerSource() decides on its own');
  check(p.app.lastFlyTo() === null && !p.graph.recorded.calls.includes('cameraPosition'),
        'and it moved nothing while deciding');
  check(p.app.provenance.lit().length === 0, 'nothing was lit by the decision');
  check(!p.app.provenance.panelOpen(), 'no panel was opened by the decision');

  p.app.provenance.showAnswerSource(plan);
  check(p.app.lastFlyTo() !== null, 'only showAnswerSource() moves the camera');

  const still = p.app.provenance.answerSource(payload({on_notes: false, nodes: []}));
  const before = p.graph.recorded.calls.length;
  p.app.provenance.showAnswerSource(still);
  check(still.kind === 'still' && p.graph.recorded.calls.length === before,
        'and asked to hold still it does nothing at all');

  check(/const plan = answerSource\(data\);[\s\S]{0,220}?showAnswerSource\(plan\);/.test(SRC),
        'the source reads decide-then-act, in that order, in one place');
}

/* ------------------------------------------------------------------------- */
group('the code keeps its promises');
{
  const defs = (SRC.match(/const PROVENANCE_FLY_MAX = (\d+);/g) || []);
  check(defs.length === 1 && /3/.test(defs[0]),
        'PROVENANCE_FLY_MAX is defined exactly once, as 3: ' + defs.join(' '));
  check((SRC.match(/PROVENANCE_FLY_MAX/g) || []).length >= 2,
        'and the rule actually uses the constant rather than a bare 4');
  const speak = (SRC.match(/const SPEAK_MAX_CHARS = (\d+);/g) || []);
  check(speak.length === 1 && /480/.test(speak[0]), 'SPEAK_MAX_CHARS is defined exactly once, as 480');

  const speakCalls = SRC.split('\n').filter(l => /\bspeak\(/.test(l) && !/^\s*(\/\/|\*)/.test(l));
  check(speakCalls.length > 0, 'the answer paths call speak() from ' + speakCalls.length + ' lines');
  check(!speakCalls.some(l => /excerpt/.test(l)), 'and never with a note excerpt');
  check(!/speak\([^)]*panel-excerpt/.test(SRC), 'nothing passes the panel\'s note text to the voice');
}

/* ------------------------------------------------------------------------- */
console.log('\n  ' + pass + ' checks, ' + pass + ' passed, ' + fail + ' failed');
if (fail){
  console.log('\n  RESULT: FAILED');
  failed.forEach(m => console.log('   - ' + m));
  process.exit(1);
}
console.log('\n  RESULT: PASSED - the galaxy shows where each answer came from, and holds still when there is nothing to show');
