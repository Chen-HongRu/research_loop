const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements = new Map();
const document = {getElementById(id) {
  if (!elements.has(id)) elements.set(id, {querySelectorAll: () => [], setAttribute() {}, style:{}});
  return elements.get(id);
}};
// Exercise actual rendering functions without a browser, network, or polling loop.
const context = vm.createContext({document, fetch: () => new Promise(() => {}), setTimeout() {}});
for (const [,src] of html.matchAll(/<script src="\/(assets\/[^\"]+)"><\/script>/g)) {
  vm.runInContext(fs.readFileSync(path.join(__dirname,'..',src),'utf8'),context);
}
vm.runInContext(script+'\nglobalThis.ui = {loadDetail, taskNodes, visibleTasks, taskState, renderDetail, telemetryGroups, metricTask, metricMatches, metricLeaves, metricSeries, metricPlot, metricCard, mergeTaskMetrics, loadTaskMetrics, longTrainingRuns, longTrainingCurve, gitLayout, graphParent, drawGraph, renderProgress, renderScout, nodeResult, researchEvidencePanel, markdown, structured, agentCard, executionHistory, executePanel, readingField, frontierPlot, axisValue, exactScore, showFrontierPoint, resolveReference, explanationPanel, explanationMeasurements, explainTechnical, readingState, renderMath, explanationCoverage, operatorPlanPanel, continuationPlanPanel};', context);
const ui = context.ui;
const plain = value => JSON.parse(JSON.stringify(value));

test('Scout refresh retains user expansion and collapse while updating findings', () => {
  let markup='', fold=null;
  const prior=elements.get('scoutBody');
  elements.set('scoutBody',{
    get innerHTML(){return markup;},
    set innerHTML(value){
      markup=value;
      const tag=value.match(/<details data-disclosure="scout-findings"[^>]*>/);
      fold=tag?{open:/\sopen(?:\s|>)/.test(tag[0])}:null;
    },
    querySelector:selector=>selector==='[data-disclosure="scout-findings"]'?fold:null,
    querySelectorAll:()=>[],
  });
  try {
    vm.runInContext("data={scout:{status:'completed',summary:'Scout summary',findings:['Initial finding']}};",context);
    ui.renderScout();
    assert.equal(fold.open,false);
    fold.open=true;
    for(let tick=1;tick<=3;tick++) {
      vm.runInContext(`data.scout.observed_at=${tick};data.scout.findings=['Updated finding ${tick}'];`,context);
      ui.renderScout();
      assert.equal(fold.open,true,'auto-refresh must preserve user expansion');
      assert.ok(markup.includes('Updated finding '+tick),'findings still refresh');
    }
    fold.open=false;
    ui.renderScout();
    assert.equal(fold.open,false,'auto-refresh must also preserve explicit collapse');
  } finally {
    if(prior) elements.set('scoutBody',prior); else elements.delete('scoutBody');
    vm.runInContext('data=null;',context);
  }
});

test('Git rows follow time; tracks follow code parents, independently of routes and references', () => {
  const nodes = [
    {id:'0001', base:null, route_id:'A'},
    {id:'0002', base:'0001', route_id:'A'},
    {id:'0003', base:'0001', route_id:'B'},
    {id:'0004', base:'0002', route_id:'B'},
    {id:'0005', base:'0003', route_id:'A'},
  ];
  const graph = ui.gitLayout(nodes);
  assert.deepEqual(plain(graph.ordered.map(node => node.id)), ['0005','0004','0003','0002','0001']);
  assert.equal(graph.positions['0005'].lane, graph.positions['0003'].lane);
  assert.equal(graph.positions['0004'].lane, graph.positions['0002'].lane);
  assert.notEqual(graph.positions['0004'].lane, graph.positions['0005'].lane);
  const changedRoutes = nodes.map(node => ({...node,route_id:'same-route',references:['0001']}));
  assert.deepEqual(plain(ui.gitLayout(changedRoutes).positions), plain(graph.positions));
});

test('References and missing parents never create imaginary code nodes', () => {
  const graph = ui.gitLayout([{id:'0003',base:'0000',references:['0002']},{id:'0002',base:null}]);
  assert.deepEqual(Object.keys(graph.positions).sort(), ['0002','0003']);
});

test('Execution continuation follows retained source with a distinct edge and original Plan', () => {
  const nodes=[{id:'0001',base:null,research_iteration:'0001'}, {id:'0002',base:null,continuation_of:'0001',research_iteration:'0001'}];
  assert.equal(ui.graphParent(nodes[1]),'0001');
  const graph=ui.gitLayout(nodes);
  assert.equal(graph.positions['0002'].lane,graph.positions['0001'].lane);
  context.continuationNodes=nodes;
  vm.runInContext('data={nodes:continuationNodes};',context);
  elements.get('routeFilter').value='';
  elements.get('sourceFilter').value='';
  document.getElementById('graph').querySelectorAll=()=>[];
  ui.drawGraph();
  assert.match(elements.get('graph').innerHTML,/stroke-dasharray="2 4"/);
  assert.match(elements.get('graph').innerHTML,/执行续跑：#0001 → #0002；沿用研究 #0001 的 Plan/);
  assert.doesNotMatch(elements.get('graph').innerHTML,/代码继承：#0001 → #0002/);
  vm.runInContext('data=null;',context);
});

test('Continuation progress labels inherited planning without claiming another call', () => {
  context.continuationProgress={state:{iteration:2,phase:'execute',status:'ready'},nodes:[{id:'active:0002',local_id:'0002',continuation_of:'active:0001',research_iteration:'active:0001'}],progress:{stages:[{key:'selection',status:'inherited',inherited_from:'0001',attempts:0},{key:'plan',status:'inherited',inherited_from:'0001',attempts:0}]}};
  vm.runInContext('data=continuationProgress;',context);
  ui.renderProgress();
  assert.match(elements.get('currentSub').textContent,/执行续跑 · 沿用研究 #0001/);
  assert.equal((elements.get('phases').innerHTML.match(/沿用 #0001 · 无新增调用/g)||[]).length,2);
  assert.doesNotMatch(elements.get('phases').innerHTML,/0 次调用|1 次调用/);
  vm.runInContext('data=null;',context);
});

test('Research scripts show evidence instead of a missing quality score', () => {
  const node={execution_kind:'research_script',job_status:'completed',published:true};
  assert.equal(ui.nodeResult(node),'研究证据已评审');
  assert.equal(ui.nodeResult({...node,job_status:'failed'}),'研究脚本失败 · 已评审');
  assert.equal(ui.nodeResult({...node,job_status:'blocked',published:false}),'研究作业待核账');
  const research={iteration:'0017',status:'completed',owned_job_id:'901',gpu_seconds:30,allocated_gpu_seconds:180,outputs:['probe/metrics.json','probe/<script>.png'],evidence:['control/research-jobs/0017/state.json'],source:'control/research-jobs/0017/receipt.json',execution_identity:'execution-v1'};
  const rendered=ui.researchEvidencePanel(research);
  for(const text of ['研究脚本 · 证据计算','脚本执行完成','没有 RMSD 评分','probe/metrics.json','分配 GPU 秒','execution-v1']) assert.ok(rendered.includes(text),text);
  assert.doesNotMatch(rendered,/<script>|<img|"gpu_seconds"\s*:/);
  const pending=ui.explanationPanel({id:'active:0017',execution:{research},explanation:{status:'unpublished'}},node);
  assert.match(pending,/已收集的证据产物/);
  assert.match(pending,/不产生固定协议 RMSD，也不进入 RMSD Frontier/);
  assert.doesNotMatch(pending,/本轮没有可在此确认的已发布且已核查测量/);
});

test('Unknown research effects remain visible and resource labels do not count them as formal scoring', () => {
  context.researchProgress={state:{iteration:16,phase:'done',status:'ready'},nodes:[],formal_gpu_seconds:300,research_gpu_seconds:30,debug_gpu_seconds:5,outstanding:[{mode:'research',status:'unknown',attempts:[]}]};
  vm.runInContext('data=researchProgress;',context);
  ui.renderProgress();
  const status=elements.get('statusline').innerHTML;
  assert.match(status,/研究脚本作业 状态待确认/);
  assert.match(status,/正式评分 300 GPU 秒 · 研究脚本 30 GPU 秒 · 调试 5 GPU 秒/);
  assert.doesNotMatch(status,/无待核账 GPU 作业/);
  vm.runInContext('data=null;',context);
});

test('A script Plan exposes the research contract as a key output', () => {
  const rendered=ui.agentCard({phase:'plan',accepted:true,output:{decision:'investigate',contract:{action:'Run the declared probe'},research_contract:{question:'Which explanation survives?',observables:[{id:'o1',method:'Compare direct gradients',timing:'Same frozen checkpoint'}],execution:{mode:'script',entrypoint:'research/probe.py',outputs:['metrics.json']}}}},0);
  for(const text of ['研究问题与验证合同','直接观测量','观测时点','Compare direct gradients','验证脚本','research/probe.py']) assert.ok(rendered.includes(text),text);
  assert.doesNotMatch(rendered,/其他结构化输出/);
});

test('Structured output is labeled prose, with links for exact code and evidence IDs', () => {
  const rendered = ui.structured({decision:'continue',knowledge_changes:['A result'],references:['0001'],verified:true});
  assert.match(rendered, /<dt>决策<\/dt>/);
  assert.match(rendered, /新增认识/);
  assert.match(rendered, /data-node="0001"/);
  assert.match(rendered, /证据已核查/);
  assert.doesNotMatch(rendered, /"decision"\s*:/);
});

test('Markdown tables, code and JSON blocks are readable while HTML and unsafe URLs stay inert', () => {
  const rendered = ui.markdown('# Findings\n\n| Metric | Value |\n| --- | --- |\n| RMSD | 2.3 |\n\n```json\n{"decision":"continue"}\n```\n\n<img src=x onerror=alert(1)> [unsafe](javascript:alert) `a < b`');
  assert.match(rendered, /<table>/);
  assert.match(rendered, /<dt>决策<\/dt>/);
  assert.match(rendered, /&lt;img/);
  assert.match(rendered, /<code>a &lt; b<\/code>/);
  assert.doesNotMatch(rendered, /<img|href="javascript:|<pre><code>\{/);
});

test('Agent cards distinguish a published Plan, a pending Review, and a failed retry', () => {
  const plan = ui.agentCard({key:'plan-0',phase:'plan',role:'Planner',attempt:'attempt-002',status:'completed',accepted:true,published:true,output:{contract:{action:'Test a bounded change'}}},0);
  assert.match(plan, /合同已发布/);
  assert.doesNotMatch(plan, /Review 已发布/);
  const review = ui.agentCard({phase:'review',status:'completed',accepted:false,published:false,output:{decision:'continue',goal_status:'undetermined',markdown:'Provisional reasoning'}},1);
  assert.match(review, /控制器是否接纳尚未确认/);
  assert.match(review, /尚未判定/);
  const failed = ui.agentCard({phase:'plan',status:'timed_out',output:null,error:'Retained timeout'},2);
  assert.match(failed, /超时/);
  assert.match(failed, /没有已完成的输出/);
  assert.doesNotMatch(failed, /控制器已接纳/);
  const proposal=ui.agentCard({phase:'proposal',status:'completed',output:{action:'propose',markdown:'Change the time weighting.'}},3);
  assert.match(proposal, /class="agent-action">Change the time weighting\./);
});

test('Long contract fields have a short excerpt and retain the entire text behind disclosure', () => {
  const text = '研究条件'.repeat(180);
  const rendered = ui.readingField('contract-evaluation','评价方法',text);
  assert.match(rendered, /展开完整评价方法/);
  assert.match(rendered, /<details data-disclosure="contract-evaluation">/);
  assert.ok(rendered.includes(text));
});

test('Execute history retains formal, debug, original failure and provenance without synthetic quality', () => {
  const rendered=ui.executionHistory({
    job:{status:'submitted',fingerprint:'formal-fingerprint',source_commit:'abc123',record_name:'fresh-formal'},
    original_failure:{cause:'Original OOM <script>bad()</script>',owned_job_id:'original-job',gpu_seconds:12,source:'original.json'},
    debug:{phase:'repair',used_gpu_seconds:17,reserved_gpu_seconds:3,remaining_gpu_seconds:80,limit_gpu_seconds:100},
    debug_attempts:[{id:'debug-1',owned_job_id:'debug-job',status:'failed',terminal_state:'OOM',gpu_seconds:17,allocated_gpu_seconds:20,verified:false,failure_kind:'memory',cause:'<img src=x onerror=bad()>',source:'debug.json',quality_score:999}],
    formal_attempts:[{owned_job_id:'formal-job',status:'running',terminal_state:null,gpu_seconds:0,seed:42,quality_score:888}],
    quality_score:777,
  });
  for (const field of ['当前正式作业','formal-fingerprint','abc123','fresh-formal','原始失败','original-job','original.json','调试预算 · 不计正式质量','已用 GPU 秒','预留 GPU 秒','剩余 GPU 秒','预算上限 GPU 秒','debug-1','debug-job','OOM','分配 GPU 秒','证据已核查','失败类别','memory','debug.json','正式作业回执','formal-job','采样种子','42']) assert.ok(rendered.includes(field),field);
  assert.match(rendered, /<dd>0<\/dd>/);
  assert.match(rendered, /&lt;script&gt;/);
  assert.match(rendered, /&lt;img/);
  assert.doesNotMatch(rendered, /<script>|<img|999|888|777|quality_score|RMSD|"owned_job_id"\s*:/);
});

test('Execute tab tolerates old records and retains Executor and Debug repair completed outputs', () => {
  assert.match(html, /data-tab="execute"[^>]*>Execute · 执行与调试/);
  assert.match(ui.executionHistory(), /尚无执行或调试回执/);
  assert.doesNotThrow(()=>ui.executionHistory({job:{status:'prepared'},debug:{},debug_attempts:[]}));
  const rendered=ui.executePanel({agents:[
    {phase:'execute',role:'Executor',status:'completed',accepted:true,output:{action:'Submitted job',markdown:'Formal receipt retained'},source:'execute-output.json'},
    {phase:'execute',role:'Debug repair',status:'completed',accepted:false,output:{markdown:'Fixed the allocation <img>'},source:'repair-output.json'},
    {phase:'review',role:'Reviewer',output:{markdown:'Separate review'}},
  ]});
  for (const field of ['Executor','Debug repair','Formal receipt retained','Fixed the allocation &lt;img&gt;','execute-output.json','repair-output.json','控制器已接纳','控制器是否接纳尚未确认','正式测量经过独立 Review 后才进入质量曲线']) assert.ok(rendered.includes(field),field);
  assert.doesNotMatch(rendered, /Separate review|Review 已发布|<img>/);
  assert.match(ui.executePanel({execute:'Legacy execution receipt'}), /Legacy execution receipt/);
});

test('Exploration proposals and reviews expose questions, discriminating tests and unresolved findings', () => {
  const proposal=ui.agentCard({phase:'proposal',role:'Advocate A',status:'completed',output:{research_design:{kind:'architecture',question:'Which message is useful?',contrasting_predictions:['A','B'],minimal_test:'Compare two fixed inputs'}}},0);
  assert.match(proposal, /研究设计：问题、对照与最小验证/);
  assert.match(proposal, /架构探索/);
  assert.match(proposal, /相互区分的预测/);
  const review=ui.agentCard({phase:'review',status:'completed',output:{insight:{observed:'A difference',supported:'A bounded claim',unresolved:'Mechanism unknown',next_discriminating_test:'A controlled comparison'}}},1);
  assert.match(review, /实际观察/);
  assert.match(review, /尚未解决/);
  assert.match(review, /下一项区分性实验/);
});

test('Frontier plot preserves tiny improvements and exact raw RMSD instead of rounding membership', () => {
  const first=2.2214044051580952,last=2.2214043051580952;
  const nodes=[{id:'h:0001',sequence:1},{id:'a:0001',sequence:2}];
  const points=[{id:nodes[0].id,sequence:1,value:first,reached:true},{id:nodes[1].id,sequence:2,value:last,reached:true,current:true}];
  const plot=ui.frontierPlot({available:true,points},nodes,'frontier');
  assert.equal(ui.exactScore(first),String(first));
  assert.equal(plot.points.length,2);
  assert.ok(plot.hi-plot.lo<1e-6);
  assert.equal(plot.axisOffset,last);
  assert.notEqual(ui.axisValue(first,plot),ui.axisValue(last,plot));
  assert.equal(plot.curve.at(-1).value,last);
});

test('Tied current members remain separate, and unknown historical membership is not fabricated', () => {
  const nodes=[1,2,3].map(sequence=>({id:'a:000'+sequence,sequence}));
  const points=nodes.map((node,index)=>({...node,value:2.2,reached:index===0?null:true,current:index>0}));
  const plot=ui.frontierPlot({available:true,points},nodes,'frontier');
  assert.deepEqual(plain(plot.points.map(point=>point.id)),['a:0002','a:0003']);
  assert.equal(plot.points.filter(point=>point.current).length,2);
});

test('Earlier unreviewed ledger results never lower the reviewed Frontier curve', () => {
  const nodes=[{id:'ledger:x',sequence:1},{id:'h:0001',sequence:2},{id:'a:0001',sequence:3}];
  const frontier={available:true,points:[{...nodes[1],value:2.3,reached:true},{...nodes[2],value:2.2,reached:true,current:true}],legacy_points:[{...nodes[0],value:1.1,reviewed:false}]};
  const plot=ui.frontierPlot(frontier,nodes,'all');
  assert.equal(plot.points.length,3);
  assert.equal(plot.points[0].reviewed,false);
  assert.deepEqual(plain(plot.curve.map(point=>point.value)),[2.3,2.2]);
  assert.equal(ui.frontierPlot(frontier,nodes,'frontier').points.length,2);
});

test('Recent-window curve carries in the earlier best rather than inventing a new baseline', () => {
  const nodes=Array.from({length:30},(_,index)=>({id:'a:'+index,sequence:index+1}));
  const points=[{...nodes[2],value:2.2,reached:true,current:true},{...nodes[27],value:2.3,reached:false}];
  const plot=ui.frontierPlot({available:true,points},nodes,'recent');
  assert.equal(plot.first,21);
  assert.equal(plot.points.length,1);
  assert.deepEqual(plain(plot.curve[0]),{sequence:21,value:2.2});
  assert.equal(plot.curve.at(-1).value,2.2);
});

test('Namespaced Git rows and local references keep colliding Study iteration IDs distinct', () => {
  const nodes=[{id:'history-1:0001',local_id:'0001',source_id:'history-1',sequence:1},{id:'active:0001',local_id:'0001',source_id:'active',sequence:2,imported_from:'history-1:0001'}];
  const graph=ui.gitLayout(nodes);
  assert.equal(Object.keys(graph.positions).length,2);
  assert.equal(graph.ordered[0].id,'active:0001');
  assert.equal(graph.positions['active:0001'].lane,graph.positions['history-1:0001'].lane);
  context.fixture={nodes,sources:[{id:'history-1'},{id:'active'}]};
  vm.runInContext("data=fixture;selected='history-1:0001';detail=null",context);
  assert.equal(ui.resolveReference('0001'),'history-1:0001');
  assert.equal(ui.resolveReference('active:0001'),'active:0001');
});

test('Inspector displays full precision and labels a repeated import as reuse despite local improvement', () => {
  const point={id:'active:0001',candidate_id:'active:0001:hash',sequence:2,value:2.2214044051580952,current:true,reached:true,improved:true,delta:1,duplicate_of:'history-1:0001'};
  context.inspectPoint=point;
  vm.runInContext('chartModel={points:[inspectPoint]};',context);
  ui.showFrontierPoint(point);
  assert.equal(elements.get('inspectValue').textContent,'2.2214044051580952 Å');
  assert.match(elements.get('inspectDelta').textContent,/不计为新测量/);
  assert.doesNotMatch(elements.get('inspectDelta').textContent,/降低约/);
});

test('Unavailable canonical Frontier never falls back to nominal score points', () => {
  const plot=ui.frontierPlot({available:false,points:[{id:'1',value:2.2,current:true}]},[{id:'1',sequence:1}],'frontier');
  assert.equal(plot.points.length,0);
  assert.equal(plot.curve.length,0);
});

const statement=text=>({text,source_ids:['s1']});
function explanationFixture(id='active:0012') {
  return {id,explanation:{status:'ready',generated_at:'2026-11-02T00:00:00Z',
    sources:[{id:'s1',path:'frozen/source.py',line_start:1,line_end:1,kind:'actual_source',text:'x = y < z'}],
    facts:{measurements:[{value:1.8843203088266018}],base_measurements:[{value:2.187434156835498}],base_experiment:'0011',evaluation:{id:'fixed-protocol',comparison_scope:'development-only',protocol:'Fixed development proteins'}},
    content:{title:'距离特征如何影响坐标更新',overview:statement('本轮增加距离信息。'),question:statement('距离特征是否有用？'),goal_connection:statement('只测了重建，未证明条件多样性。'),change:statement('增加零初始化投影。'),observations:[statement('观察到误差改善。')],supported:[statement('此协议下有收益。')],hypotheses:[statement('容量或梯度路径也可能解释。')],unmeasured:[statement('没有测量条件多样性。')],next_tests:[{...statement('固定容量后比较。'),proposed:true,not_run:true}],technical:{before:statement('原坐标系数。'),after:statement('加入投影。'),formulas:[{before_latex:'c_{ij}=W_hh_j',after_latex:'c_{ij}=W_hh_j+W_rr_{ij}',scope:'省略层与头索引，后续聚合保持原样。',variables:[{symbol_latex:'r_{ij}',meaning:'径向特征',shape:'B × L × L × 16'}],source_ids:['s1']}],flow:[{label:'径向特征',shape:'B,L,L,16',source_ids:['s1']}],unchanged:[],missing:[]},glossary:[],missing_material:[]}}};
}

test('Explanation defaults to steps, preserves per-node reading state, and separates claims', () => {
  const record=explanationFixture(), node={frontier_current:true};
  let rendered=ui.explanationPanel(record,node);
  assert.match(rendered,/本轮想弄清什么/);
  assert.match(rendered,/未证明条件多样性/);
  assert.doesNotMatch(rendered,/参数量|c_\{ij\}/);
  const state=ui.readingState(record.id);state.step=1;
  rendered=ui.explanationPanel(record,node);
  assert.match(rendered,/class="katex/);
  assert.match(rendered,/B × L × L × 16/);
  assert.match(rendered,/省略层与头索引/);
  assert.equal(ui.readingState('history-1:0012').step,0);
  state.step=3;
  rendered=ui.explanationPanel(record,node);
  assert.match(rendered,/证据支持到哪里/);
  assert.match(rendered,/仍是待区分的解释/);
  assert.match(rendered,/尚未测量/);
  state.mode='brief';
  rendered=ui.explanationPanel(record,node);
  assert.match(rendered,/1\.8843203088266018 Å/);
  assert.match(rendered,/2\.187434156835498 Å/);
  assert.match(rendered,/建议 · 本说明未执行/);
  assert.equal(ui.readingState(record.id).mode,'brief');
  state.mode='steps';state.step=4;
  rendered=ui.explanationPanel(record,node);
  assert.match(rendered,/后续轮次可能已做过相近验证/);
  assert.doesNotMatch(rendered,/建议 · 尚未执行/);
});

test('Measurement panel discloses reuse and current exclusions without turning either into improvement', () => {
  const record=explanationFixture('active:import');
  const rendered=ui.explanationMeasurements(record,{imported:true,frontier_exclusion_reason:'excluded by later Review'});
  assert.match(rendered,/不计为新的独立测量/);
  assert.match(rendered,/当前资格已排除/);
  assert.match(rendered,/fixed-protocol/);
  assert.doesNotMatch(rendered,/RMSD 降低/);
});

test('Pending, failed, stale and unpublished explanations stay honest and keep authoritative values', () => {
  for (const status of ['pending','failed','stale','unpublished','insufficient']) {
    const rendered=ui.explanationPanel({id:status,explanation:{status,reason:'<script>bad</script>'}},{value:2.21});
    assert.match(rendered,/2\.21 Å/);
    assert.doesNotMatch(rendered,/<script>|逐步讲解|证据支持到哪里/);
  }
  const coverage=ui.explanationCoverage({label:'历史',explanation:{counts:{ready:3,pending:2,failed:1},latest_published_id:'0009',covered_through_id:'0008'}});
  assert.match(coverage,/已生成 3 \/ 6/);
  assert.match(coverage,/不表示更早各轮均已覆盖/);
});

test('Local formula renderer blocks trusted HTML and catches malformed TeX', () => {
  assert.match(ui.renderMath('x_{ij}+W_r r_{ij}'),/katex-mathml/);
  assert.match(ui.renderMath('\\notARealCommand{'),/暂不能可靠显示/);
  assert.doesNotMatch(ui.renderMath('\\href{javascript:alert(1)}{x}'),/<a |href="javascript:/);
  assert.doesNotMatch(ui.markdown('![x](https://example.com/x.png) [x](data:text/html,test)'),/<img|href="data:/);
});

test('Operator recovery Plan keeps scientific origin and never masquerades as a Planner call', () => {
  context.operatorFixture={nodes:[{id:'active:0014',local_id:'0014',source_id:'active',label:'当前 #0014'},{id:'active:0015',local_id:'0015',source_id:'active'}],sources:[{id:'active'}]};
  vm.runInContext("data=operatorFixture;selected='active:0015';detail={id:selected,source_id:'active'}",context);
  const plan={kind:'operator_resume',target_iteration:'0015',source_iteration:'0014',authorization:'Authorized <script>bad</script>',source:'study/iterations/0015/operator-plan/response.json',receipt_source:'study/iterations/0015/operator-plan/operator.json',output:{decision:'investigate',base_experiment:'0014',contract:{action:'Frozen candidate',evaluation:'Fixed protocol'},markdown:'Retained operator instruction'}};
  const rendered=ui.operatorPlanPanel(plan);
  for(const text of ['恢复评测计划 · 操作员授权','没有新增 Planner 模型调用','原始研究 Plan #0014','data-node="active:0014"','Fixed protocol','operator-plan/operator.json']) assert.ok(rendered.includes(text),text);
  assert.doesNotMatch(rendered,/<script>|<h3>Planner|调用已完成/);
  const pending=ui.explanationPanel({id:'active:0015',explanation:{status:'unpublished'},plan_record:plan},{});
  assert.match(pending,/恢复评测计划 · 操作员授权/);
  assert.match(pending,/本轮尚未发布/);
  assert.doesNotMatch(pending,/未提供可确认的 Agent 调用元数据/);
});

test('Proven continuation shows the original Planner contract and separates execution authorization', () => {
  context.inheritedFixture={nodes:[{id:'history-1:0014',source_id:'history-1',label:'历史1 #0014'},{id:'history-1:0015',source_id:'history-1'}],sources:[{id:'history-1'}]};
  vm.runInContext("data=inheritedFixture;selected='history-1:0015';detail={id:selected,source_id:'history-1'}",context);
  const plan={kind:'execution_continuation',plan_iteration:'0014',source_iteration:'0014',target_iteration:'0015',plan_ref:'study/iterations/0014/plan/attempt-001/response.json',authorization:'Authorized <script>bad</script>',directive:'Submit preserved candidate',planner:{key:'plan-0',phase:'plan',role:'Planner',attempt:'attempt-001',status:'completed',accepted:true,published:true,output:{contract:{action:'Original controlled intervention',question:'Original research question'},markdown:'Original hypothesis'}}};
  const rendered=ui.continuationPlanPanel(plan);
  for(const text of ['沿用','原始 Plan','没有新增 Planner 调用','原调查已接纳的合同','原调查的调用回执，本次续跑不重复计数','Original controlled intervention','Original research question','Original hypothesis','本次续跑授权与执行指令','data-node="history-1:0014"']) assert.ok(rendered.includes(text),text);
  assert.doesNotMatch(rendered,/<script>|恢复评测计划 · 操作员授权|未提供可确认的 Agent 调用元数据/);
  const pending=ui.explanationPanel({id:'history-1:0015',explanation:{status:'unpublished'},plan_record:plan},{});
  assert.match(pending,/阅读原始研究合同与 Planner 输出/);
  assert.match(pending,/Original controlled intervention/);
});


test('Degraded accounting is unknown while retained research and interaction state survive refresh', async () => {
  vm.runInContext(`
    data={state:{iteration:1,status:'needs_attention'},nodes:[{id:'0001',route:'Retained question'}],
      accounting_available:false,backend_error:'ProTokError: Invalid research GPU charge',
      gpu_seconds:null,formal_gpu_seconds:null,research_gpu_seconds:null,debug_gpu_seconds:null,
      progress:{title:'后端核账不可用 · 需要处理',reasons:[{title:'后端读取失败',detail:'Invalid research GPU charge'}]}};
    selected='0001';following=false;tab='plan';view='routes';globalView=false;
  `, context);
  // Run the actual polling/connection update with drawing and detail I/O isolated.
  vm.runInContext(`globalThis.savedRefresh={api,renderSnapshot,loadDetail};
    api=async()=>data;renderSnapshot=()=>renderProgress();loadDetail=async()=>{};`,context);
  try { await vm.runInContext('refresh()',context); }
  finally {vm.runInContext('({api,renderSnapshot,loadDetail}=savedRefresh);delete globalThis.savedRefresh;',context);}
  assert.match(elements.get('connection').textContent,/已连接 · 后端核账不可用/);
  assert.equal(elements.get('connection').className,'amber');
  assert.equal(elements.get('gpu').textContent,'未知');
  assert.match(elements.get('statusline').innerHTML,/GPU 用量与待核账作业未知/);
  assert.doesNotMatch(elements.get('statusline').innerHTML,/无待核账 GPU 作业|正式评分 0/);
  assert.match(elements.get('reasons').innerHTML,/Invalid research GPU charge/);
  assert.match(elements.get('currentSub').textContent,/Retained question/);
  assert.deepEqual(plain(vm.runInContext('({selected,following,tab,view,globalView})',context)),
    {selected:'0001',following:false,tab:'plan',view:'routes',globalView:false});
  vm.runInContext("data=null;selected=null;following=true;tab='explanation';view='git';",context);
});


test('Settled research overruns show actual accounting without marking it unknown', () => {
  vm.runInContext(`data={state:{iteration:17,phase:'done',status:'ready'},nodes:[],
    accounting_available:true,gpu_seconds:180,formal_gpu_seconds:100,research_gpu_seconds:75,debug_gpu_seconds:5,
    research_overruns:[{iteration:'0017',allocated_seconds:60,gpu_seconds:75,overrun_seconds:15}]};`,context);
  ui.renderProgress();
  assert.equal(elements.get('gpu').textContent,'0.05 小时');
  assert.match(elements.get('statusline').innerHTML,/研究脚本 75 GPU 秒/);
  assert.match(elements.get('statusline').innerHTML,/研究 #0017 已按实际 75 GPU 秒结算（申请 60 秒，超出 15 秒）/);
  assert.doesNotMatch(elements.get('statusline').innerHTML,/核账作业未知|后端读取失败/);
  vm.runInContext('data=null;',context);
});

test('Long training separates reviewed 10k endpoints and best checkpoints with aligned metrics', () => {
  const request={source_iteration:'0003',candidate:{id:'candidate3'},nomination:{basis:'quality',reason:'test reason',evidence:['short/result.json']},recipe:{total_steps:10000,validation_split:'full',initialization:'fresh'}};
  const run={id:'L0123456789abcdef',request,status:'reviewed',phase:'done',result:{candidate_id:'candidate3',review:{markdown:'Independent knowledge',knowledge_changes:['long-run evidence'],observation_reviews:[{step:2000,valid:true,verified:true,reason:'complete'},{step:10000,valid:true,verified:true,reason:'complete'}]},execute:{artifacts:['metrics.json']},result:{curve:[{step:2000,ca_rmsd:1,tm_score:.5,lddt:.6,checkpoint:'checkpoint2k',proteins:100},{step:10000,ca_rmsd:2,tm_score:.7,lddt:.8,checkpoint:'checkpoint10k',proteins:100}],checkpoints:['checkpoint2k','checkpoint10k']}}};
  const rendered=ui.longTrainingRuns([run]);
  for(const label of ['最终 10k','曲线最优 CA RMSD','CA RMSD ↓','TM-score ↑','lDDT ↑','checkpoint2k','checkpoint10k','Independent knowledge','short/result.json','metrics.json'])assert.ok(rendered.includes(label),label);
  assert.match(rendered, /最终 10k<\/th><td>10000<\/td><td>2<\/td><td>0.7<\/td><td>0.8/);
  assert.match(rendered, /曲线最优 CA RMSD<\/th><td>2000<\/td><td>1<\/td><td>0.5<\/td><td>0.6/);
  assert.equal((rendered.match(/<svg /g)||[]).length,3);
  const pending=ui.longTrainingRuns([{...run,result:undefined,error:'<script>bad</script>'}]);
  assert.match(pending,/尚未独立 Review 发布/);
  assert.doesNotMatch(pending,/<svg |<script>|Independent knowledge|checkpoint2k/);
});

test('Long training allocations appear in accounting when no short job is outstanding', () => {
  vm.runInContext(`data={state:{},nodes:[],accounting_available:true,outstanding:[],
    formal_gpu_seconds:10,research_gpu_seconds:0,debug_gpu_seconds:0,
    long_training_gpu_seconds:80,long_training_outstanding:['/jobs/L0123456789abcdef']};`,context);
  ui.renderProgress();
  assert.match(elements.get('statusline').innerHTML,/长训练作业 L0123456789abcdef · 待核账/);
  assert.match(elements.get('statusline').innerHTML,/长训练 80 GPU 秒/);
  assert.doesNotMatch(elements.get('statusline').innerHTML,/无待核账 GPU 作业/);
  vm.runInContext('data=null;',context);
});


test('Timed-out long training retains every complete observation and reports its missing 10k endpoint', () => {
  const curve=[2000,4000,6000].map(step=>({step,proteins:4491,ca_rmsd:4-step/10000,tm_score:step/10000,lddt:.6,checkpoint:'checkpoint'+step,checkpoint_path:'/train/checkpoint-'+step+'.pt',evidence:['/validation/'+step+'/eval.csv']}));
  const run={id:'L0123456789abcdef',source_label:'历史1',request:{source_iteration:'0003'},status:'reviewed',result:{review:{observation_reviews:curve.map(point=>({step:point.step,valid:true,verified:true,reason:'full validation'}))},result:{status:'failed',terminal_state:'TIMEOUT',endpoint_complete:false,missing_validation_steps:[8000,10000],progress:{optimizer_step:6750,phase:'training'},curve}}};
  const rendered=ui.longTrainingRuns([run]);
  for(const value of ['训练未完整完成','TIMEOUT','历史1','8000, 10000','6750','缺少完整 10k 全量验证端点','全部保留的完整全量验证观测'])assert.ok(rendered.includes(value),value);
  assert.match(rendered,/曲线最优 CA RMSD<\/th><td>6000<\/td>/);
  for(const step of [2000,4000,6000]){
    assert.ok(rendered.includes('/train/checkpoint-'+step+'.pt'));
    assert.ok(rendered.includes('/validation/'+step+'/eval.csv'));
    assert.match(rendered,new RegExp('<tr><td>'+step+'</td>'));
  }
  assert.doesNotMatch(rendered, /最终 10k<\/th><td>10000/);
});

test('Rejected and unverified full observations remain visible but never become accepted best or curve', () => {
  const point=(step,value)=>({step,proteins:4491,ca_rmsd:value,tm_score:.7,lddt:.8,checkpoint:'checkpoint'+step});
  const curve=[point(2000,3),point(4000,1),point(6000,2)];
  const result={curve,endpoint_complete:false};
  const review={observation_reviews:[{step:2000,valid:true,verified:true,reason:'accepted'},{step:4000,valid:false,verified:true,reason:'wrong source <script>'},{step:6000,valid:true,verified:false,reason:'missing evidence'}]};
  const rendered=ui.longTrainingRuns([{id:'L0123456789abcdef',result:{review,result}}]);
  assert.match(rendered,/曲线最优 CA RMSD<\/th><td>2000<\/td><td>3/);
  for(const label of ['观测被拒绝','证据未核实','wrong source &lt;script&gt;','missing evidence','checkpoint4000','checkpoint6000'])assert.ok(rendered.includes(label),label);
  assert.doesNotMatch(rendered,/<script>/);
  const charts=[...rendered.matchAll(/<svg[\s\S]*?<\/svg>/g)].map(match=>match[0]).join('');
  assert.match(charts,/2000 steps/);
  assert.doesNotMatch(charts,/4000 steps|6000 steps/);
  const unreviewed=ui.longTrainingRuns([{id:'L0123456789abcdef',result:{result}}]);
  assert.match(unreviewed,/尚无逐点核查通过的观测/);
  assert.doesNotMatch(unreviewed,/<svg /);
});

test('No full-validation points still exposes collected training and log evidence without invented metrics', () => {
  const rendered=ui.longTrainingRuns([{id:'L0123456789abcdef',result:{review:{observation_reviews:[]},result:{status:'failed',terminal_state:'TIMEOUT',endpoint_complete:false,missing_validation_steps:[2000,4000,6000,8000,10000],progress:{optimizer_step:1250,phase:'validation'},curve:[],log_evidence:['/train/progress.json','/train/train.csv'],training_log_tail:'step=1250 loss=.2 <script>',stdout_tail:'TIME LIMIT & interrupted'}}}]);
  for(const value of ['尚无完整全量验证观测','1250','/train/progress.json','/train/train.csv','训练日志尾部','标准输出尾部','step=1250 loss=.2 &lt;script&gt;','TIME LIMIT &amp; interrupted'])assert.ok(rendered.includes(value),value);
  assert.doesNotMatch(rendered,/<svg |<script>|最终 10k<\/th><td>10000/);
});


test('Legacy completed curves retain explicit whole-run review labels while partial runs require point reviews', () => {
  const result={curve:[{step:2000,ca_rmsd:2,tm_score:.6,lddt:.7},{step:10000,ca_rmsd:3,tm_score:.5,lddt:.6}]};
  const published={candidate_id:'legacy',review:{candidate_reviews:[{candidate_id:'legacy',valid:true,verified:true}]},result};
  const full=ui.longTrainingRuns([{id:'L0123456789abcdef',result:published}]);
  assert.match(full,/整轮核查通过（旧完整记录）/);
  assert.match(full,/当时未单列逐点核查/);
  assert.match(full,/曲线最优 CA RMSD<\/th><td>2000<\/td>/);
  assert.match(full,/最终 10k<\/th><td>10000<\/td>/);
  const partial=ui.longTrainingRuns([{id:'L0123456789abcdef',result:{...published,result:{...result,status:'failed',endpoint_complete:false}}}]);
  assert.doesNotMatch(partial,/整轮核查通过|<svg /);
  assert.match(partial,/尚无逐点核查通过的观测/);
});


test('Live telemetry stays unreviewed, escaped and split by metric unit and conditions', () => {
  const event={name:'loss',value:.2,step:1,unit:'nats',tags:['dashboard'],context:{seed:42,scope:'dev'}};
  const stream={stream_id:'same',source_id:'active',status:'running',events:[event,{...event,step:2,value:.1},{...event,context:{seed:43,scope:'dev'}},{...event,unit:'bits'},{...event,tags:['private'],value:'SECRET'}]};
  assert.equal(ui.telemetryGroups(stream).length,3);
  const groups=ui.metricSeries([stream,{...stream,source_id:'history-1',events:[{...event,value:{nonfinite:'NaN<script>'}}]}]);
  assert.equal(groups.length,4);
  const shown=groups.map(group=>ui.metricCard(group,{pinned:new Map()})).join('');
  for(const text of ['loss','nats','NaN&lt;script&gt;'])assert.ok(shown.includes(text),text);
  assert.match(html,/dashboard 标签 · 尚未评审/);
  assert.doesNotMatch(shown,/<script>|SECRET/);
  assert.equal((shown.match(/<svg /g)||[]).length,3);
  assert.equal(ui.metricSeries([{stream_id:'empty',events:[]}]).length,0);
});

function concurrentTasks() {
  return [
    {id:'active:0001',label:'当前 #0001',task_sequence:1,task_kind:'investigation',task_status:'completed',published:true,phase:'done',source_id:'active'},
    {id:'active:long:L0123456789abcdef',label:'当前 长训 L0123456789abcdef',task_sequence:2,task_kind:'long_training',validation_of:'active:0001',task_status:'running',status:'running',phase:'execute',source_id:'active',progress:{title:'Long task execution',message:'Waiting for its own job',stages:[]}},
    {id:'active:0002',label:'当前 #0002',task_sequence:3,task_kind:'research_script',task_status:'running',status:'running',phase:'plan',source_id:'active'},
    {id:'active:long:L1111111111111111',task_sequence:4,task_kind:'long_training',validation_of:'active:0001',task_status:'needs_attention',status:'needs_attention',phase:'execute',source_id:'active'},
    {id:'history-1:long:L0123456789abcdef',task_sequence:5,task_kind:'long_training',task_status:'running',archived:true,source_id:'history-1'},
  ];
}

test('Work tree shows concurrent lifecycles and labels long validation as its own relationship', () => {
  const tasks=concurrentTasks();context.taskFixture={tasks,nodes:tasks.filter(task=>task.task_kind!=='long_training'),state:{iteration:2},active_node_id:'active:0002'};
  tasks[0].frontier_exclusion_reason='invalidated';tasks[0].imported=true;
  vm.runInContext("data=taskFixture;selected='active:long:L0123456789abcdef';following=false;",context);
  elements.get('taskFilter').value='';
  const graph=ui.gitLayout(tasks);
  assert.notEqual(graph.positions[tasks[1].id].lane,graph.positions[tasks[0].id].lane);
  assert.equal(ui.graphParent(tasks[1]),tasks[0].id);
  ui.drawGraph();
  const markup=elements.get('graph').innerHTML;
  assert.match(markup,/长训验证：active:0001 → active:long:L0123456789abcdef/);
  assert.match(markup,/data-node="active:long:L0123456789abcdef"/);
  assert.match(markup,/data-node="active:0002"/);
  assert.match(markup,/● 进行中/);
  assert.match(markup,/归档 · ● 进行中/);
  assert.match(markup,/资格已排除 · 历史导入 · 已完成/);
  assert.doesNotMatch(markup,/代码继承：#undefined → #active:long/);
  vm.runInContext('data=null;selected=null;',context);
});

test('Running-task filters retain source and selected completed task without counting archived activity', () => {
  const tasks=concurrentTasks();context.taskFixture={tasks,nodes:tasks.filter(task=>task.task_kind!=='long_training'),state:{iteration:2},active_node_id:'active:0002'};
  vm.runInContext("data=taskFixture;selected='active:long:L0123456789abcdef';following=false;",context);
  elements.get('taskFilter').value='running';
  assert.deepEqual(plain(ui.visibleTasks().map(task=>task.id)),tasks.slice(0,3).map(task=>task.id));
  ui.renderProgress();
  assert.equal(elements.get('current').textContent,'2 项进行中');
  assert.equal(elements.get('now').textContent,'Long task execution');
  assert.match(elements.get('intent').textContent,/Waiting for its own job/);
  assert.doesNotMatch(elements.get('phases').innerHTML,/路线选择/);
  tasks[1].task_status='completed';tasks[1].published=true;
  assert.deepEqual(plain(ui.visibleTasks().map(task=>task.id)),tasks.slice(0,3).map(task=>task.id));
  ui.renderProgress();
  assert.equal(elements.get('current').textContent,'1 项进行中');
  assert.equal(vm.runInContext('selected',context),tasks[1].id);
  elements.get('taskFilter').value='needs_attention';
  assert.ok(ui.visibleTasks().some(task=>task.id===tasks[3].id));
  assert.ok(ui.visibleTasks().some(task=>task.id===tasks[0].id));
  elements.get('taskFilter').value='';
  vm.runInContext('data=null;selected=null;following=true;',context);
});

test('Unreviewed long observations keep their evidence state and do not invent the final endpoint', () => {
  const rendered=ui.longTrainingRuns([{id:'L0123456789abcdef',status:'running',phase:'review',request:{source_iteration:'0001'},observations:{curve:[{step:2000,ca_rmsd:2.0123456789,tm_score:.5,lddt:.6,checkpoint:'checkpoint2k'}]}}]);
  assert.match(rendered,/未评审观测/);
  assert.match(rendered,/2.0123456789/);
  assert.match(rendered,/尚无逐点评审/);
  assert.doesNotMatch(rendered,/<svg |曲线最优 CA RMSD<\/th><td>2000/);
  assert.doesNotMatch(rendered,/最终 10k<\/th><td>10000/);
});

test('Native telemetry groups scientific conditions independently of transport cursors', () => {
  const point={name:'native/training',value:.2,step:10,tags:['dashboard'],context:{scope:'training',source:'/train.csv',remote_start_offset:10,remote_end_offset:20}};
  const stream={source:{kind:'fixed-short'},events:[point,{...point,step:20,value:.1,context:{...point.context,remote_start_offset:20,remote_end_offset:30}}]};
  assert.equal(ui.telemetryGroups(stream).length,1);
  assert.equal(ui.telemetryGroups(stream)[0].length,2);
  stream.events.push({...point,context:{...point.context,scope:'full'}});
  assert.equal(ui.telemetryGroups(stream).length,2);
});

test('Task metric scope separates source, parent investigation and independent long run', () => {
  const short=ui.metricTask({id:'active:0001',source_id:'active',local_id:'0001'});
  const stream={source_id:'active',run_id:null,source:{experiment_id:'0001'}};
  assert.equal(ui.metricMatches(stream,short),true);
  assert.equal(ui.metricMatches({...stream,source_id:'history-1'},short),false);
  assert.equal(ui.metricMatches({...stream,run_id:'L0123456789abcdef'},short),false);
  assert.equal(ui.metricMatches({...stream,source:{experiment_id:'0002',parent_iteration:'0001'}},short),false);
  const long=ui.metricTask({id:'active:long:L0123456789abcdef',source_id:'active',local_id:'L0123456789abcdef',task_kind:'long_training'});
  assert.equal(ui.metricMatches({...stream,run_id:'L0123456789abcdef'},long),true);
  assert.equal(ui.metricMatches(stream,long),false);
  assert.equal(ui.metricTask({id:'ledger:hash',source_id:'ledger'}),null);
});

test('Native numeric fields become precise scalar series without interpreting arrays as histograms', () => {
  const leaves=ui.metricLeaves({'loss':.123456789012345,'a/b':{'x~y':2},array:[1,2],flag:true,broken:{nonfinite:'NaN'}});
  assert.deepEqual(plain(leaves.map(leaf=>leaf.path)),[['loss'],['a~1b','x~0y'],['broken']]);
  assert.equal(leaves[0].value,.123456789012345);
  const base={name:'native/training',value:{loss:.2},step:10,time:100,tags:['dashboard'],context:{scope:'training',remote_start_offset:10,remote_end_offset:20}};
  const series=ui.metricSeries([{source_id:'active',stream_id:'native',source:{kind:'fixed-short'},events:[base,{...base,step:20,value:{loss:.1},context:{...base.context,remote_start_offset:20,remote_end_offset:30}},{...base,unit:'different'}, {...base,context:{...base.context,scope:'validation'}},{...base,tags:['internal'],value:{secret:999}}]}]);
  assert.equal(series.length,3);
  assert.equal(series[0].name,'native/training/loss');
  assert.equal(series[0].points.length,2);
  assert.doesNotMatch(series.map(item=>ui.metricCard(item,{pinned:new Map()})).join(''),/secret/);
  const alias=ui.metricSeries([{stream_id:'aliases',events:[{...base,name:'loss/part',value:1},{...base,name:'loss',value:{part:2}}]}]);
  assert.equal(alias.length,2,'a scalar observation name and an object field cannot merge');
});

test('Missing values and step resets break plotted segments while missing steps use a time axis', () => {
  const event={name:'loss',step:1,time:100,value:1,tags:['dashboard'],context:{}};
  const series=ui.metricSeries([{stream_id:'gaps',events:[event,{...event,step:2,value:{nonfinite:'NaN'}},{...event,step:3,value:3},{...event,step:2,value:2},{...event,step:3,value:2.5},{...event,step:null,time:105,value:.5}]}]);
  assert.equal(series.length,2);
  assert.equal(series[0].axis,'time');
  const stepped=series.find(item=>item.axis==='step'),chart=ui.metricPlot(stepped);
  assert.equal((chart.match(/<polyline /g)||[]).length,3);
  assert.doesNotMatch(chart,/NaN|Infinity/);
  const view={pinned:new Map([[stepped.key,stepped.points[0]]])};
  const pinned=ui.metricCard(stepped,view);
  assert.match(pinned,/data-metric-value>1<\/div>/);
  assert.match(pinned,/已固定 · Step 1/);
  for(const values of [[1,null,3],[{loss:1},{loss:null},{loss:3}]]) {
    const missing=ui.metricSeries([{stream_id:'explicit-null',events:values.map((value,index)=>({...event,value,step:index+1}))}])[0];
    assert.equal(missing.points.length,3);
    assert.equal((ui.metricPlot(missing).match(/<polyline /g)||[]).length,2,'explicit null must split a scalar curve');
  }
});

test('Recent task metric cache deduplicates refresh and never carries values across task selection', () => {
  const first={id:'active:0001',source_id:'active',local_id:'0001'},second={id:'active:0002',source_id:'active',local_id:'0002'};
  const event={id:'a',offset:0,end_offset:10,name:'loss',value:.2,step:1,time:100,tags:['dashboard'],context:{}};
  const stream={source_id:'active',run_id:null,stream_id:'same',source:{experiment_id:'0001',source_identity:'s',execution_identity:'e'},status:'live',updated_at:1,events:[event]};
  ui.mergeTaskMetrics(first,[stream]);
  let merged=ui.mergeTaskMetrics(first,[stream,{...stream,updated_at:3,status:'sync_incomplete',events:[{...event,id:'b',offset:10,end_offset:20,value:.1,step:2}]}]);
  assert.equal(merged[0].events.length,2);
  merged=ui.mergeTaskMetrics(first,[stream]);
  assert.equal(merged[0].status,'sync_incomplete');
  const selected=ui.mergeTaskMetrics(second,[{...stream,source:{...stream.source,experiment_id:'0002'},events:[{...event,value:999}]}]);
  assert.equal(selected.length,1);
  assert.deepEqual(plain(selected[0].events.map(point=>point.value)),[999]);
  assert.equal(ui.mergeTaskMetrics(second,[stream]).length,1,'another experiment cannot add a stream');
});

test('A settled partial long task exposes progress and logs before review without accepted curves', () => {
  const rendered=ui.longTrainingRuns([{id:'L0123456789abcdef',status:'running',phase:'review',observations:{status:'failed',terminal_state:'TIMEOUT',endpoint_complete:false,missing_validation_steps:[8000,10000],progress:{optimizer_step:6750},training_log_tail:'retained loss=.2',curve:[{step:2000,ca_rmsd:2,tm_score:.5,lddt:.6,checkpoint:'checkpoint2k'}]}}]);
  for(const text of ['TIMEOUT','6750','8000, 10000','retained loss=.2','checkpoint2k','未完整完成 · 未评审','尚无逐点评审'])assert.ok(rendered.includes(text),text);
  assert.doesNotMatch(rendered,/<svg |未完整完成 · 已评审/);
});

test('Selecting a long task loads its task URL instead of falling back to an iteration URL', async () => {
  const task={...concurrentTasks()[1],detail_url:'/api/task/active/long/L0123456789abcdef'};
  context.taskFixture={tasks:[task],nodes:[],state:{}};
  context.requestedTaskPaths=[];
  vm.runInContext(`data=taskFixture;selected=taskFixture.tasks[0].id;detail=null;globalView=false;
    globalThis.savedTaskIO={api,renderDetailHeader,renderDetail};
    api=async path=>{requestedTaskPaths.push(path);return {task_kind:'long_training',long_training:[]};};
    renderDetailHeader=()=>{};renderDetail=()=>{};`,context);
  try {
    await ui.loadDetail(true);
    assert.deepEqual(plain(context.requestedTaskPaths),[task.detail_url]);
    assert.equal(vm.runInContext('detail.id',context),task.id);
  } finally {
    vm.runInContext('({api,renderDetailHeader,renderDetail}=savedTaskIO);delete globalThis.savedTaskIO;data=null;detail=null;selected=null;',context);
  }
});

test('Task detail refresh and task switching retain each task reading position and disclosures', () => {
  const prior=elements.get('detailBody');let markup='',folds=[];
  const body={scrollTop:0,querySelectorAll:selector=>selector==='details'?folds:[],
    get innerHTML(){return markup;},set innerHTML(value){markup=value;folds=[...value.matchAll(/<details data-disclosure="([^"]+)"([^>]*)>/g)].map(match=>({dataset:{disclosure:match[1]},open:/\sopen(?:\s|$)/.test(match[2])}));}};
  elements.set('detailBody',body);
  const tasks=concurrentTasks();
  context.taskFixture={tasks,nodes:[],state:{}};
  context.longDetail={id:tasks[1].id,task_kind:'long_training',long_training:[{id:'L0123456789abcdef',status:'reviewed',request:{source_iteration:'0001'},result:{review:{markdown:'Independent result'},result:{curve:[]}}}]};
  try {
    vm.runInContext("data=taskFixture;selected=longDetail.id;detail=longDetail;tab='explanation';globalView=false;",context);
    ui.renderDetail();
    let fold=folds.find(item=>item.dataset.disclosure==='long-review-L0123456789abcdef');
    fold.open=true;body.scrollTop=173;
    context.longDetail.long_training[0].result.review.markdown='Updated independent result';
    ui.renderDetail();
    assert.equal(folds.find(item=>item.dataset.disclosure===fold.dataset.disclosure).open,true);
    assert.equal(body.scrollTop,173);
    assert.match(markup,/Updated independent result/);
    vm.runInContext("selected='active:0002';detail=null;",context);ui.renderDetail();
    vm.runInContext("detail={id:selected,task_kind:'long_training',long_training:[]};",context);ui.renderDetail();
    assert.equal(body.scrollTop,0,'a different task starts at its own reading position');
    vm.runInContext('selected=longDetail.id;detail=longDetail;',context);ui.renderDetail();
    assert.equal(body.scrollTop,173);
    assert.equal(folds.find(item=>item.dataset.disclosure===fold.dataset.disclosure).open,true);
  } finally {
    elements.set('detailBody',prior);
    vm.runInContext('data=null;detail=null;selected=null;renderedDetailKey=null;detailReading.clear();',context);
  }
});

test('Manual task aliases preserve experiment links and current follow identity', () => {
  context.manualView={state:{iteration:1},nodes:[{id:'active:0001'}],active_node_id:'active:manual:operator-check',tasks:[{id:'active:manual:operator-check',experiment_id:'active:0001',task_origin:'user',task_kind:'investigation'}]};
  vm.runInContext('data=manualView;',context);
  assert.equal(ui.resolveReference('active:0001'),'active:manual:operator-check');
  assert.equal(vm.runInContext('activeNode().id',context),'active:manual:operator-check');
  assert.equal(vm.runInContext('taskKind(data.tasks[0])',context),'人工调查');
  assert.equal(vm.runInContext('taskKind({...data.tasks[0],task_kind:"long_training"})',context),'人工长训');
  assert.equal(vm.runInContext('taskStateLabel({task_status:"cancelled"})',context),'已取消');
  assert.match(vm.runInContext('taskStateLabel({task_status:"expired"})',context),/过期/);
  vm.runInContext('data=null;',context);
});

test('Manual long metrics use the stable task selector and isolate all other lanes', async () => {
  const node={id:'active:manual:manual-check',source_id:'active',local_id:'manual-check',manual_task_id:'manual-check',task_kind:'long_training'};
  const task=ui.metricTask(node),stream={source_id:'active',manual_task_id:'manual-check',run_id:null,source:{experiment_id:'0001'}};
  assert.deepEqual(plain(task),{source:'active',task:'manual-check',run:null,experiment:null});
  assert.equal(ui.metricMatches(stream,task),true);
  assert.equal(ui.metricMatches({...stream,source_id:'history-1'},task),false);
  assert.equal(ui.metricMatches({...stream,manual_task_id:'another-task'},task),false);
  assert.equal(ui.metricMatches({...stream,manual_task_id:null,run_id:'L0123456789abcdef'},task),false);
  assert.equal(ui.metricMatches(stream,ui.metricTask({source_id:'active',local_id:'0001'})),false);
  context.manualMetricNode=node;context.manualMetricQueries=[];
  vm.runInContext(`data={tasks:[manualMetricNode]};selected=manualMetricNode.id;globalView=false;
    globalThis.savedManualMetricIO={api,renderTaskMetrics};
    api=async path=>{manualMetricQueries.push(path);return {streams:[]};};renderTaskMetrics=()=>{};`,context);
  try {
    await ui.loadTaskMetrics();
    assert.deepEqual(plain(context.manualMetricQueries),['/api/telemetry?source=active&task=manual-check']);
    assert.equal(ui.metricTask({task_kind:'investigation',source_id:'active',local_id:'0001',manual_task_id:'investigate',experiment_id:'active:0001'}).experiment,'0001');
  } finally {
    vm.runInContext('({api,renderTaskMetrics}=savedManualMetricIO);delete globalThis.savedManualMetricIO;data=null;selected=null;',context);
  }
});
