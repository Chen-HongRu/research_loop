// Exercise the shipped HTML and frontend with synthetic HTTP responses, no dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const frontend = path.join(__dirname, '../src/research_loop/dashboard');
class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.listeners = {}; this.attributes = {}; this._text = ''; this.classes = new Set(); this.hidden = false; this.scrollTop = 0;
    this.classList = { toggle: (name, force) => { const on = force ?? !this.classes.has(name); if (on) this.classes.add(name); else this.classes.delete(name); }, contains: name => this.classes.has(name) };
  }
  set className(value) { this.classes = new Set(value.split(' ').filter(Boolean)); }
  get className() { return [...this.classes].join(' '); }
  set textContent(value) { this._text = String(value ?? ''); this.children.forEach(child => { child.parent = null; }); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  append(...nodes) { nodes.forEach(node => { node.parent = this; this.children.push(node); }); }
  prepend(...nodes) { nodes.forEach(node => { node.parent = this; }); this.children.unshift(...nodes); }
  replaceChildren(...nodes) { this.textContent = ''; this.append(...nodes); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  dispatch(name, extra = {}) { (this.listeners[name] || []).forEach(fn => fn({ target: this, ...extra })); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(node => node !== this); this.parent = null; }
  get isConnected() { return !!(this.root || this.parent?.isConnected); }
  querySelector(selector) { return this.descendants().find(el => selector[0] === '.' ? el.classList.contains(selector.slice(1)) : el.tagName.toLowerCase() === selector); }
  descendants() { return this.children.flatMap(child => [child, ...child.descendants()]); }
}
const elements = new Map(), navs = [], viewElements = [], pending = [], requests = [], windowListeners = {};
const html = fs.readFileSync(path.join(frontend, 'index.html'), 'utf8');
for (const match of html.matchAll(/<(\w+)\b([^>]*)>/g)) {
  const attrs = match[2], id = /\bid="([^"]+)"/.exec(attrs)?.[1], view = /data-view="([^"]+)"/.exec(attrs)?.[1];
  const el = new Element(match[1]); el.root = true;
  if (id) { el.id = id; elements.set(id, el); if (id.startsWith('view-')) viewElements.push(el); }
  if (view) { el.dataset.view = view; navs.push(el); }
}
const document = {
  getElementById(id) { assert.ok(elements.has(id), `Unknown shipped HTML ID: ${id}`); return elements.get(id); },
  createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag),
  querySelectorAll: selector => selector === '.view' ? viewElements : selector === '.nav' ? navs : [], addEventListener() {},
};
const location = { search: '', href: 'http://localhost/prefix/' };
const context = vm.createContext({ document, window: { addEventListener: (key, fn) => { windowListeners[key] = fn; }, scrollTo({ top }) { this.scrollY = top; }, scrollY: 0 }, location,
  history: { replaceState(_, __, value) { const url = new URL(value); location.href = url.href; location.search = url.search; } }, URL, URLSearchParams, AbortController,
  setTimeout: (...args) => setTimeout(...args).unref(), clearTimeout, setInterval() {}, HTMLDetailsElement: Element,
  fetch(url) { requests.push(url); return new Promise(resolve => pending.push({ url, resolve })); },
});
const run = code => vm.runInContext(code, context);
const tick = () => new Promise(resolve => setImmediate(resolve));
const el = id => document.getElementById(id);
function matches(request, endpoint, params) { const url = new URL(request.url, location.href); return url.pathname.endsWith(`/api/${endpoint}`) && Object.entries(params).every(([key, value]) => url.searchParams.get(key) === String(value)); }
async function respond(endpoint, params, data, status = 200) {
  const index = pending.findIndex(request => matches(request, endpoint, params));
  assert.ok(index >= 0, `Missing ${endpoint} ${JSON.stringify(params)}; pending ${pending.map(item => item.url)}`);
  pending.splice(index, 1)[0].resolve({ ok: status >= 200 && status < 300, status, json: async () => data }); await tick();
}
const nav = view => navs.find(node => node.dataset.view === view).dispatch('click');
const change = (id, value) => { el(id).value = value; el(id).dispatch('change'); };
const catalog = { default: 'a', studies: [{ id: 'a', name: 'Alpha', status: 'running' }, { id: 'b', name: 'Beta', status: 'stopped' }] };
const overview = name => ({ as_of: '2026-01-01T12:00:00Z', status: { status: 'stopped', iteration: 50 }, research: { summary: { text: `${name} latest report`, iteration: 49, stale: true, source: 'iterations/000049/result.json' }, next_action: { text: 'Recorded recommendation', iteration: 49 } }, publication: { status: 'unpublished' }, stats: { agents: 3, working: 0, completed: 2 } });
const history = (page = 1, status = 'completed') => ({ page, total_pages: 5, page_size: 10, counts: { total: 50, with_result: 49, missing_result: 1 }, iterations: [{ iteration: page === 1 ? 50 : 40, status, result_available: status === 'completed', summary_preview: 'Bounded preview' }, { iteration: page === 1 ? 49 : 39, status: 'completed', result_available: true, summary_preview: 'Previous report' }] });
const iteration = (n, summary) => ({ iteration: n, status: 'completed', result_available: true, result: { summary, limitations: ['Synthetic limit'], evidence_refs: ['public-reference'] }, publication: { status: 'unknown' } });
const agents = (role = 'custom-reader', status = 'working') => ({ page: 1, total_pages: 2, total: 21, agents: [{ id: 'task', title: `${role} task`, role, role_source: 'thread-record', iteration: 50, iteration_source: 'time-range', status, history_available: false, preview: 'Short task preview' }] });
(async () => {
  run(fs.readFileSync(path.join(frontend, 'app.js'), 'utf8'));
  await respond('studies', {}, catalog);
  assert.ok(pending.some(req => matches(req, 'overview', { study: 'a' })));
  assert.ok(!requests.some(url => /api\/(state|history|agents|activity|metrics)/.test(url)), 'Initial overview must fetch no full snapshots or operational lists');
  // A late overview response from a previous Study cannot replace the new Study.
  change('study-select', 'b'); await respond('studies', {}, catalog); await respond('overview', { study: 'b' }, overview('Beta'));
  await respond('overview', { study: 'a' }, overview('Alpha'));
  assert.match(el('research-summary').textContent, /Beta latest report/); assert.doesNotMatch(el('research-summary').textContent, /Alpha|未发布/);
  assert.match(el('research-summary').textContent, /第 49 轮.*历史报告/); assert.match(el('overview-status').textContent, /本轮：未发布知识/);
  assert.equal(el('view-overview').hidden, false); assert.ok(el('view-history').hidden);
  // Repeated polls share a slow identical request; the latest consumer can render it.
  const overviewRequests = requests.filter(url => /api\/overview\?/.test(url)).length;
  run('loadOverview(); loadOverview();');
  assert.equal(requests.filter(url => /api\/overview\?/.test(url)).length, overviewRequests + 1);
  await respond('overview', { study: 'b' }, overview('Beta delayed'));
  assert.match(el('research-summary').textContent, /Beta delayed latest report/);
  // History requests are bounded, selection explicit, and newer detail wins.
  nav('history'); await respond('history', { study: 'b', page: 1 }, history());
  el('history-list').children[0].dispatch('click'); el('history-list').children[1].dispatch('click');
  await respond('iteration', { iteration: 49 }, iteration(49, 'Selected full report'));
  await respond('iteration', { iteration: 50 }, iteration(50, 'Late full report'));
  assert.match(el('iteration-detail').textContent, /Selected full report/); assert.doesNotMatch(el('iteration-detail').textContent, /Late full report/);
  assert.match(location.search, /view=history/); assert.match(location.search, /iteration=49/);
  el('history-pagination').children[2].dispatch('click'); await respond('history', { page: 2 }, history(2));
  await respond('iteration', { iteration: 49 }, iteration(49, 'Selected full report'));
  // The selected detail is retained even outside the active page.
  assert.equal(run('ui.selectedIteration'), '49'); assert.match(el('iteration-detail').textContent, /Selected full report/); assert.match(el('iteration-detail').textContent, /不在当前页/);
  // Active page polling preserves page, selected ID, expansion, and scroll.
  run("ui.details.add('result-iteration-49'); window.scrollY = 135; refresh();"); await respond('studies', {}, catalog);
  await respond('overview', { study: 'b' }, overview('Beta')); await respond('history', { page: 2 }, history(2));
  await respond('iteration', { iteration: 49 }, { ...iteration(49, 'Selected full report'), publication: { status: 'published' } });
  assert.match(el('iteration-detail').textContent, /已发布知识/);
  assert.equal(run('ui.pages.history'), 2); assert.equal(run('window.scrollY'), 135); assert.ok(run("ui.details.has('result-iteration-49')"));
  const unchangedDetail = el('iteration-detail').children[0]; el('iteration-detail').scrollTop = 72;
  run('refresh();'); await respond('studies', {}, catalog); await respond('overview', {}, overview('Beta')); await respond('history', { page: 2 }, history(2));
  await respond('iteration', { iteration: 49 }, { ...iteration(49, 'Selected full report'), publication: { status: 'published' } });
  assert.equal(el('iteration-detail').children[0], unchangedDetail, 'Unchanged detail must retain its DOM'); assert.equal(el('iteration-detail').scrollTop, 72);
  run("renderIteration({...ui.data.history.iterations[0],publication:{status:'historical'}})"); assert.match(el('iteration-detail').textContent, /历史发布记录（非当前）/);

  // A filter response cannot replace a later filter's list.
  nav('agents'); change('role-filter', 'custom-reviewer'); await respond('agents', { role: 'custom-reviewer' }, agents('custom-reviewer'));
  await respond('agents', { role: 'all' }, agents('custom-reader'));
  assert.match(el('agent-list').textContent, /custom-reviewer task/); assert.doesNotMatch(el('agent-list').textContent, /custom-reader task/);
  el('agent-list').children[0].dispatch('click'); await respond('agent', { id: 'task' }, { ...agents('custom-reviewer').agents[0], messages: [], result: 'Initial return' });
  assert.match(el('agent-detail').textContent, /原生会话历史不可用/);
  // Changed task status refreshes the selected detail without replacing selection.
  run('refresh();'); await respond('studies', {}, catalog); await respond('overview', {}, overview('Beta')); await respond('agents', { role: 'custom-reviewer' }, agents('custom-reviewer', 'completed'));
  await respond('agent', { id: 'task' }, { ...agents('custom-reviewer', 'completed').agents[0], messages: [], result: 'Final return' });
  assert.match(el('agent-detail').textContent, /Final return/); assert.equal(run('ui.agent'), 'task');
  // Transient background detail failure preserves readable data and its observation time.
  const detailObservedAt = run("detailObserved.get('agent:task')");
  run("loadDetail('agent', 'task', true)"); await respond('agent', { id: 'task' }, {}, 500);
  assert.match(el('agent-detail').textContent, /Final return/); assert.match(el('agent-detail').textContent, /HTTP 500.*保留上次记录/);
  assert.ok(el('agent-detail').textContent.includes(detailObservedAt));
  // Identical recovered data must clear the stale notice without replacing the result.
  run("loadDetail('agent', 'task', true)"); await respond('agent', { id: 'task' }, { ...agents('custom-reviewer', 'completed').agents[0], messages: [], result: 'Final return' });
  assert.match(el('agent-detail').textContent, /Final return/); assert.equal(el('agent-detail').querySelector('.detail-refresh-error'), undefined);
  run("loadDetail('agent', 'task', true)"); await respond('agent', { id: 'task' }, {}, 404);
  assert.match(el('agent-detail').textContent, /详情不可用.*HTTP 404/); assert.doesNotMatch(el('agent-detail').textContent, /Final return/);
  // Filter changes keep a selected off-page ID explicit; missing detail gives a real error.
  change('role-filter', 'custom-analyst'); await respond('agents', { role: 'custom-analyst' }, { ...agents(), agents: [] }); await respond('agent', { id: 'task' }, {}, 404);
  assert.match(el('agent-detail').textContent, /task.*不可用.*HTTP 404/);
  run('refresh();'); await respond('studies', {}, catalog); await respond('overview', {}, overview('Beta')); await respond('agents', { role: 'custom-analyst' }, { ...agents(), agents: [] }); await respond('agent', { id: 'task' }, { ...agents('custom-reviewer', 'completed').agents[0], messages: [], result: 'Off-page completion' });
  assert.match(el('agent-detail').textContent, /Off-page completion/); assert.match(el('agent-detail').textContent, /不在当前页/);
  // Context is on-demand; ordinary refresh never fetches it twice.
  nav('context'); await respond('state', {}, { goal: 'Synthetic goal', research: {}, curves: {} });
  assert.equal(el('goal').textContent, 'Synthetic goal'); const stateCount = requests.filter(url => /api\/state\?/.test(url)).length;
  run('refresh();'); await respond('studies', {}, catalog); await respond('overview', {}, overview('Beta'));
  assert.equal(requests.filter(url => /api\/state\?/.test(url)).length, stateCount);
  // Metrics preserve missing, legacy, versioned, and independent behavior-only states.
  nav('metrics'); await respond('metrics', {}, { available: true, schema_version: 1, llm_authority: 'legacy-session-usage', coverage: { complete: false }, llm_measurable: false, llm: { input_tokens: 0 } });
  assert.match(el('metrics').textContent, /legacy.*历史口径/); assert.match(el('metrics').textContent, /部分覆盖/); assert.match(el('metrics').textContent, /未测Input/);
  run("renderMetrics({available:true,schema_version:2,llm_authority:'unique-request-receipts',llm_measurable:true,llm:{input_tokens:0}})"); assert.match(el('metrics').textContent, /Schema 2/); assert.match(el('metrics').textContent, /0Input/);
  run('renderMetrics({available:true,llm:{}})'); assert.match(el('metrics').textContent, /未知（未标版本）/);
  run("renderMetrics({available:false,status:'missing',behavior:{available:true,phase:'synthetic',window:{since:'start',until:'end'},coverage:{complete:false},total:{}}})"); assert.match(el('metrics').textContent, /常规指标尚不可用/); assert.match(el('metrics').textContent, /行为描述统计.*独立采集.*阶段：synthetic.*部分覆盖/);
  // Activity expands only the chosen public message or tool and handles HTTP failures.
  nav('activity'); await respond('activity', {}, { page: 1, total_pages: 1, messages: [{ id: 'm', text: 'Message preview' }], tools: [{ id: 'same', title: 'Tool preview', at: 'now', status: 'working' }] });
  const message = el('messages').children[0]; message.open = true; message.dispatch('toggle'); await respond('message', { id: 'm' }, { id: 'm', text: 'Full public message' }); assert.match(message.textContent, /Full public message/);
  const tool = el('tools').children[0]; tool.open = true; tool.dispatch('toggle'); await respond('tool', { id: 'same' }, {}, 404); assert.match(tool.textContent, /HTTP 404/);
  tool.dispatch('toggle'); await respond('tool', { study: 'b', id: 'same' }, { id: 'same', at: 'now', status: 'working', output: 'Beta tool' }); assert.match(tool.textContent, /Beta tool/);
  // Same ID is isolated by Study and by record version, and late detail cannot repopulate it.
  change('study-select', 'a'); await respond('studies', {}, catalog); await respond('overview', { study: 'a' }, overview('Alpha'));
  await respond('activity', { study: 'a' }, { page: 1, total_pages: 1, messages: [], tools: [{ id: 'same', at: 'now', status: 'working' }] });
  const alphaTool = el('tools').children[0]; alphaTool.open = true; alphaTool.dispatch('toggle'); await respond('tool', { study: 'a' }, { id: 'same', at: 'now', status: 'working', output: 'Alpha tool' }); assert.match(alphaTool.textContent, /Alpha tool/); assert.doesNotMatch(alphaTool.textContent, /Beta tool/);
  run('refresh();'); await respond('studies', {}, catalog); await respond('overview', {}, {}, 503); await respond('activity', {}, {}, 503);
  assert.match(el('refresh-error').textContent, /HTTP 503.*观测于 2026-01-01T12:00:00Z/); assert.match(el('tools').textContent, /Alpha tool/);
  run('refresh();'); await respond('studies', {}, { default: 'b', studies: [catalog.studies[1]] }); assert.equal(run('ui.study'), 'a'); assert.match(el('refresh-error').textContent, /所选 Study 已不可用/);
  run('refresh();'); await respond('studies', {}, { default: null, studies: [] }); assert.equal(el('study-select').disabled, true); assert.equal(run('ui.study'), null); assert.equal(el('tools').children.length, 0);
  assert.equal(pending.length, 0, `Unresolved requests: ${pending.map(request => request.url)}`);
  console.log('Frontend checks passed: bounded navigation, pagination, detail races, polling transitions, Study isolation, stale timestamps, HTTP failures, empty collections, metrics authorities.');
})().catch(error => { console.error(error); process.exitCode = 1; });
