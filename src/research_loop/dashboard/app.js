'use strict';
const byId = id => document.getElementById(id);
const views = ['overview', 'history', 'agents', 'activity', 'metrics', 'context'];
const query = new URLSearchParams(location.search);
const positivePage = value => /^\d+$/.test(value || '') && Number(value) > 0 ? Number(value) : 1;
const validIteration = value => /^\d+$/.test(value || '') ? value : null;
const ui = { study: query.get('study'), view: views.includes(query.get('view')) ? query.get('view') : 'overview', epoch: 0, viewGeneration: 0, requests: {}, data: {}, signatures: {}, errors: {}, observed: {}, pages: { history: 1, agents: 1, activity: 1 }, selectedIteration: validIteration(query.get('iteration')), agent: query.get('agent'), role: 'all', period: 'current', details: new Set(), expanded: new Set(), scroll: {}, iteration: null };
ui.pages[ui.view] = positivePage(query.get('page'));
if (/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(query.get('role') || '')) ui.role = query.get('role');
if (['current','all','unknown'].includes(query.get('period')) || /^\d+$/.test(query.get('period') || '')) ui.period = query.get('period');
const toolCache = new Map();
const stateNames = { starting: '准备下一轮', 'iteration-completed': '本轮已归档', initialized: '已初始化', running: '运行中', completed: '已完成', stopped: '已停止', failed: '失败', interrupted: '已中断', working: '工作中', inProgress: '进行中', pending: '等待中', unknown: '未知', 'needs-reconciliation': '需要对账' };
const nameState = value => stateNames[value] || value || '未知';
function friendlyTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const time = new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }).format(date);
  const today = new Date();
  return date.toDateString() === today.toDateString() ? `今天 ${time}` : `${date.getMonth() + 1}月${date.getDate()}日 ${time}`;
}
function structured(value) {
  if (typeof value === 'string') {
    try { value = JSON.parse(value); } catch { return null; }
  }
  return value && typeof value === 'object' && !Array.isArray(value) && ['summary', 'goal', 'limitations', 'next_action'].some(key => key in value) ? value : null;
}
function structuredContent(node, value, key) {
  const result = structured(value);
  if (!result) { renderMarkdown(node, value); return; }
  const labels = { summary: '总结', goal: '目标', limitations: '局限', next_action: '下一步', study_action: '研究安排', stop_reason: '停止原因' };
  for (const [field, label] of Object.entries(labels)) {
    if (result[field] == null || result[field] === '') continue;
    node.append(text('strong', label));
    const content = document.createElement('div'); content.className = 'markdown structured-field';
    if (Array.isArray(result[field])) {
      const list = document.createElement('ul');
      result[field].forEach(item => { const li = document.createElement('li'); renderMarkdown(li, item); list.append(li); });
      content.append(list);
    } else if (field === 'goal') {
      content.textContent = ({ achieved: '问题已回答', 'not-achieved': '尚未解决', unknown: '尚不能判断' })[result[field]] || printable(result[field]);
    } else if (field === 'study_action') { content.textContent = ({continue:'继续下一轮',stop:'结束 Study'})[result[field]] || result[field]; } else renderMarkdown(content, result[field]);
    node.append(content);
  }
  const rest = Object.fromEntries(Object.entries(result).filter(([field]) => !(field in labels)));
  if (Object.keys(rest).length) {
    const title = text('summary', '证据引用与其他字段');
    const raw = text('pre', printable(rest), 'raw');
    node.append(details(`result-${key}`, title, raw));
  }
}
const printable = value => typeof value === 'string' ? value : JSON.stringify(value, null, 2);
const text = (tag, value, className) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = value ?? '';
  return node;
};

let markdown;
if (window.markdownit) {
  markdown = window.markdownit({ html: false, linkify: true, breaks: false });
  markdown.renderer.rules.image = (tokens, index) => markdown.utils.escapeHtml(tokens[index].content || '[图片]');
  const linkStack = [];
  markdown.renderer.rules.link_open = (tokens, index) => {
    const href = tokens[index].attrGet('href') || '';
    const external = /^https?:\/\//i.test(href);
    linkStack.push(external);
    if (!external) return `<span class="local-ref" title="${markdown.utils.escapeHtml(href)}">`;
    return `<a href="${markdown.utils.escapeHtml(href)}" target="_blank" rel="noopener noreferrer">`;
  };
  markdown.renderer.rules.link_close = () => linkStack.pop() ? '</a>' : '</span>';
}
function renderMarkdown(node, value) {
  const content = printable(value ?? '');
  if (markdown) node.innerHTML = markdown.render(content);
  else node.textContent = content;
}

function renderCharts(curves) {
  const root = byId('charts'); root.replaceChildren();
  (Array.isArray(curves?.series) ? curves.series : []).forEach(series => {
    const rows = (series.rows || []).filter(row => Array.isArray(row) && row.length === 2 && row.every(Number.isFinite));
    const box = document.createElement('div'); box.className = 'chart'; box.append(text('strong', `${series.branch} · ${series.metric} (${series.unit})`));
    if (rows.length) {
      const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg'); svg.setAttribute('viewBox', '0 0 480 220');
      const xs = rows.map(r => r[0]), ys = rows.map(r => r[1]);
      const xmin = Math.min(...xs), xmax = Math.max(...xs), min = Math.min(...ys), max = Math.max(...ys), pad = Math.max((max - min) * .18, .02);
      const x = n => 55 + (n - xmin) / (xmax - xmin || 1) * 405, y = n => 180 - (n - min + pad) / (max - min + 2 * pad) * 145;
      function svgEl(tag, attrs, label) { const el = document.createElementNS(ns, tag); Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v)); if (label != null) el.textContent = label; svg.append(el); return el; }
      svgEl('path', { d: 'M55 25 V180 H460', fill: 'none', stroke: '#aebbb1' });
      svgEl('polyline', { points: rows.map(r => `${x(r[0])},${y(r[1])}`).join(' '), fill: 'none', stroke: '#658d74', 'stroke-width': 2 });
      rows.forEach((r, i) => {
        const circle = svgEl('circle', { cx: x(r[0]), cy: y(r[1]), r: 4, fill: '#658d74' });
        const tip = document.createElementNS(ns, 'title'); tip.textContent = `${series.x_label || "x"} ${r[0]} · ${series.metric}: ${r[1]} ${series.unit}`; circle.append(tip);
        if (rows.length <= 8 || i === 0 || i === rows.length - 1 || i === ys.indexOf(min)) svgEl('text', { x: x(r[0]), y: y(r[1]) - 9, 'text-anchor': 'middle', 'font-size': 10, fill: '#66746a' }, r[1].toFixed(3));
      });
      svgEl('text', { x: 55, y: 200, 'font-size': 11, fill: '#808a82' }, xmin); svgEl('text', { x: 460, y: 200, 'text-anchor': 'end', 'font-size': 11, fill: '#808a82' }, xmax);
      svgEl('text', { x: 255, y: 217, 'text-anchor': 'middle', 'font-size': 11, fill: '#808a82' }, series.x_label || 'x'); box.append(svg);
    }
    root.append(box);
  });
  byId('provenance').textContent = curves?.provenance || '历史来源不可用';
}
function apiURL(endpoint, study = ui.study, params = {}) {
  const q = new URLSearchParams(params); if (study) q.set('study', study);
  return `api/${endpoint}?${q}`;
}
const inFlight = new Map();
async function get(endpoint, study, params) {
  const url = endpoint === 'studies' ? 'api/studies' : apiURL(endpoint, study, params);
  const key = JSON.stringify([ui.epoch, url]);
  if (inFlight.has(key)) return inFlight.get(key);
  const request = (async () => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 25000);
    try {
      const response = await fetch(url, { cache: 'no-store', signal: controller.signal });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json();
    } finally { clearTimeout(timeout); inFlight.delete(key); }
  })();
  inFlight.set(key, request);
  return request;
}
function ticket(key) {
  const generation = (ui.requests[key] || 0) + 1; ui.requests[key] = generation;
  const epoch = ui.epoch, view = ui.viewGeneration;
  return () => epoch === ui.epoch && generation === ui.requests[key] && (key === 'overview' || key === 'catalog' || view === ui.viewGeneration);
}
function syncURL() {
  const url = new URL(location.href);
  ['study','view','page','iteration','agent','role','period'].forEach(key => url.searchParams.delete(key));
  if (ui.study) url.searchParams.set('study', ui.study);
  url.searchParams.set('view', ui.view);
  if (ui.pages[ui.view] > 1) url.searchParams.set('page', ui.pages[ui.view]);
  if (ui.view === 'history' && ui.selectedIteration != null) url.searchParams.set('iteration', ui.selectedIteration);
  if (ui.view === 'agents') {
    if (ui.agent) url.searchParams.set('agent', ui.agent);
    if (ui.role !== 'all') url.searchParams.set('role', ui.role);
    if (ui.period !== 'current') url.searchParams.set('period', ui.period);
  }
  history.replaceState(null, '', url);
}
function notice() {
  const failures = Object.entries(ui.errors).filter(([key]) => key === 'catalog' || key === 'overview' || key === ui.view);
  const root = byId('refresh-error'); root.hidden = !failures.length;
  root.textContent = failures.map(([key, error]) => `${key === 'catalog' ? 'Study 目录' : key === 'overview' ? '研究概览' : '当前页面'}读取失败：${error}。${ui.observed[key] ? `保留上次记录，观测于 ${ui.observed[key]}。` : '尚无可用记录。'}`).join(' ');
}
function showViews() {
  document.querySelectorAll('.view').forEach(el => { el.hidden = el.id !== `view-${ui.view}`; });
  document.querySelectorAll('.nav').forEach(el => { el.classList.toggle('active', el.dataset.view === ui.view); el.setAttribute('aria-current', el.dataset.view === ui.view ? 'page' : 'false'); });
  notice();
}
function setView(view) {
  if (!views.includes(view)) return;
  ui.scroll[ui.view] = window.scrollY; ui.view = view; ui.viewGeneration++; syncURL(); showViews();
  window.scrollTo({ top: ui.scroll[view] || 0, behavior: 'instant' });
  if (ui.study) loadPage(true);
}
function details(key, title, body) {
  const el = document.createElement('details'); el.className = 'panel'; el.dataset.key = key; el.open = ui.details.has(key); el.append(title, body); return el;
}
document.addEventListener('toggle', event => {
  const el = event.target; if (!(el instanceof HTMLDetailsElement) || !el.dataset.key) return;
  if (el.open) ui.details.add(el.dataset.key); else ui.details.delete(el.dataset.key);
}, true);
function bounded(root, value, key) {
  const body = text('div', '', 'markdown bounded'); renderMarkdown(body, value || '暂无可用记录。');
  body.classList.toggle('collapsed', !ui.expanded.has(key)); root.append(body);
  {
    const button = text('button', ui.expanded.has(key) ? '收起' : '展开完整内容', 'inline-link expand');
    button.addEventListener('click', () => { if (ui.expanded.has(key)) ui.expanded.delete(key); else ui.expanded.add(key); body.classList.toggle('collapsed', !ui.expanded.has(key)); button.textContent = ui.expanded.has(key) ? '收起' : '展开完整内容'; }); root.append(button);
  }
}
function provenance(item) {
  return [item?.iteration != null ? `第 ${item.iteration} 轮${item.stale ? ' · 历史报告' : ''}` : '', item?.source ? `来源：${item.source}` : '', item?.at ? friendlyTime(item.at) : ''].filter(Boolean).join(' · ');
}
function publicationLabel(value) {
  return ({ published: '当前已发布知识', historical: '历史发布记录（非当前）', unpublished: '未发布知识', invalid: '发布证据无效', unknown: '发布状态未知' })[value?.status] || '发布状态未知';
}
function budgetLabel(budget) {
  return budget?.state === 'recorded' && Number.isFinite(budget.remaining_seconds) && Number.isFinite(budget.authorized_seconds) ? `剩余 ${(budget.remaining_seconds / 3600).toFixed(2)} / 授权 ${(budget.authorized_seconds / 3600).toFixed(2)} GPU 小时` : '预算状态未确认';
}
function renderOverview(data) {
  const status = data.status || {}; ui.iteration = status.iteration ?? null;
  byId('state').textContent = status.status === 'completed' ? 'Study 已结束' : nameState(status.status);
  byId('iteration').textContent = status.iteration != null ? `第 ${status.iteration} 轮` : '轮次未知';
  byId('last-refresh').textContent = `最近同步 ${friendlyTime(data.as_of) || '时间未知'}`;
  const card = byId('overview-status'); card.replaceChildren(text('span', nameState(status.status), 'badge'), text('span', data.activity?.label || '暂无当前活动记录'), text('small', `最后事件 ${friendlyTime(data.last_event_at) || '尚无'}`, 'muted'), text('span', `本轮：${publicationLabel(data.publication)}`, 'badge neutral'));
  const error = byId('overview-error'); error.hidden = !status.error; error.textContent = status.error ? printable(status.error) : '';
  for (const [id, item, fallback] of [['research-summary', data.research?.summary, '暂无已报告的研究总结。'], ['next-action', data.research?.next_action, '暂无已记录的下一步。']]) {
    const root = byId(id); root.replaceChildren(); bounded(root, item?.text || fallback, id);
    root.append(text('p', provenance(item), 'muted provenance'));
  }
  byId('summary-link').disabled = data.research?.summary?.iteration == null;
  const stats = byId('task-stats'); stats.replaceChildren();
  for (const [key, label] of [['agents','全部'],['working','工作中'],['completed','已完成']]) { const box = text('div', '', 'stat'); box.append(text('strong', data.stats?.[key] ?? '—'), text('span', label)); stats.append(box); }
  byId('budget').textContent = budgetLabel(data.budget); byId('metrics-budget').textContent = budgetLabel(data.budget);
  updatePeriodOptions();
}
function updatePeriodOptions() {
  const select = byId('period-filter'); select.replaceChildren();
  for (const [value,label] of [['current','当前轮'],['all','全部轮次'],['unknown','轮次未知']]) { const opt = text('option', label); opt.value = value; select.append(opt); }
  const latest = Number.isInteger(ui.iteration) ? ui.iteration : 0;
  const iterations = new Set([...Array.from({ length: Math.min(latest, 100) }, (_, index) => String(latest - index)), /^\d+$/.test(ui.period) ? ui.period : null].filter(Boolean));
  for (const value of iterations) { const opt = text('option', `第 ${value} 轮`); opt.value = value; select.append(opt); }
  select.value = ui.period;
}
function pager(rootId, data, view) {
  const root = byId(rootId); root.replaceChildren();
  const page = data.page || 1, total = data.total_pages || 1;
  const prev = text('button', '← 上一页', 'page-button'), next = text('button', '下一页 →', 'page-button');
  prev.disabled = page <= 1; next.disabled = page >= total;
  prev.addEventListener('click', () => changePage(view, page - 1)); next.addEventListener('click', () => changePage(view, page + 1));
  root.append(prev, text('span', `第 ${page} / ${total} 页`, 'muted'), next);
}
function changePage(view, page) { ui.pages[view] = page; ui.viewGeneration++; syncURL(); loadPage(); }
function listButton(title, meta, preview, selected, onClick) {
  const button = text('button', '', `record${selected ? ' selected' : ''}`); button.setAttribute('aria-pressed', String(selected));
  button.append(text('strong', title), text('span', meta, 'record-meta'), text('span', preview || '', 'record-preview')); button.addEventListener('click', onClick); return button;
}
function renderHistory(data) {
  const rows = data.iterations || [], counts = data.counts || {}, root = byId('history-list'); root.replaceChildren();
  byId('history-count').textContent = `${counts.total ?? 0} 轮记录 · ${counts.with_result ?? 0} 轮有结果 · ${counts.missing_result ?? 0} 轮未报告结果`;
  pager('history-pagination', data, 'history');
  if (!rows.length) root.append(text('p', '此页暂无轮次记录。', 'empty'));
  rows.forEach(row => { const id = String(row.iteration); root.append(listButton(`第 ${id} 轮`, `${nameState(row.status)} · ${row.result_available ? '有结果' : '未报告结果'}`, row.summary_preview, id === ui.selectedIteration, () => selectIteration(id))); });
  selectionNote('iteration-detail', ui.selectedIteration, rows.some(row => (String(row.iteration)) === ui.selectedIteration), '轮次');
}
function selectionNote(rootId, selected, onPage, label) {
  const root = byId(rootId);
  const old = root.querySelector?.('.selection-note'); if (old) old.remove();
  if (selected == null) { root.replaceChildren(text('p', `选择一个${label}查看详情。`, 'muted')); return; }
  if (!onPage) root.prepend(text('p', `所选${label} ${selected} 不在当前页或筛选中；保留该选择。`, 'selection-note muted'));
}
function selectIteration(id) { ui.selectedIteration = id; syncURL(); if (ui.data.history) renderHistory(ui.data.history); loadDetail('iteration', id); }
function renderIteration(row) {
  const root = byId('iteration-detail'); root.replaceChildren(text('h2', row.iteration == null ? '早期研究记录' : `第 ${row.iteration} 轮`), text('p', `${nameState(row.status)} · ${publicationLabel(row.publication)}`, 'muted'));
  if (row.iteration != null) { const tasks = text('button', '查看本轮任务 →', 'inline-link'); tasks.addEventListener('click', () => { ui.period = String(row.iteration); ui.pages.agents = 1; ui.agent = null; updatePeriodOptions(); setView('agents'); }); root.append(tasks); }
  if (!row.result_available) root.append(text('p', '本轮尚无结果记录。执行状态不代表科学目标已达成。', 'empty'));
  else { const content = text('div', '', 'markdown'); structuredContent(content, row.result || {}, `iteration-${row.iteration}`); root.append(content); }
  root.append(text('p', `来源：${row.source || '无可用记录'}`, 'muted provenance'));
  selectionNote('iteration-detail', ui.selectedIteration, (ui.data.history?.iterations || []).some(item => (String(item.iteration)) === ui.selectedIteration), '轮次');
}
function roleName(agent) { return `${agent.role || 'unclassified'} · ${agent.role_source === 'thread-record' ? '已记录' : '身份未确认'}${agent.group_run_id ? ` · Group ${agent.group_run_id}` : ''}`; }
function roleOptions(values = []) {
  const root = byId('role-filter'); root.replaceChildren();
  ['all', ...new Set([...values, ...(ui.role === 'all' ? [] : [ui.role])])].forEach(value => { const option = text('option', value === 'all' ? '全部 Agent' : value); option.value = value; root.append(option); });
  root.value = ui.role;
}
function association(agent) { return agent.iteration == null ? '轮次未知' : `第 ${agent.iteration} 轮${agent.iteration_source && agent.iteration_source !== 'host-role-record' ? ` · ${agent.iteration_source.includes('time') ? '按时间关联' : agent.iteration_source}` : ''}`; }
function renderAgents(data) {
  roleOptions(data.roles || (data.agents || []).map(agent => agent.role));
  const rows = data.agents || [], root = byId('agent-list'); root.replaceChildren(); pager('agents-pagination', data, 'agents');
  if (!rows.length) root.append(text('p', '此筛选下暂无任务记录。', 'empty'));
  rows.forEach(agent => root.append(listButton(agent.title || agent.name || agent.task_name || '任务', `${roleName(agent)} · ${association(agent)} · ${nameState(agent.status)}${agent.history_available === false ? ' · 原生历史不可用' : ''}`, agent.preview, String(agent.id) === ui.agent, () => selectAgent(String(agent.id)))));
  selectionNote('agent-detail', ui.agent, rows.some(row => String(row.id) === ui.agent), '任务');
}
function selectAgent(id) { ui.agent = id; syncURL(); if (ui.data.agents) renderAgents(ui.data.agents); loadDetail('agent', id); }
function renderAgent(agent) {
  const root = byId('agent-detail'); root.replaceChildren(text('h2', agent.title || agent.name || '任务'), text('p', `${roleName(agent)} · ${association(agent)} · ${nameState(agent.status)}`, 'muted'));
  if (agent.purpose) { const body = text('div', '', 'markdown'); renderMarkdown(body, agent.purpose); root.append(details(`purpose-${agent.id}`, text('summary', '任务说明'), body)); }
  if (agent.history_available === false) root.append(text('p', '原生会话历史不可用；保留委派记录。Fresh context 状态未知。', 'notice'));
  if (agent.result) { root.append(text('h3', '返回值')); const body = text('div', '', 'markdown'); structuredContent(body, agent.result, `agent-${agent.id}`); root.append(body); }
  const messages = text('div', '', 'conversation'); renderMessages(messages, agent.messages || [], agent.name || '任务'); root.append(details(`messages-${agent.id}`, text('summary', `公开消息（${(agent.messages || []).length}）`), messages));
  root.append(text('p', `线程 ID：${agent.id} · Fresh context：${agent.fresh_context || 'unknown'}`, 'muted provenance'));
  selectionNote('agent-detail', ui.agent, (ui.data.agents?.agents || []).some(row => String(row.id) === ui.agent), '任务');
}
async function loadDetail(kind, id, background = false) {
  const current = ticket(`${kind}-detail`), study = ui.study, root = byId(kind === 'agent' ? 'agent-detail' : 'iteration-detail');
  const key = `${kind}:${id}`;
  if (!background) { root.dataset.detailKey = ''; root.replaceChildren(text('p', `正在读取${kind === 'agent' ? '任务' : '轮次'} ${id}…`, 'muted')); }
  try {
    const data = await get(kind, study, kind === 'agent' ? { id } : { iteration: id });
    if (!current() || (kind === 'agent' ? ui.agent : ui.selectedIteration) !== id) return;
    const signature = JSON.stringify(data);
    root.querySelector('.detail-refresh-error')?.remove();
    if (!background || root.dataset.detailKey !== key || detailSignatures.get(key) !== signature) {
      const scroll = background ? root.scrollTop : 0;
      if (kind === 'agent') renderAgent(data); else renderIteration(data);
      root.scrollTop = scroll; detailSignatures.set(key, signature);
    }
    root.dataset.detailKey = key; detailObserved.set(key, new Date().toISOString());
  } catch (error) {
    if (!current() || (kind === 'agent' ? ui.agent : ui.selectedIteration) !== id) return;
    if (background && root.dataset.detailKey === key && detailObserved.has(key) && !/^HTTP 4\d\d$/.test(error.message)) {
      root.querySelector('.detail-refresh-error')?.remove();
      const stale = text('div', '', 'notice detail-refresh-error');
      stale.append(text('p', `详情刷新失败（${error.message}）。保留上次记录，成功观测于 ${detailObserved.get(key)}。`), retryButton(() => loadDetail(kind, id, true)));
      root.prepend(stale); return;
    }
    detailSignatures.delete(key); detailObserved.delete(key); root.dataset.detailKey = '';
    root.replaceChildren(text('p', `所选记录 ${id} 详情不可用（${error.message}）。`, 'notice'), retryButton(() => loadDetail(kind, id)));
  }
}
function retryButton(action) { const button = text('button', '重新读取', 'page-button'); button.addEventListener('click', action); return button; }
function message(entry, label, index) {
  const root = text('article', '', 'message'), body = text('div', '', 'markdown'); structuredContent(body, entry.text || '', `message-${entry.id ?? index}`);
  root.append(text('p', `${label} · ${friendlyTime(entry.at) || '时间未知'}`, 'muted'), body); return root;
}
function renderMessages(root, rows, label) { root.replaceChildren(); if (!rows.length) root.append(text('p', '暂无公开消息。', 'muted')); rows.forEach((row, i) => root.append(message(row, label, i))); }
function renderActivity(data) {
  pager('activity-pagination', data, 'activity'); byId('activity-count').textContent = `${data.total_messages ?? 0} 条协调消息 · ${data.total_tools ?? 0} 条工具记录（保留上限 ${data.retention ?? 250}）`;
  for (const [key, rows] of [['messages', data.messages || []], ['tools', data.tools || []]]) {
    const root = byId(key); root.replaceChildren(); if (!rows.length) root.append(text('p', '此页暂无记录。', 'empty'));
    rows.forEach(row => {
      const title = text('summary', '', 'activity-title'); title.append(text('span', key === 'tools' ? `${row.title || '工具调用'} · ${nameState(row.status)}` : row.text || '协调消息'), text('time', friendlyTime(row.at)));
      const body = text(key === 'tools' ? 'pre' : 'div', '', key === 'tools' ? 'raw' : 'markdown doc-content'), el = details(`${key}-${row.id}`, title, body);
      const load = () => key === 'tools' ? loadToolDetails(row, body) : loadMessage(row, body);
      el.addEventListener('toggle', () => { if (el.open) load(); }); root.append(el); if (el.open) load();
    });
  }
}
async function loadMessage(row, body) {
  const current = ticket(`message-${row.id}`), study = ui.study;
  body.textContent = '正在读取完整消息…';
  try { const data = await get('message', study, { id: row.id }); if (current() && body.isConnected) { body.replaceChildren(); structuredContent(body, data.text || '', row.id); } }
  catch (error) { if (current() && body.isConnected) body.textContent = `消息详情不可用（${error.message}）。收起后展开可重试。`; }
}
async function loadToolDetails(tool, body) {
  const study = ui.study, version = JSON.stringify([study, tool.id, tool.at, tool.status]);
  if (body.dataset.loading === version) return;
  const current = ticket(`tool-${tool.id}`); body.dataset.version = version;
  if (toolCache.has(version)) { body.textContent = toolCache.get(version); return; }
  body.dataset.loading = version; body.textContent = '正在读取工具详情…';
  try {
    const full = await get('tool', study, { id: tool.id }); if (!current()) return;
    if (String(full.id) !== String(tool.id) || full.at !== tool.at || full.status !== tool.status) throw new Error('记录已变化');
    const content = `线程：${full.thread_id || '未知'}\n\n输入：\n${printable(full.input ?? {})}\n\n输出：\n${printable(full.output ?? {})}`;
    toolCache.set(version, content); if (body.isConnected && body.dataset.version === version) body.textContent = content;
  } catch (error) { if (current() && body.isConnected && body.dataset.version === version) body.textContent = `工具详情不可用（${error.message}）。收起后展开可重试。`; }
  finally { if (body.dataset.loading === version) delete body.dataset.loading; }
}
function renderMetrics(data) {
  const root = byId('metrics'); root.replaceChildren();
  if (!data.available) { root.append(text('p', data.status === 'missing' ? '常规指标尚不可用。' : '常规指标文件无效或无法读取。', 'empty')); renderBehavior(root, data.behavior); return; }
  const show = value => value == null ? '未测' : String(value);
  const line = (label, value, parent = root) => parent.append(text('p', `${label}：${show(value)}`));
  line('计量版本', data.schema_version === 2 ? 'Schema 2' : data.schema_version === 1 ? 'Schema 1 · legacy（历史口径）' : `未知${data.schema_version != null ? `（${data.schema_version}）` : '（未标版本）'}`);
  line('Token 计量依据', data.llm_authority || '未知；不能默认与其他口径比较');
  line('采样范围', `${show(data.window?.since)} → ${show(data.window?.until)}`); line('最近源事件', data.last_source_event_at);
  line('采集覆盖', data.coverage?.complete === true ? '已记录范围完整（不证明全 Study 完整）' : data.coverage?.complete === false ? '部分覆盖' : '未知');
  line('Token 可计量性', data.llm_measurable === true ? '可计量' : data.llm_measurable === false ? '不完整或不可计量；仅显示已观测非零值' : '未知');
  const tokens = text('div', '', 'metric-grid'); root.append(tokens);
  for (const [key,label] of [['input_tokens','Input'],['cached_input_tokens','Cache'],['uncached_input_tokens','Uncached'],['output_tokens','Output']]) {
    const box = text('div', '', 'stat'); box.append(text('strong', show(data.llm_measurable !== true && data.llm?.[key] === 0 ? null : data.llm?.[key])), text('span', label)); tokens.append(box);
  }
  line('任务（已完成非混合 / 待完成 / 混合）', ['completed','pending','mixed'].map(key => show(data.tasks?.[key])).join(' / '));
  line('资源调用 / 失败 / 空轮询', ['calls','failed_calls','successful_empty_running_gpu_polls'].map(key => show(data.resources?.[key])).join(' / '));
  line('Allocation 秒数', data.allocation?.available === true ? data.allocation?.elapsed_seconds : null);
  const diagnostics = text('div', '', 'doc-content'); line('采集阶段', data.phase, diagnostics); line('覆盖元数据', printable(data.coverage || {}), diagnostics); line('采集器耗时（秒） / 读取字节', `${show(data.reader?.previous_poll_total_seconds ?? data.reader?.elapsed_seconds)} / ${show(data.reader?.bytes_read)}`, diagnostics); line('等待超时', data.wait_timeouts, diagnostics);
  root.append(details('metrics-diagnostics', text('summary', '采集器诊断'), diagnostics));
  renderBehavior(root, data.behavior);
  if (data.comparison) { const body = text('div', '', 'doc-content'); body.append(text('p', '描述性对照；不同计量版本、任务和覆盖范围不能直接比较。', 'muted')); for (const key of ['baseline','optimized']) line(key, printable(data.comparison[key] || {}), body); root.append(details('metrics-comparison', text('summary', '已有描述性对照'), body)); }
  else if (data.comparison_status === 'invalid') line('描述性对照', '文件无效');
}
function renderBehavior(root, behavior) {
  if (!behavior?.available) { root.append(text('p', `行为描述统计：${behavior?.status || '不可用'}`, 'muted')); return; }
  const body = text('div', '', 'doc-content');
  body.append(text('p', `阶段：${behavior.phase ?? '未知'} · Schema：${behavior.schema_version ?? '未知'}`), text('p', `采样：${behavior.window?.since ?? '未测'} → ${behavior.window?.until ?? '未测'}`), text('p', `覆盖：${behavior.coverage?.complete === true ? '已记录范围完整' : behavior.coverage?.complete === false ? '部分覆盖' : '未知'}`), text('pre', printable(behavior), 'raw'));
  root.append(details('metrics-behavior', text('summary', '行为描述统计（独立采集）'), body));
}
function renderContext(data) {
  renderMarkdown(byId('goal'), data.goal || '暂无目标记录。'); renderCharts(data.curves);
  const plan = byId('plan'); plan.replaceChildren(); if (Array.isArray(data.plan) && data.plan.length) data.plan.forEach(row => plan.append(text('p', `${row.step} · ${nameState(row.status)}`))); else plan.append(text('p', '暂无已记录的计划。', 'muted'));
  const docs = byId('research-docs'); docs.replaceChildren();
  for (const [key,label] of [['summary','研究总结文档'],['next_action','已记录的建议'],['coordination','实验安排'],['frontier','候选方向']]) {
    const item = data.research?.[key]; const body = text('div', '', 'doc-content'); body.append(text('p', provenance(item), 'muted')); const content = text('div', '', 'markdown'); renderMarkdown(content, item?.text || '暂无可用文档。'); body.append(content); docs.append(details(`context-${key}`, text('summary', label), body));
  }
  if (data.research?.policy) docs.append(details('context-policy', text('summary', '安排依据'), text('pre', printable(data.research.policy), 'raw')));
}
const detailSignatures = new Map();
const detailObserved = new Map();
const pageEndpoints = { history: 'history', agents: 'agents', activity: 'activity', metrics: 'metrics', context: 'state' };
async function loadPage(navigation = false) {
  const view = ui.view; if (!ui.study || view === 'overview' || (view === 'context' && !navigation)) return;
  const current = ticket('page'), study = ui.study;
  const params = { ...(ui.pages[view] ? { page: ui.pages[view] } : {}) };
  if (view === 'agents') { params.iteration = ui.period; params.role = ui.role; }
  try {
    const data = await get(pageEndpoints[view], study, params); if (!current()) return;
    const signature = JSON.stringify(data); ui.data[view] = data;
    if (signature !== ui.signatures[view] || navigation) {
      const scroll = window.scrollY;
      ({ history: renderHistory, agents: renderAgents, activity: renderActivity, metrics: renderMetrics, context: renderContext })[view](data);
      ui.signatures[view] = signature; window.scrollTo({ top: scroll, behavior: 'instant' });
    }
    ui.observed[view] = data.as_of || new Date().toISOString(); delete ui.errors[view]; notice();
    if (view === 'history' && ui.selectedIteration != null) loadDetail('iteration', ui.selectedIteration, !navigation);
    if (view === 'agents' && ui.agent != null) loadDetail('agent', ui.agent, !navigation);
  } catch (error) { if (current()) { ui.errors[view] = error.message; notice(); } }
}
async function loadOverview() {
  const current = ticket('overview'), study = ui.study;
  try {
    const data = await get('overview', study); if (!current()) return;
    ui.data.overview = data; ui.observed.overview = data.as_of || new Date().toISOString(); delete ui.errors.overview; renderOverview(data); notice();
  } catch (error) { if (current()) { ui.errors.overview = error.message; notice(); } }
}
function selectStudy(study, keepRoute = false) {
  ui.study = study; ui.epoch++; ui.viewGeneration++; ui.requests = {}; ui.data = {}; ui.signatures = {}; ui.errors = {}; ui.observed = {}; ui.scroll = {}; ui.iteration = null;
  if (!keepRoute) { ui.pages = { history: 1, agents: 1, activity: 1 }; ui.selectedIteration = null; ui.agent = null; ui.role = 'all'; ui.period = 'current'; }
  ui.details.clear(); ui.expanded.clear(); toolCache.clear(); detailSignatures.clear(); detailObserved.clear();
  for (const id of ['overview-status','overview-error','research-summary','next-action','task-stats','budget','history-count','history-list','history-pagination','iteration-detail','agent-list','agent-detail','agents-pagination','messages','tools','activity-count','activity-pagination','metrics','metrics-budget','goal','plan','charts','provenance','research-docs']) byId(id).replaceChildren();
  byId('state').textContent = study ? '加载中' : '暂无 Study'; byId('iteration').textContent = ''; byId('last-refresh').textContent = '尚未同步'; roleOptions(); updatePeriodOptions(); syncURL(); showViews(); window.scrollTo({ top: 0, behavior: 'instant' });
}
async function refresh() {
  const current = ticket('catalog');
  try {
    const catalog = await get('studies'); if (!current()) return;
    delete ui.errors.catalog;
    const selector = byId('study-select'); selector.replaceChildren();
    for (const study of catalog.studies || []) { const option = text('option', `${study.name} · ${nameState(study.status)}`); option.value = study.id; selector.append(option); }
    if (!catalog.studies?.length) {
      selectStudy(null); selector.append(text('option', '暂无可用 Study')); selector.disabled = true; ui.errors.catalog = '目录中暂无 Study；下次同步会重新检查'; notice(); return;
    }
    selector.disabled = false;
    let initial = false;
    if (!ui.study) { selectStudy(catalog.default || catalog.studies[0].id, true); initial = true; }
    if (!catalog.studies.some(study => study.id === ui.study)) {
      const option = text('option', '所选 Study 已不可用，请切换'); option.value = ui.study; selector.append(option); selector.value = ui.study; ui.errors.catalog = '所选 Study 已不可用'; notice(); return;
    }
    selector.value = ui.study; notice(); await Promise.all([loadOverview(), loadPage(initial || !ui.data[ui.view])]);
  } catch (error) { if (current()) { ui.errors.catalog = error.message; notice(); } }
}
document.querySelectorAll('.nav').forEach(button => button.addEventListener('click', () => setView(button.dataset.view)));
byId('study-select').addEventListener('change', event => { selectStudy(event.target.value); refresh(); });
roleOptions(); updatePeriodOptions();
for (const [id,key] of [['role-filter','role'],['period-filter','period']]) byId(id).addEventListener('change', event => { ui[key] = event.target.value; ui.pages.agents = 1; ui.viewGeneration++; syncURL(); loadPage(true); });
byId('summary-link').addEventListener('click', () => { const iteration = ui.data.overview?.research?.summary?.iteration; if (iteration == null) return; ui.selectedIteration = String(iteration); setView('history'); });
byId('tasks-link').addEventListener('click', () => setView('agents'));
window.addEventListener('popstate', () => {
  const params = new URLSearchParams(location.search); selectStudy(params.get('study')); ui.view = views.includes(params.get('view')) ? params.get('view') : 'overview'; ui.pages[ui.view] = positivePage(params.get('page')); ui.selectedIteration = validIteration(params.get('iteration')); ui.agent = params.get('agent');
  ui.role = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(params.get('role') || '') ? params.get('role') : 'all'; ui.period = ['current','all','unknown'].includes(params.get('period')) || /^\d+$/.test(params.get('period') || '') ? params.get('period') : 'current'; roleOptions(); updatePeriodOptions(); syncURL(); showViews(); refresh();
});
showViews(); refresh(); setInterval(refresh, 5000);
