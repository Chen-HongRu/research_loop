'use strict';

const TREE = {cardWidth:232, column:272, track:56, left:64, top:84, axis:54};
let treeLayout = null, treeFollowed = null;

function treeTime(node) { return timestamp(node.created_at); }
function treeOrder(a, b) {
  const first=treeTime(a), second=treeTime(b);
  if(first!==second)return first===null?1:second===null?-1:first-second;
  const sequence=(nodeOrder(a)||0)-(nodeOrder(b)||0);
  return sequence || a.id.localeCompare(b.id);
}

// Horizontal slots are chronological and equally spaced, never elapsed duration.
// Each additional child gets a track; routes and evidence references do not assign tracks.
function gitLayout(nodes) {
  const ordered=[...nodes].sort(treeOrder), byId=new Map(ordered.map(node=>[node.id,node]));
  const tracks=new Map(), rootTracks=new Map(), continued=new Set(), visiting=new Set();
  let count=0;
  function track(node) {
    if(tracks.has(node.id))return tracks.get(node.id);
    visiting.add(node.id);
    const parent=byId.get(graphParent(node));
    let lane;
    if(parent && !visiting.has(parent.id)) {
      const inherited=track(parent);
      if(!node.validation_of && !continued.has(parent.id)){lane=inherited;continued.add(parent.id);}
      else lane=count++;
    } else {
      const source=node.source_id || '';
      if(!rootTracks.has(source))rootTracks.set(source,count++);
      lane=rootTracks.get(source);
    }
    tracks.set(node.id,lane);visiting.delete(node.id);return lane;
  }
  for(const node of ordered)track(node);
  const positions=Object.fromEntries(ordered.map((node,column)=>{
    const lane=tracks.get(node.id),left=TREE.left+column*TREE.column,y=TREE.top+lane*TREE.track;
    return [node.id,{lane,column,left,x:left+TREE.cardWidth/2,y}];
  }));
  return {ordered,positions,lanes:count,width:TREE.left+ordered.length*TREE.column,height:TREE.top+count*TREE.track};
}

function focusTreeTask(id=selected) {
  const point=treeLayout?.positions[id], wrap=$('graphWrap');
  if(!point)return;
  setTreeLeft(Math.max(0,point.left-((wrap.clientWidth||TREE.cardWidth)-TREE.cardWidth)/2));
  const freeHeight=Math.max(0,(wrap.clientHeight||240)-TREE.axis);
  wrap.scrollTop=Math.max(0,point.y-(TREE.axis+freeHeight/2));
}
function setTreeLeft(left) {
  $('graphWrap').scrollLeft=left;
  $('treeCardsWrap').scrollLeft=left;
}
function syncTreeScroll(from, to) {
  if(to.scrollLeft!==from.scrollLeft)to.scrollLeft=from.scrollLeft;
}
function panTree(direction) {
  const wrap=$('graphWrap');
  setTreeLeft(Math.max(0,(wrap.scrollLeft||0)+direction*Math.max(TREE.column,(wrap.clientWidth||TREE.column)*.8)));
}
function treeEdge(parent, child) {
  const direction=Math.sign(child.x-parent.x), rail=child.y-18;
  // Fork beside the actual parent. Cross-track turns use the adjacent column gap;
  // the arrival rail clears unrelated node dots, including reverse/undated edges.
  const turn=parent.y===child.y?parent.x+direction*18:direction>0?parent.left+TREE.cardWidth+18:parent.left-18;
  return `M${parent.x+direction*9},${parent.y} H${turn} V${rail} H${child.x} V${child.y-10}`;
}
function drawGraph() {
  const wrap=$('graphWrap'), graph=$('graph'), cards=$('treeCards'), before=treeLayout;
  const focusRegion=[graph,cards].find(region=>region.contains?.(document.activeElement));
  const focused=focusRegion?document.activeElement.closest('[data-node]')?.dataset.node:null;
  const left=wrap.scrollLeft||0, top=wrap.scrollTop||0;
  // Preserve a visible task's offset when a refresh inserts an earlier record.
  const anchor=before?.ordered.reduce((best,node)=>{
    const point=before.positions[node.id], distance=Math.abs(point.left-left);
    return !best||distance<best.distance?{id:node.id,point,distance}:best;
  },null);
  const model=gitLayout(visibleTasks()), {ordered,positions,width,height}=model;
  treeLayout=model;
  $('treeTotals').textContent=ordered.length+' 项任务 · '+model.lanes+' 条分支轨道';
  if(!ordered.length){graph.innerHTML='<div class="empty">当前筛选下没有任务。</div>';cards.innerHTML='';graph.style.width='';graph.style.height='';cards.style.width='';return;}
  const route=$('routeFilter').value,source=$('sourceFilter').value,state=$('taskFilter').value;
  const dimmed=node=>(route&&node.route_id!==route)||(source&&node.source_id!==source)||(state&&taskState(node)!==state);
  const styles={code:['#8ab8ff',''],continuation:['#72dfb3','2 4'],validation:['#c4a0ef','8 4'],import:['#f2cb85','7 4'],reference:['#a99bc9','3 5']};
  let svg='<defs>'+Object.entries(styles).map(([name,[color]])=>`<marker id="tree-${name}" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto"><path d="M0 0 L6 3 L0 6" fill="${color}"/></marker>`).join('')+'</defs>';
  function edge(parentId,node,kind,title) {
    const parent=positions[parentId],child=positions[node.id];if(!parent)return;
    const [color,dash]=styles[kind],active=node.id===selected||parentId===selected;
    svg+=`<path d="${treeEdge(parent,child)}" fill="none" stroke="${color}"${dash?' stroke-dasharray="'+dash+'"':''} stroke-width="${active?2.6:1.7}" stroke-linejoin="round" opacity="${active?1:.65}" marker-end="url(#tree-${kind})"><title>${esc(title)}</title></path>`;
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
    const point=positions[node.id],color=node.frontier_current===true?'#72dfb3':COLORS[point.lane%COLORS.length];
    svg+=`<circle cx="${point.x}" cy="${point.y}" r="${node.id===selected?7:5}" fill="${node.published?color:'#151f30'}" stroke="${color}" stroke-width="2"/>`;
    if(node.frontier_current===true)svg+=`<circle cx="${point.x}" cy="${point.y}" r="10" fill="none" stroke="#72dfb3" stroke-width="1.3"/>`;
  }
  const ticks=ordered.map(node=>{
    const date=treeTime(node),point=positions[node.id];
    return `<div class="timeline-tick" style="left:${point.left}px" title="${esc(clock(node.created_at))}">${date===null?'时间未记录':esc(new Date(date).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}))}<span>${esc(node.source_label||node.source_id||'研究记录')}</span></div>`;
  }).join('');
  const lanes=Array.from({length:model.lanes},(_,index)=>`<div class="timeline-lane" style="top:${TREE.top+index*TREE.track}px"><span class="timeline-lane-label">轨道 ${index+1}</span></div>`).join('');
  const nodes=ordered.map(node=>{
    const point=positions[node.id],local=node.local_id||node.id;
    const label=node.task_kind==='long_training'?'长训 '+local.slice(-8):'#'+local;
    return `<button class="tree-node ${node.id===selected?'selected ':''}${dimmed(node)?'other-route':''}" data-node="${esc(node.id)}" aria-pressed="${node.id===selected}" aria-label="${esc(nodeLabel(node)+' · '+taskStateLabel(node))}" title="${esc(nodeLabel(node)+' · '+graphOrigin(node))}" style="left:${point.x-22}px;top:${point.y-22}px"><span class="tree-node-label">${esc(label)}</span></button>`;
  }).join('');
  const cardMarkup=ordered.map(node=>{
    const point=positions[node.id];
    const action=explanationEntry(node)?.title||node.action_summary||node.action||node.title||node.route||'路线选择中';
    const status=(node.frontier_exclusion_reason?'资格已排除 · ':'')+(node.imported?'历史导入 · ':'')+(node.archived?'归档 · ':'')+taskStateLabel(node);
    return `<button class="git-row ${node.id===selected?'selected ':''}${dimmed(node)?'other-route':''}" data-node="${esc(node.id)}" aria-pressed="${node.id===selected}" style="left:${point.left}px" title="${esc(nodeLabel(node)+' · '+action+' · '+graphOrigin(node)+' · '+status)}"><div class="row-top"><span class="commit-id">${esc(nodeLabel(node))}</span></div><div class="row-action">${esc(action)}</div><div class="row-meta">${badge(taskKind(node),node.task_kind==='long_training'?'amber':'')}${badge(node.route||node.source_label||'待选择','route')}</div><div class="row-result"><span class="score ${nodeTone(node)}" title="${esc(status)}">${esc(nodeResult(node))}</span><span class="graph-frontier">${node.frontier_current===true?'◎ 当前 Frontier':node.frontier_reached===true?'◇ 曾进入 Frontier':esc(status)}</span></div></button>`;
  }).join('');
  const selection=positions[selected]?`<div class="timeline-selection" style="left:${positions[selected].left}px"></div>`:'';
  graph.style.width=width+'px';graph.style.height=height+'px';
  graph.innerHTML='<div class="timeline-axis">'+ticks+'</div>'+selection+lanes+`<svg class="git-rail" width="${width}" height="${height}" aria-hidden="true">${svg}</svg>`+nodes;
  cards.style.width=width+'px';cards.innerHTML=cardMarkup;
  bindExperiments(graph);bindExperiments(cards);
  if(focused)for(const button of focusRegion.querySelectorAll('[data-node]'))if(button.dataset.node===focused)button.focus({preventScroll:true});
  if(anchor && positions[anchor.id]){setTreeLeft(left+positions[anchor.id].left-anchor.point.left);wrap.scrollTop=top+positions[anchor.id].y-anchor.point.y;}
  else {setTreeLeft(left);wrap.scrollTop=top;}
  if(!before || following&&treeFollowed!==selected)focusTreeTask();
  treeFollowed=following?selected:null;
}
