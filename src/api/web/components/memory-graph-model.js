export function graphModel(snapshot){
  if(!snapshot||!Array.isArray(snapshot.nodes)||!Array.isArray(snapshot.edges)||snapshot.nodes.length>500||snapshot.edges.length>1000)throw Error('图谱快照无效');
  if(new TextEncoder().encode(JSON.stringify(snapshot)).length>8*1024*1024)throw Error('图谱快照超过会话上限（8 MiB）');
  snapshot=structuredClone(snapshot);
  const freeze=value=>{if(value&&typeof value==='object'){for(const child of Object.values(value))freeze(child);Object.freeze(value);}return value;};freeze(snapshot);
  const nodes=new Map(),edges=new Map(),incoming=new Map(),outgoing=new Map(),positions=new Map();
  for(const n of snapshot.nodes){if(typeof n.id!=='string'||!n.id||nodes.has(n.id))throw Error('节点身份无效');nodes.set(n.id,n);incoming.set(n.id,[]);outgoing.set(n.id,[]);}
  for(const e of snapshot.edges){if(typeof e.id!=='string'||edges.has(e.id)||!nodes.has(e.source)||!nodes.has(e.target))throw Error('关系端点无效');edges.set(e.id,e);incoming.get(e.target).push(e);outgoing.get(e.source).push(e);}
  let index=0;for(const n of [...nodes.values()].sort((a,b)=>a.id.localeCompare(b.id))){const angle=index*2.399963229728653,radius=45*Math.sqrt(index++);positions.set(n.id,{x:radius*Math.cos(angle),y:radius*Math.sin(angle)});}
  return {snapshot,nodes,edges,incoming,outgoing,positions,filter(search='',nodeType='',edgeType=''){
    const q=search.toLowerCase();const selected=snapshot.nodes.filter(n=>(!nodeType||(n.type||'未知')===nodeType)&&(!q||[n.id,n.label,n.type].join(' ').toLowerCase().includes(q)));
    const ids=new Set(selected.map(n=>n.id));return {nodes:selected,edges:snapshot.edges.filter(e=>ids.has(e.source)&&ids.has(e.target)&&(!edgeType||e.label===edgeType))};
  },search(text=''){const q=text.trim().toLowerCase();return q?snapshot.nodes.filter(n=>[n.id,n.label,n.type].join(' ').toLowerCase().includes(q)):[];},visible(nodeType='',edgeType=''){return this.filter('',nodeType,edgeType);},neighbors(id){return new Set([id,...(incoming.get(id)||[]).map(e=>e.source),...(outgoing.get(id)||[]).map(e=>e.target)]);}};
}
export function edgePath(edge,positions,index=0){const a=positions.get(edge.source),b=positions.get(edge.target);if(edge.source===edge.target)return `M ${a.x} ${a.y-15} C ${a.x+65} ${a.y-80} ${a.x-65} ${a.y-80} ${a.x-8} ${a.y-15}`;const dx=b.x-a.x,dy=b.y-a.y,length=Math.hypot(dx,dy)||1,offset=18+index*16;return `M ${a.x} ${a.y} Q ${(a.x+b.x)/2-dy/length*offset} ${(a.y+b.y)/2+dx/length*offset} ${b.x-dx/length*20} ${b.y-dy/length*20}`;}
export function edgeRoutes(edges){const groups=new Map(),routes=new Map();for(const e of [...edges].sort((a,b)=>a.id.localeCompare(b.id))){const key=JSON.stringify([e.source,e.target].sort());if(!groups.has(key))groups.set(key,[]);groups.get(key).push(e);}for(const group of groups.values())group.forEach((e,i)=>routes.set(e.id,e.source===e.target?{loopPlacement:'top',loopDist:40+i*18}:{curveOffset:(i-(group.length-1)/2)*32*(e.source<e.target?1:-1)}));return routes;}

