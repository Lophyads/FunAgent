import {el,button,pageHeader,errorBox,jsonView} from '../components/ui.js';
const titles={tools:'工具',connections:'连接',models:'模型',config:'配置'};
export function mount(root,{store,api,page}) {
  let disposed=false,generation=0;
  pageHeader(root,titles[page],'当前进程的只读视图。配置文件修改后，重启服务生效。',button('刷新',()=>load()));
  const content=el('div',{class:'catalog-grid'});root.append(content);
  async function load(){const gen=++generation;content.classList.add('loading');try{
    const data=await api.request(`/api/${page}`);if(disposed||gen!==generation)return;store.server('catalog',{...store.get().server.catalog,[page]:data});content.replaceChildren();
    if(page==='tools')for(const tool of data.tools){const card=el('article',{class:'card catalog-item'},el('header',{},el('h2',{text:tool.name}),el('span',{class:'badge',text:tool.toolset})),el('p',{text:tool.description}),el('div',{class:'tags'},el('span',{class:'badge',text:tool.requires_approval?'需要审批':'按调用策略执行'}),el('span',{class:'badge',text:`副作用：${tool.side_effects}`})),el('details',{},el('summary',{text:'参数与权限'}),jsonView(tool)));content.append(card);}
    else if(page==='models')for(const route of data.routes)content.append(el('article',{class:'card catalog-item'},el('header',{},el('h2',{text:route.name}),el('span',{class:'badge',text:route.provider})),el('h3',{text:route.model}),el('p',{class:'muted',text:route.base_url||'默认 endpoint'}),jsonView(route)));
    else if(page==='connections'){for(const [name,backend]of Object.entries(data.backends))content.append(el('article',{class:'card catalog-item'},el('h2',{text:name}),el('span',{class:'badge',text:backend.status})));for(const server of data.mcp_servers)content.append(el('article',{class:'card catalog-item'},el('header',{},el('h2',{text:server.id}),el('span',{class:'badge',text:server.status})),jsonView(server.config)));}
    else {const card=el('article',{class:'card config-card'},el('p',{class:'muted',text:'密钥、环境变量与请求头值已屏蔽。'}),jsonView(data));content.append(card);card.querySelector('details').open=true;}
    if(!content.childNodes.length)content.append(el('div',{class:'empty',text:'没有配置项目。'}));
  }catch(e){if(!disposed&&gen===generation)content.replaceChildren(errorBox(e.message,()=>load()));}finally{if(gen===generation)content.classList.remove('loading');}}
  load();return()=>{disposed=true;generation++;};
}
