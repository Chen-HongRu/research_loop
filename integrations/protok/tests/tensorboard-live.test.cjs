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
