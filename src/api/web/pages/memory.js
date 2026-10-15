import {el,button,pageHeader,notify,errorBox} from '../components/ui.js';
import {dateLabel} from '../api.js';
import {mountInspection} from '../components/memory-datasets.js';

const labels={all:'全部',user:'用户事实',note:'长期笔记',episode:'历史事件',knowledge:'知识文档'};
const recordTypes={user:'profile',note:'note',episode:'episode',knowledge:'knowledge'};
const drivers={sqlite:'SQLite',supabase:'Supabase',cognee:'Cognee'};

export function mount(root,{store,api}) {
  let sources=[],sourceId='',kind='user',generation=0,disposed=false,saving=false,importing=false,loading=false,loadError=null;
  const sourceSelector=el('select',{'aria-label':'记忆库'},el('option',{text:'加载记忆库…',value:''}));
  const selector=el('select',{'aria-label':'记忆类型'});
  const file=el('input',{type:'file',accept:'.md',hidden:true,'aria-label':'Markdown 文件'});
  const upload=button('导入 Markdown',()=>file.click()),refresh=button('刷新',()=>{if(['data','graph'].includes(store.get().ui.memoryInspectionTab)&&source()?.inspection)inspection.refresh();else load();});
  pageHeader(root,'Memory','选择记忆库，导入知识文档或管理已有记录。',sourceSelector,selector,upload,refresh);
  root.classList.add('memory-page');
  const scope=el('p',{class:'muted memory-scope'});
  const search=el('input',{type:'search',placeholder:'输入关键词搜索','aria-label':'搜索记忆'});
  const searchButton=button('搜索',()=>load()),newEntry=button('＋ 新条目',()=>setDraft(emptyDraft()));
  const count=el('p',{class:'muted','aria-live':'polite'}),entries=el('div',{class:'memory-entries'}),title=el('h2'),version=el('small',{class:'muted memory-version'});
  const subject=el('input',{type:'text',maxlength:512,placeholder:'标题或主题（可选）','aria-label':'记忆主题'});
  const origin=el('input',{type:'text',maxlength:512,placeholder:'来源文件名（可选）','aria-label':'来源文件名'});
  const eventDate=el('input',{type:'text',maxlength:100,placeholder:'事件日期，如 2026-10-05（可选）','aria-label':'事件日期'});
  const textarea=el('textarea',{class:'memory-editor',rows:16,maxlength:65536,'aria-label':'记忆内容',placeholder:'记忆正文或 Markdown 原文…'});
  const status=el('p',{class:'muted','aria-live':'polite'});
  const conflict=el('div',{class:'conflict',hidden:true},el('p',{text:'记录已变化。草稿已保留，请比较最新记录后采用当前版本。'}));
  const adopt=button('采用当前版本',()=>{
    const d=draft(),latest=page()?.entries.find(entry=>entry.id===d.id);
    if(!latest||page()?.readonly){notify('请加载该记录的最新版本；若已删除，可将草稿另存为新条目。',true);return;}
    setDraft({...d,expected_version:latest.version,conflict:false,uncertain:false,failure:undefined,uncertain_action:undefined,reconciled:undefined});
  });
  const saveAsNew=button('草稿另存为新条目',()=>setDraft({...draft(),action:'add',id:undefined,expected_version:undefined,request_key:undefined,conflict:false,uncertain:false,failure:undefined,uncertain_action:undefined,reconciled:undefined}));
  conflict.append(adopt,saveAsNew);
  const save=button('保存',()=>write(),'primary'),remove=button('删除条目',()=>write('remove'),'danger'),more=button('加载更多',()=>load(true));
  root.append(file,scope,el('div',{class:'two-columns memory-layout'},
    el('section',{class:'card'},el('div',{class:'actions memory-search'},search,searchButton),newEntry,count,entries,more),
    el('section',{class:'card memory-panel'},title,version,conflict,subject,origin,eventDate,textarea,status,el('div',{class:'actions'},remove,save))));
  const layout=root.querySelector('.memory-layout');layout.id='memory-panel-records';layout.setAttribute('role','tabpanel');layout.setAttribute('aria-labelledby','memory-tab-records');
  const inspectionRoot=el('div');layout.before(inspectionRoot);
  const inspection=mountInspection(inspectionRoot,{store,api,onTab:tab=>{const browsing=tab!=='records';layout.hidden=selector.hidden=upload.hidden=scope.hidden=browsing;}});

  function source(){return sources.find(item=>item.source_id===sourceId);}
  function target(){return JSON.stringify([sourceId,kind]);}
  function busy(){return saving||importing;}
  function can(capability){return Boolean(source()?.capabilities?.includes(capability)&&(!['write','update','delete'].includes(capability)||source().writable));}
  function emptyDraft(){return {action:'add',type:recordTypes[kind],content:'',subject:'',origin:'',event_at:''};}
  function draft(){return store.get().drafts.memoryByTarget[target()]||emptyDraft();}
  function page(){return store.get().server.memory[target()];}
  function setDraft(value){store.draft('memoryByTarget',target(),value);}
  function recordDraft(entry,readonly=false){return {action:readonly||!entry.version?'view':'replace',id:entry.id||entry.record_id,expected_version:entry.version,
    type:entry.type,content:entry.content,subject:entry.subject||'',origin:entry.origin||'',event_at:entry.event_at||'',event_timestamp:entry.event_timestamp};}
  function selectRecord(entry){
    if(busy())return;
    const current=draft(),keep=current.action==='replace'&&current.id===entry.id&&(current.conflict||current.expected_version!==entry.version);
    const next=recordDraft(entry,page()?.readonly);
    setDraft(keep?{...current,...next,content:current.content,subject:current.subject,origin:current.origin,event_at:current.event_at,conflict:false}:next);
    render(true);
  }
  function changeTarget(nextSource,nextKind){
    sourceId=nextSource;
    const kinds=source()?.store==='cognee'?['note']:source()?.kind==='semantic'?['knowledge']:Object.keys(labels);
    kind=kinds.includes(nextKind)?nextKind:kinds[0];
    store.ui('memorySourceId',sourceId);store.ui('memoryKind',kind);
    selector.replaceChildren(...kinds.map(value=>el('option',{value,text:labels[value]})));
    selector.value=kind;sourceSelector.value=sourceId;search.value='';loadError=null;generation++;
    entries.signature=null;render(true);load();
    inspection.setSource(source());
  }
  for(const [node,key] of [[textarea,'content'],[subject,'subject'],[origin,'origin'],[eventDate,'event_at']])
    node.addEventListener('input',()=>setDraft({...draft(),request_key:undefined,uncertain:false,failure:undefined,uncertain_action:undefined,reconciled:undefined,[key]:node.value}));
  sourceSelector.addEventListener('change',()=>changeTarget(sourceSelector.value,kind));
  selector.addEventListener('change',()=>changeTarget(sourceId,selector.value));
  search.addEventListener('keydown',event=>{if(event.key==='Enter'&&!busy())load();});
  file.addEventListener('change',()=>importMarkdown());

  async function importMarkdown(){
    const selected=file.files?.[0];file.value='';
    if(!selected||busy()||!can('write'))return;
    importing=true;render();
    try{
      if(!/\.md$/i.test(selected.name))throw new Error('请选择 .md 文件。');
      if(selected.size>65536)throw new Error('文档超过 64 KiB，请缩小文档或拆成多个文件。');
      let content;
      try{content=new TextDecoder('utf-8',{fatal:true}).decode(await selected.arrayBuffer());}catch{throw new Error('文档必须使用 UTF-8 编码。');}
      if(!content.trim()||content.length>65536)throw new Error('文档内容为空或超过长度上限。');
      if(disposed)return;
      const importedKind=source()?.store==='cognee'?'note':'knowledge';
      changeTarget(sourceId,importedKind);
      setDraft({action:'add',type:recordTypes[importedKind],content,subject:selected.name.replace(/\.md$/i,''),origin:selected.name,event_at:'',imported:true});
      notify('文档已载入，点击“导入文档”保存至所选记忆库。');
    }catch(error){if(!disposed)notify(error.message,true);}
    finally{importing=false;if(!disposed)render(true);}
  }

  async function load(append=false){
    if(!source())return false;
    const ownSource=source(),own=target(),gen=++generation,old=page(),query=search.value.trim();
    append=append&&!query&&!!old?.next_cursor;
    const params=new URLSearchParams({limit:'50'});
    if(query)params.set('q',query);
    if(append)params.set('cursor',old.next_cursor);
    loading=true;loadError=null;render();
    try{
      const value=await api.readMemory(ownSource,kind,params);
      if(disposed||gen!==generation)return false;
      if(append)value.entries=[...new Map([...(old?.entries||[]),...value.entries].map(entry=>[entry.id,entry])).values()];
      store.server('memory',{...store.get().server.memory,[own]:value});
      return true;
    }catch(error){if(!disposed&&gen===generation)loadError=error.message;return false;}
    finally{if(!disposed&&gen===generation){loading=false;entries.signature=null;render();}}
  }

  async function write(action){
    if(busy()||draft().action==='view')return;
    const d={...draft()},ownSource=source(),ownKind=kind,own=target();action=action||d.action;
    const body={...d,action};
    if(action==='add'){body.request_key=d.request_key||crypto.randomUUID();store.draft('memoryByTarget',own,{...d,request_key:body.request_key});}
    saving=true;render();
    try{
      const result=await api.writeMemory(ownSource,ownKind,body);
      if(result.declined){if(!disposed)notify('已放弃操作');return;}
      const next=action==='remove'?emptyDraft():recordDraft({...result,event_timestamp:result.event_at,event_at:dateLabel(result.event_at)});
      store.draft('memoryByTarget',own,next);
      inspection.invalidate();
      if(!disposed){search.value='';notify(action==='remove'?'记忆已删除':'记忆已保存');await load();}
    }catch(error){
      if(error.status===409||error.status===404){store.draft('memoryByTarget',own,{...store.get().drafts.memoryByTarget[own],conflict:true,
        uncertain:false,failure:undefined,uncertain_action:undefined,reconciled:undefined});if(!disposed){search.value='';await load();}}
      else{
        const uncertain=(!error.status||error.status>=500)&&!['EMBEDDING_SOURCE','EMBEDDING_PROTOCOL'].includes(error.code);
        store.draft('memoryByTarget',own,{...store.get().drafts.memoryByTarget[own],failure:error.message,uncertain,
          uncertain_action:uncertain?action:undefined,reconciled:undefined});
        if(uncertain&&action!=='add'&&!disposed){
          search.value='';
          const reconciled=await load();
          if(!disposed)store.draft('memoryByTarget',own,{...store.get().drafts.memoryByTarget[own],reconciled});
        }
      }
      if(!disposed)notify(error.message,true);
    }finally{saving=false;if(!disposed)render(true);}
  }

  function render(force=false){
    inspection.busy(busy());
    const d=draft(),value=page(),selected=source(),latest=!value?.readonly&&value?.entries.find(entry=>entry.id===d.id);
    const changed=d.action==='replace'&&latest&&latest.version!==d.expected_version;
    const readonly=d.action==='view'||!can(d.action==='replace'?'update':'write');
    title.textContent=d.action==='view'?'查看检索结果':d.action==='replace'?'编辑'+(labels[Object.keys(recordTypes).find(k=>recordTypes[k]===d.type)]||'条目'):d.imported?'导入 Markdown':'新增条目';
    version.textContent=d.id?'来源 '+sourceId+' · 记录 '+d.id+' · 版本 '+(d.expected_version||'只读'):selected?'目标：'+sourceId:'请先配置可读取的记忆来源';
    scope.textContent=[selected?.source_description,selected?.store==='cognee'?'仅管理和检索本项目的长期笔记。':''].filter(Boolean).join(' · ');
    conflict.hidden=!(d.conflict||changed);adopt.disabled=busy()||!latest?.version;saveAsNew.disabled=busy()||!can('write');
    for(const [node,key] of [[textarea,'content'],[subject,'subject'],[origin,'origin'],[eventDate,'event_at']]){
      if(force||document.activeElement!==node)node.value=d[key]||'';
      node.readOnly=busy()||readonly;
    }
    eventDate.hidden=d.type!=='episode';sourceSelector.disabled=busy()||!sources.length;selector.disabled=busy()||!selected;
    upload.disabled=busy()||!can('write');refresh.disabled=busy()||loading||!selected;
    newEntry.disabled=busy()||!can('write')||kind==='all';search.disabled=busy()||!can('search');searchButton.disabled=busy()||loading||!can('search');
    save.textContent=d.imported&&d.action==='add'?'导入文档':'保存';
    save.disabled=Boolean(busy()||readonly||!d.type||!d.content?.trim()||changed||d.conflict);
    remove.hidden=d.action!=='replace';remove.disabled=Boolean(busy()||changed||d.conflict||!can('delete'));
    more.hidden=!value?.next_cursor||!!search.value.trim();more.disabled=busy()||loading;
    status.textContent=saving?(d.imported?'导入处理中…':'保存处理中…'):importing?'读取文档…':d.uncertain?(d.uncertain_action&&d.uncertain_action!=='add'?
      '操作结果未确认，草稿已保留。'+(d.reconciled?'列表已自动刷新，请核对当前记录状态后再决定是否重试。':'自动刷新失败，请手动刷新核对后再决定是否重试。'):
      '操作结果未确认，草稿已保留。请刷新核对；原样重试导入会使用同一请求标识。')+(d.failure?' 原因：'+d.failure:''):d.failure?d.failure+' 草稿已保留，修复后可重试。':d.action==='view'?'搜索结果仅供查看；编辑或删除请清空搜索，在管理列表中选择记录。':d.type==='knowledge'?'Markdown 原文保存；请求体最多 64 KiB。':'';
    count.textContent=loadError?'读取失败':loading?'读取中…':value?.no_list?'此来源只支持搜索，无法列出可管理记录。':value?value.entries.length+' 条已加载'+(value.readonly?' · 搜索结果只读':''):selected?'读取中…':'没有可读取的记忆来源，请检查 memory.conf 和管理读取权限。';
    const signature=JSON.stringify([value,d.id,busy(),loadError]);
    if(entries.signature!==signature){
      entries.signature=signature;
      if(loadError){entries.replaceChildren(errorBox(loadError,()=>load()));return;}
      entries.replaceChildren(...(value?.entries||[]).map(entry=>{
        const item=button('',()=>selectRecord(entry),'memory-entry '+(entry.id===d.id?'selected':''));item.disabled=busy();
        item.append(el('strong',{text:entry.subject||entry.origin||entry.type}),el('p',{class:'memory-summary',text:entry.content.slice(0,160)+(entry.content.length>160?'…':'')}),
          el('small',{text:[entry.origin,entry.updated_at?dateLabel(entry.updated_at):'',entry.version?'v'+entry.version.slice(0,8):'只读'].filter(Boolean).join(' · ')}));return item;
      }));
      if(value&&!value.entries.length&&!value.no_list)entries.append(el('p',{class:'empty',text:'暂无匹配记忆。'}));
    }
  }
  const unsubscribe=store.subscribe(s=>s.server.memory,()=>render());
  const unsubscribeDraft=store.subscribe(s=>s.drafts.memoryByTarget,()=>render());
  const focus=()=>{if(!busy())load();};window.addEventListener('focus',focus);
  async function initialize(){
    try{
      const data=await api.memorySources();if(disposed)return;sources=data.sources;
      sourceSelector.replaceChildren(...sources.map(item=>el('option',{value:item.source_id,text:item.source_id+' · '+(drivers[item.store]||item.store)})));
      store.migrateMemoryDrafts(data.default_source_id&&sources.some(item=>item.source_id===data.default_source_id)?data.default_source_id:null);
      if(['user','note','episode','knowledge'].some(key=>store.get().drafts.memoryByTarget[key]))notify('旧草稿尚未绑定来源；请恢复默认来源配置后再载入。',true);
      const selected=sources.find(item=>item.source_id===store.get().ui.memorySourceId)||sources.find(item=>item.source_id===data.default_source_id)||sources[0];
      if(selected)changeTarget(selected.source_id,store.get().ui.memoryKind||'user');else{sourceSelector.replaceChildren(el('option',{value:'',text:'无可用记忆库'}));render(true);}
    }catch(error){if(!disposed){sourceSelector.replaceChildren(el('option',{value:'',text:'来源读取失败'}));render();entries.replaceChildren(errorBox(error.message,()=>initialize()));}}
  }
  render();initialize();
  return ()=>{disposed=true;generation++;inspection.close();unsubscribe();unsubscribeDraft();window.removeEventListener('focus',focus);root.classList.remove('memory-page');};
}
