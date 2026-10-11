import {applyEvent, terminal} from './store.js';
import {notify, confirmAction} from './components/ui.js';

const types={user:'profile',note:'note',episode:'episode'};
export const dateLabel=value=>{if(!value)return '';const d=new Date(value);return Number.isNaN(d.getTime())?value:`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;};
export function dateISO(value){
  if(!value?.trim())return null;
  if(/^\d{4}-\d{2}-\d{2}$/.test(value)){const d=new Date(`${value}T00:00:00`);if(Number.isNaN(d.getTime())||dateLabel(d.toISOString())!==value)throw new Error('请输入有效的事件日期。');return d.toISOString();}
  if(!/T.*(?:Z|[+-]\d\d:\d\d)$/.test(value)||Number.isNaN(Date.parse(value)))throw new Error('请输入日期或带时区的 ISO 时间。');return new Date(value).toISOString();
}
export class ApiClient {
  constructor(store){this.store=store;this.source=null;this.generations={};this.seq=-1;this.epoch=null;this.pending=[];
    this.historyTimers=new Map();this.historyFlights=new Map();this.closed=false;this.expiryTimer=setInterval(()=>this.interactions().catch(()=>{}),5000);}
  async wire(path,options={}){const response=await fetch(path,{...options,headers:{'Content-Type':'application/json',...options.headers}});
    let data;try{data=await response.json();}catch{data={};}if(!response.ok){const e=new Error(data.error?.message||`HTTP ${response.status}`);e.status=response.status;e.code=data.error?.code;throw e;}return data;}
  async request(path,options={}){
    // Logical page resources are translated here; no legacy HTTP routes exist.
    const memory=path.match(/^\/api\/memory\/(user|note|episode)(?:\?(.*))?$/);
    if(memory)return options.method==='PATCH'?this.writeMemory(memory[1],JSON.parse(options.body)):this.readMemory(memory[1],new URLSearchParams(memory[2]));
    if(path.startsWith('/api/data/'))return this.wire(path.slice(4),options);
    const catalog=path.match(/^\/api\/(tools|connections|models|config|status)$/);
    if(catalog){const kind=catalog[1],data=await this.wire(`/diagnostics/${kind}`,options);
      if(kind==='models')return {routes:data.profiles.map(p=>({...p,name:p.profile_id,provider:p.provider_id}))};
      if(kind==='connections')return {backends:Object.fromEntries(data.connections.filter(c=>c.kind!=='mcp').map(c=>[c.backend_id,{...c,config:data.settings[c.backend_id],status:c.available===true?'就绪':c.available===false?'不可用':'未选择'}])),
        mcp_servers:data.connections.filter(c=>c.kind==='mcp').map(c=>({...c,id:c.backend_id,status:c.available===true?'就绪':c.available===false?'不可用':'未选择',config:data.settings[c.backend_id]}))};
      return data;}
    return this.wire(path,options);
  }
  async resource(name,path,key=name){const generation=this.generations[key]=(this.generations[key]||0)+1,value=await this.request(path);if(generation===this.generations[key])this.store.server(name,value);return value;}
  async sessions(){let items=[],cursor;do{const data=await this.wire(`/sessions?limit=200${cursor?'&cursor='+encodeURIComponent(cursor):''}`);items.push(...data.items);cursor=data.next_cursor;}while(cursor);
    this.store.server('sessions',items.map(s=>({...s,id:s.session_id})).sort((a,b)=>b.created_at-a.created_at));return items;}
  async createSession(){const session=await this.wire('/sessions',{method:'POST',body:JSON.stringify({title:'新会话'})});await this.sessions();this.selectSession(session.session_id);return session.session_id;}
  async tasks(sid){const value=await this.wire(`/sessions/${encodeURIComponent(sid)}/tasks?limit=200`);
    this.store.update(s=>{const turns={...s.server.turns};for(const t of value.items){const old=turns[t.turn_id]||{attempts:{},tools:{},phases:{},timeline:[]};turns[t.turn_id]={...old,...t,status:!terminal(t.status)&&['waiting','cancelling'].includes(old.status)?old.status:t.status};}return {...s,server:{...s.server,turns}};});return value;}
  async history(sid,options={}){const old=this.store.get().server.messagesBySession[sid],earlier=options.before||options.cursor,high=old?.messages.at(-1)?.id||0;let cursor=earlier,rows=[],first;
    do{const data=await this.wire(`/sessions/${encodeURIComponent(sid)}/messages?direction=backward&limit=${options.limit||50}${cursor?'&cursor='+encodeURIComponent(cursor):''}`);first||=data;rows.push(...data.items);cursor=data.next_cursor;if(earlier||!high||data.items.some(m=>m.message_id<=high))break;}while(cursor);
    this.store.update(s=>{const previous=s.server.messagesBySession[sid]||{messages:[]},merged=new Map(previous.messages.map(m=>[m.id,m]));
      for(const m of rows)merged.set(m.message_id,{...m,id:m.message_id,metadata:{turn_id:m.task_id},tool_calls:m.calls.map(c=>({id:c.call_id,name:c.name,arguments:JSON.stringify(c.args)})),tool_outcome_state:m.status});
      const messages=[...merged.values()].sort((a,b)=>a.id-b.id),names=new Map(messages.flatMap(m=>(m.tool_calls||[]).map(c=>[c.id,c.name])));for(const m of messages)if(m.role==='tool')m.tool_name=names.get(m.call_id)||'TOOL';
      const value={...previous,messages};if(earlier||!previous.messages.length){value.has_more=!!cursor;value.before=cursor;}return {...s,server:{...s.server,messagesBySession:{...s.server.messagesBySession,[sid]:value}}};});return first;}
  scheduleHistory(sid){if(this.closed||this.historyTimers.has(sid))return;this.historyTimers.set(sid,setTimeout(async()=>{this.historyTimers.delete(sid);if(this.historyFlights.has(sid)){this.scheduleHistory(sid);return;}this.historyFlights.set(sid,true);
    try{await this.history(sid,{limit:200});}catch(e){notify(e.message,true);}finally{this.historyFlights.delete(sid);}},180));}
  async interactions(){const sid=this.store.get().ui.activeSessionId;if(!sid||this.closed)return;const turns=Object.values(this.store.get().server.turns).filter(t=>t.session_id===sid&&!terminal(t.status));
    const pages=await Promise.all(turns.map(t=>this.wire(`/tasks/${encodeURIComponent(t.turn_id)}/interactions`)));if(this.store.get().ui.activeSessionId!==sid||this.closed)return;
    const items=pages.flatMap(p=>p.items).map(r=>({...r,request_id:r.interaction_id,session_id:sid,payload:r.kind==='approval'?{tool_name:r.approval?.tool_name,scope:r.prompt,arguments:r.approval,choices:['批准','拒绝']}:
      {questions:r.questions.map(q=>({qid:q.id,question:q.prompt,kind:q.kind,choices:q.options||[],multi_select:q.kind==='multiple'}))}}));this.store.server('interactions',items);}
  selectSession(sid){if(this.frame){cancelAnimationFrame(this.frame);this.frame=null;}this.source?.close();this.pending=[];this.seq=-1;this.epoch=null;
    this.store.ui('activeSessionId',sid);sessionStorage.setItem('funagent.session',sid);this.store.server('interactions',[]);this.store.ui('connectionStatus','connecting');
    this.history(sid).catch(e=>notify(e.message,true));this.tasks(sid).then(()=>this.interactions()).catch(e=>notify(e.message,true));
    const source=this.source=new EventSource(`/sessions/${encodeURIComponent(sid)}/events`);source.onopen=()=>{if(source===this.source)this.store.ui('connectionStatus','connected');};source.onerror=()=>{if(source===this.source)this.store.ui('connectionStatus','reconnecting');};
    source.addEventListener('update',event=>{if(source!==this.source)return;let e;try{e=JSON.parse(event.data);}catch{return;}if(e.session_id!==sid)return;
      if(!['snapshot','reset'].includes(e.kind)&&e.epoch===this.epoch&&e.seq<=this.seq)return;this.epoch=e.epoch;this.seq=e.seq;this.pending.push(e);
      if(!this.frame)this.frame=requestAnimationFrame(()=>{this.frame=null;for(const item of this.pending.splice(0)){applyEvent(this.store,item);
        if(['snapshot','reset','turn.finished'].includes(item.kind))this.scheduleHistory(sid);
        if(['snapshot','reset','turn.finished'].includes(item.kind)||item.kind.startsWith('interaction.'))this.interactions().catch(()=>{});
        if(item.kind==='turn.finished')this.sessions().catch(()=>{});}});});}
  async sourceInfo(){const data=await this.wire('/memory/sources');return data.sources.find(s=>s.source_id===data.default_source_id)||null;}
  async readMemory(kind,params){const source=await this.sourceInfo();if(!source)return {entries:[],disabled:true,next_cursor:null};const q=new URLSearchParams({source_id:source.source_id,type:types[kind],limit:params.get('limit')||'50'}),search=params.get('q');
    if(search)q.set('text',search);else if(params.get('cursor'))q.set('cursor',params.get('cursor'));const data=await this.wire(`/memory/${search?'search':'records'}?${q}`);
    return {...data,entries:data.records.map(r=>({...r,id:r.record_id,kind,event_timestamp:r.event_at,event_at:dateLabel(r.event_at)})),writable:source.writable,capabilities:source.capabilities,source_id:source.source_id};}
  async writeMemory(kind,body){const source=await this.sourceInfo();if(!source)throw new Error('未启用可管理的记忆来源。');const metadata={content:body.content,type:types[kind],subject:body.subject?.trim()||null,
    event_at:kind==='episode'?(body.event_timestamp&&dateLabel(body.event_timestamp)===body.event_at?body.event_timestamp:dateISO(body.event_at)):null};
    if(body.action==='add')return this.wire('/memory/records',{method:'POST',body:JSON.stringify({source_id:source.source_id,...metadata,request_key:body.request_key})});
    const proposal=await this.wire('/memory/changes',{method:'POST',body:JSON.stringify({action:body.action,source_id:source.source_id,record_id:body.id,expected_version:body.expected_version,...(body.action==='replace'?{patch:metadata}:{})})});
    const approve=await confirmAction(body.action==='remove'?'删除记忆':'确认修改记忆',`来源：${proposal.source_id}\n记录：${proposal.record_id}\n版本：${proposal.expected_version}\n\n${proposal.patch?.content||'确认后删除这条记录。'}`);
    const result=await this.wire(`/memory/challenges/${proposal.challenge_id}/confirm`,{method:'POST',body:JSON.stringify({arguments_digest:proposal.arguments_digest,approve})});return {...result,declined:!approve};}
  close(){this.closed=true;this.source?.close();clearInterval(this.expiryTimer);for(const timer of this.historyTimers.values())clearTimeout(timer);if(this.frame)cancelAnimationFrame(this.frame);}
}
export function createCommands(store,api){return {
  async newSession(){try{await api.createSession();}catch(e){notify(e.message,true);}},
  async send(){const sid=store.get().ui.activeSessionId,draft=store.get().drafts.chatBySession[sid]||{};if(!sid||draft.submitting||(!draft.uncertain&&!draft.text?.trim()))return;
    const text=draft.uncertain?draft.pendingText:draft.text,requestId=draft.requestId||crypto.randomUUID();store.draft('chatBySession',sid,{...draft,requestId,pendingText:text,submitting:true});
    try{const submitted=await api.wire(`/sessions/${sid}/tasks`,{method:'POST',body:JSON.stringify({text,request_key:requestId})});if(!/^[A-Za-z0-9_-]{1,128}$/.test(submitted?.task_id||''))throw new Error('提交响应缺少任务 ID。');const current=store.get().drafts.chatBySession[sid];store.draft('chatBySession',sid,{text:current.text===text?'':current.text});api.tasks(sid).catch(e=>notify(e.message,true));api.scheduleHistory(sid);}
    catch(e){const current=store.get().drafts.chatBySession[sid],uncertain=!e.status||e.status>=500;store.draft('chatBySession',sid,{...current,submitting:false,uncertain,requestId:uncertain?requestId:null});notify(uncertain?'提交结果不确定。重试将使用同一请求 ID；也可先检查历史。':e.message,true);}},
  async stop(tid){const sid=store.get().ui.activeSessionId,turns=Object.values(store.get().server.turns).filter(t=>t.session_id===sid&&!terminal(t.status)).sort((a,b)=>a.created_at-b.created_at);const t=tid?turns.find(t=>t.turn_id===tid):turns.find(t=>t.status!=='queued')||turns[0];if(t)await api.wire(`/tasks/${t.turn_id}/cancel`,{method:'POST',body:'{}'});},
  async resolve(request,response){try{const answer=request.kind==='approval'?response.decision==='批准':{answers:request.payload.questions.map(q=>({question_id:q.qid,value:response.answers[q.qid]}))};
    await api.wire(`/interactions/${request.request_id}/answer`,{method:'POST',body:JSON.stringify({task_id:request.task_id,run_id:request.run_id,answer})});await api.interactions();}catch(e){notify(e.message,true);await api.interactions();}},
};}
