import {el,button,pageHeader,notify,errorBox} from '../components/ui.js';

export function mount(root,{store,api}) {
  let kind='user',generation=0,disposed=false,saving=false;
  const selector=el('select',{'aria-label':'记忆类型'},
    el('option',{value:'user',text:'用户事实'}),el('option',{value:'note',text:'长期笔记'}),el('option',{value:'episode',text:'历史事件'}));
  pageHeader(root,'Memory','管理共享数据库记忆。新会话加载核心事实；已有会话可通过工具查询最新记录。',selector,button('刷新',()=>load()));
  const search=el('input',{type:'search',placeholder:'输入关键词搜索','aria-label':'搜索记忆'});
  const count=el('p',{class:'muted'}),entries=el('div',{class:'memory-entries'}),title=el('h2'),version=el('small',{class:'muted'});
  const subject=el('input',{type:'text',maxlength:200,placeholder:'主题（可选）','aria-label':'记忆主题'});
  const eventDate=el('input',{type:'text',maxlength:100,placeholder:'事件日期，如 2026-10-05（可选）','aria-label':'事件日期'});
  const textarea=el('textarea',{class:'memory-editor',rows:12,maxlength:5000,'aria-label':'记忆内容',placeholder:'写下一条值得记住的信息…'});
  const conflict=el('div',{class:'conflict',hidden:true},el('p',{text:'记录已变化。草稿已保留，请比较最新记录后采用当前版本。'}));
  conflict.append(button('采用当前版本',()=>{
    const d=draft(),latest=page()?.entries.find(entry=>entry.id===d.id);
    if(!latest){notify('请搜索或加载该记录的最新版本；若已删除，可将草稿另存为新条目。',true);return;}
    setDraft({...d,expected_version:latest.version,conflict:false});
  }),button('草稿另存为新条目',()=>setDraft({...draft(),action:'add',id:undefined,expected_version:undefined,conflict:false})));
  const save=button('保存',()=>write(),'primary'),remove=button('删除条目',()=>write('remove'),'danger');
  const more=button('加载更多',()=>load(true));
  root.append(el('div',{class:'two-columns memory-layout'},
    el('section',{class:'card'},el('div',{class:'actions'},search,button('搜索',()=>load())),
      button('＋ 新条目',()=>setDraft(emptyDraft())),count,entries,more),
    el('section',{class:'card memory-panel'},title,version,conflict,subject,eventDate,textarea,el('div',{class:'actions'},remove,save))));

  function emptyDraft(){return {action:'add',content:'',subject:'',event_at:''};}
  function draft(){return store.get().drafts.memoryByTarget[kind]||emptyDraft();}
  function page(){return store.get().server.memory[kind];}
  function setDraft(value){store.draft('memoryByTarget',kind,value);}
  for(const [node,key] of [[textarea,'content'],[subject,'subject'],[eventDate,'event_at']])
    node.addEventListener('input',()=>setDraft({...draft(),[key]:node.value}));
  selector.addEventListener('change',()=>{kind=selector.value;search.value='';render(true);load();});
  search.addEventListener('keydown',event=>{if(event.key==='Enter')load();});

  async function load(append=false){
    const own=kind,gen=++generation,old=page(),query=search.value.trim();
    append=append&&!query&&!!old?.next_cursor;
    const params=new URLSearchParams({limit:'50'});
    if(query)params.set('q',query);
    if(append&&old?.next_cursor&&!query)params.set('cursor',old.next_cursor);
    try{
      const value=await api.request('/api/memory/'+own+'?'+params);
      if(disposed||gen!==generation)return;
      if(append)value.entries=[...new Map([...(old?.entries||[]),...value.entries].map(entry=>[entry.id,entry])).values()];
      entries.signature=null;
      store.server('memory',{...store.get().server.memory,[own]:value});
    }catch(error){if(!disposed&&gen===generation){entries.signature=null;entries.replaceChildren(errorBox(error.message,()=>load()));}}
  }

  async function write(action){
    if(saving)return;
    const d=draft(),own=kind;action=action||d.action;
    if(action==='remove'&&!window.confirm('删除这条记忆？'))return;
    const body={action,id:d.id,expected_version:d.expected_version};
    if(action!=='remove')Object.assign(body,{content:d.content,subject:d.subject||'',event_at:d.event_at?.trim()||null});
    saving=true;render();
    try{
      const result=await api.request('/api/memory/'+own,{method:'PATCH',body:JSON.stringify(body)});
      store.draft('memoryByTarget',own,emptyDraft());
      notify(result.reason==='duplicate'?'相同记忆已存在':'记忆已保存');await load();
    }catch(error){
      notify(error.message,true);
      if(error.status===409||error.status===404){store.draft('memoryByTarget',own,{...d,conflict:true});search.value='';await load();}
    }finally{saving=false;if(!disposed)render(true);}
  }

  function render(force=false){
    const d=draft(),value=page(),latest=value?.entries.find(entry=>entry.id===d.id);
    title.textContent=d.action==='replace'?'编辑条目':'新增条目';
    version.textContent=d.id?'ID '+d.id+' · 编辑版本 '+d.expected_version+' · 当前版本 '+(latest?.version??'未加载'):'保存后生成记录 ID 和版本';
    const changed=d.action==='replace'&&latest&&latest.version!==d.expected_version;
    conflict.hidden=!(d.conflict||changed);
    for(const [node,key] of [[textarea,'content'],[subject,'subject'],[eventDate,'event_at']])
      if(force||document.activeElement!==node)node.value=d[key]||'';
    eventDate.hidden=kind!=='episode';selector.disabled=saving;
    save.disabled=saving||!d.content?.trim()||!!changed||!!d.conflict;
    remove.hidden=d.action!=='replace';remove.disabled=saving||!!changed||!!d.conflict;
    more.hidden=!value?.next_cursor;more.disabled=saving;
    count.textContent=value?value.entries.length+' 条已加载'+(value.total==null?'（搜索结果）':' / 共 '+value.total+' 条'):'读取中…';
    if(!value)return;
    const signature=JSON.stringify([value.entries,d.id]);
    if(entries.signature!==signature){
      entries.signature=signature;
      entries.replaceChildren(...value.entries.map(entry=>button(
        (entry.subject?entry.subject+' · ':'')+entry.content+(entry.event_at?' · '+entry.event_at:'')+'  (v'+entry.version+')',()=>{
          const current=draft();
          const keep=current.conflict||(current.id===entry.id&&current.expected_version!==entry.version);
          setDraft({action:'replace',id:entry.id,expected_version:entry.version,
            content:keep?current.content:entry.content,subject:keep?current.subject:entry.subject,
            event_at:keep?current.event_at:entry.event_at,conflict:false});render(true);
        },'memory-entry '+(entry.id===d.id?'selected':''))));
      if(!value.entries.length)entries.append(el('p',{class:'empty',text:'暂无匹配记忆。'}));
    }
  }
  const unsubscribe=store.subscribe(s=>s.server.memory,()=>render());
  const unsubscribeDraft=store.subscribe(s=>s.drafts.memoryByTarget,()=>render());
  const focus=()=>load();window.addEventListener('focus',focus);render();load();
  return ()=>{disposed=true;generation++;unsubscribe();unsubscribeDraft();window.removeEventListener('focus',focus);};
}
