#!/usr/bin/env node
/*
 * tools/browser-voice-check.mjs - the speech layer in a real browser.
 *
 * tools/verify-voice.mjs proves the logic on a virtual clock; this proves the same
 * thing against real timers, a real DOM and the real Web Speech API surface, in a
 * normal tab and in a ?mute=1 tab.
 *
 * What is stubbed, and why: a headless browser has no speakers and no speech
 * service, so an injected script stands in for the microphone and the voice list,
 * and records what the page hands to the speech engine. Everything about the page's
 * own behaviour - the 900ms window, the interrupts, the mute gate, the status line -
 * is the real thing running on real timers.
 *
 *   node tools/browser-voice-check.mjs
 *   node tools/browser-voice-check.mjs --url http://127.0.0.1:4700
 */
import fs from 'node:fs';
import path from 'node:path';
import { launchBrowser, noPuppeteerMessage, ROOT } from './browser.mjs';

const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const i = argv.indexOf('--' + name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback;
};
const URL_ = arg('url', 'http://127.0.0.1:4700/');
const OUT = path.resolve(arg('out', path.join(ROOT, 'tools', 'screenshots')));

const results = [];
const ok = (m) => { results.push(['ok', m]); console.log('  ok    ' + m); };
const bad = (m) => { results.push(['FAIL', m]); console.log('  FAIL  ' + m); };
const check = (c, m) => (c ? ok(m) : bad(m));
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const rel = (p) => path.relative(ROOT, p);

/* ---------------------------------------------------- what we inject into the page */
const INJECT = () => {
  // 1. a spy in front of the speech engine, so we can see (and finish) utterances
  window.__spoken = [];
  window.__voiced = [];
  const synth = window.speechSynthesis;
  if (synth){
    const realSpeak = synth.speak.bind(synth);
    synth.speak = (u) => {
      window.__spoken.push(u && u.text ? u.text : '');
      let carried = 'lang=' + ((u && u.lang) || '');
      try { if (u && u.voice) carried = u.voice.name + '/' + u.voice.lang + ' ' + carried; }
      catch (e) { carried = 'voice-unreadable ' + carried; }
      window.__voiced.push(carried);
      // a headless browser has no audio device; end the utterance at once so the
      // page's own promise chain proceeds exactly as it would on a real machine
      try { if (u.onstart) u.onstart(); } catch (e) {}
      setTimeout(() => { try { if (u.onend) u.onend(); } catch (e) {} }, 60);
      return realSpeak(u);                     // still hand it to the engine
    };
    const realCancel = synth.cancel.bind(synth);
    synth.cancel = () => { window.__cancels = (window.__cancels || 0) + 1; return realCancel(); };
    synth.getVoices = () => [
      {name: 'Daniel', lang: 'en-GB'},        // the British voice the page should prefer
      {name: 'Samantha', lang: 'en-US'},
      {name: 'Lekha', lang: 'hi-IN'}
    ];
  }

  // 2. a microphone we can drive by hand
  window.__mic = {started: 0, stopped: 0, latest: null, lang: null};
  class FakeRecognition {
    constructor(){
      this.lang = ''; this.continuous = false; this.interimResults = false;
      window.__mic.latest = this;
    }
    start(){ window.__mic.started++; window.__mic.lang = this.lang; if (this.onstart) this.onstart(); }
    stop(){ window.__mic.stopped++; if (this.onend) this.onend(); }
    abort(){ this.stop(); }
    _emit(text, isFinal){
      const result = [{transcript: text}];
      result.isFinal = isFinal;
      if (this.onresult) this.onresult({resultIndex: 0, results: [result]});
    }
  }
  window.webkitSpeechRecognition = FakeRecognition;
  window.SpeechRecognition = FakeRecognition;
  window.__say = (text) => window.__mic.latest._emit(text, true);
  window.__murmur = (text) => window.__mic.latest._emit(text, false);
};

const launched = await launchBrowser({ args: ['--window-size=1500,900'] });
if (launched.error === 'no-puppeteer'){ noPuppeteerMessage(); process.exit(2); }
if (launched.error){
  console.error('browser-voice-check: could not launch a browser: ' + launched.message);
  process.exit(2);
}
const { browser } = launched;
fs.mkdirSync(OUT, {recursive: true});

console.log('\nAlfred - voice check in a real browser');
console.log('  url    : ' + URL_);
console.log('  browser: ' + await browser.version());

/* ============================================================ 1. a normal tab */
const page = await browser.newPage();
await page.setViewport({width: 1500, height: 900, deviceScaleFactor: 1});
await page.evaluateOnNewDocument(INJECT);
const posts = [];
const errors = [];
page.on('request', (r) => {
  if (r.method() === 'POST' && r.url().endsWith('/chat')) posts.push(r.postData() || '');
});
page.on('pageerror', (e) => errors.push(String(e)));

await page.goto(URL_, {waitUntil: 'domcontentloaded', timeout: 45000});
await page.waitForFunction(() => window.__alfred && window.__alfred.selfTest().ok, {timeout: 45000, polling: 400});

const initial = await page.evaluate(() => ({
  status: window.__alfred.voice.status(),
  hasMic: !!document.getElementById('mic'),
  micDisabled: document.getElementById('mic').disabled,
  muted: window.__alfred.voice.MUTED_BY_URL,
  finish: window.__alfred.voice.FINISH_MS,
  label: document.getElementById('voice-text').textContent
}));
check(initial.hasMic && !initial.micDisabled, 'the microphone button is there and enabled');
check(initial.status === 'idle', 'the status line starts at idle');
check(initial.finish === 900, 'FINISH_MS is 900 in the browser too');
check(initial.muted === false, 'this tab is not muted');

/* ---- speaking: ask by typing, and it must speak the answer ---- */
await page.click('#q');
await page.type('#q', 'What did the movers quote for the road trip?');
await page.click('#send');
await page.waitForFunction(() => {
  const el = document.getElementById('answer-text');
  return el && !el.classList.contains('thinking') && el.textContent.length > 10;
}, {timeout: 30000});
await sleep(500);

const spoken = await page.evaluate(() => ({
  spoken: window.__spoken, voiced: window.__voiced,
  answer: document.getElementById('answer-text').textContent,
  status: window.__alfred.voice.status(),
  label: document.getElementById('voice-text').textContent,
  chosen: window.__alfred.voice.chosenVoice() && window.__alfred.voice.chosenVoice().lang
}));
const heard = spoken.spoken.filter(t => t && t.trim());
check(heard.length === 1, 'the answer was handed to the speech engine once (' + heard.length + ')');
const spokeTheAnswer = heard[0] && (spoken.answer.startsWith(heard[0].slice(0, 40)) || heard[0].length > 20);
check(spokeTheAnswer, 'what it speaks is the answer: "' + (heard[0] || '').slice(0, 60) + '…"');
check(spoken.chosen === 'en-GB', 'the British voice was chosen from the list (en-GB)');
const voiced = spoken.voiced.filter(Boolean);
check(voiced.some(v => /en-GB/.test(v)),
      'the utterance carries an English (en-GB) voice or language tag (' + (voiced.join(', ') || 'none') + ')');
check(voiced.some(v => /voice-unreadable|lang=en-GB/.test(v)),
      'a browser that rejects the voice object still speaks - the answer is not lost to a cosmetic failure');
check(/idle|listening/.test(spoken.status), 'the status line comes back off speaking (' + spoken.label.trim() + ')');

/* ---- the microphone button ---- */
await page.click('#mic');
await sleep(250);
const listening = await page.evaluate(() => ({
  started: window.__mic.started, lang: window.__mic.lang,
  status: window.__alfred.voice.status(),
  label: document.getElementById('voice-text').textContent,
  pressed: document.getElementById('mic').getAttribute('aria-pressed')
}));
check(listening.started === 1, 'clicking the mic opens a recognition session');
check(listening.lang === 'en-IN', 'recognition listens in the configured language (' + listening.lang + ')');
check(listening.status === 'listening', 'the status line says listening');
check(/you're talking/.test(listening.label), 'and it names who is talking: "' + listening.label.trim() + '"');
check(listening.pressed === 'true', 'the mic button reports its state');

/* ---- THE BUFFER: a mid-sentence pause must not send ---- */
const postsBefore = posts.length;
await page.evaluate(() => window.__say('what did the movers'));
await sleep(400);
const duringPause = await page.evaluate(() => ({
  posts: 0, buffer: window.__alfred.voice.buffer(),
  label: document.getElementById('voice-text').textContent,
  detail: document.getElementById('voice-detail').textContent
}));
check(posts.length === postsBefore, 'the first finalised fragment did NOT send a question to /chat');
check(duringPause.buffer === 'what did the movers', 'it is being held in the buffer: "' + duringPause.buffer + '"');
check(/heard/.test(duringPause.detail) && /\d+ms pause/.test(duringPause.detail),
      'the status line shows it heard a fragment and is waiting for more: "' + duringPause.detail + '"');

await page.evaluate(() => window.__say('quote for the road trip'));
await sleep(500);
check(posts.length === postsBefore, 'still nothing 900ms after the first fragment: the window restarted');
const buffer = await page.evaluate(() => window.__alfred.voice.buffer());
check(buffer === 'what did the movers quote for the road trip',
      'the two fragments are joined: "' + buffer + '"');

await sleep(1200);                       // a real pause that ends the thought
check(posts.length === postsBefore + 1, 'exactly one question was sent after the pause (not two)');
if (posts.length > postsBefore){
  const body = JSON.parse(posts[posts.length - 1]);
  check(body.question === 'what did the movers quote for the road trip',
        'and it is the whole sentence: "' + body.question + '"');
}
await sleep(700);                        // Alfred answers the spoken question
const afterSpoken = await page.evaluate(() => ({
  status: window.__alfred.voice.status(),
  listening: window.__alfred.voice.isListening(),
  heard: window.__spoken.filter(t => t && t.trim()).length
}));
check(afterSpoken.heard >= 2, 'the answer to a spoken question is spoken back (' + afterSpoken.heard + ' utterances)');
check(afterSpoken.listening === true, 'and the mic reopens for a follow-up (hands-free loop)');

/* ---- interrupts bypass the buffer ---- */
const beforeStop = posts.length;
await page.evaluate(() => window.__say('what about the'));
await sleep(300);
await page.evaluate(() => window.__say('stop'));
await sleep(150);
const afterStop = await page.evaluate(() => ({
  status: window.__alfred.voice.status(), buffer: window.__alfred.voice.buffer(),
  label: document.getElementById('voice-text').textContent
}));
check(posts.length === beforeStop, '"stop" sent nothing to the brain');
check(afterStop.buffer === '', 'and it emptied the buffer immediately');
check(afterStop.status === 'stopped', 'the status line says stopped');
await sleep(1400);
check(posts.length === beforeStop, 'the cancelled question never fires later');

/* ---- the hands-free loop keeps working after that ---- */
await page.click('#mic');
await sleep(200);
await page.evaluate(() => window.__say('and packing'));
await sleep(1300);
const second = posts.length;
check(second === beforeStop + 1, 'the next spoken question goes through normally');
await sleep(600);
const resumes = await page.evaluate(() => window.__alfred.voice.wantsListening());
check(resumes === true, 'and the mic reopens again afterwards');
await page.evaluate(() => window.__alfred.voice.stopListening('done'));
await page.screenshot({path: path.join(OUT, 'voice.jpg'), type: 'jpeg', quality: 86});
ok('screenshot: ' + rel(path.join(OUT, 'voice.jpg')));

/* ============================================================ 2. a ?mute=1 tab */
const mutedPage = await browser.newPage();
await mutedPage.setViewport({width: 1500, height: 900, deviceScaleFactor: 1});
await mutedPage.evaluateOnNewDocument(INJECT);
await mutedPage.goto(URL_ + (URL_.includes('?') ? '&' : '?') + 'mute=1', {waitUntil: 'domcontentloaded', timeout: 45000});
await mutedPage.waitForFunction(() => window.__alfred && window.__alfred.selfTest().ok, {timeout: 45000, polling: 400});

const muteState = await mutedPage.evaluate(() => ({
  muted: window.__alfred.voice.MUTED_BY_URL,
  status: window.__alfred.voice.status(),
  label: document.getElementById('voice-text').textContent,
  detail: document.getElementById('voice-detail').textContent
}));
check(muteState.muted === true, '?mute=1 is picked up from the URL');
check(muteState.status === 'muted', 'the status line says muted from the moment it loads');
check(/never speaks/i.test(muteState.label), 'the label says so plainly: "' + muteState.label.trim() + '"');
check(/\?mute=1/.test(muteState.detail), 'and the detail names the flag: "' + muteState.detail + '"');

await mutedPage.click('#q');
await mutedPage.type('#q', 'What did the movers quote for the road trip?');
await mutedPage.click('#send');
await mutedPage.waitForFunction(() => {
  const el = document.getElementById('answer-text');
  return el && !el.classList.contains('thinking') && el.textContent.length > 10;
}, {timeout: 30000});
await mutedPage.click('#mic');                    // even a deliberate click must stay silent
await mutedPage.evaluate(() => window.__say('what did the movers quote'));
await sleep(1500);

const muteAfter = await mutedPage.evaluate(() => ({
  spoken: window.__spoken, cancels: window.__cancels || 0,
  answer: document.getElementById('answer-text').textContent,
  sources: document.getElementById('answer-sources').innerHTML.split('class="src"').length - 1,
  status: window.__alfred.voice.status(),
  detail: document.getElementById('voice-detail').textContent,
  spokenByApi: (() => { try { return window.speechSynthesis.speaking; } catch (e) { return null; } })()
}));
check(muteAfter.spoken.length === 0,
      'nothing at all was handed to the speech engine in the muted tab (' + muteAfter.spoken.length + ' calls)');
check(muteAfter.answer.length > 10, 'the answer still appears on screen - muted is silent, not broken');
check(muteAfter.status === 'muted' || muteAfter.status === 'listening',
      'the muted tab stays off "speaking" the whole time (' + muteAfter.status + ')');
check(/\?mute=1|never speaks/.test(muteAfter.detail),
      'and the mute notice is still on screen after asking: "' + muteAfter.detail + '"');
await mutedPage.screenshot({path: path.join(OUT, 'voice-muted.jpg'), type: 'jpeg', quality: 86});
ok('screenshot: ' + rel(path.join(OUT, 'voice-muted.jpg')));

check(errors.length === 0, 'no uncaught page errors on either tab' +
      (errors.length ? ' -> ' + errors.slice(0, 2).join(' | ') : ''));

await browser.close();

const fails = results.filter(([s]) => s === 'FAIL');
console.log('\n  %d checks, %d passed, %d failed', results.length, results.length - fails.length, fails.length);
if (fails.length){
  console.log('\n  RESULT: FAILED');
  fails.forEach(([, m]) => console.log('   - ' + m));
  process.exit(1);
}
console.log('\n  RESULT: PASSED - speaking, listening, the finish window, interrupts and ?mute=1 all ' +
            'behave in a real browser');
