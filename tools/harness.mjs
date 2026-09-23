/*
 * tools/harness.mjs - shared pieces for running viewer/index.html outside a browser.
 *
 * Provides: a virtual DOM, a virtual clock (so timing rules can be tested exactly),
 * mocks for the CDN bundle and three.js, and mocks for the two Web Speech APIs.
 * Used by tools/verify-voice.mjs.
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
export const HTML = fs.readFileSync(path.join(ROOT, 'viewer', 'index.html'), 'utf8');
export const GRAPH_JS = fs.readFileSync(path.join(ROOT, 'viewer', 'graph-data.js'), 'utf8');
export const GRAPH_DATA = (() => {
  const m = GRAPH_JS.match(/const GRAPH\s*=\s*(\{[\s\S]*?\});/);
  return m ? JSON.parse(m[1]) : {nodes: [], links: []};
})();
export const API = JSON.parse(fs.readFileSync(path.join(ROOT, 'tools', 'api-3d-force-graph.json'), 'utf8'));

export const ELEMENT_IDS = [
  'stage','grade','hud','stats','status','status-text','legend','hint','panel','panel-close',
  'panel-label','panel-group','panel-body','panel-excerpt','neigh-title','panel-neighbours',
  'panel-meta','ask-wrap','answer','a-who','a-model','a-close','answer-text','answer-sources',
  'a-foot','ask-form','q','send','boot','boot-msg','key-note',
  'mic','speak-toggle','voice-status','voice-text','voice-detail',
  'sight','sight-ring','sight-badge','sight-badge-text','sight-badge-sub',
  'a-frame','frame-shot','frame-meta'
];

/* ------------------------------------------------------------- virtual clock */
export function makeClock(){
  let now = 0, nextId = 1;
  const timers = new Map();
  return {
    now: () => now,
    setTimeout(fn, ms = 0){
      const id = nextId++;
      timers.set(id, {fn, at: now + Math.max(0, ms|0)});
      return id;
    },
    clearTimeout(id){ timers.delete(id); },
    setInterval(fn, ms = 0){                    // simple repeating timer
      const id = nextId++;
      const tick = () => {
        if (!timers.has(id)) return;
        fn();
        timers.set(id, {fn: tick, at: now + Math.max(1, ms|0)});
      };
      timers.set(id, {fn: tick, at: now + Math.max(1, ms|0)});
      return id;
    },
    clearInterval(id){ timers.delete(id); },
    /** run every timer due within `ms`, in order, advancing the clock */
    advance(ms){
      const target = now + ms;
      let guard = 0;
      while (guard++ < 20000){
        let dueId = null, dueAt = Infinity;
        for (const [id, t] of timers){
          if (t.at <= target && t.at < dueAt){ dueAt = t.at; dueId = id; }
        }
        if (dueId === null) break;
        const t = timers.get(dueId);
        timers.delete(dueId);
        now = t.at;
        t.fn();
      }
      now = target;
      return this;
    },
    pending(){ return [...timers.values()].map(t => t.at - now).sort((a, b) => a - b); },
    /** advance the clock in small steps, yielding between them.
     *
     * This fake clock freezes during real time (setTimeout, setImmediate and promise
     * continuations do not move it), which is what lets the suites drive animation
     * frames by hand instead of waiting for a real paint. Anything that spins on
     * performance.now() - the birth glow of a capture, say - needs the clock walked
     * forward instead, which is what this is for.
     */
    async advanceAsync(ms, step = 16){
      const target = now + ms;
      let guard = 0;
      while (now < target && guard++ < 5000){
        this.advance(step);
        await new Promise(resolve => setImmediate(resolve));
      }
      return this;
    }
  };
}

/* -------------------------------------------------------------- DOM elements */
export function makeDom(){
  const elements = new Map();
  const makeEl = (id, tag = 'div') => {
    const el = {
      id, tagName: tag.toUpperCase(), children: [], style: {}, dataset: {},
      hidden: false, disabled: false, className: '', textContent: '', value: '',
      placeholder: '', src: '', title: '',
      classList: {
        _s: new Set(),
        add(...c){ c.forEach(x => this._s.add(x)); },
        remove(...c){ c.forEach(x => this._s.delete(x)); },
        contains(c){ return this._s.has(c); },
        toggle(c, on){ (on === undefined ? !this._s.has(c) : !!on) ? this._s.add(c) : this._s.delete(c); }
      },
      set innerHTML(v){ this._html = v; },
      get innerHTML(){ return this._html || ''; },
      appendChild(c){ this.children.push(c); return c; },
      remove(){}, focus(){ this.focused = true; }, blur(){},
      addEventListener(type, fn){ (this._ev = this._ev || {})[type] = fn; },
      removeEventListener(){},
      getBoundingClientRect(){ return {left:0, top:0, width:1440, height:900, right:1440, bottom:900}; },
      _attr: {},
      getAttribute(k){ return Object.prototype.hasOwnProperty.call(this._attr, k) ? this._attr[k] : null; },
      setAttribute(k, v){ this._attr[k] = v; this[k] = v; }
    };
    elements.set(id, el);
    return el;
  };
  return {elements, makeEl};
}

/* ----------------------------------------------------- three.js + graph mocks */
export function makeThreeMock(){
  class V3 {
    constructor(x = 0, y = 0, z = 0){ this.x = x; this.y = y; this.z = z; }
    set(x, y, z){ this.x = x; this.y = y; this.z = z; return this; }
    setScalar(s){ this.x = this.y = this.z = s; return this; }
    clone(){ return new V3(this.x, this.y, this.z); }
    sub(v){ this.x -= v.x; this.y -= v.y; this.z -= v.z; return this; }
    add(v){ this.x += v.x; this.y += v.y; this.z += v.z; return this; }
    dot(v){ return this.x*v.x + this.y*v.y + this.z*v.z; }
    distanceTo(){ return 1; }
    length(){ return Math.hypot(this.x, this.y, this.z); }
    normalize(){ const l = this.length() || 1; this.x /= l; this.y /= l; this.z /= l; return this; }
    multiplyScalar(s){ this.x *= s; this.y *= s; this.z *= s; return this; }
    applyQuaternion(){ return this; }
    project(){ return this.set(0, 0, -1); }
  }
  class Object3D {
    constructor(){ this.children = []; this.position = new V3(); this.scale = new V3(1,1,1); this.userData = {}; this.parent = null; }
    add(...o){ o.forEach(c => { c.parent = this; this.children.push(c); }); return this; }
    remove(){ return this; }
    updateMatrixWorld(){}
  }
  class Color { constructor(v){ this.v = v; } set(v){ this.v = v; return this; } setHSL(){ return this; } }
  return {
    REVISION: '183', Vector2: V3, Vector3: V3, Color,
    Group: class extends Object3D {}, Mesh: class extends Object3D {}, Scene: class extends Object3D {},
    Sprite: class extends Object3D { constructor(m){ super(); this.material = m; } },
    BufferGeometry: class { setAttribute(n, a){ this[n] = a; return this; } computeBoundingSphere(){} },
    BufferAttribute: class { constructor(arr, size){ this.array = arr; this.itemSize = size; } },
    Points: class extends Object3D { constructor(g, m){ super(); this.geometry = g; this.material = m; } },
    PointsMaterial: class { constructor(o = {}){ Object.assign(this, o); } },
    SpriteMaterial: class { constructor(o = {}){ Object.assign(this, o); this.color = new Color(o.color); } },
    MeshLambertMaterial: class { constructor(o = {}){ Object.assign(this, o); this.color = new Color(o.color); } },
    CanvasTexture: class { constructor(c){ this.image = c; } },
    Raycaster: class { setFromCamera(){ this.ray = {origin: new V3(), direction: new V3(0,0,-1), distanceToPoint: () => 0}; return this; } },
    AdditiveBlending: 2
  };
}

export function makeGraphMock(three){
  const recorded = {calls: [], data: null};
  const t = {_graphData: null, _scene: new three.Scene(), _onEngineStop: null};
  t._camera = Object.assign(new three.Vector3(0, 0, 520), {
    fov: 50, near: 1.5, far: 45000, position: new three.Vector3(0, 0, 520),
    updateProjectionMatrix(){ this._projected = true; }, lookAt(){}
  });
  t._renderer = {
    domElement: {getBoundingClientRect: () => ({left:0, top:0, width:1440, height:900})},
    getContext: () => ({getParameter: () => 'WebGL 2.0 (mock)'}), info: {render: {calls: 3}}
  };
  t._controls = {target: new three.Vector3(), autoRotate: false, autoRotateSpeed: 2};
  t._lights = [{intensity: Math.PI}, {intensity: 0.6*Math.PI}];
  const known = new Set([...API.accessors, ...API.methods]);
  let instance;
  instance = new Proxy(t, {
    get(target, prop){
      const specials = {
        graphData: (d) => {
          // 3d-force-graph keeps the node objects it is given, and d3 only seeds a
          // position for a node that has none - so a node's own x/y/z survive every
          // graphData() call. Adding a note mid-session relies on exactly that.
          (d.nodes || []).forEach((n, i) => {
            // d3 seeded a position for a node that has none; it never clears fx/fy/fz
            if (n.x == null || n.y == null || n.z == null){
              const a = i * 0.7; n.x = Math.cos(a)*60; n.y = Math.sin(a)*50; n.z = Math.sin(a)*30;
            }
          });
          target._graphData = d; recorded.data = d; return instance;
        },
        camera: () => target._camera,
        scene: () => target._scene,
        renderer: () => target._renderer,
        controls: () => target._controls,
        lights: () => target._lights,
        d3Force: () => ({strength: () => {}, distance: () => {}}),
        zoomToFit: () => instance,
        onEngineStop: (fn) => { target._onEngineStop = fn; return instance; },
        getGraphBbox: () => ({x:[0,80], y:[0,60], z:[0,40]})
      };
      if (prop in specials) return specials[prop];
      if (prop in target) return target[prop];
      if (typeof prop === 'symbol') return undefined;
      if (known.has(prop)) {
        return (...args) => {
          if (args.length === 0) return target['_set_' + prop];
          recorded.calls.push(String(prop));
          target['_set_' + prop] = args[0];
          return instance;
        };
      }
      return () => { throw new TypeError('ForceGraph3D.' + String(prop) + ' is not a function'); };
    },
    set(target, prop, v){ target['_set_' + prop] = v; return true; }
  });
  return {instance, target: t, recorded};
}

/* ------------------------------------------------------------ speech mocks */
export function makeSpeechMock({ voices = [], neverEnds = false, clock } = {}){
  const spoken = [];
  const utterances = [];
  let cancelCount = 0;
  class Utterance {
    constructor(text){ this.text = text; this.voice = null; this.lang = ''; this.rate = 1; this.pitch = 1; this.volume = 1; }
  }
  const synth = {
    getVoices: () => voices,
    speak(u){
      spoken.push(u.text);
      utterances.push(u);
      if (typeof u.onstart === 'function') u.onstart();
      if (!neverEnds){
        // a short, real utterance takes roughly this long
        clock.setTimeout(() => { if (typeof u.onend === 'function') u.onend(); }, 40);
      }
    },
    cancel(){ cancelCount++; },
    addEventListener(){},
    onvoiceschanged: null
  };
  // `spoken` is faithful (it includes the silent unlock primer, text ' '); `words()`
  // is what the user would actually hear.
  return {synth, Utterance, spoken, utterances,
          words: () => spoken.filter(t => t && t.trim().length > 0),
          cancelCount: () => cancelCount};
}

export function makeRecognitionMock({ clock, unsupported = false }){
  const state = {instances: []};
  if (unsupported) return state;
  class FakeRecognition {
    constructor(){
      this.lang = ''; this.continuous = false; this.interimResults = false; this.maxAlternatives = 1;
      this.started = false; this.stopCount = 0;
      state.instances.push(this);
    }
    start(){ this.started = true; if (typeof this.onstart === 'function') clock.setTimeout(() => this.onstart(), 1); }
    stop(){ this.stopCount++; this.started = false; if (typeof this.onend === 'function') clock.setTimeout(() => this.onend(), 1); }
    abort(){ this.stop(); }
    /** test driver: hand the app a finalised fragment the way the browser would */
    emitFinal(text){
      this.onresult({resultIndex: 0, results: [Object.assign([{transcript: text}], {isFinal: true})]});
    }
    emitInterim(text){
      this.onresult({resultIndex: 0, results: [Object.assign([{transcript: text}], {isFinal: false})]});
    }
    emitError(error){ if (typeof this.onerror === 'function') this.onerror({error}); }
    emitEnd(){ if (typeof this.onend === 'function') this.onend(); }
  }
  state.Ctor = FakeRecognition;
  state.latest = () => state.instances[state.instances.length - 1];
  return state;
}

/* ------------------------------------------------------------------- a screen
 * A stand-in for getDisplayMedia: the shape the viewer really meets (a stream with a
 * video track, readyState, onended) plus a queue of frames that the canvas mock hands
 * back from toDataURL(). Tests use it to drive the capture path - changing the frames
 * between questions is how "the frame is taken now, never cached" gets proved.
 */
export function makeScreenMock({ frames = null, unsupported = false, deny = false,
                                 videoWidth = 1280, videoHeight = 720 } = {}){
  const state = {
    streams: [], tracks: [], ended: 0, stopped: 0, asked: 0, got: 0,
    frames: frames ? [...frames] : [],
    served: [],                       // the data URLs actually handed to the page
    videoWidth, videoHeight, deny, unsupported,
    next(){                          // one frame per toDataURL call, in order
      if (!state.frames.length) return tinyFrame(state.got++);
      const f = state.frames.shift();
      state.frames.push(f);           // cycle, so a long test never runs dry
      return f;
    }
  };
  if (unsupported) return state;
  state.getDisplayMedia = async (constraints) => {
    state.asked++;
    state.constraints = constraints;
    if (deny){
      const err = new Error('Permission denied');
      err.name = 'NotAllowedError';
      throw err;
    }
    const track = {
      kind: 'video', label: 'screen:mock:0', readyState: 'live',
      onended: null, muted: false, enabled: true,
      stop(){ this.readyState = 'ended'; state.stopped++; },
      getSettings: () => ({width: state.videoWidth, height: state.videoHeight, frameRate: 30}),
      addEventListener(){}, removeEventListener(){}
    };
    const stream = {
      id: 'mock-stream-' + state.streams.length, active: true,
      oninactive: null,
      getVideoTracks: () => [track],
      getTracks: () => [track],
      getAudioTracks: () => [],
      addEventListener(){}, removeEventListener(){}
    };
    state.streams.push(stream);
    state.tracks.push(track);
    return stream;
  };
  state.end = (i = -1) => {                  // the browser's own "Stop sharing"
    const t = state.tracks.at(i);
    if (!t) return false;
    state.ended++;
    t.readyState = 'ended';
    if (typeof t.onended === 'function') t.onended();
    return true;
  };
  state.kill = (i = -1) => {                 // the track dies with no event at all
    const t = state.tracks.at(i);
    if (t) t.readyState = 'ended';
    return !!t;
  };
  return state;
}

/* A real 2x2 JPEG, so an encoded frame is genuinely a JPEG even in the harness. */
export const TINY_JPEG = 'data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsL' +
  'DBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAACAAEBAREA/8QAFAABAAAAAAAAAAAAAAAA' +
  'AAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==';
export function tinyFrame(n = 0){
  // every frame is a real JPEG; the padding keeps them different from one another, so
  // a test can tell "this question's frame" from "the last question's frame"
  return TINY_JPEG + (n ? '/*' + n + '*/' : '');
}

/* A dull but valid answer to POST /remember: the real tests pass their own fetchImpl. */
export function defaultCapture(health = {}){
  const i = (health && health.notes) || GRAPH_DATA.nodes.length;
  const label = 'The Finish Window Should Be 900';
  const line = 'Filed and lit, sir. \u201c' + label + '\u201d is in the galaxy now, born beside ' +
               'First Week in Ayodhya, holding on to nothing at all, and the galaxy is ' +
               (i + 1) + ' notes strong.';
  return {
    ok: true, captured: true, filed: true, indexed: true, answer: line, line,
    title: label, date: '2026-09-23', file: 'captures/the-finish-window-should-be-900.md',
    index: i, notes: i + 1,
    node: {id: i, index: i, label, group: 'captures', excerpt: 'The finish window should be 900 milliseconds.',
           path: 'captures/the-finish-window-should-be-900.md', words: 15, chars: 103, degree: 0,
           wikilinks: [], mentions: []},
    anchor: {index: 0, label: GRAPH_DATA.nodes.length ? GRAPH_DATA.nodes[0].label : 'First Week in Ayodhya',
             score: 1.15},
    links: [], link_labels: [], graph_file: 'behind', model: 'stub-model', turns: 0
  };
}

/* A dull but valid answer to POST /see, on the same pattern. */
export function defaultSee(health = {}){
  const sight = (health && health.sight) || {};
  const line = sight.lines || {};
  return {
    ok: true, decision: 'screen', on_notes: false, nodes: [], sources: [], read: [],
    answer: 'That is a dark screen with a few windows open, sir, and nothing in it looks wrong.',
    frame: {media_type: 'image/jpeg', bytes: 2048, width: 1280, height: 720, at: Math.floor(Date.now() / 1000)},
    model: 'stub-model', turns: 1,
    lines: line
  };
}

/* ------------------------------------------------------------------- boot it */
export function patchModuleSource(){
  const m = HTML.match(/<script type="module">([\s\S]*?)<\/script>/);
  if (!m) throw new Error('no module script found in viewer/index.html');
  const patched = m[1]
    .replace(/THREE = await loadThree\(\);/, 'THREE = globalThis.__threeMock; window.THREE = THREE;')
    .replace(/const ForceGraph3D = window\.ForceGraph3D;/, 'const ForceGraph3D = globalThis.__ForceGraphMock;');
  return {original: m[1], patched};
}

export async function boot(options = {}){
  const {
    search = '', voices = [], neverEnds = false, noRecognition = false,
    fetchImpl = null, unlockSpeech = true, screen = null,
    // what GET /health answers with. No greeting by default: pages booted for other
    // tests should not start talking, and a greeting is a thing a test asks for on
    // purpose (see the greeting group in verify-voice.mjs).
    health = {ok: true, notes: 12, key: {state: 'set'}}
  } = options;

  const clock = makeClock();
  const {elements, makeEl} = makeDom();
  ELEMENT_IDS.forEach(id => makeEl(id));

  const canvases = [];
  const documentMock = {
    createElement(tag){
      if (tag === 'video'){
        // a video element fed by a stream: readyState/videoWidth appear as soon as it
        // "plays", which is what the capture path waits for
        const v = makeEl('video-' + Math.random().toString(36).slice(2), 'video');
        v.srcObject = null; v.muted = false; v.paused = true;
        v.videoWidth = 0; v.videoHeight = 0; v.readyState = 0;
        v.play = async () => {
          screenMock.got++;
          v.paused = false;
          if (v.srcObject){
            const t = v.srcObject.getVideoTracks()[0];
            if (t && t.readyState === 'live'){
              v.videoWidth = screenMock.videoWidth;
              v.videoHeight = screenMock.videoHeight;
              v.readyState = 4;
            }
          }
          return undefined;
        };
        v.pause = () => { v.paused = true; };
        v.removeAttribute = () => {};
        return v;
      }
      if (tag === 'canvas'){
        const c = makeEl('canvas-' + canvases.length, 'canvas');
        c.width = 0; c.height = 0;
        // the encoder: hands back a real JPEG data URL, one per call
        c.toDataURL = (type = 'image/jpeg', quality) => {
          const url = screenMock.next();
          screenMock.served.push({url, type, quality, width: c.width, height: c.height});
          return url;
        };
        c.removeAttribute = () => {};
        c.getContext = () => new Proxy({}, {
          get(_t, p){
            if (p === 'createRadialGradient') return () => ({addColorStop(){}});
            return () => {};
          }, set(){ return true; }
        });
        canvases.push(c);
        return c;
      }
      return makeEl(tag + '-' + Math.random().toString(36).slice(2), tag);
    },
    getElementById: (id) => elements.get(id) || null,
    querySelectorAll: () => [],
    head: {appendChild: (s) => { if (s.onload) clock.setTimeout(() => s.onload(), 1); return s; }},
    addEventListener(){}, activeElement: null
  };

  const windowEvents = {};
  const speech = makeSpeechMock({voices, neverEnds, clock});
  const screenMock = makeScreenMock(screen === null ? {} : screen);
  const recognition = makeRecognitionMock({clock, unsupported: noRecognition});
  const fetchCalls = [];

  const windowMock = {
    document: documentMock,
    addEventListener(type, fn){ (windowEvents[type] = windowEvents[type] || []).push(fn); },
    removeEventListener(){},
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    setInterval: clock.setInterval, clearInterval: clock.clearInterval,
    requestAnimationFrame: (fn) => clock.setTimeout(() => fn(clock.now()), 16),
    innerWidth: 1440, innerHeight: 900, devicePixelRatio: 1,
    location: {href: 'http://127.0.0.1:4700/' + search, search: navSearch(search), origin: 'http://127.0.0.1:4700'},
    navigator: {userAgent: 'alfred-harness',
                mediaDevices: screenMock.unsupported ? undefined
                             : {getDisplayMedia: screenMock.getDisplayMedia}},
    performance: {now: () => clock.now()},
    speechSynthesis: speech.synth,
    SpeechSynthesisUtterance: speech.Utterance,
    console
  };
  if (recognition.Ctor) windowMock.webkitSpeechRecognition = recognition.Ctor;

  const sandbox = {
    window: windowMock, document: documentMock, console,
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    setInterval: clock.setInterval, clearInterval: clock.clearInterval,
    requestAnimationFrame: windowMock.requestAnimationFrame,
    performance: windowMock.performance,
    location: windowMock.location,
    navigator: windowMock.navigator,
    speechSynthesis: speech.synth,
    SpeechSynthesisUtterance: speech.Utterance,
    fetch: fetchImpl || (async (url, opts) => {
      fetchCalls.push({url, opts, body: opts && opts.body ? JSON.parse(opts.body) : null});
      if (String(url).indexOf('/health') >= 0)
        return {ok: true, status: 200, json: async () => health};
      if (String(url).indexOf('/remember') >= 0)
        return {ok: true, status: 200, json: async () => defaultCapture(health)};
      if (String(url).indexOf('/see') >= 0)
        return {ok: true, status: 200, json: async () => defaultSee(health)};
      return {ok: true, status: 200, json: async () => ({
        ok: true, answer: 'The movers quoted 26,000 for the road trip.',
        nodes: [7], sources: [{index: 7, label: 'Budget for the Move', score: 3}],
        model: 'stub-model', turns: 1
      })};
    })
  };
  if (recognition.Ctor) sandbox.webkitSpeechRecognition = recognition.Ctor;
  sandbox.globalThis = sandbox;
  sandbox.window.window = sandbox.window;

  // the sandbox's own globals must win over window.* for the module's bare references
  const three = makeThreeMock();
  sandbox.__threeMock = {default: three, ...three};
  const graph = makeGraphMock(three);
  sandbox.__ForceGraphMock = function(){ return graph.instance; };
  sandbox.window.ForceGraph3D = sandbox.__ForceGraphMock;

  const {patched} = patchModuleSource();
  const context = vm.createContext(sandbox);

  // flushing microtasks matters: the viewer boots in an async function, so its
  // continuations only run when the synchronous run yields. Advance the clock and
  // yield alternately until the app has finished coming up.
  const flush = async (rounds = 6, step = 40) => {
    for (let i = 0; i < rounds; i++){
      clock.advance(step);
      await new Promise((resolve) => setImmediate(resolve));
    }
    clock.advance(step);
    return true;
  };

  const api = {clock, elements, sandbox, context, windowMock, windowEvents, documentMock,
               speech, recognition, fetchCalls, graph, three, flush, screen: screenMock,
               chatCalls(){ return fetchCalls.filter(c => c.url === '/chat'); },
               rememberCalls(){ return fetchCalls.filter(c => c.url === '/remember'); },
               fireWindowEvent(type){ (windowEvents[type] || []).forEach(fn => fn({type})); },
               advance(ms){ clock.advance(ms); return this; },
               get app(){ return sandbox.window.__alfred; }};

  vm.runInContext(GRAPH_JS, context, {filename: 'graph-data.js'});
  vm.runInContext('(function(){\n' + patched + '\n})();', context, {filename: 'viewer-app.js'});
  await flush();
  if (!sandbox.window.__alfred){
    const why = elements.get('boot-msg').innerHTML || elements.get('status-text').textContent || 'unknown';
    throw new Error('the viewer did not finish booting: ' + why);
  }
  // A real visit always involves a gesture (opening the page, clicking). Default to
  // that; tests that care about the pre-gesture state pass unlockSpeech: false.
  if (unlockSpeech) api.fireWindowEvent('pointerdown');
  return api;
}

function navSearch(search){
  return search ? (search.startsWith('?') ? search : '?' + search) : '';
}
