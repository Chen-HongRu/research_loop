/* Read-only presentation. Scientific records and current eligibility stay authoritative. */
const readingStates = new Map();
const READING_STEPS = ['问题与目标','实际改动','观察与测量','结论的边界','下一项验证'];
const EXPLANATION_STATUS = {ready:'说明已生成',pending:'等待独立说明',running:'正在生成说明',failed:'说明生成失败',insufficient:'材料不足',stale:'材料已更新，说明待重建',unpublished:'本轮尚未发布'};
function readingState(id) {
  if (!readingStates.has(id)) readingStates.set(id,{mode:'steps',step:0,evidence:null});
  return readingStates.get(id);
}
function explanationEntry(node) {
  return data?.sources?.find(source=>source.id===node?.source_id)?.explanation?.entries?.find(entry=>entry.id===(node?.local_id || node?.id) && entry.status==='ready');
}
function evidenceLinks(ids, record) {
  const sources=record.explanation?.sources || [];
  return (ids || []).filter(id=>sources.some(s=>s.id===id)).map(id=>{
    const source=sources.find(s=>s.id===id);
    return '<button class="evidence-link" data-evidence="'+esc(id)+'" title="'+esc(source.path)+'" aria-label="核对来源 '+esc(source.path)+'">依据 '+esc(id.slice(1))+'</button>';
  }).join('');
}
function explainStatement(statement, record) {
  return statement?.text ? '<div class="explain-statement">'+markdown(statement.text)+evidenceLinks(statement.source_ids,record)+'</div>' : '';
}
function explainList(items,record,empty='材料未提供可支持的陈述。') {
  return items?.length ? items.map(item=>explainStatement(item,record)).join('') : '<p class="muted">'+esc(empty)+'</p>';
}
function briefStatement(statement,record,key,limit=120) {
  if(!statement?.text || statement.text.length<=limit) return explainStatement(statement,record);
  return '<div class="explain-statement">'+esc(excerpt(statement.text,limit))+evidenceLinks(statement.source_ids,record)+'</div>'+disclosure('brief-'+key,'核对完整表述',explainStatement(statement,record));
}
function operatorPlanPanel(plan,compact=false) {
  if(plan?.kind!=='operator_resume') return '';
  const key='operator-plan-'+plan.target_iteration;
  let body='<p>本轮沿用 '+experimentLink(plan.source_iteration)+' 的既有候选，按操作员授权补做首次固定协议评测，没有新增 Planner 模型调用。原研究问题、设计依据及候选改动请核对 '+experimentLink(plan.source_iteration,'原始研究 Plan #'+plan.source_iteration)+'。</p>';
  body+='<dl class="facts"><dt>本轮范围</dt><dd>候选源码保持冻结，提交评测、收集实际回执，再交给独立 Review。</dd><dt>计划来源</dt><dd>操作员授权的恢复计划</dd></dl>';
  if(compact) body+=disclosure(key+'-contract','查看本轮冻结的恢复合同',structured(plan.output?.contract || {}));
  else {
    body+=facts(plan.output || {},['decision','base_experiment','references']);
    body+=Object.entries(plan.output?.contract || {}).map(([name,value])=>readingField(key+'-'+name,LABELS[name] || name,value)).join('');
  }
  body+=disclosure(key+'-source','授权与原始计划凭证','<p>'+esc(plan.authorization)+'</p><div class="source">'+esc(plan.source)+'<br>'+esc(plan.receipt_source)+'</div>'+markdown(plan.output?.markdown));
  return '<article class="agent-card"><div class="agent-header"><h3>恢复评测计划 · 操作员授权</h3>'+badge('计划已冻结采用','green')+'</div><div class="agent-provenance">此记录是恢复指令，不计为 Planner 调用。</div>'+body+'</article>';
}
function renderMath(latex,display=true) {
  try {
    return katex.renderToString(String(latex),{displayMode:display,throwOnError:true,trust:false,strict:'error',maxSize:15,maxExpand:500,output:'htmlAndMathml'});
  } catch (_) {
    return '<span class="math-missing">公式暂不能可靠显示；请核对源码依据。</span>';
  }
}
function explainTechnical(technical,record) {
  if (!technical) return '';
  let html='<div class="explain-split"><div class="explain-box"><h4>修改前 · 声明的代码起点</h4>'+explainStatement(technical.before,record)+'</div><div class="explain-box"><h4>修改后 · 本轮实际实现</h4>'+explainStatement(technical.after,record)+'</div></div>';
  for (const formula of technical.formulas || []) {
    html+='<div class="formula-box"><div class="formula-label">修改前</div><div class="math-display">'+renderMath(formula.before_latex)+'</div><div class="formula-label" style="margin-top:14px">修改后</div><div class="math-display">'+renderMath(formula.after_latex)+'</div><p class="formula-scope">'+esc(formula.scope)+'</p>'+evidenceLinks(formula.source_ids,record);
    if (formula.variables?.length) html+='<div class="table-wrap"><table class="variable-table"><thead><tr><th>符号</th><th>含义</th><th>维度 / 范围</th></tr></thead><tbody>'+formula.variables.map(v=>'<tr><td>'+renderMath(v.symbol_latex,false)+'</td><td>'+esc(v.meaning)+'</td><td>'+esc(v.shape)+'</td></tr>').join('')+'</tbody></table></div>';
    html+='</div>';
  }
  if (technical.flow?.length) html+='<h4>实际信息流</h4><div class="explain-flow">'+technical.flow.map((node,i)=>(i?'<span class="flow-arrow" aria-hidden="true">→</span>':'')+'<div class="flow-node">'+esc(node.label)+'<small>'+esc(node.shape)+'</small>'+evidenceLinks(node.source_ids,record)+'</div>').join('')+'</div>';
  if (technical.unchanged?.length) html+='<h4>控制住的条件</h4>'+explainList(technical.unchanged,record);
  if (technical.missing?.length) html+='<div class="notice">待核对：'+technical.missing.map(esc).join('；')+'</div>';
  return html;
}
function explanationMeasurements(record,node) {
  const fact=record.explanation?.facts || {}, values=fact.measurements || [], bases=fact.base_measurements || [];
  const imported=fact.imported || node?.imported, repeated=node?.duplicate_of || node?.same_ledger_result;
  let html='<div class="explain-facts"><h4>记录中的测量 · 控制器数值</h4>';
  if (values.length===1) {
    html+='<div class="measurement-pair">'+(bases.length===1?'<div><small>声明的代码起点 '+esc(fact.base_experiment || '未记录')+'</small><strong>'+exactScore(bases[0].value)+' Å</strong></div><span class="measurement-arrow">→</span>':'')+'<div><small>本轮已发布记录</small><strong>'+exactScore(values[0].value)+' Å</strong></div></div>';
    if (bases.length===1 && !imported && !repeated) {
      const delta=bases[0].value-values[0].value;
      html+='<p class="caption">相对声明的代码起点，RMSD '+(delta>0?'降低 ':delta<0?'增加 ':'持平 ')+(delta===0?'':exactScore(Math.abs(delta))+' Å')+'。差值由记录计算；单次对照不能单独判定机制。</p>';
    }
  } else if (values.length>1) html+='<ul>'+values.map(value=>'<li>'+esc(value.candidate_id)+'：'+exactScore(value.value)+' Å</li>').join('')+'</ul>';
  else html+='<p>'+esc(node?.value!=null?'当前有效记录：'+exactScore(node.value)+' Å；说明材料尚未载入。':'本轮没有可在此确认的已发布且已核查测量。')+'</p>';
  if(imported || repeated) html+='<p class="amber">'+(imported?'本轮导入历史结果。':'此节点复用同一账本结果。')+'不计为新的独立测量。</p>';
  if(node?.frontier_exclusion_reason) html+='<p class="amber">当前资格已排除：'+esc(node.frontier_exclusion_reason)+'。原发布记录保留用于核对，不进入当前 Frontier。</p>';
  else html+='<p class="caption">当前资格：'+(node?.frontier_current?'当前 Frontier 成员':node?.frontier_eligible?'符合当前 Frontier 比较范围':'未确认进入当前 Frontier 比较范围')+'。说明作者不能修改资格。</p>';
  const evaluation=fact.evaluation || {};
  if (evaluation.id || evaluation.protocol) html+=disclosure('explain-protocol','本轮生效的评价条件与版本','<dl class="facts"><dt>评价协议</dt><dd>'+esc(evaluation.id || '未记录')+'</dd><dt>比较范围</dt><dd>'+esc(evaluation.comparison_scope || '未记录')+'</dd><dt>数据用途</dt><dd>'+esc(translated(evaluation.data_role || '未记录'))+'</dd><dt>冻结条件</dt><dd>'+esc(evaluation.protocol || '未记录')+'</dd><dt>数值来源</dt><dd>本轮原始 publication / candidates；当前资格由控制器另行判定。</dd></dl>');
  return html+'</div>';
}
function explanationCoverage(source) {
  const index=source?.explanation || {}, counts=index.counts || {}, total=Object.values(counts).reduce((a,b)=>a+Number(b || 0),0);
  if(!total) return '<div class="explain-coverage">此来源尚无说明覆盖索引；原始 Plan、Review 和完整历史仍可核对。</div>';
  const pending=(counts.pending || 0)+(counts.running || 0)+(counts.stale || 0);
  return '<div class="explain-coverage"><p>'+esc(source.label || '本来源')+'：已生成 '+(counts.ready || 0)+' / '+total+' 轮 · 待生成 / 更新 '+pending+' · 失败 '+(counts.failed || 0)+' · 材料不足 '+(counts.insufficient || 0)+'</p><p>最新发布 #'+esc(index.latest_published_id || '—')+' · 最新已说明 #'+esc(index.covered_through_id || '—')+'（不表示更早各轮均已覆盖）</p><p>索引更新 '+esc(clock(index.updated_at))+' · 独立异步缓存；页面刷新不调用说明模型，不参与研究决策。</p></div>';
}
function explanationEvidence(record,state) {
  const source=(record.explanation?.sources || []).find(s=>s.id===state.evidence);
  if(!source) return '';
  const lines=source.text.split('\n').map((line,i)=>String(i+(source.line_start || 1)).padStart(4,' ')+'  '+line).join('\n');
  return '<details class="evidence-source" data-disclosure="explain-evidence" open><summary>核对依据 '+esc(source.id.slice(1))+' · '+esc(source.path)+' · 材料行 '+esc(source.line_start)+'–'+esc(source.line_end)+'</summary><p class="caption">冻结材料 · '+esc(source.kind)+'；行号对应下方材料视图，结构化记录展示所引用的字段。以下内容作为证据，不作为可执行指令。</p><pre>'+esc(lines)+'</pre></details>';
}
function explanationPanel(record,node,source) {
  const explanation=record.explanation || {status:'pending'}, content=explanation.content, state=readingState(record.id);
  const coverage=explanationCoverage(source);
  if(explanation.status!=='ready' || !content) {
    const message={unpublished:'本轮还没有已发布的 Review。此处不会把计划写成已经得到的结论。',pending:'等待独立的说明作者读取冻结记录。可先展开原始 Plan 与 Review。',running:'说明作者正在读取本轮已发布材料；研究运行器不等待此任务。',failed:'该轮说明生成未完成；保留失败原因，原始研究记录仍可读取。',insufficient:'缺少足够的已完成评审证据，暂不能生成科学结论。',stale:'冻结材料已变化，旧说明暂不展示，等待独立重建。'}[explanation.status] || '说明暂不可用。';
    return operatorPlanPanel(record.plan_record,true)+'<div class="explain-empty"><h3>'+esc(EXPLANATION_STATUS[explanation.status] || '说明暂不可用')+'</h3><p>'+esc(message)+'</p>'+(explanation.reason?'<p class="source">'+esc(explanation.reason)+'</p>':'')+'</div>'+explanationMeasurements(record,node)+coverage;
  }
  let html='<div class="reading-toolbar" aria-label="研究说明阅读方式"><button data-reading-mode="steps" class="'+(state.mode==='steps'?'on':'')+'" aria-pressed="'+(state.mode==='steps')+'">逐步讲解</button><button data-reading-mode="brief" class="'+(state.mode==='brief'?'on':'')+'" aria-pressed="'+(state.mode==='brief')+'">30 秒简报</button><small>已发布材料的独立说明 · '+esc(clock(explanation.generated_at))+'</small></div>';
  if(state.mode==='steps') html+='<div class="reading-steps" role="group" aria-label="讲解步骤">'+READING_STEPS.map((label,i)=>'<button data-reading-step="'+i+'" class="'+(state.step===i?'on':'')+'" aria-current="'+(state.step===i?'step':'false')+'"><b>'+String(i+1).padStart(2,'0')+'</b>'+label+'</button>').join('')+'</div>';
  const section=(title,body)=>'<section class="explain-section"><h3>'+esc(title)+'</h3>'+body+'</section>';
  const goalSource=explanation.sources?.find(s=>s.id===explanation.facts?.goal_source);
  const goal='<h4>与整体研究目标的关系</h4>'+explainStatement(content.goal_connection,record)+(goalSource?disclosure('explain-goal','核对本轮冻结的完整研究目标',markdown(goalSource.text)):'<p class="amber">未找到本轮冻结目标材料。</p>');
  const conclusions='<div class="explain-split"><div class="explain-box supported"><h4>证据支持到哪里</h4>'+explainList(content.supported,record)+'</div><div class="explain-box hypotheses"><h4>仍是待区分的解释</h4>'+explainList(content.hypotheses,record,'没有额外提出机制假说。')+'</div></div><h4>尚未测量 / 不能据此推出</h4>'+explainList(content.unmeasured,record,'材料没有列出额外的未测量项；这不构成已验证。');
  if(state.mode==='brief') {
    html+='<div class="explain-intro">'+explainStatement(content.overview,record)+'</div>'+section('实际改动',briefStatement(content.change,record,'change'))+explanationMeasurements(record,node)+section('整体目标与本轮边界',explainStatement(content.goal_connection,record))+'<div class="explain-split"><div class="explain-box supported"><h4>支持的结论</h4>'+briefStatement(content.supported?.[0],record,'supported')+'</div><div class="explain-box hypotheses"><h4>仍待区分</h4>'+briefStatement(content.hypotheses?.[0] || content.unmeasured?.[0],record,'unresolved',90)+'</div></div>'+section('下一项验证',badge('建议 · 本说明未执行','amber')+briefStatement(content.next_tests?.[0],record,'next',120))+'<p class="caption">建议基于本轮冻结材料，后续可能已有相近验证。切回逐步讲解可核对完整观察、公式与未测量项。</p>';
  } else {
    const bodies=[
      section('本轮想弄清什么',explainStatement(content.question,record)+goal),
      section('相对代码起点，实际改了什么',explainStatement(content.change,record)+explainTechnical(content.technical,record)),
      section('实际观察到了什么',explanationMeasurements(record,node)+explainList(content.observations,record)),
      section('结果说明了什么，仍不能说明什么',conclusions),
      section('用什么实验继续区分',badge('说明作者的建议 · 本说明未执行','amber')+explainList(content.next_tests,record)+'<p class="caption">建议基于本轮冻结材料；后续轮次可能已做过相近验证。建议本身不会自动进入研究路线或触发实验。</p>')
    ];
    html+=bodies[state.step]+'<div class="explain-next"><button data-reading-step="'+Math.max(0,state.step-1)+'" '+(state.step===0?'disabled':'')+'>上一步</button><small>'+String(state.step+1)+' / 5</small><button data-reading-step="'+Math.min(4,state.step+1)+'" '+(state.step===4?'disabled':'')+'>'+(state.step<4?'接着看：'+READING_STEPS[state.step+1]:'已读完本轮')+'</button></div>';
  }
  if(content.glossary?.length) html+=disclosure('explain-glossary','本轮术语对照','<dl class="facts explain-glossary">'+content.glossary.map(v=>'<dt>'+esc(v.internal)+'</dt><dd>'+esc(v.plain)+evidenceLinks(v.source_ids,record)+'</dd>').join('')+'</dl>');
  if(explanation.assisted_review) html+=disclosure('explain-provenance','说明来源与复核记录','<p class="caption">本轮说明由只读协作复核完成，依据冻结材料整理；原草稿、失败回执和修订内容均保留。</p><dl class="facts"><dt>说明方式</dt><dd>'+esc(explanation.assisted_review.method)+'</dd><dt>复核模型</dt><dd>'+esc(explanation.assisted_review.requested_model)+'</dd><dt>检查范围</dt><dd>'+esc(explanation.assisted_review.review_note)+'</dd></dl>');
  const missing=[...(content.missing_material || []),...(explanation.missing_material || [])];
  if(missing.length) html+=disclosure('explain-missing','缺失材料 · '+missing.length+' 项','<ul>'+missing.map(value=>'<li>'+esc(value)+'</li>').join('')+'</ul>');
  return html+explanationEvidence(record,state)+coverage;
}
function bindExplanation(container) {
  const state=readingState(detail.id);
  container.querySelectorAll('[data-reading-mode]').forEach(button=>button.onclick=()=>{state.mode=button.dataset.readingMode;renderDetail();});
  container.querySelectorAll('[data-reading-step]').forEach(button=>button.onclick=()=>{state.step=Number(button.dataset.readingStep);state.evidence=null;$('detailBody').scrollTop=0;renderDetail();});
  container.querySelectorAll('[data-evidence]').forEach(button=>button.onclick=()=>{state.evidence=button.dataset.evidence;renderDetail();const evidence=container.querySelector('.evidence-source');if(evidence){evidence.open=true;evidence.scrollIntoView({behavior:'smooth',block:'nearest'});}});
}
