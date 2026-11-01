const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements = new Map();
const document = {getElementById(id) {
  if (!elements.has(id)) elements.set(id, {querySelectorAll: () => [], setAttribute() {}});
  return elements.get(id);
}};
// Exercise actual rendering functions without a browser, network, or polling loop.
const context = vm.createContext({document, fetch: () => new Promise(() => {}), setTimeout() {}});
vm.runInContext(script+'\nglobalThis.ui = {gitLayout, markdown, structured, agentCard, readingField, frontierPlot, axisValue, exactScore, showFrontierPoint, resolveReference};', context);
const ui = context.ui;
const plain = value => JSON.parse(JSON.stringify(value));

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
