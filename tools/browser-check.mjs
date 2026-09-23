#!/usr/bin/env node
/*
 * tools/browser-check.mjs - drive the real galaxy in a real browser.
 *
 * Everything else in tools/ verifies the page's logic headlessly. This one opens
 * the actual URL, waits for WebGL to draw, clicks a node, asks a question and
 * takes screenshots - so "the graph renders" is something observed, not inferred.
 *
 * Needs a Chrome/Chromium binary. Either:
 *   npm i -g puppeteer-core                      # or install it in this folder
 *   node tools/browser-check.mjs                 # uses your installed Chrome
 *
 *   node tools/browser-check.mjs --exe /path/to/chrome
 *   node tools/browser-check.mjs --url http://127.0.0.1:4700 --out tools/screenshots
 *
 * On a machine with no browser of its own (a bare container), @sparticuz/chromium
 * works: it ships a chromium build plus its shared libraries inside the npm
 * package. Point ALFRED_BROWSER_DEPS at where you installed it, or let the script
 * find /tmp/browser-check automatically.
 */
import fs from 'node:fs';
import path from 'node:path';
import { launchBrowser, noPuppeteerMessage, ROOT } from './browser.mjs';

/* ------------------------------------------------------------------- args */
const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const i = argv.indexOf('--' + name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback;
};
const URL_ = arg('url', 'http://127.0.0.1:4700/');
const OUT = path.resolve(arg('out', path.join(ROOT, 'tools', 'screenshots')));
const EXE = arg('exe', null);
const CHANNEL = arg('channel', null);

/* ------------------------------------------------- a browser, from somewhere */
const launched = await launchBrowser({ exe: EXE, channel: CHANNEL, args: ['--window-size=1600,900'] });
if (launched.error === 'no-puppeteer'){ noPuppeteerMessage(); process.exit(2); }
if (launched.error){
  console.error('\nbrowser-check: could not launch a browser.');
  console.error('  ' + launched.message);
  console.error('  pass --exe /path/to/chrome, or --channel chrome|chromium, or install @sparticuz/chromium.');
  process.exit(2);
}
const { browser } = launched;

/* ------------------------------------------------------------------ report */
const results = [];
const ok = (m) => { results.push(['ok', m]); console.log('  ok    ' + m); };
const bad = (m) => { results.push(['FAIL', m]); console.log('  FAIL  ' + m); };
const check = (c, m) => (c ? ok(m) : bad(m));
const rel = (p) => path.relative(ROOT, p);

console.log('\nAlfred - real browser check');
console.log('  url        : ' + URL_);
console.log('  output     : ' + rel(OUT) + '/');
fs.mkdirSync(OUT, { recursive: true });

ok('launched ' + await browser.version());

const page = await browser.newPage();
// software rendering (SwiftShader) is slow; 1 device pixel per CSS pixel keeps the
// whole check quick while still being a real WebGL render
await page.setViewport({ width: 1600, height: 900, deviceScaleFactor: 1 });

const consoleLines = [];
const pageErrors = [];
page.on('console', (m) => consoleLines.push({ type: m.type(), text: m.text() }));
page.on('pageerror', (e) => pageErrors.push(String(e)));
page.on('requestfailed', (r) => consoleLines.push({ type: 'requestfailed', text: r.url() + ' (' + (r.failure() || {}).errorText + ')' }));

const t0 = Date.now();
await page.goto(URL_, { waitUntil: 'domcontentloaded', timeout: 45000 });
try {
  await page.waitForFunction(() => {
    const st = window.__alfred && window.__alfred.selfTest();
    return st && st.ok;
  }, { timeout: 40000, polling: 500 });
} catch (err) {
  bad('the renderer never reported a drawn frame within 40s');
}
const bootMs = Date.now() - t0;
// let the boot overlay finish fading before anything is photographed
await page.waitForFunction(() => document.getElementById('boot').classList.contains('gone'),
                           { timeout: 15000, polling: 200 }).catch(() => {});
await new Promise(r => setTimeout(r, 1200));

/* -------------------------------------------------- what the page reported */
const report = await page.evaluate(() => {
  const el = document.getElementById('status-text');
  const scripts = Array.from(document.querySelectorAll('script[data-src]')).map(s => s.dataset.src);
  const out = { selfTest: window.__alfred.selfTest(), status: el ? el.textContent : null,
                loadedFrom: scripts, hasGraph: !!window.GRAPH,
                graphNodeCount: (window.GRAPH && window.GRAPH.nodes || []).length,
                graphLinkCount: (window.GRAPH && window.GRAPH.links || []).length };
  const r = window.__alfred.graph.renderer();
  const c = r.domElement;
  out.canvas = { w: c.width, h: c.height, wCss: c.clientWidth, hCss: c.clientHeight };
  out.sceneChildren = window.__alfred.graph.scene().children.length;
  return out;
});
const st = report.selfTest;

check(st.ok === true, 'the page drew frames: ' + st.drawCalls + ' draw calls');
check(st.notes === report.graphNodeCount && st.notes > 0,
      'all ' + st.notes + ' notes reached the renderer');
check(st.links === report.graphLinkCount, 'all ' + st.links + ' links reached the renderer');
check(typeof st.webgl === 'string' && st.webgl.length > 0, 'WebGL context: ' + st.webgl);
check(!!st.starfield, 'the starfield is attached to the scene');
check(report.sceneChildren >= 3, 'scene holds the graph, the starfield and the core glow (' + report.sceneChildren + ')');
check(report.canvas.wCss > 800 && report.canvas.hCss > 400,
      'the canvas is laid out at ' + report.canvas.wCss + 'x' + report.canvas.hCss);
check(/three r\d+/.test(report.status || ''), 'the HUD reads: "' + report.status + '"');
check(bootMs < 40000, 'boot to first drawn frame: ' + bootMs + ' ms');

const usedVendor = report.loadedFrom.some(u => u.startsWith('./vendor/'));
const usedCdn = report.loadedFrom.some(u => /^https?:/.test(u));
check(report.loadedFrom.length > 0, 'the graph bundle was loaded from: ' + report.loadedFrom.join(', '));
if (usedVendor && !usedCdn) ok('the CDN was unreachable here, so the page fell back to the local copy - the fallback works in a real browser');
else if (usedCdn) ok('the page loaded the bundle from the CDN');

check(pageErrors.length === 0, 'no uncaught page errors' +
      (pageErrors.length ? ' -> ' + pageErrors.slice(0, 3).join(' | ') : ''));
const threeRev = await page.evaluate(() => (window.THREE && window.THREE.REVISION) || window.__THREE__ || null);
check(String(threeRev) === '183', 'the renderer is using the pinned three.js revision (r' + threeRev + ')');
const failed = consoleLines.filter(l => l.type === 'requestfailed');
if (failed.length) {
  ok('failed requests (expected when a CDN is blocked): ' + failed.length);
  failed.slice(0, 4).forEach(f => console.log('          ' + f.text));
}

/* ------------------------------------ is the picture actually non-empty? */
async function pixelStats(saveAs) {
  const shot = saveAs
    ? await page.screenshot({ type: 'jpeg', quality: 86, path: saveAs })
    : await page.screenshot({ type: 'png' });
  return page.evaluate(async (b64) => {
    const img = new Image();
    await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = 'data:image/png;base64,' + b64; });
    const c = document.createElement('canvas');
    c.width = img.width; c.height = img.height;
    const ctx = c.getContext('2d');
    ctx.drawImage(img, 0, 0);
    const d = ctx.getImageData(0, 0, c.width, c.height).data;
    let lit = 0, maxL = 0;
    const colors = new Set();
    for (let i = 0; i < d.length; i += 4) {
      const l = (d[i] + d[i + 1] + d[i + 2]) / 3;
      if (l > 26) lit++;
      if (l > maxL) maxL = l;
      if (colors.size < 6000) colors.add((d[i] >> 3) + ',' + (d[i + 1] >> 3) + ',' + (d[i + 2] >> 3));
    }
    return { pixels: d.length / 4, lit: lit, litFraction: lit / (d.length / 4), maxLuma: maxL,
             distinctColors: colors.size, w: c.width, h: c.height };
  }, shot.toString('base64'));
}

const settled = await pixelStats(path.join(OUT, 'galaxy.jpg'));
check(settled.litFraction > 0.0005,
      'the rendered frame is not blank: ' + (settled.litFraction * 100).toFixed(2) + '% of pixels lit, ' +
      settled.distinctColors + ' distinct colours');
check(settled.distinctColors > 40, 'the frame has real visual variety (starfield + glowing nodes)');
check(settled.maxLuma > 200, 'the frame contains bright pixels (max luminance ' + Math.round(settled.maxLuma) + ')');

/* ---------------------------------------- let the force layout settle first */
async function waitForLayout(timeoutMs = 40000) {
  const sum = () => page.evaluate(() =>
    (window.__alfred.data.nodes || []).reduce((a, n) => a + Math.abs(n.x || 0) + Math.abs(n.y || 0) + Math.abs(n.z || 0), 0));
  let prev = await sum(), stable = 0;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 700));
    const now = await sum();
    stable = Math.abs(now - prev) < 0.75 ? stable + 1 : 0;
    prev = now;
    if (stable >= 2) return true;
  }
  return false;
}
const settledLayout = await waitForLayout();
check(settledLayout, 'the force layout settled before interacting');
// deliberately do NOT wait for a full cooldown: clicking while the simulation is
// still cooling is the case that used to let the auto-frame steal the camera
const engineWasCool = await page.evaluate(() => {
  const g = window.__alfred.graph;
  return typeof g.graphData === 'function' && (g.graphData().nodes || []).length > 0;
});
check(engineWasCool, 'the graph still holds its nodes when the click happens');
console.log('        (layout took ' + ((Date.now() - t0) / 1000).toFixed(1) + 's of wall clock under software rendering)');

/* ------------------------------------------------------ click a node: fly-to */
const focusInfo = await page.evaluate(() => {
  const g = window.__alfred;
  const before = g.graph.camera().position.clone();
  const target = window.GRAPH.nodes.find(n => n.label === 'Booking Train to Ayodhya') || window.GRAPH.nodes[0];
  g.focus(target);
  return { label: target.label, before: { x: before.x, y: before.y, z: before.z } };
});
// wait for the camera to actually ARRIVE rather than for a fixed number of
// seconds: the fly-to tween is driven by requestAnimationFrame, and under
// software rendering the frame rate can be low enough that a 1.3s animation
// takes several wall-clock seconds.
// The tween moves the camera position AND its look-at target on their own
// curves, so waiting on the position alone can catch the moment the camera
// arrives while the target is still swinging - the node then measures far off
// centre even though the flight is fine a few frames later. Wait for both.
let flew = true;
try {
  await page.waitForFunction(() => {
    const g = window.__alfred;
    const dest = g.lastFlyTo && g.lastFlyTo();
    if (!dest) return false;
    const cam = g.graph.camera().position;
    const ctrl = g.graph.controls();
    return Math.hypot(cam.x - dest.pos.x, cam.y - dest.pos.y, cam.z - dest.pos.z) < 1.5 &&
           Math.hypot(ctrl.target.x - dest.target.x, ctrl.target.y - dest.target.y,
                      ctrl.target.z - dest.target.z) < 1.5;
  }, { timeout: 30000, polling: 300 });
} catch (err) { flew = false; }
// Reaching the destination is not the same as the view coming to rest: focus()
// writes controls.target straight away while the tween keeps overwriting it each
// frame, so the projection of the focused note is still swinging. Wait until that
// projection holds still for two samples, then measure it. A flight that ends in
// the wrong place still fails the check below - this only removes the race.
let atRest = true;
try {
  await page.waitForFunction(() => {
    const g = window.__alfred;
    const label = document.getElementById('panel-label').textContent;
    const node = (g.data.nodes || []).find(n => n.label === label);
    if (!node || node.x == null) return false;
    const cam = g.graph.camera();
    cam.updateMatrixWorld();
    const v = new window.THREE.Vector3(node.x, node.y, node.z).project(cam);
    const now = [v.x, v.y];
    const prev = window.__alfredFramingPrev;
    window.__alfredFramingPrev = now;
    if (!prev) return false;
    return Math.abs(now[0] - prev[0]) * window.innerWidth < 3 &&
           Math.abs(now[1] - prev[1]) * window.innerHeight < 3;
  }, { timeout: 20000, polling: 250 });
} catch (err) { atRest = false; }
check(atRest, 'the camera view came to rest after the flight (not still swinging)');
check(flew, 'the camera tween ran to completion (reached the destination it recorded)');
const clickState = await page.evaluate(() => {
  const panel = document.getElementById('panel');
  const cam = window.__alfred.graph.camera().position;
  const ctrl = window.__alfred.graph.controls();
  const label = document.getElementById('panel-label').textContent;
  const node = (window.__alfred.data.nodes || []).find(n => n.label === label);
  const d = (node && node.x != null)
    ? Math.hypot(cam.x - node.x, cam.y - node.y, cam.z - node.z) : null;
  return {
    framedRadius: window.__alfred.camRadius ? window.__alfred.camRadius() : null,
    panelOpen: panel.classList.contains('open'),
    label: document.getElementById('panel-label').textContent,
    excerptLen: document.getElementById('panel-excerpt').textContent.length,
    neighbours: document.getElementById('panel-neighbours').innerHTML.split('class="neigh"').length - 1,
    camera: { x: cam.x, y: cam.y, z: cam.z },
    target: { x: ctrl.target.x, y: ctrl.target.y, z: ctrl.target.z },
    distanceToNode: d
  };
});
check(clickState.panelOpen, 'clicking a node opens the side panel');
check(clickState.label === focusInfo.label, 'the panel shows the clicked note: "' + clickState.label + '"');
check(clickState.excerptLen > 400, 'the panel shows the excerpt (' + clickState.excerptLen + ' chars)');
check(clickState.neighbours > 0, 'the panel lists its ' + clickState.neighbours + ' connected notes');
check(clickState.distanceToNode !== null && clickState.distanceToNode < 260,
      'the camera flew close to the node (' + Math.round(clickState.distanceToNode) + ' units away, ' +
      'galaxy framed at ' + Math.round(clickState.framedRadius || 0) + ')');
const moved = Math.hypot(clickState.camera.x - focusInfo.before.x,
                         clickState.camera.y - focusInfo.before.y,
                         clickState.camera.z - focusInfo.before.z);
check(moved > 1, 'the camera actually moved (' + Math.round(moved) + ' units)');
const framing = await page.evaluate(() => {
  const g = window.__alfred;
  const label = document.getElementById('panel-label').textContent;
  const node = (g.data.nodes || []).find(n => n.label === label);
  const cam = g.graph.camera();
  cam.updateMatrixWorld();
  const v = new window.THREE.Vector3(node.x, node.y, node.z).project(cam);
  const W = window.innerWidth, H = window.innerHeight;
  const panelW = document.getElementById('panel').getBoundingClientRect().width;
  return {screenX: (v.x * 0.5 + 0.5) * W, screenY: (-v.y * 0.5 + 0.5) * H,
          visibleCentre: (W - panelW) / 2, W: W, H: H, panelW: panelW, inFront: v.z < 1};
});
check(framing.inFront, 'the focused note is in front of the camera');
check(Math.abs(framing.screenX - framing.visibleCentre) < framing.W * 0.12,
      'the focused note is centred in the visible area, clear of the panel (node at x=' +
      Math.round(framing.screenX) + ', visible centre x=' + Math.round(framing.visibleCentre) + ')');
check(framing.screenY > 0 && framing.screenY < framing.H, 'the focused note is on screen vertically (y=' + Math.round(framing.screenY) + ')');

const focused = await pixelStats(path.join(OUT, 'galaxy-focused.jpg'));
check(focused.litFraction > settled.litFraction * 0.3, 'the focused view still renders (' +
      (focused.litFraction * 100).toFixed(2) + '% lit)');

/* ----------------------------------------------------------- the ask bar */
const askBefore = consoleLines.length;
await page.click('#q');
await page.type('#q', 'What did the movers quote for the road trip?');
await page.click('#send');
await page.waitForFunction(() => {
  const el = document.getElementById('answer-text');
  return el && !el.classList.contains('thinking') && el.textContent.length > 10;
}, { timeout: 30000 });
const ask = await page.evaluate(() => ({
  open: !document.getElementById('answer').hidden,
  text: document.getElementById('answer-text').textContent,
  cls: document.getElementById('answer-text').className,
  sources: document.getElementById('answer-sources').innerHTML.split('class="src"').length - 1
}));
check(ask.open, 'the ask bar shows an answer panel');
if (/placeholder/i.test(ask.text)) {
  ok('with the placeholder key the page shows the plain-language fix: "' + ask.text.slice(0, 68) + '…"');
  check(ask.cls === 'error', 'the message is styled as an error, not a silent failure');
  check(ask.sources === 0, 'no fake sources are shown when the brain has no key');
} else {
  ok('the brain answered: "' + ask.text.slice(0, 90) + '…"');
  check(ask.sources > 0, 'the answer links ' + ask.sources + ' source notes');
}
check(pageErrors.length === 0, 'still no uncaught errors after asking');
await pixelStats(path.join(OUT, 'ask.jpg'));

/* -------------------------------------------------------- idle drift check */
const rot = await page.evaluate(async () => {
  const ctrl = window.__alfred.graph.controls();
  const canvas = window.__alfred.graph.renderer().domElement;
  canvas.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }));
  canvas.dispatchEvent(new MouseEvent('pointerup', { bubbles: true }));
  await new Promise(r => setTimeout(r, 200));
  const rightAfter = ctrl.autoRotate;
  await new Promise(r => setTimeout(r, 8200));
  return { rightAfter, afterIdle: ctrl.autoRotate };
});
check(rot.rightAfter === false, 'the idle drift stops as soon as you interact');
check(rot.afterIdle === true, 'the idle drift resumes after ~7s of no interaction');

/* ------------------------------------------------------------- keyboard UX */
await page.keyboard.press('/');
const focusedInput = await page.evaluate(() => document.activeElement && document.activeElement.id);
check(focusedInput === 'q', 'pressing "/" focuses the ask bar');
await page.keyboard.press('Escape');
const panelClosed = await page.evaluate(() => !document.getElementById('panel').classList.contains('open'));
check(panelClosed, 'pressing Escape closes the side panel');

/* ------------------------------------------------------------------ album */
await page.evaluate(() => window.__alfred.reset());
await new Promise(r => setTimeout(r, 2000));
await pixelStats(path.join(OUT, 'galaxy-wide.jpg'));

await browser.close();

const files = fs.readdirSync(OUT).filter(f => /\.(jpg|png)$/.test(f)).sort();
console.log('\n  screenshots (' + files.length + '):');
for (const f of files) {
  console.log('    %s  %d KB', rel(path.join(OUT, f)), Math.round(fs.statSync(path.join(OUT, f)).size / 1024));
}

const fails = results.filter(([s]) => s === 'FAIL');
console.log('\n  %d checks, %d passed, %d failed', results.length, results.length - fails.length, fails.length);
if (fails.length) {
  console.log('\n  RESULT: FAILED');
  fails.forEach(([, m]) => console.log('   - ' + m));
  process.exit(1);
}
console.log('\n  RESULT: PASSED - the galaxy renders in a real browser and every interaction works');
