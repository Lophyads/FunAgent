import {el,button,pageHeader,notify,errorBox} from '../components/ui.js';
export function mount(root,{store,api}) {
  let target='memory',generation=0,disposed=false,saving=false;
  const selector=el('select',{'aria-label':'选择记忆文档'},el('option',{value:'memory',text:'MEMORY.md'}),el('option',{value:'user',text:'USER.md'}));
  pageHeader(root,'Memory','编辑共享记忆条目。已有会话继续使用原提示词快照。',selector,button('刷新',()=>load()));
  const count=el('div',{class:'memory-capacity'}),entries=el('div',{class:'memory-entries'}),editorTitle=el('h2'),version=el('small',{class:'muted'}),conflict=el('div',{class:'conflict',hidden:true});
  const textarea=el('textarea',{class:'memory-editor',rows:12,'aria-label':'记忆条目内容',placeholder:'写下一条值得记住的信息…'});
  const save=button('保存',()=>write(),'primary'),remove=button('删除条目',()=>write('remove'),'danger'),rebase=button('采用当前版本',()=>{
    const d=draft(),doc=document();if(!doc)return;
    if(d.action==='replace'&&!doc.entries.includes(d.old_text)){notify('原条目已更改，请在左侧选择最新条目；编辑内容会保留。',true);return;}
    setDraft({...d,baseVersion:doc.version,conflict:false});
  });
  conflict.append(el('p',{text:'文件已变化。草稿已保留；请确认最新条目与版本后再保存。'}),rebase);
  const panel=el('section',{class:'card memory-panel'},editorTitle,version,conflict,textarea,el('div',{class:'actions'},remove,save));
  const listing=el('section',{class:'card'},button('＋ 新条目',()=>setDraft({action:'add',content:'',old_text:'',baseVersion:document()?.version})),count,entries);
  root.append(el('div',{class:'two-columns memory-layout'},listing,panel));
  function draft(){return store.get().drafts.memoryByTarget[target]||{action:'add',content:'',old_text:'',baseVersion:document()?.version};}
  function document(){return store.get().server.memory[target];}
  function setDraft(d){store.draft('memoryByTarget',target,d);}
  textarea.addEventListener('input',()=>setDraft({...draft(),content:textarea.value}));
  selector.addEventListener('change',()=>{target=selector.value;render(true);load();});
  async function load(){const own=target,gen=++generation;try {
    const doc=await api.request(`/api/memory/${own}`);if(disposed||gen!==generation)return;
    store.server('memory',{...store.get().server.memory,[own]:doc});
    const d=store.get().drafts.memoryByTarget[own];if(!d)setDraft({action:'add',content:'',old_text:'',baseVersion:doc.version});
  }catch(e){if(!disposed&&gen===generation)entries.replaceChildren(errorBox(e.message,()=>load()));}}
  async function write(action){if(saving)return;const d=draft(),own=target;action=action||d.action;
    if(action==='remove'&&!window.confirm('删除这条记忆？'))return;
    saving=true;render();try{const doc=await api.request(`/api/memory/${own}`,{method:'PATCH',body:JSON.stringify({action,content:d.content,old_text:d.old_text,expected_version:d.baseVersion})});
      store.server('memory',{...store.get().server.memory,[own]:doc});store.draft('memoryByTarget',own,{action:'add',content:'',old_text:'',baseVersion:doc.version});notify('记忆已保存');
    }catch(e){notify(e.message,true);if(e.status===409){store.draft('memoryByTarget',own,{...d,conflict:true});await load();}}
    finally{saving=false;render(true);}}
  function render(force=false){const d=draft(),doc=document();editorTitle.textContent=d.action==='replace'?'编辑条目':'新增条目';
    version.textContent=`编辑基于版本 ${(d.baseVersion||'').slice(0,12)} · 当前 ${(doc?.version||'').slice(0,12)}`;
    conflict.hidden=!(d.conflict || d.content&&doc&&d.baseVersion!==doc.version);
    if(force||window.document.activeElement!==textarea)if(textarea.value!==d.content)textarea.value=d.content;
    save.disabled=saving||!doc||!d.content?.trim();remove.hidden=d.action!=='replace';remove.disabled=saving;
    if(!doc){count.textContent='读取中…';return;}
    const proposed=[...doc.entries];if(d.action==='replace'){const idx=proposed.indexOf(d.old_text);if(idx>=0)proposed[idx]=d.content;}else if(d.content)proposed.push(d.content);
    const chars=Array.from(proposed.join('\n§\n')).length;count.textContent=`当前 ${doc.chars} / ${doc.limit} 字符 · 编辑后约 ${chars}`;
    count.classList.toggle('error',chars>doc.limit);
    const key=JSON.stringify([doc.entries,d.old_text]);if(entries.signature!==key){entries.signature=key;entries.replaceChildren(...doc.entries.map((entry,i)=>button(`${String(i+1).padStart(2,'0')}  ${entry}`,()=>{
      const current=draft();setDraft({action:'replace',old_text:entry,content:current.conflict?current.content:entry,baseVersion:document().version,conflict:false});render(true);
    },`memory-entry ${entry===d.old_text?'selected':''}`)));if(!doc.entries.length)entries.append(el('p',{class:'empty',text:'记忆库为空。添加第一条记忆。'}));}
  }
  const unsubscribe=store.subscribe(s=>s.server.memory,()=>render()),unsubDraft=store.subscribe(s=>s.drafts.memoryByTarget,()=>render());
  const focus=()=>load(),poll=setInterval(()=>{if(!window.document.hidden)load();},5000);window.addEventListener('focus',focus);load();
  return ()=>{disposed=true;generation++;unsubscribe();unsubDraft();clearInterval(poll);window.removeEventListener('focus',focus);};
}
