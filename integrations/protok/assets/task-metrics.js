'use strict';

// These are presentation caches. Source telemetry and Review boundaries stay unchanged.
const metricViews = new Map();
let metricCache = {id:null, streams:new Map()}, metricRequest = 0, metricRenderedFor = null, metricCardSignature = '';
function metricStable(value) {
  return Array.isArray(value)?value.map(metricStable):value&&typeof value==='object'?Object.fromEntries(Object.keys(value).sort().map(key=>[key,metricStable(value[key])])):value ?? null;
}
const metricJSON = value => JSON.stringify(metricStable(value));

function metricTask(node) {
  if(!node || node.source_id==='ledger')return null;
  const source=node.source_id || 'active', local=node.local_id || String(node.id).split(':').at(-1);
  const long=node.task_kind==='long_training';
  if(long && node.manual_task_id)return {source,task:node.manual_task_id,run:null,experiment:null};
  if(!long && !/^\d{4,}$/.test(local))return null;
  return {source,run:long?local:null,experiment:long?null:local};
}
function metricMatches(stream, task) {
  return !!task && stream.source_id===task.source && (task.task
    ? stream.manual_task_id===task.task
    : !stream.manual_task_id && (task.run
    ? stream.run_id===task.run
    : !stream.run_id && stream.source?.experiment_id===task.experiment));
}
function metricView(id) {
  if(!metricViews.has(id))metricViews.set(id,{filter:'',pinned:new Map(),error:null});
  return metricViews.get(id);
}
function mergeTaskMetrics(node, streams) {
  if(metricCache.id!==node.id)metricCache={id:node.id,streams:new Map()};
  const task=metricTask(node);
  for(const stream of streams || []) {
    if(!metricMatches(stream,task))continue;
    const key=metricJSON([stream.source_id,stream.manual_task_id,stream.run_id,stream.stream_id,stream.source?.source_identity,stream.source?.execution_identity].map(value=>value ?? null));
    const previous=metricCache.streams.get(key), events=new Map();
    for(const event of [...(previous?.events || []),...(stream.events || [])]) {
      if(!Array.isArray(event.tags) || !event.tags.includes('dashboard'))continue;
      events.set(event.end_offset ?? event.id ?? metricJSON(event),event);
    }
    const rows=[...events.values()].sort((a,b)=>(a.offset ?? a.time ?? 0)-(b.offset ?? b.time ?? 0)).slice(-256);
    const metadata=(previous?.updated_at || 0)>(stream.updated_at || 0)?previous:stream;
    metricCache.streams.set(key,{...(previous || {}),...metadata,key,events:rows});
  }
  return [...metricCache.streams.values()];
}
function metricLeaves(value, path=[]) {
  if(value===null)return [{path,value:null,invalid:'缺失'}];
  if(typeof value==='number')return [{path,value:Number.isFinite(value)?value:null,invalid:Number.isFinite(value)?null:String(value)}];
  if(value && !Array.isArray(value) && typeof value==='object') {
    if(typeof value.nonfinite==='string' && Object.keys(value).length===1)return [{path,value:null,invalid:value.nonfinite}];
    return Object.entries(value).flatMap(([key,item])=>metricLeaves(item,[...path,key.replace(/~/g,'~0').replace(/\//g,'~1')]));
  }
  return [];
}
function metricSeries(streams) {
  const result=[];
  for(const stream of streams) {
    const groups=new Map();
    for(const events of telemetryGroups(stream)) {
      for(const event of events)for(const leaf of metricLeaves(event.value)) {
        const name=event.name+(leaf.path.length?'/'+leaf.path.join('/'):'');
        const axis=typeof event.step==='number'&&Number.isFinite(event.step)?'step':'time';
        const transport=new Set(['remote_start_offset','remote_end_offset','raw_fragment_bytes','raw_fragment_complete','csv_discard_until_newline']);
        const conditions=stream.source?.kind==='fixed-short'?Object.fromEntries(Object.entries(event.context || {}).filter(([key])=>!transport.has(key))):event.context || {};
        const key=metricJSON([stream.key || [stream.source_id,stream.run_id,stream.stream_id],event.name,leaf.path,event.unit ?? null,conditions,axis]);
        if(!groups.has(key))groups.set(key,{key,name,observation:event.name,field:leaf.path,axis,unit:event.unit,conditions,stream,points:[]});
        groups.get(key).points.push({...leaf,event,x:axis==='step'?event.step:event.time});
      }
    }
    result.push(...groups.values());
  }
  return result.sort((a,b)=>(b.points.at(-1).event.time || 0)-(a.points.at(-1).event.time || 0));
}
function metricPlot(series) {
  const valid=series.points.filter(point=>point.value!==null&&Number.isFinite(point.x));
  if(!valid.length)return '<div class="metric-empty">尚无可绘制的有限数值</div>';
  const W=440,H=150,L=55,R=14,T=16,B=30;
  const xs=valid.map(point=>point.x),ys=valid.map(point=>point.value),x0=Math.min(...xs),x1=Math.max(...xs);
  let y0=Math.min(...ys),y1=Math.max(...ys);const pad=(y1-y0)*.12 || Math.abs(y1)*.025 || .025;y0-=pad;y1+=pad;
  const x=value=>L+(value-x0)/(x1-x0 || 1)*(W-L-R),y=value=>H-B-(value-y0)/(y1-y0)*(H-T-B);
  const segments=[];let segment=[],last=null;
  for(const point of series.points) {
    if(point.value===null || !Number.isFinite(point.x) || last!==null&&point.x<last) {
      if(segment.length)segments.push(segment);segment=[];
    }
    if(point.value!==null&&Number.isFinite(point.x)){segment.push(point);last=point.x;}else last=null;
  }
  if(segment.length)segments.push(segment);
  const axis=value=>series.axis==='step'?String(value):new Date(value*1000).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
  return '<svg class="metric-plot" viewBox="0 0 '+W+' '+H+'" role="group" aria-label="'+esc(series.name)+' 实时标量曲线">'+
    '<line x1="'+L+'" x2="'+(W-R)+'" y1="'+(H-B)+'" y2="'+(H-B)+'" stroke="#435269"/>'+
    [y0,y1].map(value=>'<text x="3" y="'+y(value)+'" fill="#a0afc5" font-size="10">'+esc(Number(value.toPrecision(4)))+'</text>').join('')+
    '<text x="'+L+'" y="'+(H-7)+'" fill="#a0afc5" font-size="10">'+esc(axis(x0))+'</text><text x="'+(W-R)+'" text-anchor="end" y="'+(H-7)+'" fill="#a0afc5" font-size="10">'+esc(axis(x1))+'</text>'+
    segments.map(points=>'<polyline fill="none" stroke="#e5bd72" stroke-width="2" points="'+points.map(point=>x(point.x)+','+y(point.value)).join(' ')+'"/>').join('')+
    valid.map(point=>{const index=series.points.indexOf(point),label=series.name+' = '+String(point.value)+' '+(series.unit || '')+' · '+(series.axis==='step'?'Step '+point.event.step:clock(point.event.time));return '<circle cx="'+x(point.x)+'" cy="'+y(point.value)+'" r="3.5" fill="#e5bd72" tabindex="0" role="button" aria-label="'+esc(label)+'" data-metric-point="'+index+'"><title>'+esc(label)+'</title></circle>';}).join('')+'</svg>';
}
function metricPointText(series, point) {
  return (point.value===null?point.invalid:String(point.value))+(series.unit?' '+series.unit:'');
}
function metricCard(item, view) {
  const pinned=view.pinned.get(item.key), point=pinned || item.points.at(-1);
  return '<article class="metric-card" data-metric-key="'+esc(item.key)+'"><div class="toolbar between"><h4>'+esc(item.name)+'</h4><button class="metric-latest"'+(!pinned?' hidden':'')+'>跟随最新</button></div><div class="metric-value" data-metric-value>'+esc(metricPointText(item,point))+'</div><div class="caption" data-metric-position>'+esc((pinned?'已固定 · ':'最新 · ')+(item.axis==='step'?'Step '+point.event.step:clock(point.event.time)))+'</div>'+metricPlot(item)+'<p class="caption">'+esc(item.axis==='step'?'横轴 Step':'横轴为上报时间 · 未记录 Step')+' · '+esc(item.stream.stream_id)+'</p>'+disclosure('metric-conditions-'+encodeURIComponent(item.key),'单位、条件与执行来源',facts({observation:item.observation,field:item.field,unit:item.unit,conditions:item.conditions,source_identity:item.stream.source?.source_identity,execution_identity:item.stream.source?.execution_identity},['observation','field','unit','conditions','source_identity','execution_identity']))+'</article>';
}
function renderTaskMetrics() {
  const node=taskNodes().find(item=>item.id===selected), task=metricTask(node), panel=$('taskMetrics');
  panel.hidden=globalView || !task;
  if(!task)return;
  const view=metricView(node.id), body=$('taskMetricCards');
  const opened=metricRenderedFor===node.id?new Map([...body.querySelectorAll('details')].map(item=>[item.dataset.disclosure,item.open])):new Map();
  const streams=mergeTaskMetrics(node,data.telemetry?.streams), series=metricSeries(streams);
  const options='<option value="">进度概览（最多 6 项）</option>'+series.map(item=>{
    const conditions=Object.entries(item.conditions).filter(([key,value])=>!['source','csv_header'].includes(key)&&typeof value!=='object').map(([key,value])=>(LABELS[key] || key)+'='+String(value)).join('，');
    return '<option value="'+esc(item.key)+'">'+esc(item.name+(item.unit?' · '+item.unit:'')+' · '+item.stream.stream_id+(conditions?' · '+conditions:'')+' · '+(item.axis==='step'?'Step':'上报时间'))+'</option>';
  }).join('');
  const filter=$('taskMetricFilter');if(filter.innerHTML!==options)filter.innerHTML=options;
  filter.value=series.some(item=>item.key===view.filter)?view.filter:'';
  const chosen=filter.value?series.filter(item=>item.key===filter.value):series.slice(0,6);
  const enabled=data.tensorboard?.enabled===true;
  $('tensorboardLink').hidden=!enabled;$('tensorboardDisabled').hidden=enabled;
  $('tensorboardLink').href=enabled?'/tensorboard/':'#';
  const statuses=[...new Set(streams.map(stream=>({live:'持续回传',running:'回传中',synced:'已同步',waiting:'等待回传',error:'同步异常',integrity_error:'来源待核对',sync_incomplete:'回传未完成'}[stream.status] || stream.status || '状态未记录')))];
  $('taskMetricStatus').textContent=view.error || (streams.length?streams.length+' 条来源 · '+statuses.join(' / ')+(streams.some(stream=>stream.sync_error||stream.error_count)?' · 部分记录缺失或异常，保留已有观测':''):'所选任务尚未回传 dashboard 标签观测');
  $('taskMetricStatus').className='caption '+(view.error||streams.some(stream=>stream.sync_error)?'amber':'');
  filter.onchange=()=>{view.filter=filter.value;renderTaskMetrics();};
  const signature=metricJSON([node.id,filter.value,chosen.map(item=>[item.key,item.points.map(point=>[point.x,point.value,point.invalid,point.event.time,point.event.end_offset])]),[...view.pinned]]);
  if(metricRenderedFor===node.id && metricCardSignature===signature)return;
  body.innerHTML=chosen.map(item=>metricCard(item,view)).join('') || '<div class="metric-empty">尚无标量曲线。非标量和未打 dashboard 标签的完整记录可在 TensorBoard 查看。</div>';
  for(const item of body.querySelectorAll('details'))if(opened.has(item.dataset.disclosure))item.open=opened.get(item.dataset.disclosure);
  for(const card of body.querySelectorAll('[data-metric-key]')) {
    const item=chosen.find(series=>series.key===card.dataset.metricKey);
    const readout=(point,pinned)=>{
      card.querySelector('[data-metric-value]').textContent=metricPointText(item,point);
      card.querySelector('[data-metric-position]').textContent=(pinned?'已固定 · ':'浏览 · ')+(item.axis==='step'?'Step '+point.event.step:clock(point.event.time));
      card.querySelector('.metric-latest').hidden=!pinned;
    };
    for(const circle of card.querySelectorAll('[data-metric-point]')) {
      const point=item.points[Number(circle.dataset.metricPoint)];
      circle.onmouseenter=circle.onfocus=()=>readout(point,false);
      circle.onmouseleave=circle.onblur=()=>readout(view.pinned.get(item.key) || item.points.at(-1),view.pinned.has(item.key));
      circle.onclick=()=>{view.pinned.set(item.key,point);metricCardSignature='';readout(point,true);};
      circle.onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();circle.onclick();}};
    }
    card.querySelector('.metric-latest').onclick=()=>{view.pinned.delete(item.key);renderTaskMetrics();};
  }
  metricRenderedFor=node.id;metricCardSignature=signature;
}
async function loadTaskMetrics() {
  const token=++metricRequest, id=selected, node=taskNodes().find(item=>item.id===id), task=metricTask(node);
  if(globalView || !task)return;
  const query='source='+encodeURIComponent(task.source)+(task.task?'&task='+encodeURIComponent(task.task):task.run?'&run='+encodeURIComponent(task.run):'&experiment='+encodeURIComponent(task.experiment));
  try {
    const result=await api('/api/telemetry?'+query);
    if(token!==metricRequest || selected!==id || globalView)return;
    mergeTaskMetrics(node,result.streams);metricView(id).error=null;renderTaskMetrics();
  } catch(error) {
    if(token===metricRequest && selected===id && !globalView){metricView(id).error='回传读取失败 · '+error.message+' · 将自动重试';renderTaskMetrics();}
  }
}
