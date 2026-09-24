#!/usr/bin/env node
/*
 * tools/verify.mjs - headless verification of viewer/index.html
 *
 * There is no browser in this sandbox, so this harness runs the viewer's real
 * application code (extracted from the <script type="module"> block) inside Node
 * against a fake window/document and a mock of the 3d-force-graph bundle.
 *
 * The mock is not hand-waved: every chained accessor call is validated against the
 * real API surface of 3d-force-graph@1.80.0 (recorded in tools/api-3d-force-graph.json)
 * and any unknown member throws exactly like the real library would - which is the
 * failure mode that shows up in a browser as a blank black page.
 */
import fs from 'node:fs';
import {ELEMENT_IDS} from './harness.mjs';
import path from 'node:path';
import vm from 'node:vm';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const HTML = fs.readFileSync(path.join(ROOT, 'viewer', 'index.html'), 'utf8');
const GRAPH_JS = fs.readFileSync(path.join(ROOT, 'viewer', 'graph-data.js'), 'utf8');
const API = JSON.parse(fs.readFileSync(path.join(ROOT, 'tools', 'api-3d-force-graph.json'), 'utf8'));

const problems = [], notes = [];
const ok = (m) => notes.push('  ok    ' + m);
const bad = (m) => { problems.push(m); notes.push('  FAIL  ' + m); };
const check = (cond, m) => (cond ? ok(m) : bad(m));
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

/* ------------------------------------------------------------------ canvas */
function makeCtx2d(calls) {
  const grad = {addColorStop: (a, c) => calls.push(['addColorStop', a, c])};
  return new Proxy({}, {
    get(_t, prop) {
      if (prop === 'createRadialGradient') return (...a) => { calls.push(['createRadialGradient', ...a]); return grad; };
      if (prop === 'measureText') return () => ({width: 10});
      return (...a) => { calls.push([String(prop), ...a]); };
    },
    set() { return true; }
  });
}

/* -------------------------------------------------------------- DOM / window */
const elements = new Map();
function makeEl(id, tag = 'div') {
  const el = {
    id, tagName: tag.toUpperCase(), children: [], style: {}, dataset: {},
    hidden: false, textContent: '', value: '', placeholder: '', src: '',
    // className and classList are two views of ONE set, the way they are in a browser.
    // Until 2026-09-23 this mock had no classList.toggle at all, and no classList at all
    // on elements whose id the test file did not list - which is how a missing id turned
    // into "paintBrain is not a function" rather than a failing assertion.
    get className() { return [...this.classList._s].join(' '); },
    set className(v) { this.classList._s = new Set(String(v || '').split(/\s+/).filter(Boolean)); },
    classList: {
      _s: new Set(),
      add(...c) { c.forEach(x => this._s.add(x)); },
      remove(...c) { c.forEach(x => this._s.delete(x)); },
      contains(c) { return this._s.has(c); },
      toggle(c, force) {
        const want = force === undefined ? !this._s.has(c) : !!force;
        if (want) this._s.add(c); else this._s.delete(c);
        return want;
      },
      item(i) { return [...this._s][i] ?? null; },
      get length() { return this._s.size; }
    },
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html || ''; },
    appendChild(c) { this.children.push(c); return c; },
    remove() {},
    addEventListener(type, fn) { (this._ev = this._ev || {})[type] = fn; },
    removeEventListener() {},
    getBoundingClientRect() { return {left: 0, top: 0, width: 1440, height: 900, right: 1440, bottom: 900}; },
    focus() {}, blur() {}, onclick: null,
    getAttribute() { return null; }, setAttribute(k, v) { this[k] = v; },
    removeAttribute() {}, src: ''
  };
  elements.set(id, el);
  return el;
}

const rafQueue = [];
const intervalCallbacks = [];
const canvases = [];
let fetchCalls = [];

const documentMock = {
  createElement(tag) {
    if (tag === 'video') {
      const v = makeEl('video-' + Math.random().toString(36).slice(2), 'video');
      v.srcObject = null; v.videoWidth = 0; v.videoHeight = 0; v.readyState = 0;
      v.play = async () => { v.readyState = 4; };
      v.pause = () => {}; v.removeAttribute = () => {};
      return v;
    }
    if (tag === 'canvas') {
      const canvas = makeEl('canvas-' + canvases.length, 'canvas');
      canvas.width = canvas.height = 0;
      const calls = [];
      canvas.getContext = () => makeCtx2d(calls);
      canvas._calls = calls;
      canvases.push(canvas);
      return canvas;
    }
    return makeEl(tag + '-' + Math.random().toString(36).slice(2), tag);
  },
  getElementById: (id) => elements.get(id) || null,
  querySelectorAll: () => [],
  head: {
    mode: 'all-ok',            // 'all-ok' | 'cdn-fails' | 'all-fail'
    attempts: [],
    appendChild(s) {
      documentMock.head.attempts.push(s.src);
      const isCdn = /^https?:/i.test(s.src);
      const fails = documentMock.head.mode === 'all-fail' || (documentMock.head.mode === 'cdn-fails' && isCdn);
      setTimeout(() => {
        if (fails) { if (s.onerror) s.onerror(); }
        else if (s.onload) s.onload();
      }, 0);
      return s;
    }
  },
  addEventListener() {},
  activeElement: null
};

const windowMock = {
  document: documentMock,
  addEventListener() {}, removeEventListener() {},
  setTimeout: (fn, ms) => setTimeout(fn, Math.min(ms || 0, 5)),
  clearTimeout,
  setInterval: (fn) => { intervalCallbacks.push(fn); return intervalCallbacks.length; },
  clearInterval() {},
  requestAnimationFrame: (fn) => { rafQueue.push(fn); return rafQueue.length; },
  innerWidth: 1440, innerHeight: 900, devicePixelRatio: 2,
  location: {href: 'http://127.0.0.1:4700/', origin: 'http://127.0.0.1:4700'},
  navigator: {userAgent: 'node-verify',
              mediaDevices: {getDisplayMedia: async () => {
                const track = {kind: 'video', readyState: 'live', label: 'screen:node-verify',
                               stop() { this.readyState = 'ended'; }};
                return {oninactive: null, getVideoTracks: () => [track], getTracks: () => [track]};
              }}},
  fetch: async (url, opts) => {
    fetchCalls.push({url, opts});
    if (String(url).indexOf('/health') === 0) {      // the page asks /health?hour=<local hour>
      return {ok: true, status: 200, json: async () => ({
        ok: true, notes: 12, model: 'gpt-6-astra', key: {state: 'placeholder', path: 'config.json'}, turns: 0,
        greeting: 'Good evening, sir. 12 notes indexed, all present and accounted for.',
        sight: {frames: 0, last_frame: null, media_types: ['image/jpeg', 'image/png', 'image/webp'],
                lines: {started: 'Watching your screen now, sir.', ended: 'The screen share has ended, sir.',
                        never: 'I have not been shown your screen yet, sir.',
                        lost: 'The share is no longer sending a picture, sir.',
                        no_frame: 'Nothing came with that question, sir.',
                        grab_failed: 'I could not take a picture of your screen, sir.'}}
      })};
    }
    return {
      ok: true, status: 200,
      json: async () => ({
        ok: true,
        answer: 'The notes say the movers quoted 26,000 and the deposit is 20,000.',
        nodes: [0, 2, 5],
        sources: [{index: 0, label: 'x', score: 9}],
        model: 'gpt-6-astra', turns: 1
      })
    };
  }
};

/* ------------------------------------------------------------- fake three.js */
class V3 {
  constructor(x = 0, y = 0, z = 0) { this.x = x; this.y = y; this.z = z; }
  set(x, y, z) { this.x = x; this.y = y; this.z = z; return this; }
  setScalar(s) { this.x = this.y = this.z = s; return this; }
  clone() { return new V3(this.x, this.y, this.z); }
  sub(v) { this.x -= v.x; this.y -= v.y; this.z -= v.z; return this; }
  add(v) { this.x += v.x; this.y += v.y; this.z += v.z; return this; }
  dot(v) { return this.x * v.x + this.y * v.y + this.z * v.z; }
  distanceTo() { return 1; }
  length() { return Math.hypot(this.x, this.y, this.z); }
  normalize() { const l = this.length() || 1; this.x /= l; this.y /= l; this.z /= l; return this; }
  multiplyScalar(s) { this.x *= s; this.y *= s; this.z *= s; return this; }
}
class Object3D {
  constructor() { this.children = []; this.position = new V3(); this.scale = new V3(1, 1, 1); this.userData = {}; this.parent = null; }
  add(...o) { o.forEach(c => { c.parent = this; this.children.push(c); }); return this; }
  remove() { return this; }
}
class Color { constructor(v) { this.v = v; } set(v) { this.v = v; return this; } setHSL() { return this; } }

const threeMock = {
  REVISION: '183',
  Vector2: V3, Vector3: V3, Color,
  Group: class extends Object3D {}, Mesh: class extends Object3D {}, Scene: class extends Object3D {},
  Sprite: class extends Object3D { constructor(mat) { super(); this.isSprite = true; this.material = mat; } },
  BufferGeometry: class { setAttribute(n, a) { this[n] = a; return this; } computeBoundingSphere() {} },
  BufferAttribute: class { constructor(arr, size) { this.array = arr; this.itemSize = size; } },
  Points: class extends Object3D { constructor(g, m) { super(); this.geometry = g; this.material = m; } },
  PointsMaterial: class { constructor(o = {}) { Object.assign(this, o); } },
  SpriteMaterial: class { constructor(o = {}) { Object.assign(this, o); this.color = new Color(o.color); } },
  MeshLambertMaterial: class { constructor(o = {}) { Object.assign(this, o); this.color = new Color(o.color); } },
  CanvasTexture: class { constructor(c) { this.image = c; } },
  Raycaster: class {
    setFromCamera() { this.ray = {origin: new V3(), direction: new V3(0, 0, -1), distanceToPoint: () => 0}; return this; }
  },
  AdditiveBlending: 2
};

/* ------------------------------------------------------- mock 3d-force-graph */
const recorded = {calls: [], data: null, unknown: []};
function buildMock() {
  const t = {_graphData: null, _scene: new threeMock.Scene(), _onEngineStop: null};
  t._camera = Object.assign(new V3(0, 0, 520), {
    fov: 50, near: 0.1, far: 2000,
    position: new V3(0, 0, 520),
    updateProjectionMatrix() { this._projected = true; },
    lookAt() {}
  });
  t._renderer = {
    domElement: {getBoundingClientRect: () => ({left: 0, top: 0, width: 1440, height: 900})},
    getContext: () => ({getParameter: () => 'WebGL 2.0 (mock)'}),
    info: {render: {calls: 0}}
  };
  t._controls = {target: new V3(), autoRotate: false, autoRotateSpeed: 2};
  t._lights = [{intensity: Math.PI}, {intensity: 0.6 * Math.PI}];
  const known = new Set([...API.accessors, ...API.methods]);
  let instance;
  instance = new Proxy(t, {
    get(target, prop) {
      const specials = {
        graphData: (d) => {
          (d.nodes || []).forEach((n, i) => {                 // the real library assigns x/y/z
            const a = i * 0.7;
            n.x = Math.cos(a) * (60 + i * 9);
            n.y = Math.sin(a * 1.3) * (50 + i * 5);
            n.z = Math.sin(a * 0.7) * (70 + i * 11);
          });
          target._graphData = d;
          recorded.data = d;
          return instance;
        },
        camera: () => target._camera,
        scene: () => target._scene,
        renderer: () => target._renderer,
        controls: () => target._controls,
        lights: () => target._lights,
        d3Force: () => ({strength: () => {}, distance: () => {}}),
        zoomToFit: () => instance,
        onEngineStop: (fn) => { target._onEngineStop = fn; return instance; },
        getGraphBbox: () => {
          if (!recorded.data) return {x: [0, 0], y: [0, 0], z: [0, 0]};
          const ax = recorded.data.nodes.map(n => n.x), ay = recorded.data.nodes.map(n => n.y), az = recorded.data.nodes.map(n => n.z);
          return {x: [Math.min(...ax), Math.max(...ax)], y: [Math.min(...ay), Math.max(...ay)], z: [Math.min(...az), Math.max(...az)]};
        }
      };
      if (prop in specials) return specials[prop];
      if (prop in target) return target[prop];
      if (typeof prop === 'symbol') return undefined;
      if (known.has(prop)) {
        // mirrors the real chained accessors: called with no arguments it is a
        // getter returning the current value, otherwise it sets and stays chainable
        return (...args) => {
          if (args.length === 0) return target['_set_' + prop];
          recorded.calls.push(String(prop));
          target['_set_' + prop] = args[0];
          return instance;
        };
      }
      recorded.unknown.push(String(prop));
      return () => { throw new TypeError('ForceGraph3D.' + String(prop) + ' is not a function'); };
    },
    set(target, prop, v) { target['_set_' + prop] = v; return true; }
  });
  return {instance, target: t};
}

/* ------------------------------------------------------------ extract the app */
const m = HTML.match(/<script type="module">([\s\S]*?)<\/script>/);
if (!m) { console.error('could not find the module script in viewer/index.html'); process.exit(1); }
const original = m[1];
let code = original
  // swap only the three.js import for the mock; loadScript keeps running for real,
  // against the controllable <head> above, so the fallback chain is exercised
  .replace(/THREE = await loadThree\(\);/, 'THREE = globalThis.__threeMock; window.THREE = THREE;')
  .replace(/const ForceGraph3D = window\.ForceGraph3D;/, 'const ForceGraph3D = globalThis.__ForceGraphMock;');

check(code !== original, 'the harness patched the three.js import for headless running');
check(code.includes('await loadScript(CFG.graph)'),
      "the viewer's real loadScript() is exercised (CDN shim, not stubbed out)");

// The id list comes from tools/harness.mjs, so a new element on the page cannot be
// forgotten here. It was a hand-copied list until 2026-09-23, and it had already drifted:
// the brain chip and the focus card were missing, and the module threw at boot the moment
// the focus wiring called addEventListener on an element this mock had never created.
ELEMENT_IDS.forEach(id => makeEl(id));

const mock = buildMock();
const sandbox = {window: windowMock, document: documentMock, console, setTimeout, clearTimeout,
                 setInterval: windowMock.setInterval, clearInterval,
                 requestAnimationFrame: windowMock.requestAnimationFrame,
                 fetch: windowMock.fetch, performance: {now: () => Date.now()}};
sandbox.globalThis = sandbox;
sandbox.window.window = sandbox.window;
sandbox.__threeMock = {default: threeMock, ...threeMock};
sandbox.__ForceGraphMock = function () { return mock.instance; };
sandbox.window.ForceGraph3D = sandbox.__ForceGraphMock;

const context = vm.createContext(sandbox);
vm.runInContext(GRAPH_JS, context, {filename: 'graph-data.js'});
try {
  // a real <script type="module"> has its own scope; replicate that with an IIFE
  // so that `const GRAPH` in the viewer does not collide with the classic-script
  // binding created by graph-data.js (which is exactly how a browser separates them)
  vm.runInContext('(function(){\n' + code + '\n})();', context, {filename: 'viewer-app.js'});
} catch (err) {
  bad('the viewer module threw while booting: ' + err.message + '\n' + (err.stack || ''));
}
await sleep(80);   // let the async boot (script load -> graph build) complete

/* --------------------------------------------------------------- assertions */
const g = sandbox.window.GRAPH;
check(!!g && Array.isArray(g.nodes) && Array.isArray(g.links), 'graph-data.js defines GRAPH with nodes[] and links[]');
check(g.nodes.every((n, i) => n.id === i && n.index === i), 'every node id equals its index in nodes[]');
check(g.nodes.every(n => n.label && n.group && n.excerpt && n.excerpt.length > 400),
      'every node carries a label, a group and a ~700-char excerpt');
check(g.links.every(l => Number.isInteger(l.source) && Number.isInteger(l.target) &&
      l.source >= 0 && l.source < g.nodes.length && l.target >= 0 && l.target < g.nodes.length),
      'every link endpoint is a valid node index');
check(new Set(g.nodes.map(n => n.label)).size === g.nodes.length, 'note titles are unique');
check(g.nodes.every(n => n.degree >= 0), 'degrees were computed for every node');

check(recorded.unknown.length === 0, 'the viewer only used real ForceGraph3D members' +
      (recorded.unknown.length ? ' -> unknown: ' + recorded.unknown.join(', ') : ''));
check(recorded.data && recorded.data.nodes.length === g.nodes.length, 'every node reached the graph engine');
check(recorded.data && recorded.data.links.length === g.links.length, 'every link reached the graph engine');
check(recorded.data && recorded.data.links.every(l => typeof l.source === 'object' && typeof l.target === 'object'),
      'link endpoints were resolved from indexes to node objects');

const nodeColor = mock.target._set_nodeColor;
if (typeof nodeColor === 'function') {
  const colors = g.nodes.map(n => nodeColor(n));
  check(colors.every(c => /^#[0-9a-f]{6}$/i.test(c)), 'nodeColor() returns a hex colour for every note');
  check(new Set(colors).size > 1, 'groups are colour-coded differently');
} else bad('nodeColor accessor was never registered');

const linkColor = mock.target._set_linkColor;
if (typeof linkColor === 'function') {
  check(recorded.data.links.map(l => linkColor(l)).every(c => /^#[0-9a-f]{6}$/i.test(c)),
        'linkColor() returns hex (three.js cannot parse rgba strings)');
} else bad('linkColor accessor was never registered');

const nodeLabel = mock.target._set_nodeLabel;
if (typeof nodeLabel === 'function') check(nodeLabel(g.nodes[0]).includes(g.nodes[0].label), 'nodeLabel() shows the note title');
else bad('nodeLabel accessor was never registered');

const nodeVal = mock.target._set_nodeVal;
if (typeof nodeVal === 'function') check(g.nodes.every(n => nodeVal(n) > 0), 'nodeVal() is a positive number for every note');
else bad('nodeVal accessor was never registered');

const nodeThree = mock.target._set_nodeThreeObject;
if (typeof nodeThree === 'function') check(!!nodeThree(g.nodes[0]), 'nodeThreeObject() builds a glow object per node');
else bad('nodeThreeObject accessor was never registered');

check(mock.target._scene.children.length >= 2, 'starfield + core glow were added to the 3D scene');
check(mock.target._camera.far > 10000, 'camera far plane was extended so the starfield is not clipped');
check(mock.target._camera._projected === true, 'camera.updateProjectionMatrix() ran after the change');
check(mock.target._lights[0].intensity > Math.PI, 'node lights were boosted from the bundle defaults, not replaced');

/* interaction paths */
const onClick = mock.target._set_onNodeClick;
if (typeof onClick === 'function') {
  try { onClick(g.nodes[2]); ok('clicking a node runs the focus path'); }
  catch (err) { bad('focus path threw: ' + err.message); }
} else bad('onNodeClick was never registered');

const onBg = mock.target._set_onBackgroundClick;
if (typeof onBg === 'function') {
  try { onBg({clientX: 5, clientY: 5}); ok('a click on empty space is handled (deselect)'); }
  catch (err) { bad('background click threw: ' + err.message); }
} else bad('onBackgroundClick was never registered');

const mustUse = ['backgroundColor','nodeId','nodeColor','nodeVal','nodeLabel','linkColor','linkWidth',
  'linkDirectionalParticles','warmupTicks','cooldownTicks','d3AlphaDecay','d3VelocityDecay',
  'onNodeClick','onBackgroundClick','nodeThreeObject','nodeThreeObjectExtend','cameraPosition'];
const missing = mustUse.filter(n => !recorded.calls.includes(n));
check(missing.length === 0, 'all required accessors were configured' + (missing.length ? ' -> missing ' + missing.join(', ') : ''));

// cameraPosition takes (position, lookAt, ms) - the value stored is an object
check(recorded.calls.includes('cameraPosition'), 'camera fly-to is wired (cameraPosition was called)');
const camArg = mock.target._set_cameraPosition;
check(camArg && typeof camArg === 'object' && 'x' in camArg && 'y' in camArg && 'z' in camArg,
      'fly-to targets a 3D position');
check(rafQueue.length > 0, 'the idle-drift loop was scheduled with requestAnimationFrame');
if (rafQueue.length) {
  try { rafQueue.forEach(fn => fn()); ok('idle-drift tick runs'); }
  catch (err) { bad('idle-drift tick threw: ' + err.message); }
}
check(mock.target._controls.autoRotateSpeed === 0.42, 'idle drift is slow (autoRotateSpeed 0.42)');

if (typeof mock.target._onEngineStop === 'function') {
  try { mock.target._onEngineStop(); ok('layout-settle handler runs and frames the camera'); }
  catch (err) { bad('layout-settle handler threw: ' + err.message); }
} else bad('onEngineStop was never registered');

/* the in-page health check the HUD shows */
if (sandbox.window.__alfred && typeof sandbox.window.__alfred.selfTest === 'function') {
  mock.target._renderer.info.render.calls = 12;
  const st = sandbox.window.__alfred.selfTest();
  check(st.ok === true, 'in-page self-test reports the renderer is drawing: ' + JSON.stringify(st));
  check(String(elements.get('status-text').textContent).startsWith('live'),
        'HUD flips to "live" once frames are being drawn');
} else bad('window.__alfred.selfTest was not exposed');

/* the ask bar -> POST /chat -> rendered answer */
const form = elements.get('ask-form');
check(!!(form && form._ev && form._ev.submit), 'the ask bar has a submit handler');
if (form && form._ev && form._ev.submit) {
  elements.get('q').value = 'what did the movers quote?';
  try { form._ev.submit({preventDefault() {}}); } catch (err) { bad('submitting the ask bar threw: ' + err.message); }
  await sleep(30);
  const chatCalls = fetchCalls.filter(c => c.url === '/chat');
  check(chatCalls.length === 1, 'the ask bar POSTs to /chat (the boot-time /health call is separate)');
  let body = {};
  try { body = JSON.parse(chatCalls[0].opts.body); } catch (_) {}
  check(body.question === 'what did the movers quote?', 'the question is sent as JSON');
  check(elements.get('answer-text').textContent.includes('26,000'), 'the answer is rendered in the bottom bar');
  check(elements.get('answer').hidden === false, 'the answer panel is shown');
  check(elements.get('q').value === '', 'the input clears after a successful answer');
  check(elements.get('key-note').hidden === false && /placeholder key/.test(elements.get('key-note').textContent),
        'the HUD warns that config.json still holds the placeholder key');

  /* placeholder-key error path: the server answers with an error object, not a crash */
  const failingFetch = async () => ({ok: true, status: 200, json: async () => ({
    ok: false, error: 'The brain has no API key yet - config.json still holds the placeholder.',
    code: 'placeholder_api_key', hint: 'Paste your key into config.json and restart server.py.',
    nodes: [1, 4]
  })});
  sandbox.fetch = failingFetch;      // the module reads the context global, not window.fetch
  elements.get('q').value = 'anything';
  try { form._ev.submit({preventDefault() {}}); } catch (err) { bad('the error path threw: ' + err.message); }
  await sleep(30);
  check(elements.get('answer-text').className === 'error', 'a placeholder-key error is styled as an error');
  check(elements.get('answer-text').textContent.includes('placeholder'),
        'the placeholder-key message is shown to the user instead of failing silently');
}

/* ---------------------------------------------- CDN fallback chain, for real */
const alfred = sandbox.window.__alfred;
check(!!(alfred && typeof alfred.loadScript === 'function' && Array.isArray(alfred.CFG.graph)),
      'the viewer exposes its loaders so the fallback chain can be tested');
if (alfred && typeof alfred.loadScript === 'function') {
  const CFG = alfred.CFG;
  check(CFG.graph.some(u => u.includes('cdn.jsdelivr.net')), 'jsDelivr is the first source for the graph bundle');
  check(/^https:\/\/unpkg\.com/.test(CFG.graph[1] || ''), 'unpkg is the second source');
  check(CFG.graph.some(u => u.startsWith('./vendor/')), 'the offline ./vendor copy is the last resort');
  check(Array.isArray(CFG.three) && CFG.three[0].includes('three@0.183.0') && CFG.three[1].startsWith('./vendor/'),
        'three.js is pinned to 0.183.0 with the same offline fallback');
  check(CFG.graph.indexOf('./vendor/3d-force-graph.min.js') === CFG.graph.length - 1,
        'the offline copy is tried last, never before a CDN');

  // a) CDN unreachable -> it must reach the vendored copy
  documentMock.head.mode = 'cdn-fails';
  documentMock.head.attempts.length = 0;
  let resolvedWith = null, resolveErr = null;
  try { resolvedWith = await alfred.loadScript(CFG.graph); } catch (e) { resolveErr = e; }
  check(resolvedWith === './vendor/3d-force-graph.min.js',
        'when the CDNs are unreachable the viewer falls back to the local copy (got ' + resolvedWith + ')');
  check(documentMock.head.attempts.length === 3, 'all three sources were tried in order: ' +
        documentMock.head.attempts.join(' -> '));
  check(documentMock.head.attempts[0].includes('jsdelivr') && documentMock.head.attempts[2].startsWith('./vendor/'),
        'the attempt order is CDN, CDN, local');

  // b) nothing reachable at all -> a clear error, not a blank page
  documentMock.head.mode = 'all-fail';
  documentMock.head.attempts.length = 0;
  let rejection = null;
  try { await alfred.loadScript(CFG.graph); } catch (e) { rejection = e; }
  check(!!rejection, 'with every source unreachable loadScript() rejects instead of hanging');
  check(rejection && Array.isArray(rejection.tried) && rejection.tried.length === CFG.graph.length,
        'the rejection carries the list of sources it tried, for the on-screen message');
  documentMock.head.mode = 'all-ok';

  // c) no three.js anywhere -> the bundle is still allowed to render with its own copy
  const three = await alfred.loadThree();
  check(three === null, 'loadThree() returns null when three.js is unavailable (the bundle then uses its own copy)');
}

check(intervalCallbacks.length > 0, 'placeholder rotation timers were registered');
try { intervalCallbacks.forEach(fn => fn()); ok('placeholder rotation tick runs'); }
catch (err) { bad('placeholder rotation tick threw: ' + err.message); }

const painted = canvases.filter(c => c._calls && c._calls.length);
check(painted.length >= 2, 'glow + star textures were painted onto real canvases (' + painted.length + ')');

/* ------------------------------------------------------------------- report */
console.log('\nviewer/index.html - headless verification (node ' + process.version + ')');
console.log(notes.join('\n'));
console.log('\n  accessors exercised : ' + [...new Set(recorded.calls)].sort().join(', '));
console.log('  API surface checked : 3d-force-graph@' + API.version + ' (' +
            API.accessors.length + ' accessors / ' + API.methods.length + ' methods)');
if (problems.length) {
  console.log('\n  RESULT: FAILED (' + problems.length + ')');
  problems.forEach(p => console.log('   - ' + p));
  process.exit(1);
}
console.log('\n  RESULT: PASSED - viewer boots, wires the graph engine and survives every interaction path');
