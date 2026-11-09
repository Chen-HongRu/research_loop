// Exercise the shipped frontend without a browser or extra dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
class Element {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.dataset = {}; this.listeners = {}; this.classList = { toggle() {} }; this.isConnected = true; this.textContent = ''; }
  append(...nodes) { this.children.push(...nodes); }
  prepend(...nodes) { this.children.unshift(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this.textContent = ''; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
}
const elements = new Map();
const pending = [];
const overview = new Element(); overview.id = 'view-conversation';
const document = {
  getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
  createElement(tag) { return new Element(tag); },
  querySelectorAll(selector) { return selector === '.view' ? [overview] : []; }, addEventListener() {},
};
const context = vm.createContext({ document, window: { addEventListener() {}, scrollTo() {}, scrollY: 0 }, location: { search: '', href: 'http://localhost/prefix/' }, history: { replaceState() {} }, URL, URLSearchParams, AbortController, setTimeout, clearTimeout, setInterval() {}, HTMLDetailsElement: Element,
  fetch(url) { return new Promise(resolve => pending.push({ url, resolve })); },
});
const run = code => vm.runInContext(code, context);
const tick = () => new Promise(resolve => setImmediate(resolve));
async function respond(part, value) {
  const index = pending.findIndex(request => request.url.includes(part));
  assert.ok(index >= 0, `Missing request: ${part}`);
  pending.splice(index, 1)[0].resolve({ ok: true, json: async () => value });
  await tick();
}
const catalog = { default: 'a', studies: [{ id: 'a', name: 'Alpha', status: 'running' }, { id: 'b', name: 'Beta', status: 'completed' }] };
const snapshot = name => ({ goal: name, status: { status: 'completed', iteration: 1 }, history: { counts: { total: 1, with_result: 1, missing_result: 0 }, iterations: [{ iteration: 1, status: 'completed', result_available: true, source: 'iterations/000001/result.json', result: { summary: `${name} result`, limitations: ['synthetic'] } }] } });
(async () => {
  run(fs.readFileSync(path.join(__dirname, '../src/research_loop/dashboard/app.js'), 'utf8'));
  await respond('api/studies', catalog);
  // Switch while Alpha requests are still outstanding.
  run("selectStudy('b'); refresh();");
  await respond('api/studies', catalog);
  await respond('api/state?study=b', snapshot('Beta'));
  await respond('api/metrics?study=b', { available: false, status: 'missing' });
  assert.equal(document.getElementById('goal').textContent, 'Beta');
  assert.equal(document.getElementById('research-history').children.length, 1);
  assert.equal(document.getElementById('history-count').textContent, '1 轮记录 · 1 轮有结果 · 0 轮未报告结果');
  await respond('api/state?study=a', snapshot('Alpha'));
  await respond('api/metrics?study=a', { available: false, status: 'missing' });
  assert.equal(document.getElementById('goal').textContent, 'Beta');
  assert.equal(run('ui.study'), 'b');
  assert.equal(overview.hidden, false);
  run("selectStudy('a'); setView('conversation');");
  assert.equal(overview.hidden, true, 'Navigation must not reveal the previous Study while loading');
  run("selectStudy('b');");
  // Identical tool IDs must be fetched separately for each Study.
  run("var bodyA = document.createElement('pre'); loadToolDetails({id:'same',at:'now',status:'completed'}, bodyA);");
  await respond('api/tool?id=same&study=b', { id: 'same', at: 'now', status: 'completed', output: 'Beta tool' });
  run("selectStudy('a'); var bodyB = document.createElement('pre'); loadToolDetails({id:'same',at:'now',status:'completed'}, bodyB);");
  await respond('api/tool?id=same&study=a', { id: 'same', at: 'now', status: 'completed', output: 'Alpha tool' });
  assert.match(run('bodyB.textContent'), /Alpha tool/);
  assert.doesNotMatch(run('bodyB.textContent'), /Beta tool/);
  // A removed selection remains explicit rather than falling back to another Study.
  run('refresh();');
  await respond('api/studies', { default: 'b', studies: [catalog.studies[1]] });
  assert.equal(run('ui.study'), 'a');
  assert.equal(document.getElementById('refresh-error').hidden, false);
  assert.match(document.getElementById('refresh-error').textContent, /已不可用/);
  run("selectStudy(null); refresh();");
  await respond('api/studies', { default: null, studies: [] });
  assert.equal(document.getElementById('study-select').disabled, true);
  assert.equal(run('ui.refreshing'), false);
  assert.equal(pending.length, 0);
  console.log('Frontend smoke passed: rendering, stale response isolation, tool cache isolation, removed and empty studies.');
})().catch(error => { console.error(error); process.exitCode = 1; });
