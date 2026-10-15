import {el,button} from './ui.js';
export function renderDetail(root,{model,selection,dataset,stale,tab,onTab,onChoose,onExplore,onRaw,rawContent}){
  root.replaceChildren();if(!model||!selection){root.append(el('h3',{text:'实体探索'}),el('p',{text:'选择节点查看属性与关联，或搜索当前子图定位实体。'}));return;}
  const node=selection.kind==='node',item=(node?model.nodes:model.edges).get(selection.id);if(!item)return;
  const name=id=>model.nodes.get(id)?.label||id;
  root.append(el('h3',{text:node?item.label||item.id:`${name(item.source)} → ${item.label} → ${name(item.target)}`}),el('p',{text:node?`${item.type||'未知'} · ${dataset}`:'当前快照内关系'}),el('p',{class:'memory-graph-id',text:item.id}),button('复制 ID',async()=>{try{await navigator.clipboard.writeText(item.id);}catch{root.append(el('p',{text:'复制失败，请选择上方 ID 文本复制。'}));}}));
  if(!node){root.append(el('p',{text:item.properties_provided?(Object.keys(item.properties||{}).length?'公开关系属性':'无可展示的公开属性'):'上游未提供关系属性'}),el('pre',{text:JSON.stringify(item.properties||{},null,2)}),button('查看起点',()=>onChoose('node',item.source)),button('查看终点',()=>onChoose('node',item.target)));return;}
  const tabs=el('div',{class:'memory-detail-tabs',role:'tablist','aria-label':'节点详情'});
  for(const [id,label]of [['properties','属性'],['relations','关联'],['raw','原文']]){const b=button(label,()=>onTab(id));b.setAttribute('role','tab');b.setAttribute('aria-selected',String(tab===id));tabs.append(b);}root.append(tabs);
  const body=el('section',{role:'tabpanel'});root.append(body);
  if(tab==='properties'){for(const key of ['description','category','value','created_at','updated_at'])body.append(el('p',{},el('strong',{text:key+'：'}),el('span',{text:item.properties?.[key]==null?'未提供':typeof item.properties[key]==='object'?JSON.stringify(item.properties[key]):String(item.properties[key])})));body.append(el('details',{},el('summary',{text:'公开属性 JSON'}),el('pre',{text:JSON.stringify(item.properties||{},null,2)})));}
  if(tab==='relations'){body.append(el('p',{text:'当前子图内'}));for(const [label,entries]of [['入边',model.incoming.get(item.id)],['出边',model.outgoing.get(item.id)]]){body.append(el('h4',{text:`${label} ${entries.length}`}));for(const e of entries)body.append(el('div',{class:'memory-relation'},button(name(e.source),()=>onChoose('node',e.source)),button(`→ ${e.label} →`,()=>onChoose('edge',e.id)),button(name(e.target),()=>onChoose('node',e.target))));}}
  if(tab==='raw'){if(item.data_id){const b=button('读取关联原文',onRaw);b.disabled=stale;body.append(b,rawContent);}else body.append(el('p',{text:'未提供已验证的原始数据关联'}));}
  const actions=el('div',{class:'memory-explore-actions'});for(const [depth,label]of [[1,'查看一跳子图'],[2,'查看两跳子图']]){const b=button(label,()=>onExplore(item.id,depth));b.disabled=stale;actions.append(b);}root.append(actions);if(stale)root.append(el('p',{text:'旧结果：刷新后恢复原文与子图读取。'}));
}
