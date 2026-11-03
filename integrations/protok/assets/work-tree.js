'use strict';

const TREE = {column:56, track:44, left:28, top:64, axis:40};
let treeLayout = null, treeFollowed = null, treeZoom = null, treeHovered = null, treeCardDismissed = null, treePopoverMarkup = '';

function frontierSpine(nodes) {
  const byId=new Map(nodes.map(node=>[node.id,node]));
  const members=nodes.filter(node=>node.frontier_current===true && !node.validation_of).sort(treeOrder);
  const tip=members.at(-1),ids=new Set();
  let node=tip;
  while(node&&!ids.has(node.id)){ids.add(node.id);node=byId.get(graphParent(node))||byId.get(node.imported_from);}
  return {tip,ids,members};
}

function treeTime(node) { return timestamp(node.created_at); }
function treeOrder(a, b) {
  const first=treeTime(a), second=treeTime(b);
  if(first!==second)return first===null?1:second===null?-1:first-second;
  const sequence=(nodeOrder(a)||0)-(nodeOrder(b)||0);
  return sequence || a.id.localeCompare(b.id);
}

// Horizontal slots are chronological and equally spaced, never elapsed duration.
// Frontier ancestry owns track zero. Other chains reuse non-overlapping tracks.
function gitLayout(nodes, options={}) {
  const ordered=[...nodes].sort(treeOrder), byId=new Map(ordered.map(node=>[node.id,node]));
  const spine=frontierSpine(ordered),tracks=new Map(),continued=new Set(),visiting=new Set();
  let count=spine.tip?1:0;
  function track(node) {
    if(spine.ids.has(node.id)){tracks.set(node.id,0);return 0;}
    if(tracks.has(node.id))return tracks.get(node.id);
    visiting.add(node.id);
    const parent=byId.get(graphParent(node));
    let lane;
    if(parent && !visiting.has(parent.id)) {
      const inherited=track(parent);
      if(!spine.ids.has(parent.id) && !node.validation_of && !continued.has(parent.id)){lane=inherited;continued.add(parent.id);}
      else lane=count++;
    } else {
      lane=count++;
    }
    tracks.set(node.id,lane);visiting.delete(node.id);return lane;
  }
  for(const node of ordered)track(node);
  const columns=new Map(ordered.map((node,index)=>[node.id,index])),intervals=new Map();
  for(const node of ordered){
    const group=tracks.get(node.id);if(spine.tip&&group===0)continue;
    const column=columns.get(node.id),parent=byId.get(graphParent(node));
    const start=parent&&tracks.get(parent.id)!==group?Math.min(column,columns.get(parent.id)):column;
    const end=parent&&tracks.get(parent.id)!==group?Math.max(column,columns.get(parent.id)):column;
    const interval=intervals.get(group)||{group,start,end};
    interval.start=Math.min(interval.start,start);interval.end=Math.max(interval.end,end);intervals.set(group,interval);
  }
  const ends=spine.tip?[Infinity]:[],lanes=new Map(spine.tip?[[0,0]]:[]);
  for(const interval of [...intervals.values()].sort((a,b)=>a.start-b.start||a.end-b.end||a.group-b.group)){
    let lane=ends.findIndex(end=>end<interval.start);
    if(lane<0)lane=ends.length;
    ends[lane]=interval.end;lanes.set(interval.group,lane);
  }
  count=ends.length;
  const zoom=options.zoom||1, overview=options.overview===true;
  const columnWidth=overview?Math.min(TREE.column,Math.max(0,(options.width-2*TREE.left)/Math.max(1,ordered.length-1))):TREE.column*zoom;
  const trackHeight=overview?Math.min(TREE.track,Math.max(0,(options.height-TREE.top-28)/Math.max(1,count-1))):TREE.track*zoom;
  const positions=Object.fromEntries(ordered.map((node,column)=>{
    const lane=lanes.get(tracks.get(node.id)),y=TREE.top+lane*trackHeight;
    return [node.id,{lane,column,mainline:spine.ids.has(node.id),x:TREE.left+column*columnWidth,y,columnWidth,trackHeight,radius:overview?2.4:5}];
  }));
  return {ordered,positions,lanes:count,overview,columnWidth,trackHeight,spine,
    width:Math.max(options.width||0,2*TREE.left+Math.max(0,ordered.length-1)*columnWidth),
    height:Math.max(options.height||0,TREE.top+Math.max(0,count-1)*trackHeight+28)};
}

function focusTreeTask(id=selected) {
  const point=treeLayout?.positions[id], wrap=$('graphWrap');
  if(!point)return;
  wrap.scrollLeft=treeLayout.overview?0:Math.max(0,point.x-(wrap.clientWidth||600)/2);
  const freeHeight=Math.max(0,(wrap.clientHeight||240)-TREE.axis);
  wrap.scrollTop=treeLayout.overview?0:Math.max(0,point.y-(TREE.axis+freeHeight/2));
  renderTreePopover();
}
function setTreeZoom(zoom) {
  treeZoom=zoom==null?null:Math.min(3,Math.max(.4,zoom));
  drawGraph();focusTreeTask();
}
function zoomTree(direction) {
  const zoom=(treeZoom??.6)*(direction>0?1.6:1/1.6);
  setTreeZoom(zoom<.4?null:zoom);
}
function treeEdge(parent, child) {
  if(parent.mainline&&child.mainline)return `M${parent.x},${parent.y} H${child.x}`;
  const direction=Math.sign(child.x-parent.x),radius=child.radius||5;
  const rail=child.y-Math.max(radius+5,Math.min(18,child.trackHeight*.42));
  // Fork beside the actual parent, using its adjacent column gap. Approach the
  // destination from above, including reverse/undated relationships.
  const turn=parent.x+direction*Math.min(18,parent.columnWidth/2);
  const start=Math.min(parent.radius+3,Math.abs(child.x-parent.x)/3);
  return `M${parent.x+direction*start},${parent.y} H${turn} V${rail} H${child.x} V${child.y-radius-3}`;
}
function drawGraph() {
  const wrap=$('graphWrap'), graph=$('graph'), before=treeLayout;
  const focusRegion=graph.contains?.(document.activeElement)?graph:null;
  const focused=focusRegion?document.activeElement.closest('[data-node]')?.dataset.node:null;
  const left=wrap.scrollLeft||0, top=wrap.scrollTop||0;
  // Preserve a visible task's offset when a refresh inserts an earlier record.
  const anchor=before?.ordered.reduce((best,node)=>{
    const point=before.positions[node.id], distance=Math.abs(point.x-left);
    return !best||distance<best.distance?{id:node.id,point,distance}:best;
  },null);
  const all=taskNodes(),mainline=frontierSpine(all);
  const visible=[...new Map([...visibleTasks(),...all.filter(node=>mainline.ids.has(node.id))].map(node=>[node.id,node])).values()];
  const model=gitLayout(visible,{overview:treeZoom===null,zoom:treeZoom,width:wrap.clientWidth||800,height:wrap.clientHeight||300}), {ordered,positions,width,height}=model;
  treeLayout=model;
  $('treeTotals').textContent=ordered.length+' 项任务 · '+model.lanes+' 条分支轨道';
  $('treeScale').textContent=model.overview?'全局概览':Math.round(treeZoom*100)+'%';
  $('treeFit').setAttribute('aria-pressed',String(model.overview));
  $('treeDetail').setAttribute('aria-pressed',String(!model.overview));
  $('treeZoomOut').disabled=model.overview;$('treeZoomIn').disabled=treeZoom>=3;
  $('treeSelection').textContent='所选：'+nodeLabel(ordered.find(node=>node.id===selected));
  $('treeMainline').textContent=model.spine.tip?'Frontier 主线 → '+nodeLabel(model.spine.tip)+(model.spine.members.length>1?' · 另有 '+(model.spine.members.length-1)+' 个 Frontier 成员':''):'尚无可确认的 Frontier 主线';
  if(!ordered.length){graph.innerHTML='<div class="empty">当前筛选下没有任务。</div>';graph.style.width='';graph.style.height='';renderTreePopover();return;}
  const route=$('routeFilter').value,source=$('sourceFilter').value,state=$('taskFilter').value;
  const dimmed=node=>(route&&node.route_id!==route)||(source&&node.source_id!==source)||(state&&taskState(node)!==state);
  const styles={trunk:['#72dfb3',''],code:['#8ab8ff',''],continuation:['#72dfb3','2 4'],validation:['#c4a0ef','8 4'],import:['#f2cb85','7 4'],reference:['#a99bc9','3 5']};
  let svg='<defs>'+Object.entries(styles).map(([name,[color]])=>`<marker id="tree-${name}" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto"><path d="M0 0 L6 3 L0 6" fill="${color}"/></marker>`).join('')+'</defs>';
  function edge(parentId,node,kind,title) {
    const parent=positions[parentId],child=positions[node.id];if(!parent)return;
    const trunk=parent.mainline&&child.mainline,style=trunk&&kind==='code'?'trunk':kind;
    const [color,dash]=styles[style],active=trunk||node.id===selected||parentId===selected;
    svg+=`<path d="${treeEdge(parent,child)}" fill="none" stroke="${color}"${dash?' stroke-dasharray="'+dash+'"':''} stroke-width="${active?2.2:model.overview?1:1.7}" stroke-linejoin="round" opacity="${active?1:.55}"${!model.overview||active?' marker-end="url(#tree-'+kind+')"':''}><title>${esc(title)}</title></path>`;
  }
  for(const node of ordered) {
    const parent=node.validation_of||node.continuation_of||node.base;
    if(positions[parent]) {
      if(node.validation_of)edge(parent,node,'validation','长训验证：'+parent+' → '+node.id+'；同源代码独立训练');
      else if(node.continuation_of)edge(parent,node,'continuation','执行续跑：#'+parent+' → #'+node.id+'；沿用研究 #'+node.research_iteration+' 的 Plan');
      else edge(parent,node,'code','代码继承：#'+parent+' → #'+node.id);
    } else if(node.imported_from)edge(node.imported_from,node,'import','同版本结果导入：'+node.imported_from+' → '+node.id+'；不增加新评价结果');
    if($('refs').checked)for(const ref of node.references||[])if(ref!==parent&&ref!==node.id)edge(ref,node,'reference','证据引用：#'+ref+' → #'+node.id+'；不继承代码');
  }
  for(const node of ordered) {
    const point=positions[node.id],color=point.mainline?'#72dfb3':COLORS[point.lane%COLORS.length];
    svg+=`<circle cx="${point.x}" cy="${point.y}" r="${node.id===selected?point.radius+2:point.radius}" fill="${node.published?color:'#151f30'}" stroke="${color}" stroke-width="${model.overview?1.2:2}"/>`;
    if(node.frontier_current===true)svg+=`<circle cx="${point.x}" cy="${point.y}" r="${point.radius+5}" fill="none" stroke="#72dfb3" stroke-width="1.3"/>`;
  }
  let lastTick=-Infinity;
  const tickNodes=model.overview&&ordered.length>1?[ordered[0],ordered.at(-1)]:ordered;
  const ticks=tickNodes.map((node,index)=>{
    const date=treeTime(node),point=positions[node.id];
    if(index && point.x-lastTick<105)return '';
    if(!model.overview && index<tickNodes.length-1 && width-point.x<100)return '';
    lastTick=point.x;
    const tickLeft=Math.max(0,Math.min(width-100,point.x-50));
    return `<div class="timeline-tick" style="left:${tickLeft}px" title="${esc(clock(node.created_at))}">${date===null?'时间未记录':esc(new Date(date).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}))}<span>${esc(node.source_label||node.source_id||'研究记录')}</span></div>`;
  }).join('');
  const lanes=model.overview?'':Array.from({length:model.lanes},(_,index)=>`<div class="timeline-lane" style="top:${TREE.top+index*model.trackHeight}px"></div>`).join('');
  const nodes=ordered.map(node=>{
    const point=positions[node.id],local=node.local_id||node.id;
    const label=node.task_kind==='long_training'?'长训 '+local.slice(-8):'#'+local;
    const hit=model.overview?10:Math.min(44,model.columnWidth,model.trackHeight);
    return `<button class="tree-node ${node.id===selected?'selected ':''}${model.overview||model.columnWidth<48?'compact ':''}${dimmed(node)?'other-route':''}" data-node="${esc(node.id)}" data-mainline="${point.mainline}" aria-pressed="${node.id===selected}" aria-label="${esc(nodeLabel(node)+' · '+taskStateLabel(node))}" title="${esc(nodeLabel(node)+' · '+graphOrigin(node))}" style="left:${point.x-hit/2}px;top:${point.y-hit/2}px;width:${hit}px;height:${hit}px"><span class="tree-node-label">${esc(label)}</span></button>`;
  }).join('');
  const selection=positions[selected]?`<div class="timeline-selection" style="left:${positions[selected].x}px"></div>`:'';
  graph.style.width=width+'px';graph.style.height=height+'px';
  graph.innerHTML='<div class="timeline-axis">'+ticks+'</div>'+selection+lanes+`<svg class="git-rail" width="${width}" height="${height}" aria-hidden="true">${svg}</svg>`+nodes;
  graph.querySelectorAll('[data-node]').forEach(button=>{
    button.onclick=()=>selectTreeTask(button.dataset.node);
    button.onpointerenter=event=>{if(event.pointerType!=='touch')previewTreeTask(button.dataset.node);};
    button.onfocus=()=>previewTreeTask(button.dataset.node);
  });
  if(focused)for(const button of focusRegion.querySelectorAll('[data-node]'))if(button.dataset.node===focused)button.focus({preventScroll:true});
  if(model.overview){wrap.scrollLeft=0;wrap.scrollTop=0;}
  else if(anchor && positions[anchor.id]){
    wrap.scrollLeft=left+positions[anchor.id].x-anchor.point.x;wrap.scrollTop=top+positions[anchor.id].y-anchor.point.y;
  } else {wrap.scrollLeft=left;wrap.scrollTop=top;}
  if(!before || following&&treeFollowed!==selected)focusTreeTask();
  treeFollowed=following?selected:null;
  renderTreePopover();
}

function previewTreeTask(id){treeHovered=id;treeCardDismissed=null;renderTreePopover();}
function selectTreeTask(id,jump=false){
  treeHovered=null;treeCardDismissed=null;
  const pending=choose(id);
  if(jump)$('taskDetail').scrollIntoView({block:'start',behavior:'smooth'});
  return pending;
}
function renderTreePopover(){
  if(treeHovered&&!treeLayout?.positions[treeHovered])treeHovered=null;
  const popup=$('treePopover'),id=treeHovered||selected,node=treeLayout?.ordered.find(item=>item.id===id),point=treeLayout?.positions[id];
  popup.hidden=!node||!!treeCardDismissed;
  if(popup.hidden)return;
  const action=explanationEntry(node)?.title||node.action_summary||node.action||node.title||node.route||'路线选择中';
  const status=(node.frontier_exclusion_reason?'资格已排除 · ':'')+(node.imported?'历史导入 · ':'')+(node.archived?'归档 · ':'')+taskStateLabel(node);
  const markup=`<div class="toolbar between"><span class="eyebrow">${id===selected?'已选任务':'任务预览'}</span><button id="treeCardClose" aria-label="关闭任务卡片">×</button></div><b class="commit-id">${esc(nodeLabel(node))}</b><p class="popup-action">${esc(action)}</p><div>${badge(taskKind(node))} ${badge(node.route||'待选择','route')}</div><p class="popup-result"><span class="score ${nodeTone(node)}">${esc(nodeResult(node))}</span> · ${esc(status)}</p><small>${esc(graphOrigin(node))}${node.frontier_current?' · 当前 Frontier':''}</small><button id="treeCardOpen" data-task="${esc(id)}">查看任务详情 ↓</button>`;
  if(markup!==treePopoverMarkup){popup.innerHTML=markup;treePopoverMarkup=markup;}
  $('treeCardClose').onclick=()=>{treeCardDismissed=true;treeHovered=null;popup.hidden=true;};
  $('treeCardOpen').onclick=()=>selectTreeTask(id,true);
  const wrap=$('graphWrap'),width=wrap.clientWidth||600,height=wrap.clientHeight||300;
  const popupWidth=popup.offsetWidth||Math.min(280,width-16),popupHeight=popup.offsetHeight||230;
  popup.style.left=Math.max(8,Math.min(width-popupWidth-8,point.x-(wrap.scrollLeft||0)+12))+'px';
  popup.style.top=Math.max(8,Math.min(height-popupHeight-8,point.y-(wrap.scrollTop||0)+16))+'px';
}
