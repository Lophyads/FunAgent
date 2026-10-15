import {edgeRoutes} from './memory-graph-model.js';
const colors={Entity:'#36b99b',EntityType:'#9e84db',TextDocument:'#579dd5',DocumentChunk:'#d5a04d'};
export const typeColor=type=>colors[type]||'#8a95a3';
// G6 receives disposable render data; domain snapshots never enter the engine.
export function createRenderer(container,{onSelect,onError,onStatus}){
  let engine,graph,model,visible,selection,matches=new Set(),version=0,active=true,frame,resolveFrame,disposed=false,dragged=false,down,drawn=false;
  let queue=Promise.resolve(),labelFrame,routes=new Map(),previousData=null;
  const run=task=>{queue=queue.then(()=>disposed?undefined:task()).catch(e=>{if(!disposed)onError(e);});return queue;};
  const resizeObserver=new ResizeObserver(()=>resize());
  const themeObserver=new MutationObserver(()=>{if(active&&graph){previousData=null;run(()=>draw());}});
  resizeObserver.observe(container);themeObserver.observe(document.documentElement,{attributes:true,attributeFilter:['class','data-theme']});
  const textColor=()=>getComputedStyle(container).color;
  function priority(id){return selection?.id===id?4:matches.has(id)?3:selection?.kind==='node'&&model.neighbors(selection.id).has(id)?2:0;}
  async function init(){if(graph)return;engine=await import('../vendor/memory-graph-engine.js');if(disposed)return;graph=new engine.Graph({container,width:Math.max(1,container.clientWidth),height:Math.max(1,container.clientHeight),animation:false,padding:50,zoomRange:[.08,4],behaviors:['drag-canvas','zoom-canvas','drag-element'],node:{type:d=>d.data.shape,style:{labelText:d=>d.data.showLabel?d.data.label:'',labelFontSize:()=>13/(graph?.getZoom()||1),labelFill:()=>textColor(),labelPlacement:'right',size:36}},edge:{type:'quadratic',style:{endArrow:true,lineWidth:1.2,stroke:()=>textColor(),labelText:d=>d.data.showLabel?d.data.label:'',labelFill:()=>textColor(),labelFontSize:()=>12/(graph?.getZoom()||1),increasedLineWidthForHitTesting:10}}});
    for(const kind of ['node','edge'])graph.on(kind+':click',e=>{if(!dragged){const data=kind==='node'?graph.getNodeData(e.target.id):graph.getEdgeData(e.target.id);onSelect(kind,data.data.domainId);}dragged=false;});
    graph.on('canvas:click',()=>{if(!dragged)onSelect(null,null);dragged=false;});
    graph.on('node:dragend',()=>{capturePositions();run(draw);}); graph.on('aftertransform',()=>{if(!labelFrame&&active)labelFrame=requestAnimationFrame(()=>{labelFrame=null;run(draw);});}); graph.on('node:pointerenter',e=>{container.title=model?.nodes.get(graph.getNodeData(e.target.id).data.domainId)?.label||'';});
  }
  function capturePositions(){if(!graph||!model)return;for(const node of graph.getNodeData())model.positions.set(node.data.domainId,{x:node.style.x,y:node.style.y});}
  function pointerDown(e){down={x:e.clientX,y:e.clientY};dragged=false;}
  function pointerMove(e){if(down&&Math.hypot(e.clientX-down.x,e.clientY-down.y)>4)dragged=true;}
  function pointerUp(){down=null;}
  container.addEventListener('pointerdown',pointerDown);container.addEventListener('pointermove',pointerMove);container.addEventListener('pointerup',pointerUp);
  async function layout(own){onStatus('整理布局…');const nodes=[...model.nodes.values()].sort((a,b)=>a.id.localeCompare(b.id)).map(n=>({id:n.id,...model.positions.get(n.id)}));const links=[...model.edges.values()].map(e=>({source:e.source,target:e.target}));
    const simulation=engine.forceSimulation(nodes).stop().force('link',engine.forceLink(links).id(n=>n.id).distance(120)).force('charge',engine.forceManyBody().strength(-180)).force('collide',engine.forceCollide(28)).force('center',engine.forceCenter(0,0));
    for(let i=0;i<180&&active&&own===version;i++){simulation.tick();if(i%5===0)await new Promise(resolve=>{resolveFrame=resolve;frame=requestAnimationFrame(()=>{frame=null;resolveFrame=null;resolve();});});}
    simulation.stop();if(disposed||!active||own!==version)return false;for(const n of nodes)model.positions.set(n.id,{x:n.x,y:n.y});onStatus('');return true;
  }
  function data(){const neighbors=selection?.kind==='node'?model.neighbors(selection.id):new Set(selection?.kind==='edge'?[model.edges.get(selection.id)?.source,model.edges.get(selection.id)?.target]:[]);const nodeKey=id=>'n:'+id,edgeKey=id=>'e:'+id;
    return {nodes:visible.nodes.map(n=>({id:nodeKey(n.id),data:{domainId:n.id,label:n.label||n.id,showLabel:false,shape:['TextDocument','DocumentChunk'].includes(n.type)?'rect':'circle'},style:{...model.positions.get(n.id),fill:typeColor(n.type),radius:8,stroke:selection?.kind==='node'&&selection.id===n.id?'#36b99b':typeColor(n.type),lineWidth:selection?.kind==='node'&&selection.id===n.id?4:1,opacity:selection&&!neighbors.has(n.id)?.3:1}})),edges:visible.edges.map(e=>({id:edgeKey(e.id),source:nodeKey(e.source),target:nodeKey(e.target),data:{domainId:e.id,label:e.label,showLabel:selection?.kind==='edge'&&selection.id===e.id||selection?.kind==='node'&&(e.source===selection.id||e.target===selection.id)},style:{...routes.get(e.id),lineWidth:selection?.kind==='edge'&&selection.id===e.id?2.5:1.2,opacity:selection?(selection.kind==='edge'?selection.id===e.id:e.source===selection.id||e.target===selection.id)?1:.12:.5}}))};}
  const measure=document.createElement('canvas').getContext('2d');
  function labels(renderData){
    const occupied=[],zoom=graph.getZoom()||1;measure.font='13px sans-serif';
    const place=(point,text,required=false)=>{const box={x:point[0]+20*zoom,y:point[1]-8,w:measure.measureText(text).width+8,h:18};const collision=occupied.some(b=>box.x<b.x+b.w&&box.x+box.w>b.x&&box.y<b.y+b.h&&box.y+box.h>b.y);if(!required&&(collision||box.x<0||box.y<0||box.x+box.w>container.clientWidth||box.y+box.h>container.clientHeight))return false;occupied.push(box);return true;};
    for(const node of [...renderData.nodes].sort((a,b)=>priority(b.data.domainId)-priority(a.data.domainId)||a.id.localeCompare(b.id))){const p=graph.getViewportByCanvas([node.style.x,node.style.y]);node.data.showLabel=(zoom>=.45||priority(node.data.domainId)>0)&&place(p,node.data.label,priority(node.data.domainId)===4);node.style.labelFontSize=node.data.showLabel?13/zoom:13;}
    for(const edge of renderData.edges){if(edge.data.showLabel){const e=model.edges.get(edge.data.domainId),a=model.positions.get(e.source),b=model.positions.get(e.target);edge.data.showLabel=place(graph.getViewportByCanvas([(a.x+b.x)/2,(a.y+b.y)/2]),edge.data.label,selection?.kind==='edge'&&selection.id===e.id);}edge.style.labelFontSize=edge.data.showLabel?12/zoom:12;}
  }
  async function draw(){if(!active||!graph||!visible)return;const renderData=data();if(drawn)labels(renderData);
    const sameTopology=previousData&&['nodes','edges'].every(kind=>previousData[kind].length===renderData[kind].length&&previousData[kind].every((item,i)=>item.id===renderData[kind][i].id));
    if(sameTopology){const changed={nodes:[],edges:[]};for(const kind of ['nodes','edges'])renderData[kind].forEach((item,i)=>{if(JSON.stringify(item)!==JSON.stringify(previousData[kind][i]))changed[kind].push(item);});if(!changed.nodes.length&&!changed.edges.length)return;graph.updateData(changed);}else graph.setData(renderData);
    previousData=renderData;if(drawn)await graph.draw();else await graph.render();drawn=true;}
  function resize(){if(active&&graph&&container.clientWidth&&container.clientHeight){graph.setSize(container.clientWidth,container.clientHeight);run(draw);}}
  return {clear(){version++;previousData=null;model=null;visible=null;selection=null;if(graph){graph.setData({nodes:[],edges:[]});run(()=>graph.draw());}},setSnapshot(next,{positions,viewport}={}){const own=++version;return run(async()=>{await init();if(!active||disposed||own!==version)return;model=next;routes=edgeRoutes(model.snapshot.edges);visible=model.visible();if(positions)model.positions=new Map(positions.map(([id,p])=>[id,{...p}]));else if(!await layout(own))return;await draw();if(disposed||!active||own!==version)return;if(viewport)await this.restoreViewport(viewport);else await this.fitView();});},setVisibleElements(value){visible=value;return run(draw);},setSelection(value,ids=[]){selection=value;matches=new Set(ids);return run(draw);},focusNode(id){return run(()=>graph?.focusElement('n:'+id,false));},fitView(){return graph?.fitView({},false);},relayout(){if(!model)return;const own=++version;return run(async()=>{if(await layout(own)){await draw();await this.fitView();}});},getViewport(){capturePositions();return graph?{zoom:graph.getZoom(),center:graph.getCanvasByViewport([container.clientWidth/2,container.clientHeight/2])}:null;},async restoreViewport(value){if(graph&&value){await graph.zoomTo(value.zoom,false);const point=graph.getViewportByCanvas(value.center);await graph.translateBy([container.clientWidth/2-point[0],container.clientHeight/2-point[1]],false);}},zoom(factor){return graph?.zoomTo(Math.max(.08,Math.min(4,graph.getZoom()*factor)),false);},resize,suspend(){active=false;version++;if(labelFrame)cancelAnimationFrame(labelFrame);labelFrame=null;if(frame)cancelAnimationFrame(frame);resolveFrame?.();frame=resolveFrame=null;resizeObserver.disconnect();themeObserver.disconnect();},resume(){active=true;resizeObserver.observe(container);themeObserver.observe(document.documentElement,{attributes:true,attributeFilter:['class','data-theme']});resize();},destroy(){disposed=true;this.suspend();graph?.destroy();graph=null;model=null;container.removeEventListener('pointerdown',pointerDown);container.removeEventListener('pointermove',pointerMove);container.removeEventListener('pointerup',pointerUp);}};
}












