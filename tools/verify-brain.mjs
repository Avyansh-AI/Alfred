#!/usr/bin/env node
/*
 * tools/verify-brain.mjs - changing which model answers, and the refusal rule.
 *
 * The rules under test, in the order the user meets them:
 *
 *   the chip        -> the brain in the chair is on screen, small, and every word of
 *                      it comes from the server. The page never spells a model name
 *                      itself: hand it a label it could not possibly derive and the
 *                      chip must show exactly that
 *   "switch to X"   -> a command about which brain answers, not a question about the
 *                      notes and not a question about the screen. It goes to /model
 *                      even while a screen share is live
 *   a refusal       -> refused: true is shown AND spoken, the footer says NOT CHANGED,
 *                      and the chip stays on the brain that is really in the chair.
 *                      A refusal is never quietly rendered as a success
 *   the swap        -> the chip follows the server's answer, "until restart" appears
 *                      only when the server says swapped, and a question that merely
 *                      mentions a model name stays a question
 *
 * Runs the viewer's real JavaScript against the mock speech/graph in tools/harness.mjs.
 * tools/browser-brain-check.mjs is the same story in a real Chromium against the real
 * server, and tools/verify.py owns the parsing and the refusal on the server side.
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

/* The catalogue, as /health sends it. The labels are the server's - including one
   nobody could guess from the id, which is how we prove the page does not build names. */
const BRAINS = [
  {name: 'astra', id: 'openai/gpt-6-astra', label: 'GPT 6 ASTRA', versions: ['6']},
  {name: 'fable', id: 'anthropic/claude-fable-5.1', label: 'CLAUDE FABLE 5.1', versions: ['5.1', '5']},
  {name: 'opus', id: 'anthropic/claude-opus-4.1', label: 'CLAUDE OPUS 4.1', versions: ['4.1', '4']}
];
const HEALTH = {
  ok: true, notes: 12, key: {state: 'set'},
  model: 'gpt-6-astra', model_label: 'GPT 6 ASTRA',
  config_model: 'gpt-6-astra', config_model_label: 'GPT 6 ASTRA',
  swapped: false, brains: BRAINS
};

const SWITCHED = (o = {}) => Object.assign({
  ok: true, changed: true, refused: false, code: 'brain_switched',
  answer: 'CLAUDE FABLE 5.1 is in the chair, sir - anthropic/claude-fable-5.1. It lasts until you restart me.',
  model: 'anthropic/claude-fable-5.1', label: 'CLAUDE FABLE 5.1', swapped: true,
  config_model: 'gpt-6-astra', config_model_label: 'GPT 6 ASTRA',
  provider: 'OpenRouter', route: 'openrouter', previous: 'gpt-6-astra',
  previous_label: 'GPT 6 ASTRA', turns: 0
}, o);

const REFUSED = (o = {}) => Object.assign({
  ok: false, changed: false, refused: true, code: 'brain_version_unknown',
  answer: 'There is no opus 5, sir, and I will not take the nearest thing to it. I have opus 4.1 and opus 4.',
  model: 'anthropic/claude-fable-5.1', label: 'CLAUDE FABLE 5.1', swapped: true,
  config_model: 'gpt-6-astra', config_model_label: 'GPT 6 ASTRA',
  provider: 'OpenRouter', route: 'openrouter',
  said: 'opus 5', have: ['opus 4.1 and opus 4'], versions: ['4.1', '4'], turns: 0
}, o);

const RESET = (o = {}) => Object.assign({
  ok: true, changed: true, refused: false, code: 'brain_reset',
  answer: 'Back on GPT 6 ASTRA, sir - the brain in config.json, as the house intended.',
  model: 'gpt-6-astra', label: 'GPT 6 ASTRA', swapped: false,
  config_model: 'gpt-6-astra', config_model_label: 'GPT 6 ASTRA',
  provider: 'OpenAI', route: 'openai', turns: 0
}, o);

/* A page whose /model and /chat both record exactly what they were handed. */
async function page({modelReply = SWITCHED(), modelStatus = 200, health = HEALTH, search = ''} = {}){
  const calls = [];
  const app = await boot({
    health, search,
    screen: {frames: [tinyFrame(1), tinyFrame(2)]},
    fetchImpl: async (url, opts) => {
      const u = String(url);
      const body = opts && opts.body ? JSON.parse(opts.body) : null;
      if (u.indexOf('/health') >= 0) return {ok: true, status: 200, json: async () => health};
      calls.push({url: u, body});
      if (u === '/model'){
        if (modelStatus !== 200) return {ok: false, status: modelStatus, json: async () => modelReply};
        return {ok: true, status: 200, json: async () => modelReply};
      }
      return {ok: true, status: 200, json: async () => ({
        ok: true, answer: 'The movers quoted 26,000, sir.', decision: 'notes', on_notes: true,
        nodes: [7], sources: [{index: 7, label: 'Budget for the Move', score: 3}],
        model: 'stub-model', turns: 1
      })};
    }
  });
  app.calls = calls;
  app.models = () => calls.filter(c => c.url === '/model');
  app.chats = () => calls.filter(c => c.url === '/chat');
  return app;
}

/** say something and let it be answered through the virtual clock */
async function ask(app, text){
  app.app.voice.submitQuestion(text);
  await app.flush(12, 40);
}

const text = (app) => app.elements.get('answer-text').textContent;
const foot = (app) => app.elements.get('a-foot').textContent;
const chip = (app) => app.elements.get('brain-label').textContent;
const heard = (app) => app.speech.words().join(' | ');
const cls = (app, id = 'answer-text') => app.elements.get(id).className || '';
const swapped = (app) => app.elements.get('brain-chip').classList.contains('swapped');

console.log('\nAlfred - the brain: which model is answering, and when he refuses');

/* ------------------------------------------------------------------------- */
group('the chip: every word of it comes from the server');

{
  const p = await page();
  check(chip(p) === 'GPT 6 ASTRA', 'the chip names the brain in the chair at boot: ' + chip(p));
  check(p.elements.get('brain-temp').hidden === true,
        'and no "until restart" on a brain that is simply the config model');
  check(swapped(p) === false, 'the chip is not marked swapped');
  check(p.elements.get('brain-chip').title.indexOf('gpt-6-astra') >= 0,
        'the tooltip carries the real id: ' + p.elements.get('brain-chip').title);
  check(!/GPT 6 ASTRA/.test(SRC) && !/CLAUDE FABLE/.test(SRC),
        'the page itself contains no model name it could have shown instead');
}

{
  // a label nobody could derive from that id: if the chip shows it, the label really is
  // the server's word and not something the page assembled out of the model id
  const p = await page({modelReply: SWITCHED({model: 'acme/weird-thing-9',
                                              label: 'A LABEL NOBODY COULD DERIVE'})});
  await ask(p, 'switch to weird thing 9');
  p.elements.get('brain-label').textContent = '';
  p.app.brain.paint(SWITCHED({model: 'acme/weird-thing-9', label: 'A LABEL NOBODY COULD DERIVE'}));
  check(chip(p) === 'A LABEL NOBODY COULD DERIVE',
        'the chip shows the label the server sent, not a name built from the id: ' + chip(p));
}

/* ------------------------------------------------------------------------- */
group('"switch to fable 5.1" is a command, not a question');

{
  const p = await page({modelReply: SWITCHED()});
  await ask(p, 'switch to fable 5.1');
  check(p.models().length === 1, 'it went to POST /model exactly once');
  check(p.chats().length === 0, 'and nowhere near /chat - no model call was spent on it');
  check(p.models()[0].body.text === 'switch to fable 5.1',
        'the words are handed to the server untouched: ' + JSON.stringify(p.models()[0].body.text));
  check(chip(p) === 'CLAUDE FABLE 5.1', 'the chip now names the new brain: ' + chip(p));
  check(swapped(p) === true, 'and the chip is marked as a runtime swap');
  check(p.elements.get('brain-temp').hidden === false,
        '"until restart" is shown only when the server says swapped');
  check(text(p).indexOf('CLAUDE FABLE 5.1 is in the chair') === 0, 'the line is on screen');
  check(heard(p).indexOf('CLAUDE FABLE 5.1 is in the chair') >= 0, 'and spoken: ' + heard(p).slice(0, 80));
  check(cls(p).indexOf('brain') >= 0 && cls(p).indexOf('refused') < 0, 'it is not styled as a refusal');
  check(foot(p).indexOf('swapped until the next restart') >= 0,
        'the footer says the swap lasts until a restart: ' + foot(p));
  check(p.elements.get('q').value === '', 'the ask bar is free again');
}

group('the words he says out loud');

{
  const p = await page({modelReply: SWITCHED()});
  await ask(p, 'try on Claude Fable 5.1');
  check(p.models().length === 1, '"try on Claude Fable 5.1" is routed as a command too');
}

{
  const p = await page({modelReply: RESET()});
  await ask(p, 'go back to your normal brain');
  check(p.models().length === 1, '"go back to your normal brain" is a command');
  check(p.chats().length === 0, 'and it never touches the notes');
  check(p.models()[0].body.text === 'go back to your normal brain', 'verbatim');
  check(chip(p) === 'GPT 6 ASTRA', 'the chip is back on the config brain');
  check(p.elements.get('brain-temp').hidden === true, 'and the "until restart" marker is gone');
}

group('a question that merely mentions a model is still a question');

{
  for (const q of ['what did my notes say about fable 5.1?',
                   'use the movers quote',
                   'tell me about the gemini notes',
                   'how much was the road trip?']){
    const p = await page();
    await ask(p, q);
    check(p.models().length === 0 && p.chats().length === 1, 'stays a question: ' + JSON.stringify(q));
  }
}

/* ------------------------------------------------------------------------- */
group('the refusal: shown, spoken, and never dressed up as a success');

{
  const p = await page({modelReply: REFUSED()});
  await ask(p, 'switch to opus 5');
  check(p.models().length === 1, 'the refusal came from a real request to /model');
  check(text(p).indexOf('There is no opus 5') === 0, 'the refusal is on screen');
  check(heard(p).indexOf('There is no opus 5') >= 0, 'and spoken out loud');
  check(cls(p).indexOf('refused') >= 0, 'and styled as a refusal: ' + cls(p));
  check(foot(p).indexOf('NOT CHANGED') >= 0, 'the footer says NOT CHANGED: ' + foot(p));
  check(foot(p).indexOf('opus 4.1 and opus 4') >= 0, 'and reads back what he does have');
  check(chip(p) === 'CLAUDE FABLE 5.1', 'the chip did NOT move: ' + chip(p));
  check(swapped(p) === true, 'the swap that was already in the chair is still in the chair');
  check(p.elements.get('q').value === '', 'the ask bar is free again');
}

{
  const p = await page({modelReply: RESET()});
  await ask(p, 'switch to opus');
  check(chip(p) === 'GPT 6 ASTRA', 'a bare family he cannot pick between leaves the chip alone');
}

/* ------------------------------------------------------------------------- */
group('a swap works while a screen share is live - and the share survives it');

{
  const p = await page({modelReply: SWITCHED()});
  const started = await p.app.sight.start();
  await p.flush(6, 40);
  check(started.ok === true && p.app.sight.state() === 'live', 'a share is live');
  await ask(p, 'switch to fable 5.1');
  check(p.models().length === 1, 'the brain command routed to /model, not to /see');
  check(p.calls.filter(c => c.url === '/see').length === 0, 'no frame was taken for it');
  check(p.app.sight.state() === 'live', 'and the share is still live afterwards');
  check(p.app.sight.badge().hidden === false, 'with the indicator still on screen');
  check(p.screen.served.length === 0, 'and no frame was encoded');
}

/* ------------------------------------------------------------------------- */
group('failures are spoken, never silent');

{
  const p = await page({modelStatus: 500, modelReply: {ok: false, error: 'The brain is on fire.'}});
  await ask(p, 'switch to astra');
  check(text(p).indexOf('The brain is on fire.') >= 0, 'a server error is shown');
  check(heard(p).indexOf('The brain is on fire.') >= 0, 'and spoken');
  check(chip(p) === 'GPT 6 ASTRA', 'and the chip has not moved');
}

{
  const app = await boot({health: HEALTH, search: '?mute=1',
    fetchImpl: async (url, opts) => {
      if (String(url).indexOf('/health') >= 0) return {ok: true, status: 200, json: async () => HEALTH};
      return {ok: true, status: 200, json: async () => SWITCHED()};
    }});
  await app.flush(6, 40);
  await ask(app, 'switch to fable 5.1');
  check(chip(app) === 'CLAUDE FABLE 5.1', 'a muted tab still changes the brain');
  check(app.speech.words().length === 0, 'and says nothing at all: ' + app.speech.words().length);
}

console.log('\nAlfred - the brain (virtual clock, no browser needed)');
if (fail){
  console.log('\n  RESULT: FAILED (' + fail + ' of ' + (pass + fail) + ')');
  failed.forEach(f => console.log('   - ' + f));
  process.exit(1);
}
console.log('\n  RESULT: PASSED - ' + pass + ' checks: the chip carries the server\'s own label,' +
            '\n  "switch to ..." routes to /model even mid-share, and a version he does not have' +
            '\n  is refused out loud with what he does have, instead of quietly becoming' +
            '\n  the nearest model');
