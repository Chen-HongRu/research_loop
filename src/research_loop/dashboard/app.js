'use strict';

const byId = id => document.getElementById(id);
const ui = { view: 'conversation', agent: null, expanded: new Set(), details: new Set(), last: '', role: 'all', period: 'all', agents: [], iteration: null };
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
function setView(view) {
  ui.view = view;
  document.querySelectorAll('.view').forEach(el => { el.hidden = el.id !== `view-${view}`; });
  document.querySelectorAll('.nav').forEach(el => el.classList.toggle('active', el.dataset.view === view));
}
document.querySelectorAll('.nav').forEach(button => button.addEventListener('click', () => setView(button.dataset.view)));
byId('agent-link').addEventListener('click', () => { ui.role = 'all'; byId('role-filter').value = 'all'; renderAgents(ui.agents); setView('agents'); });
byId('role-filter').addEventListener('change', event => { ui.role = event.target.value; renderAgents(ui.agents); });
byId('period-filter').addEventListener('change', event => { ui.period = event.target.value; renderAgents(ui.agents); });
const roles = { Scout: '探索', Proposer: '方案', Experimenter: '实验', Analyst: '分析', Critic: '批判' };
const roleDuties = { Scout: '探索替代方向', Proposer: '提出研究方案', Experimenter: '执行实验', Analyst: '分析研究结果', Critic: '审视问题与局限' };
function agentRole(agent) { return Object.hasOwn(roles, agent.role) ? agent.role : 'unknown'; }
function agentPeriod(agent) {
  if (agent.iteration == null || ui.iteration == null) return 'unknown';
  return String(agent.iteration) === String(ui.iteration) ? 'current' : 'historical';
}
function roleName(agent) { const role = agentRole(agent); return role === 'unknown' ? '角色未知' : `${role} · ${roles[role]}（推测）`; }
function renderRoles(agents) {
  const root = byId('role-cards'); root.replaceChildren();
  for (const [role, label] of Object.entries(roles)) {
    const matching = agents.filter(agent => agentRole(agent) === role);
    const current = matching.filter(agent => agentPeriod(agent) === 'current');
    const historical = matching.filter(agent => agentPeriod(agent) === 'historical');
    const unknown = matching.length - current.length - historical.length;
    const button = document.createElement('button'); button.className = 'role-card';
    button.append(text('strong', `${role} · ${label}`), text('span', roleDuties[role], 'role-duty'));
    const latest = [...current].sort((a, b) => String(b.last_at || b.started_at || '').localeCompare(String(a.last_at || a.started_at || '')))[0];
    if (latest) {
      button.append(text('span', latest.title || latest.name || latest.task_name || '未命名任务', 'role-task'));
      button.append(text('span', nameState(latest.status), 'role-status'));
      const preview = latest.result || [...(Array.isArray(latest.messages) ? latest.messages : [])].reverse().find(item => item.text)?.text || latest.purpose;
      if (preview) {
        const rendered = document.createElement('div'); renderMarkdown(rendered, structured(preview)?.summary ?? preview);
        button.append(text('span', rendered.textContent.trim(), 'role-preview'));
      }
    } else button.append(text('span', '本轮暂无可识别任务', 'role-task'));
    button.append(text('small', `当前 ${current.length} · 历史 ${historical.length}${unknown ? ` · 轮次未知 ${unknown}` : ''}`));
    button.addEventListener('click', () => { ui.role = role; ui.period = 'all'; byId('role-filter').value = role; byId('period-filter').value = 'all'; renderAgents(ui.agents); setView('agents'); });
    root.append(button);
  }
  const unknown = agents.filter(agent => agentRole(agent) === 'unknown');
  const hint = byId('role-note');
  if (hint) hint.textContent = unknown.length ? `另有 ${unknown.length} 个任务的角色未确认，可在“查看全部任务”中查看。角色标签依据任务名推测。` : '角色标签依据任务名推测；点击卡片查看任务与返回结果。';
}
function provenance(item, currentIteration) {
  if (!item) return '';
  const parts = [];
  if (item.iteration != null) {
    const age = item.stale ? (Number(currentIteration) - Number(item.iteration) === 1 ? '（上轮已报告）' : '（历史轮次）') : '';
    parts.push(`第 ${item.iteration} 轮${age}`);
  }
  if (item.source || item.path) parts.push(`来源：${item.source || item.path}`);
  if (item.at) parts.push(friendlyTime(item.at));
  return parts.join(' · ');
}
function renderResearch(data) {
  const research = data.research || {}, iteration = data.status?.iteration;
  for (const [id, item, fallback] of [['research-summary', research.summary, '暂无已报告的研究总结。'], ['next-action', research.next_action, '暂无已记录的下一步。']]) {
    const root = byId(id); root.replaceChildren();
    const content = document.createElement('div'); content.className = 'markdown'; renderMarkdown(content, item?.text || fallback); root.append(content);
    const meta = provenance(item, iteration); if (meta) root.append(text('p', meta, 'muted provenance'));
  }
  const docs = byId('research-docs'); docs.replaceChildren();
  for (const [key, label] of [['coordination', '实验安排与调度理由'], ['frontier', '候选方向']]) {
    const item = research[key];
    const body = document.createElement('div'); body.className = 'markdown doc-content';
    if (item?.available && item.text) renderMarkdown(body, item.text); else body.append(text('p', '暂无可用记录。', 'muted'));
    const meta = provenance(item, iteration); if (meta) body.prepend(text('p', meta, 'muted'));
    docs.append(details(`research-${key}`, text('summary', label), body));
  }
  if (research.policy?.name || research.policy?.application) {
    const body = document.createElement('div'); body.className = 'markdown doc-content';
    if (research.policy.name) body.append(text('strong', research.policy.name));
    if (research.policy.application) renderMarkdown(body, research.policy.application);
    docs.append(details('research-policy', text('summary', '安排依据'), body));
  }
}

document.addEventListener('toggle', event => {
  const el = event.target;
  if (el instanceof HTMLDetailsElement && el.dataset.key) {
    if (el.open) ui.details.add(el.dataset.key); else ui.details.delete(el.dataset.key);
  }
}, true);
function details(key, title, body) {
  const el = document.createElement('details');
  el.className = 'tool'; el.dataset.key = key; el.open = ui.details.has(key);
  el.append(title, body);
  return el;
}
function message(entry, label, index) {
  const id = String(entry.id ?? `${label}-${index}`);
  const wrapper = document.createElement('article'); wrapper.className = 'message';
  wrapper.append(text('div', label.startsWith('主 Agent') ? 'R' : '子', 'avatar'));
  const body = document.createElement('div');
  const head = document.createElement('div'); head.className = 'message-head';
  const time = text('time', friendlyTime(entry.at));
  if (entry.at) time.dateTime = entry.at;
  head.append(text('strong', label), time);
  const content = document.createElement('div'); content.className = 'markdown long-message';
  const value = entry.text ?? '';
  structuredContent(content, value, id);
  body.append(head, content);
  if (String(value).length > 1400) {
    const button = text('button', '', 'expand'); button.type = 'button';
    const apply = () => {
      const open = ui.expanded.has(id);
      content.classList.toggle('collapsed', !open);
      button.textContent = open ? '收起' : '展开完整消息';
    };
    button.addEventListener('click', () => { if (ui.expanded.has(id)) ui.expanded.delete(id); else ui.expanded.add(id); apply(); });
    apply(); body.append(button);
  }
  wrapper.append(body); return wrapper;
}
function renderMessages(root, messages, label) {
  root.replaceChildren();
  const usable = messages.filter(item => item && typeof item.text === 'string' && item.text.trim());
  if (!usable.length) { root.append(text('div', '暂无已记录的消息。', 'empty')); return; }
  usable.forEach((item, index) => root.append(message(item, label, index)));
}
function renderAgents(agents) {
  const list = byId('agent-list'); const detail = byId('agent-detail');
  list.replaceChildren(); detail.replaceChildren();
  const allAgents = agents;
  agents = agents.filter(agent => (ui.role === 'all' || agentRole(agent) === ui.role) && (ui.period === 'all' || agentPeriod(agent) === ui.period));
  byId('agent-count').textContent = allAgents.length ? `(${allAgents.length})` : '';
  if (!agents.length) { list.append(text('div', '此筛选下暂无任务记录。', 'empty')); return; }
  if (!agents.some(a => String(a.id) === ui.agent)) ui.agent = String(agents[0].id);
  agents.forEach(agent => {
    const button = document.createElement('button'); button.className = `agent-button${String(agent.id) === ui.agent ? ' selected' : ''}`;
    button.append(text('strong', agent.title || agent.name || agent.task_name || '子 Agent'), text('span', `${roleName(agent)} · ${nameState(agent.status)}`));
    const preview = agent.result || [...(Array.isArray(agent.messages) ? agent.messages : [])].reverse().find(item => item.text)?.text;
    if (preview) {
      const parsed = structured(preview);
      const rendered = document.createElement('div');
      renderMarkdown(rendered, parsed?.summary ?? preview);
      button.append(text('span', rendered.textContent.trim(), 'agent-preview'));
    }
    button.addEventListener('click', () => { ui.agent = String(agent.id); renderAgents(allAgents); });
    list.append(button);
  });
  const agent = agents.find(a => String(a.id) === ui.agent);
  detail.append(text('h2', agent.title || agent.name || agent.task_name || '子 Agent'));
  detail.append(text('p', `${roleName(agent)} · ${agentPeriod(agent) === 'current' ? '当前轮' : agentPeriod(agent) === 'historical' ? '历史轮' : '轮次未知'} · ${nameState(agent.status)}${agent.last_at ? ` · 最近记录 ${friendlyTime(agent.last_at)}` : ''}`, 'muted'));
  if (agent.purpose) detail.append(text('p', agent.purpose));
  const messages = Array.isArray(agent.messages) ? agent.messages.filter(item => item && item.text) : [];
  const final = agent.result || messages.at(-1)?.text;
  if (final) { detail.append(text('strong', '返回消息')); detail.append(message({ id: `agent-result-${agent.id}`, text: printable(final), at: agent.last_at }, agent.name || '子 Agent', 0)); }
  const older = final ? messages.filter(item => item.text !== final) : messages;
  if (older.length) {
    const title = text('summary', '查看其余对话');
    const conversation = document.createElement('div'); conversation.className = 'conversation agent-conversation';
    renderMessages(conversation, older, agent.name || '子 Agent');
    detail.append(details(`agent-thread-${agent.id}`, title, conversation));
  }
  const meta = text('pre', `线程 ID：${agent.id || '未知'}\n工具调用：${agent.tool_count ?? 0}`, 'raw');
  detail.append(details(`agent-meta-${agent.id}`, text('summary', '技术详情'), meta));
}
const toolCache = new Map();
async function loadToolDetails(tool, body) {
  const version = JSON.stringify([tool.id, tool.at, tool.status]);
  if (body.dataset.loading === version) return;
  body.dataset.version = version;
  const cached = toolCache.get(version);
  if (cached) { body.textContent = cached; return; }
  body.dataset.loading = version;
  body.textContent = '正在读取工具详情…';
  try {
    const response = await fetch(`api/tool?id=${encodeURIComponent(tool.id)}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const full = await response.json();
    if (String(full.id) !== String(tool.id) || full.at !== tool.at || full.status !== tool.status) {
      throw new Error('记录已变化');
    }
    const content = `线程：${full.thread_id || '未知'}\n\n输入：\n${printable(full.input ?? {})}\n\n输出：\n${printable(full.output ?? {})}`;
    toolCache.set(version, content);
    if (body.isConnected && body.dataset.version === version) body.textContent = content;
  } catch (error) {
    if (body.isConnected && body.dataset.version === version) body.textContent = `工具详情暂不可用（${error.message}）。请收起后重新展开以重试。`;
  } finally {
    if (body.dataset.loading === version) delete body.dataset.loading;
  }
}
function renderTools(tools) {
  const root = byId('tools'); root.replaceChildren();
  if (!tools.length) { root.append(text('div', '暂无工具记录。', 'empty')); return; }
  [...tools].reverse().forEach((tool, index) => {
    const title = document.createElement('summary');
    title.append(text('span', `${tool.title || '工具调用'} · ${nameState(tool.status)}`), text('time', friendlyTime(tool.at)));
    const raw = text('pre', '', 'raw');
    const el = details(`tool-${tool.id ?? index}`, title, raw);
    el.addEventListener('toggle', () => { if (el.open) loadToolDetails(tool, raw); });
    root.append(el);
    if (el.open) loadToolDetails(tool, raw);
  });
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
function render(data) {
  const status = data.status || {}, budget = data.budget || {};
  byId('state').textContent = status.status === 'completed' ? '研究已结束' : nameState(status.status);
  byId('budget').textContent = budget.state === 'not-applicable' ? 'SSH 作业记录；无 GPU 预算计量' : budget.state === 'recorded' ? `剩余 ${(budget.remaining_seconds / 3600).toFixed(2)} / ${(budget.authorized_seconds / 3600).toFixed(0)} GPU 小时` : '预算状态待确认';
  const activity = data.activity || {};
  byId('activity').textContent = `${status.iteration ? `第 ${status.iteration} 轮 · ` : ''}${activity.label || '暂无当前活动记录'} · 最后事件 ${friendlyTime(data.last_event_at) || '尚无'}`;
  ui.iteration = status.iteration ?? null;
  renderResearch(data);
  const messages = (Array.isArray(data.messages) ? data.messages : []).filter(item => item && typeof item.text === 'string' && item.text.trim())
    .sort((a, b) => (Date.parse(a.at) || 0) - (Date.parse(b.at) || 0));
  renderMessages(byId('latest-message'), messages.slice(-1), '主 Agent · 最新');
  const latest = messages.at(-1);
  const coordinator = byId('coordinator-message'); coordinator.replaceChildren();
  if (latest) { const parsed = structured(latest.text); renderMarkdown(coordinator, parsed?.summary ?? latest.text); } else coordinator.textContent = '暂无协调消息。';
  byId('coordinator-meta').textContent = latest ? `主 Agent 消息 · ${friendlyTime(latest.at) || '时间未知'}` : '';
  const agents = Array.isArray(data.agents) ? data.agents : [];
  const agentLink = byId('agent-link');
  agentLink.hidden = false;
  const working = agents.filter(a => ['working', 'inProgress'].includes(a.status)).length;
  const done = agents.filter(a => a.status === 'completed').length;
  agentLink.textContent = `查看全部 ${agents.length} 个任务 →`;
  const older = byId('older-section'); older.hidden = !messages.length;
  renderMessages(byId('conversation'), messages.slice(0, -1).reverse(), '主 Agent');
  const result = status.result ?? status.error;
  const last = messages.at(-1)?.text;
  const duplicate = result != null && last && JSON.stringify(structured(last) || last) === JSON.stringify(structured(result) || result);
  byId('result-section').hidden = result == null || duplicate;
  if (result != null && !duplicate) {
    const root = byId('result'); root.className = 'markdown'; root.replaceChildren();
    if (status.result == null && result && typeof result === 'object' && !Array.isArray(result)) {
      root.append(text('p', [result.type, result.message].filter(Boolean).join('：') || '发生错误'));
      root.append(details('result-error', text('summary', '错误详情'), text('pre', printable(result), 'raw')));
    } else structuredContent(root, result, 'final');
  }
  ui.agents = agents; renderRoles(agents); renderAgents(agents);
  renderTools(Array.isArray(data.tools) ? data.tools : []);
  renderCharts(data.curves);
  renderMarkdown(byId('goal'), data.goal || '暂无目标记录。');
  const plan = byId('plan'); plan.replaceChildren();
  if (Array.isArray(data.plan) && data.plan.length) data.plan.forEach(item => plan.append(text('p', `${item.step} · ${nameState(item.status)}`)));
  else plan.append(text('p', '暂无原生计划记录。', 'muted'));
}
function renderMetrics(data) {
  const root = byId('metrics'); root.replaceChildren();
  if (!data.available) {
    root.append(text('p', data.status === 'missing' ? '指标尚不可用。' : '指标文件无效或无法读取。'));
    return;
  }
  const show = value => value == null ? '未测' : String(value);
  const line = (label, value) => root.append(text('p', `${label}：${show(value)}`));
  line('阶段', data.phase);
  line('采样时间', data.window?.until);
  line('最近源事件', data.last_source_event_at);
  line('覆盖', data.coverage?.complete === true ? '完整' : data.coverage?.complete === false ? '不完整' : null);
  line('Token 计量', data.llm_measurable === true ? '可计量' : data.llm_measurable === false ? '不完整或不可计量；仅显示已观测非零值' : '未测');
  line('Token（input / cache / uncached / output）', ['input_tokens', 'cached_input_tokens', 'uncached_input_tokens', 'output_tokens'].map(k => show(data.llm_measurable !== true && data.llm?.[k] === 0 ? null : data.llm?.[k])).join(' / '));
  line('任务（已完成非混合 / 待完成 / 混合）', ['completed', 'pending', 'mixed'].map(k => show(data.tasks?.[k])).join(' / '));
  line('等待超时', data.wait_timeouts);
  line('资源失败 / 空 poll', [show(data.resources?.failed_calls), show(data.resources?.successful_empty_running_gpu_polls)].join(' / '));
  line('Allocation 秒数', data.allocation?.state === 'not-applicable' ? '不适用（SSH 作业记录）' : data.allocation?.available === true ? data.allocation?.elapsed_seconds : null);
  line('采集器最近耗时（秒） / 读取字节', [show(data.reader?.previous_poll_total_seconds ?? data.reader?.elapsed_seconds), show(data.reader?.bytes_read)].join(' / '));
  if (data.comparison) {
    for (const key of ['baseline', 'optimized']) {
      const item = data.comparison[key];
      const token = item.coverage?.usage_records_observed ? item.llm?.uncached_input_tokens : null;
      line(`${key} 描述性对照（已完成非混合 / 待完成 / 混合；已观测 uncached input；覆盖完整）`, [show(item.tasks?.completed_unmixed), show(item.tasks?.pending), show(item.tasks?.mixed), show(token), show(item.coverage?.complete)].join(' / '));
    }
  } else if (data.comparison_status === 'invalid') line('前后对照', '文件无效');
}
async function refreshMetrics() {
  try {
    const response = await fetch('api/metrics', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderMetrics(await response.json());
  } catch (_) { renderMetrics({ available: false, status: 'invalid' }); }
}
async function refresh() {
  if (ui.refreshing) return;
  ui.refreshing = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 25000);
  try {
    const response = await fetch('api/state', { cache: 'no-store', signal: controller.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    byId('refresh-error').hidden = true;
    const serialized = JSON.stringify(data, (key, value) => key === 'as_of' ? undefined : value);
    if (serialized !== ui.last) {
      const scroll = window.scrollY;
      render(data); ui.last = serialized;
      window.scrollTo({ top: scroll, behavior: 'instant' });
    }
  } catch (error) {
    byId('refresh-error').textContent = `刷新失败：${error.message}。当前显示上次获取的记录。`;
    byId('refresh-error').hidden = false;
  } finally {
    clearTimeout(timeout); ui.refreshing = false;
  }
}
setView(ui.view); refresh(); refreshMetrics(); setInterval(refresh, 5000); setInterval(refreshMetrics, 5000);
