import {el,button,pageHeader,errorBox,jsonView,confirmAction,notify} from '../components/ui.js';
const titles={tools:'工具',connections:'连接',models:'模型',config:'配置'};
export function mount(root,{store,api,page}) {
  let disposed=false,generation=0;
  pageHeader(root,titles[page],page==='config'?'记忆配置可保存并即时应用，其他配置修改后重启服务生效。':'当前进程的只读视图。',button('刷新',()=>load()));
  const content=el('div',{class:'catalog-grid'});root.append(content);
  if(page==='config'){
    let editing=false,loadingConfig=false;
    const editor=el('textarea',{class:'memory-config-editor',rows:20,spellcheck:'false','aria-label':'memory.conf TOML 配置'});
    const status=el('p',{role:'status','aria-live':'polite'});
    const reload=button('重新读取文件',()=>readConfig());
    const apply=button('保存并应用',async()=>{
      if(editing||loadingConfig)return;
      editing=true;controls();
      try{
        const drafts=Object.values(store.get().drafts.memoryByTarget);
        if(drafts.some(d=>d&&(d.dirty||d.uncertain||d.sync_failed||d.saved_unconfirmed||(d.dirty===undefined&&d.content)))){
          const approved=await confirmAction('应用记忆配置','存在未保存或待核对的记忆草稿。请先保存或核对；确认继续将清除这些草稿及旧的记忆浏览状态。');
          if(!approved||disposed)return;
        }
        status.textContent='正在保存并应用…';
        const result=await api.applyMemoryConfig(editor.value);
        if(result.applied!==true)throw new Error('未收到配置应用成功回执，请刷新生效配置核对。');
        store.resetMemory();
        if(disposed)return;
        status.textContent='已保存并应用，后续任务将使用新配置。';
        notify('记忆配置已应用');
        await load();
      }catch(error){
        if(!disposed)status.replaceChildren(errorBox(error.status?error.message:`应用结果待核对：${error.message}。请刷新生效配置确认。`));
      }finally{editing=false;if(!disposed)controls();}
    },'primary');
    function controls(){editor.disabled=editing||loadingConfig;reload.disabled=apply.disabled=editing||loadingConfig;}
    async function readConfig(){
      if(editing||loadingConfig)return;
      loadingConfig=true;controls();status.textContent='正在读取 memory.conf…';
      try{const data=await api.memoryConfig();if(disposed)return;editor.value=data.content;status.textContent='已读取文件，保存并应用后生效。';}
      catch(error){if(!disposed)status.replaceChildren(errorBox(error.message,()=>readConfig()));}
      finally{loadingConfig=false;if(!disposed)controls();}
    }
    root.append(el('section',{class:'card memory-config','aria-label':'记忆配置'},
      el('div',{class:'actions'},el('h2',{text:'记忆配置'}),reload,apply),
      el('p',{class:'muted',text:'编辑 memory.conf 的 TOML 文本，应用后影响所有会话。请先结束任务、复盘、导入和评测。凭据使用环境变量名引用。'}),
      editor,status,el('p',{class:'muted',text:'下方摘要显示当前实际生效配置。已有会话的系统提示保持冻结，需要重新加载画像时请创建新会话。'})));
    readConfig();
    const result=el('div',{'aria-live':'polite'});
    const check=button('检查记忆检索',async()=>{
      check.disabled=true;result.replaceChildren(el('p',{text:'正在检查记忆来源…'}));
      try{const data=await api.request('/diagnostics/memory');if(disposed)return;
        result.replaceChildren(...data.sources.map(source=>el('article',{class:'card'},
          el('h3',{text:`${source.source_id} · ${source.status}`}),
          el('p',{text:`检查时间：${source.checked_at}`}),jsonView(source))));
        if(!data.sources.length)result.replaceChildren(el('p',{text:'没有可检查的授权来源。'}));
      }catch(error){if(!disposed)result.replaceChildren(errorBox(error.message));}
      finally{if(!disposed)check.disabled=false;}
    });
    root.append(el('section',{'aria-label':'记忆检索检查'},el('h2',{text:'记忆检索检查'}),
      el('p',{class:'muted',text:'手动执行只读检索，显示各来源的检查时间和结果。应用就绪仅表示初始化完成。'}),check,result));
  }
  async function load(){const gen=++generation;content.classList.add('loading');try{
    const data=await api.request(`/api/${page}`);if(disposed||gen!==generation)return;store.server('catalog',{...store.get().server.catalog,[page]:data});content.replaceChildren();
    if(page==='tools')for(const tool of data.tools){const card=el('article',{class:'card catalog-item'},el('header',{},el('h2',{text:tool.name}),el('span',{class:'badge',text:tool.toolset})),el('p',{text:tool.description}),el('div',{class:'tags'},el('span',{class:'badge',text:tool.requires_approval?'需要审批':'按调用策略执行'}),el('span',{class:'badge',text:`副作用：${tool.side_effects}`})),el('details',{},el('summary',{text:'参数与权限'}),jsonView(tool)));content.append(card);}
    else if(page==='models')for(const route of data.routes)content.append(el('article',{class:'card catalog-item'},el('header',{},el('h2',{text:route.name}),el('span',{class:'badge',text:route.provider})),el('h3',{text:route.model}),el('p',{class:'muted',text:route.base_url||'默认 endpoint'}),jsonView(route)));
    else if(page==='connections'){for(const [name,backend]of Object.entries(data.backends))content.append(el('article',{class:'card catalog-item'},el('h2',{text:name}),el('span',{class:'badge',text:backend.status}),jsonView(backend)));for(const server of data.mcp_servers)content.append(el('article',{class:'card catalog-item'},el('header',{},el('h2',{text:server.id}),el('span',{class:'badge',text:server.status})),jsonView(server.config)));}
    else {const card=el('article',{class:'card config-card'},el('p',{class:'muted',text:'密钥、环境变量与请求头值已屏蔽。'}),jsonView(data));content.append(card);card.querySelector('details').open=true;}
    if(!content.childNodes.length)content.append(el('div',{class:'empty',text:'没有配置项目。'}));
  }catch(e){if(!disposed&&gen===generation)content.replaceChildren(errorBox(e.message,()=>load()));}finally{if(gen===generation)content.classList.remove('loading');}}
  load();return()=>{disposed=true;generation++;};
}
