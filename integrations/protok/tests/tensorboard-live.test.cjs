const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const script=fs.readFileSync(path.join(__dirname,'../assets/tensorboard-live.js'),'utf8');
function initialize(initial={}) {
  const values=new Map(Object.entries(initial));
  vm.runInNewContext(script,{window:{localStorage:{getItem:key=>values.get(key) ?? null,setItem:(key,value)=>values.set(key,value)}}});
  return values;
}
test('TensorBoard first visit enables native browser reload without changing its period',()=>{
  const settings=JSON.parse(initialize().get('_tb_global_settings'));
  assert.deepEqual(settings,{autoReload:true});
});
test('TensorBoard current and legacy explicit preferences remain authoritative',()=>{
  for(const key of ['_tb_global_settings','_tb_global_settings.timeseries']) {
    const original=JSON.stringify({autoReload:false,autoReloadPeriodInMs:60000,unrelated:'keep'});
    const values=initialize({[key]:original});
    assert.equal(values.get(key),original);
    if(key.endsWith('timeseries'))assert.equal(values.has('_tb_global_settings'),false);
  }
  const existing=JSON.stringify({autoReloadPeriodInMs:60000,unrelated:'keep'});
  assert.deepEqual(JSON.parse(initialize({'_tb_global_settings':existing}).get('_tb_global_settings')),{autoReloadPeriodInMs:60000,unrelated:'keep',autoReload:true});
});
test('Disabled or malformed storage cannot break TensorBoard startup',()=>{
  for(const value of ['invalid','[]','null'])assert.equal(initialize({'_tb_global_settings':value}).get('_tb_global_settings'),value);
  assert.doesNotThrow(()=>vm.runInNewContext(script,{window:{get localStorage(){throw Error('blocked');}}}));
});

function page(url) {
  const calls=[], opens=[], values=new Map();
  function XHR() {}
  XHR.prototype.open=function(...args){opens.push(args);};
  const window={location:{href:url},Request,XMLHttpRequest:XHR,
    localStorage:{getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value)},
    fetch(...args){calls.push(args);return Promise.resolve('response');}};
  vm.runInNewContext(script,{window,URL,URLSearchParams});
  return {window,calls,opens,values};
}
const taskURL=task=>'http://example.invalid/tensorboard/?dashboard_task='+encodeURIComponent(JSON.stringify(task))+'#scalars';

test('Each page scopes fetch and XHR metadata and curves independently',async()=>{
  const first={source:'active',task:'manual-a'},second={source:'history-1',experiment:'0026'};
  const a=page(taskURL(first)),b=page(taskURL(second));
  for(const [view,task] of [[a,first],[b,second]]) {
    const init={method:'POST',body:'original request bytes'};
    await view.window.fetch('/tensorboard/experiment/default/data/plugin/timeseries/timeSeries',init);
    const [url,sent]=view.calls.at(-1);
    assert.deepEqual(JSON.parse(new URL(url).searchParams.get('dashboard_task')),task);
    assert.equal(sent,init);
    const xhr=new view.window.XMLHttpRequest();
    xhr.open('GET','/tensorboard/data/plugin/scalars/tags',true);
    assert.deepEqual(JSON.parse(new URL(view.opens[0][1]).searchParams.get('dashboard_task')),task);
    assert.equal(view.opens[0][2],true);
    assert.ok(![...view.values.keys()].some(key=>key.includes('task')),'scope is never shared through storage');
  }
  for(const url of ['https://example.org/data/runs','/assets/tensorboard-live.js','/api/snapshot']) {
    await a.window.fetch(url);assert.equal(a.calls.at(-1)[0],url);
  }
});

test('Request objects preserve method, headers and body when scope is attached',async()=>{
  const view=page(taskURL({source:'active',experiment:'0026'}));
  const input=new Request('http://example.invalid/tensorboard/data/plugin/timeseries/timeSeries',{method:'POST',headers:{'X-Example':'keep'},body:'retained body'});
  await view.window.fetch(input);
  const request=view.calls[0][0];
  assert.equal(request.method,'POST');assert.equal(request.headers.get('X-Example'),'keep');
  assert.equal(await request.text(),'retained body');
  assert.ok(new URL(request.url).searchParams.has('dashboard_task'));
});

test('Existing generated task links upgrade but arbitrary regexes stay global',async()=>{
  for(const [filter,task] of [
    ['^active/manual/manual-a/',{source:'active',task:'manual-a'}],
    ['^history-1/#0026(?:$| \\(attempt [0-9]+\\)|/)',{source:'history-1',experiment:'0026'}],
    ['^active/long/L0123456789abcdef(?:$| \\(attempt [0-9]+\\)|/)',{source:'active',run:'L0123456789abcdef'}],
  ]) {
    const view=page('http://example.invalid/tensorboard/#scalars&runFilter='+encodeURIComponent(filter));
    await view.window.fetch('/tensorboard/data/runs');
    assert.deepEqual(JSON.parse(new URL(view.calls[0][0]).searchParams.get('dashboard_task')),task);
  }
  const global=page('http://example.invalid/tensorboard/#scalars&runFilter=.*');
  await global.window.fetch('/tensorboard/data/runs');
  assert.equal(global.calls[0][0],'/tensorboard/data/runs');
  const invalid=page('http://example.invalid/tensorboard/?dashboard_task=%7Bbad#scalars');
  await invalid.window.fetch('/tensorboard/data/runs');
  assert.equal(new URL(invalid.calls[0][0]).searchParams.get('dashboard_task'),'{bad');
});


test('Prefixed TensorBoard keeps native requests and task scope within its public path',async()=>{
  const task={source:'active',experiment:'0001'};
  const view=page(taskURL(task).replace('/tensorboard/','/fm/tensorboard/'));
  await view.window.fetch('/tensorboard/data/plugin/scalars/tags');
  const first=new URL(view.calls.at(-1)[0]);
  assert.equal(first.pathname,'/fm/tensorboard/data/plugin/scalars/tags');
  assert.deepEqual(JSON.parse(first.searchParams.get('dashboard_task')),task);
  await view.window.fetch('/fm/tensorboard/data/plugin/scalars/tags');
  assert.equal(new URL(view.calls.at(-1)[0]).pathname,'/fm/tensorboard/data/plugin/scalars/tags');
  const global=page('http://example.invalid/fm/tensorboard/#scalars');
  await global.window.fetch('/tensorboard/data/runs');
  assert.equal(new URL(global.calls.at(-1)[0]).pathname,'/fm/tensorboard/data/runs');
});
