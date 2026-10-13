import {el, button, markdown, notify, statusText} from './ui.js';
import {terminal} from '../store.js';

export function mount(root, {store, api, commands}) {
  const sessions = el('select', {'aria-label':'选择会话'}), newButton = button('＋', () => commands.newSession());
  const sessionLabel = el('div', {class:'session-id'});
  const historyButton = button('加载更早消息', async () => {const sid=store.get().ui.activeSessionId; const history=store.get().server.messagesBySession[sid];
    try {await api.history(sid,{before:history.before});} catch(e) {notify(e.message,true);}});
  const messages = el('div', {class:'messages', role:'log', 'aria-label':'聊天消息'});
  const messageNodes = new Map(), interactionNodes = new Map();
  const interactions = el('div', {class:'interactions'}), turnStatus = el('div', {class:'chat-status'}), turnQueue=el('div',{class:'turn-queue'});
  const textarea = el('textarea', {placeholder:'向 FunAgent 提问…', rows:3, maxlength:16000, 'aria-label':'消息内容'});
  const send = button('发送 ↵', () => commands.send(), 'primary'), stop = button('停止', () => commands.stop().catch(e => notify(e.message,true)));
  const uncertain = el('div', {class:'uncertain', text:'提交结果不确定；点击重试原提交，或先核对历史。', hidden:true});
  const composer = el('div',{class:'composer'}, uncertain, textarea, el('div',{class:'composer-actions'}, el('small',{text:'Shift + Enter 换行'}), stop, send));
  root.replaceChildren(el('header', {class:'chat-header'}, el('div',{},el('h2',{text:'ChatDock'}), el('small',{text:'与当前会话交互'})), newButton),
    el('div',{class:'session-picker'},sessions,sessionLabel), historyButton, messages, interactions, turnStatus, turnQueue, composer);
  let active, sessionKey='', composing=false, frame=null, disposed=false;
  sessions.addEventListener('change', () => api.selectSession(sessions.value));
  textarea.addEventListener('input', () => {const sid=store.get().ui.activeSessionId; store.draft('chatBySession',sid,{...store.get().drafts.chatBySession[sid],text:textarea.value});});
  textarea.addEventListener('compositionstart', () => composing=true);
  textarea.addEventListener('compositionend', () => composing=false);
  textarea.addEventListener('keydown', e => {if (e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&!composing) {e.preventDefault(); commands.send();}});

  function patchMessages(sid) {
    const state=store.get(), history=state.server.messagesBySession[sid], rows=history?.messages || [];
    historyButton.hidden = !history?.has_more;
    const turns=Object.values(state.server.turns).filter(t=>t.session_id===sid).sort((a,b)=>a.created_at-b.created_at);
    // Durable assistants without tool calls are final replies committed by FinishTurn.
    const items = rows.filter(m=>m.role==='user'||(m.role==='assistant'&&!m.tool_calls?.length))
      .map(m=>({key:`db:${m.id}`,role:m.role,text:m.content || m.api_content || '',turn_id:m.metadata?.turn_id,id:m.id}));
    for (const t of turns) {
      const own=items.filter(m=>m.turn_id===t.turn_id);
      if (t.input&&!own.some(m=>m.role==='user')) items.push({key:`input:${t.turn_id}`,role:'user',text:t.input,status:'提交', turn_id:t.turn_id});
    }
    // Group pending user input with its durable turn while preserving history order.
    const groups=new Map(), order=[];
    for(const item of items) {
      const key=item.turn_id||item.key;
      if(!groups.has(key)){groups.set(key,[]);order.push(key);}
      groups.get(key).push(item);
    }
    order.sort((a,b)=>{const x=state.server.turns[a]?.created_at,y=state.server.turns[b]?.created_at;
      return x==null&&y==null?0:x==null?-1:y==null?1:x-y;});
    items.splice(0,items.length,...order.flatMap(key=>groups.get(key).sort((a,b)=>
      a.role==='user'&&b.role!=='user'?-1:b.role==='user'&&a.role!=='user'?1:0)));
    const atBottom=messages.scrollHeight-messages.scrollTop-messages.clientHeight<70;
    const oldHeight=messages.scrollHeight, oldTop=messages.scrollTop;
    const keys=new Set();
    if(!items.length) {
      let empty=messages.querySelector('.chat-empty'); if(!empty) messages.append(el('div',{class:'chat-empty'},el('div',{class:'orbit',text:'F'}),el('h3',{text:'开启一次对话'}),el('p',{text:'在这里查看你的问题与最终回复。'})));
    } else messages.querySelector('.chat-empty')?.remove();
    let predecessor=null;
    for(const item of items) {
      keys.add(item.key); let node=messageNodes.get(item.key);
      if(!node) {node=el('article',{class:`message ${item.role}`},el('header'),el('div',{class:'message-body'}));messageNodes.set(item.key,node);}
      const signature=JSON.stringify(item);
      if(node.signature!==signature) {
        node.signature=signature;
        const title={user:'YOU',assistant:'FUNAGENT'}[item.role];
        node.firstChild.replaceChildren(el('span',{text:title}),el('small',{text:item.status||''}),button('复制',()=>navigator.clipboard.writeText(item.text).then(()=>notify('已复制')).catch(()=>notify('复制失败',true))));
        const body=node.lastChild;
        body.innerHTML=markdown(item.text);
      }
      if(node.parentNode!==messages || node.previousSibling!==predecessor) messages.insertBefore(node,predecessor?predecessor.nextSibling:messages.firstChild);
      predecessor=node;
    }
    for(const [key,node] of messageNodes) if(!keys.has(key)) {node.remove();messageNodes.delete(key);}
    if(atBottom) messages.scrollTop=messages.scrollHeight;
    else if(messages.scrollHeight>oldHeight && history?.before!==messages.oldBefore) messages.scrollTop=oldTop+messages.scrollHeight-oldHeight;
    messages.oldBefore=history?.before;
    const live=turns.filter(t=>!terminal(t.status));
    turnStatus.textContent=live.length?`${live.length} 个在途轮次 · ${live.map(t=>statusText(t.status)).join(' / ')}`:turns.length?statusText(turns.at(-1).status):'等待输入';
    stop.disabled=!live.length;
    const liveKey=JSON.stringify(live.map(t=>[t.turn_id,t.status]));
    if(turnQueue.signature!==liveKey){turnQueue.signature=liveKey;turnQueue.replaceChildren();
      if(live.length>1)for(const t of live)turnQueue.append(el('div',{class:'queue-row'},el('span',{text:`${t.turn_id.slice(0,8)} · ${statusText(t.status)}`}),button('停止此轮',()=>commands.stop(t.turn_id).catch(e=>notify(e.message,true)))));
    }
  }
  function patchInteractions() {
    const requests=store.get().server.interactions, keys=new Set();
    for(const request of requests) {
      keys.add(request.request_id); if(interactionNodes.has(request.request_id)) continue;
      const form=el('form',{class:'interaction-card'},el('h3',{text:request.kind==='approval'?'工具审批':'需要补充信息'}));
      if(request.kind==='approval') {
        form.append(el('p',{text:`${request.payload.tool_name} · ${request.payload.scope}`}),el('details',{},el('summary',{text:'查看调用参数'}),el('pre',{text:JSON.stringify(request.payload.arguments,null,2)})));
        for(const choice of request.payload.choices) form.append(button(choice,()=>commands.resolve(request,{decision:choice})));
      } else {
        const fields=[];
        for(const q of request.payload.questions) {
          const group=el('fieldset',{},el('legend',{text:q.question})), choices=[];
          if(q.multi_select) for(const choice of q.choices) {const input=el('input',{type:'checkbox',value:choice}); choices.push(input);group.append(el('label',{},input,el('span',{text:choice})));}
          const input=q.kind==='single'?el('select',{'aria-label':`人工答复：${q.question}`},el('option',{value:'',text:'请选择'}),...q.choices.map(c=>el('option',{value:c,text:c}))):el('input',{type:'text','aria-label':`人工答复：${q.question}`,placeholder:q.multi_select?'选择答案':'输入答案',maxlength:4000});
          if(q.multi_select)input.hidden=true;
          group.dataset.questionId=q.qid;
          group.append(input);form.append(group);fields.push({q,input,choices});
        }
        form.append(el('button',{type:'submit',class:'primary',text:'提交答复'}));
        form.addEventListener('submit',e=>{e.preventDefault();const answers={};for(const {q,input,choices} of fields) answers[q.qid]=q.multi_select?[...choices.filter(c=>c.checked).map(c=>c.value),...(input.value.trim()?[input.value.trim()]:[])]:input.value.trim();commands.resolve(request,{answers});});
      }
      form.append(el('small',{text:`有效期至 ${new Date(request.expires_at*1000).toLocaleTimeString()}`}));
      interactions.append(form);interactionNodes.set(request.request_id,form);
    }
    for(const [key,node] of interactionNodes) if(!keys.has(key)) {node.remove();interactionNodes.delete(key);}
  }
  function render() {
    if(disposed) return;
    const state=store.get(),sid=state.ui.activeSessionId;
    if(active!==sid) {active=sid;textarea.value=state.drafts.chatBySession[sid]?.text || '';messages.scrollTop=messages.scrollHeight;}
    sessionLabel.textContent=sid;
    const key=JSON.stringify([sid,state.server.sessions]);
    if(sessionKey!==key) {sessionKey=key; sessions.replaceChildren();
      const values=[...state.server.sessions];if(!values.some(s=>s.id===sid))values.unshift({id:sid,title:'新会话'});
      for(const s of values)sessions.append(el('option',{value:s.id,text:`${s.title || '未命名会话'} · ${s.id.slice(0,8)}`}));sessions.value=sid;
    }
    const draft=state.drafts.chatBySession[sid] || {};
    send.disabled=!!draft.submitting || !sid || (!draft.uncertain&&!draft.text?.trim()); send.textContent=draft.submitting?'提交中…':draft.uncertain?'重试原提交':'发送 ↵';uncertain.hidden=!draft.uncertain;
    if(document.activeElement!==textarea&&textarea.value!==(draft.text||'')) textarea.value=draft.text||'';
    patchMessages(sid);patchInteractions();
  }
  const schedule=()=>{if(!frame)frame=requestAnimationFrame(()=>{frame=null;render();});};
  const unsub=[store.subscribe(s=>s.server.turns,schedule),store.subscribe(s=>s.server.messagesBySession,schedule),store.subscribe(s=>s.server.sessions,schedule),
    store.subscribe(s=>s.server.interactions,schedule),store.subscribe(s=>s.ui.activeSessionId,schedule),store.subscribe(s=>s.drafts.chatBySession,schedule)];
  return ()=>{disposed=true;unsub.forEach(fn=>fn());if(frame)cancelAnimationFrame(frame);};
}
