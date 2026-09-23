#!/usr/bin/env node
/*
 * tools/verify-voice.mjs - the speech layer, tested exactly.
 *
 * Everything here runs on a virtual clock, so the FINISH_MS buffering rule is
 * checked to the millisecond instead of "wait and hope": a fragment does not
 * dispatch, more speech restarts the window, and the pause that ends the thought
 * sends the whole sentence - once.
 *
 *   node tools/verify-voice.mjs
 */
import { boot } from './harness.mjs';

const problems = [], notes = [];
const ok = (m) => notes.push('  ok    ' + m);
const bad = (m) => { problems.push(m); notes.push('  FAIL  ' + m); };
const check = (cond, m) => (cond ? ok(m) : bad(m));

const BRITISH = [
  {name: 'Daniel', lang: 'en-GB'},
  {name: 'Samantha', lang: 'en-US'},
  {name: 'Lekha', lang: 'hi-IN'}
];

async function ask(app, question){
  app.app.voice.submitQuestion(question);
  await app.flush(4, 30);               // resolve the fetch chain and the utterance
}

/* ============================================================ 1. the constants */
{
  const app = await boot({ voices: BRITISH });
  const v = app.app.voice;
  check(v.FINISH_MS === 900, 'FINISH_MS is 900 (' + v.FINISH_MS + ')');
  check(v.RECOGNITION_LANG === 'en-IN', 'recognition language is en-IN (' + v.RECOGNITION_LANG + ')');
  check(Array.isArray(v.INTERRUPT_WORDS) && v.INTERRUPT_WORDS.includes('stop') && v.INTERRUPT_WORDS.includes('wait'),
        'interrupt words include stop and wait');

  const src = (await import('node:fs')).readFileSync(new URL('../viewer/index.html', import.meta.url), 'utf8');
  const definitions = src.match(/const FINISH_MS\s*=/g) || [];
  check(definitions.length === 1, 'FINISH_MS is defined exactly once (' + definitions.length + ')');
  // the constants sit at the top of the file; the speech implementation sits further
  // down, and must never repeat the number - it refers to FINISH_MS by name
  const constants = src.slice(src.indexOf('VOICE - speaking and listening'), src.indexOf('const $ = (id)'));
  const impl = src.slice(src.indexOf('const voiceEl'), src.indexOf('the brain UI'));
  check((constants.match(/\b900\b/g) || []).length === 1,
        'the finish window is written as 900 in exactly one place: its definition');
  check(!/\b900\b/.test(impl),
        'the speech code itself never repeats a bare 900 - it uses FINISH_MS by name');
  const uses = (src.match(/FINISH_MS/g) || []).length;
  check(uses >= 3, 'FINISH_MS is the constant actually used by the buffer (' + uses + ' references)');
}

/* ================================================= 2. the mid-sentence pause rule */
{
  const app = await boot({ voices: BRITISH });
  const rec = app.recognition;
  app.app.voice.startListening();
  const recognizer = rec.latest();
  check(!!recognizer, 'clicking the mic starts a recognition session');
  check(app.app.voice.status() === 'listening', 'status says listening');

  // first fragment finalises - the naive implementation sends here
  recognizer.emitFinal('what did the movers');
  app.clock.advance(400);
  check(app.chatCalls().length === 0, 'the first finalised fragment does NOT dispatch');
  check(app.app.voice.buffer() === 'what did the movers', 'the fragment is buffered ("' + app.app.voice.buffer() + '")');

  // the user pauses mid-sentence and carries on before the window closes
  recognizer.emitFinal('quote for the road trip');
  app.clock.advance(400);
  check(app.chatCalls().length === 0, 'still nothing 800ms in: the window restarted on new speech');
  check(app.app.voice.buffer() === 'what did the movers quote for the road trip',
        'the fragments were appended, not replaced ("' + app.app.voice.buffer() + '")');

  // now a real pause: the window closes and the whole sentence goes
  app.clock.advance(1000);
  check(app.chatCalls().length === 1, 'the pause that ends the thought dispatches exactly one question');
  check(app.chatCalls()[0].body.question === 'what did the movers quote for the road trip',
        'the whole combined sentence was sent: "' + (app.chatCalls()[0].body.question || '') + '"');
  check(app.app.voice.buffer() === '', 'the buffer is empty after sending');

  // a pause under the window must never send
  const app2 = await boot({ voices: BRITISH });
  app2.app.voice.startListening();
  const r2 = app2.recognition.latest();
  r2.emitFinal('three bags');
  app2.clock.advance(899);
  check(app2.chatCalls().length === 0, 'a 899ms pause does not send (one millisecond short)');
  app2.clock.advance(2);
  check(app2.chatCalls().length === 1, 'a 901ms pause does send');

  // three short fragments stitched into one question
  const app3 = await boot({ voices: BRITISH });
  app3.app.voice.startListening();
  const r3 = app3.recognition.latest();
  r3.emitFinal('book the'); app3.clock.advance(300);
  r3.emitFinal('train to'); app3.clock.advance(300);
  r3.emitFinal('ayodhya'); app3.clock.advance(1200);
  check(app3.chatCalls().length === 1 && app3.chatCalls()[0].body.question === 'book the train to ayodhya',
        'three fragments stitched into one question: "' + (app3.chatCalls()[0] || {}).body?.question + '"');
}

/* ============================================================ 3. interrupt words */
{
  const app = await boot({ voices: BRITISH });
  app.app.voice.startListening();
  const rec = app.recognition.latest();

  rec.emitFinal('what did the movers');
  app.clock.advance(200);
  check(app.app.voice.buffer() === 'what did the movers', 'a fragment is buffered before the interrupt');

  rec.emitFinal('stop');
  check(app.chatCalls().length === 0, '"stop" does not get sent to the brain');
  check(app.app.voice.buffer() === '', '"stop" bypassed the buffer and cleared it immediately');
  check(app.app.voice.status() === 'stopped', 'the status line says stopped');
  app.clock.advance(2000);
  check(app.chatCalls().length === 0, 'nothing fires afterwards: the pending window was cancelled');

  // an interrupt spoken mid-utterance (interim) must also act at once
  const app2 = await boot({ voices: BRITISH });
  app2.app.voice.startListening();
  const r2 = app2.recognition.latest();
  r2.emitFinal('remind me about');
  r2.emitInterim('wait');
  check(app2.app.voice.status() === 'stopped', 'an interim "wait" interrupts instantly, before finalisation');
  check(app2.app.voice.buffer() === '', 'the interim interrupt cleared the buffer');

  // and it stops Alfred mid-sentence too
  const app3 = await boot({ voices: BRITISH });
  await ask(app3, 'what about packing?');
  const spokenBefore = app3.speech.words().length;
  check(spokenBefore > 0, 'an answer was spoken');
  app3.app.voice.startListening();
  app3.recognition.latest().emitFinal('stop');
  check(app3.speech.cancelCount() > 0, '"stop" cancels speech that is already playing');
}

/* ==================================================================== 4. ?mute=1 */
{
  const app = await boot({ search: '?mute=1', voices: BRITISH });
  const v = app.app.voice;
  check(v.MUTED_BY_URL === true, '?mute=1 is detected');
  check(v.status() === 'muted', 'the status line shows muted on load');
  await ask(app, 'what did the movers quote?');
  check(app.speech.spoken.length === 0,
        'nothing at all is handed to the speech engine in a muted tab, not even the unlock primer (' +
        app.speech.spoken.length + ' calls)');
  check(app.elements.get('answer-text').textContent.includes('26,000'),
        'the answer is still shown on screen - muted means silent, not broken');
  const directly = await v.speak('this must not be heard');
  check(directly === false, 'speak() refuses even when called directly');
  check(app.speech.spoken.length === 0, 'still nothing spoken: the check lives inside speak() itself');

  // nothing may route around the gate - every path that talks goes through speak()
  const src = (await import('node:fs')).readFileSync(new URL('../viewer/index.html', import.meta.url), 'utf8');
  const speakSites = (src.match(/\bsynth\.speak\(/g) || []).length;
  check(speakSites === 2, 'the synth is driven from exactly two places: speak() and the silent unlock ' +
        'primer (' + speakSites + ')');

  const normal = await boot({ voices: BRITISH });
  await ask(normal, 'what did the movers quote?');
  check(normal.speech.words()[0] && normal.speech.words()[0].includes('26,000'),
        'the same question in a normal tab IS spoken: "' + (normal.speech.words()[0] || '').slice(0, 48) + '…"');
  check(normal.app.voice.status() !== 'muted', 'and that tab is not muted');
}

/* ============================================================== 5. voice choice */
{
  const withBritish = await boot({ voices: BRITISH });
  check(withBritish.app.voice.chosenVoice().lang === 'en-GB',
        'with a British voice available it is chosen (' + withBritish.app.voice.chosenVoice().name + ')');
  const withoutBritish = await boot({ voices: [{name: 'Samantha', lang: 'en-US'}] });
  check(withoutBritish.app.voice.chosenVoice().lang === 'en-US',
        'without one it falls back to another English voice');
  const noneEnglish = await boot({ voices: [{name: 'Lekha', lang: 'hi-IN'}] });
  check(!!noneEnglish.app.voice.chosenVoice(), 'with no English voice it still picks something');
  const noVoices = await boot({ voices: [] });
  const speaking = noVoices.app.voice.speak('hello');
  noVoices.clock.advance(200);              // the utterance needs the clock to end
  const spoken = await speaking;
  check(spoken === true && noVoices.speech.words().length === 1,
        'with an empty voice list it still speaks through the system default');
}

/* ================================================== 6. audio unlocked by a click */
{
  const app = await boot({ voices: BRITISH, unlockSpeech: false });
  check(app.app.voice.isUnlocked() === false, 'a fresh page is not unlocked (nothing may speak yet)');
  const queued = await app.app.voice.speak('the first spoken line');
  check(queued === false, 'speak() before any interaction returns false rather than talking');
  check(app.speech.words().length === 0, 'nothing was spoken before the first click');

  app.fireWindowEvent('pointerdown');
  app.clock.advance(5);
  check(app.app.voice.isUnlocked() === true, 'the first click unlocks audio');
  check(app.speech.words().includes('the first spoken line'),
        'the line queued before the click is spoken straight after it');
  const order = app.speech.words();
  check(order.indexOf('the first spoken line') >= 0, 'order respected: no speech before the gesture (' + order.length + ' utterances)');
}

/* ================================================================= 7. the mic button */
{
  const app = await boot({ voices: BRITISH });
  const mic = app.elements.get('mic');
  check(!!(mic && mic._ev && mic._ev.click), 'the mic button has a click handler');
  mic._ev.click();
  check(app.app.voice.isListening() === true, 'clicking the mic starts listening');
  check(mic.getAttribute('aria-pressed') === 'true', 'mic state is announced to screen readers');
  mic._ev.click();
  check(app.app.voice.isListening() === false, 'clicking again stops listening');
  check(app.app.voice.status() === 'stopped', 'the status line says the mic is off');

  const unsupported = await boot({ voices: BRITISH, noRecognition: true });
  check(unsupported.elements.get('mic').disabled === true,
        'with no webkitSpeechRecognition the mic is disabled rather than broken');
  check(unsupported.app.voice.status() === 'error', 'and the status line says this browser cannot listen');
  await ask(unsupported, 'can I still type?');
  check(unsupported.speech.words().length === 1, 'typing and speaking still work without recognition');
}

/* ================================================= 8. status line, end to end */
{
  const app = await boot({ voices: BRITISH });
  app.app.voice.startListening();
  check(app.elements.get('voice-text').textContent.includes('listening'), 'status: listening');
  check(app.elements.get('voice-text').textContent.includes("you're talking"),
        'status names who is talking (you)');
  app.recognition.latest().emitFinal('what did the movers quote');
  app.clock.advance(1000);
  check(app.elements.get('voice-text').textContent.includes('thinking'), 'status: thinking after dispatch');
  await app.flush(4, 25);
  check(app.speech.words().length === 1, 'the answer was spoken (' + app.speech.words().length + ')');
  app.clock.advance(300);
  check(app.app.voice.status() === 'idle' || app.app.voice.status() === 'listening',
        'status settles back to idle when the answer is done (' + app.app.voice.status() + ')');
}

/* ============================================== 9. hands-free loop and typed path */
{
  const app = await boot({ voices: BRITISH });
  app.app.voice.startListening();
  app.recognition.latest().emitFinal('what about the deposit');
  await app.flush(4, 300);
  check(app.chatCalls().length === 1, 'a spoken question is sent');
  check(app.app.voice.wantsListening() === true,
        'after Alfred finishes talking the mic reopens by itself (hands-free)');

  const typed = await boot({ voices: BRITISH });
  typed.app.voice.startListening();
  typed.elements.get('q').value = 'what did the movers quote?';
  typed.elements.get('ask-form')._ev.submit({preventDefault(){}});
  const heardTyping = typed.app.voice.wantsListening();
  await typed.flush(4, 40);
  check(typed.chatCalls().length === 1 && typed.chatCalls()[0].body.question === 'what did the movers quote?',
        'typing goes through the same /chat flow');
  check(heardTyping === false, ' typing closes the mic first, so it cannot hear the answer');
  check(typed.speech.words().length === 1, 'a typed question still gets a spoken answer');
}

/* ================================ 10. a browser that never reports speech ending */
{
  const app = await boot({ voices: BRITISH, neverEnds: true });
  await ask(app, 'does this hang?');
  check(app.app.voice.status() === 'speaking', 'a voice that never reports onend shows as speaking at first');
  check(app.speech.words().length === 1, 'and the answer was handed to the engine once');
  app.clock.advance(30000);                 // the guard has to fire
  await Promise.resolve();
  check(app.app.voice.status() !== 'speaking', 'the safety net stops the status line sticking on speaking');
  check(app.elements.get('send').disabled === false, 'and the ask button is usable again');
  // a second question must still work after that
  await ask(app, 'and again?');
  check(app.speech.words().length === 2, 'the next answer still speaks after a stuck utterance is cleared');
}

/* ------------------------------------------------------------------- report */
console.log('\nAlfred - voice verification (virtual clock, no browser needed)');
console.log(notes.join('\n'));
if (problems.length){
  console.log('\n  RESULT: FAILED (' + problems.length + ')');
  problems.forEach(p => console.log('   - ' + p));
  process.exit(1);
}
console.log('\n  RESULT: PASSED - speaking, listening, buffering, interrupts and ?mute=1 all behave');
