import {applyEvent, terminal} from './store.js';
import {notify} from './components/ui.js';

const types={user:'profile',note:'note',episode:'episode',knowledge:'knowledge'};
function memoryRecordReceipt(value){
  if(!value?.record_id||!value.version||typeof value.content!=='string')throw new Error('正文操作未返回完整记录回执，结果未确认，请刷新核对。');
  return value;
}
export function memoryJSON(value){
  const body=JSON.stringify(value);
  if(new TextEncoder().encode(body).byteLength>65536){const error=new Error('请求超过 64 KiB，请缩小文档或拆成多个文件。');error.status=413;error.code='PAYLOAD_TOO_LARGE';throw error;}
  return body;
}
export const dateLabel=value=>{if(!value)return '';const d=new Date(value);return Number.isNaN(d.getTime())?value:`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;};
export function dateISO(value){
  if(!value?.trim())return null;
  if(/^\d{4}-\d{2}-\d{2}$/.test(value)){const d=new Date(`${value}T00:00:00`);if(Number.isNaN(d.getTime())||dateLabel(d.toISOString())!==value)throw new Error('请输入有效的事件日期。');return d.toISOString();}
  if(!/T.*(?:Z|[+-]\d\d:\d\d)$/.test(value)||Number.isNaN(Date.parse(value)))throw new Error('请输入日期或带时区的 ISO 时间。');return new Date(value).toISOString();
}
export class ApiClient {
  constructor(store){this.store=store;this.source=null;this.generations={};this.seq=-1;this.epoch=null;this.pending=[];
    this.historyTimers=new Map();this.historyFlights=new Map();this.closed=false;this.expiryTimer=setInterval(()=>this.interactions().catch(()=>{}),5000);}
  async wire(path,options={}){const response=await fetch(path,{...options,headers:{...(options.body instanceof FormData?{}:{'Content-Type':'application/json'}),...options.headers}});
    let data;try{data=await response.json();}catch{data={};}if(!response.ok){const e=new Error(data.error?.message||`HTTP ${response.status}`);e.status=response.status;e.code=data.error?.code;e.path=data.error?.path;
      for(const key of ['effect','ref','version','issues'])e[key]=data.error?.[key];throw e;}return data;}
  traceSessions({limit=50,offset=0,signal}={}){return this.wire(`/trace-sessions?limit=${limit}&offset=${offset}`,{signal});}
  traceOverview({sessionId,unassigned=false,limit=50,offset=0,signal}={}){const query=new URLSearchParams({view:'overview',limit,offset});if(unassigned)query.set('unassigned','1');else if(sessionId)query.set('session_id',sessionId);return this.wire(`/traces?${query}`,{signal});}
  traceReview(taskId,{signal}={}){return this.wire(`/tasks/${encodeURIComponent(taskId)}/trace`,{signal});}
  evalOptions({signal}={}){return this.wire('/eval/options',{signal});}
  evalRuns({signal}={}){return this.wire('/eval/runs',{signal});}
  evalRun(runId,{signal}={}){return this.wire(`/eval/runs/${encodeURIComponent(runId)}`,{signal});}
  createEval(body){return this.wire('/eval/runs',{method:'POST',body:JSON.stringify(body)});}
  async request(path,options={}){
    // Logical page resources are translated here; no legacy HTTP routes exist.
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
      {questions:r.questions.map(q=>({qid:q.id,question:q.prompt,choices:q.options}))}}));this.store.server('interactions',items);}
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
  submitMemoryImport(sourceId,file){const body=new FormData();body.append('source_id',sourceId);body.append('file',file);return this.wire('/memory/imports',{method:'POST',body});}
  memoryImport(id){return this.wire(`/memory/imports/${encodeURIComponent(id)}`);}
  latestMemoryImport(sourceId){return this.wire(`/memory/imports?${new URLSearchParams({source_id:sourceId})}`);}
  memoryConfig(){return this.wire('/memory/config');}
  applyMemoryConfig(content){return this.wire('/memory/config',{method:'POST',body:memoryJSON({content})});}
  memorySources(){return this.wire('/memory/sources');}
  memoryTopics(source){return this.wire(`/memory/topics?${new URLSearchParams({source_id:source.source_id})}`);}
  createMemoryTopic(source,topic){return this.wire('/memory/topics',{method:'POST',body:memoryJSON({source_id:source.source_id,...topic})});}
  updateMemoryTopic(source,topicId,changes,revision){return this.wire(`/memory/topics/${encodeURIComponent(topicId)}`,{method:'PATCH',body:memoryJSON({source_id:source.source_id,...changes,expected_revision:revision})});}
  async deleteMemoryTopic(source,topicId,revision){
    const result=await this.wire(`/memory/topics/${encodeURIComponent(topicId)}`,{method:'DELETE',body:memoryJSON({source_id:source.source_id,expected_revision:revision})});
    if(result.deleted!==true||!Number.isInteger(result.revision))throw new Error('删除主题未返回完整回执，结果未确认，请刷新核对。');
    return result;
  }
  assignMemoryTopic(source,recordId,topicId,revision){return this.wire(`/memory/records/${encodeURIComponent(recordId)}/topic`,{method:'PATCH',body:memoryJSON({source_id:source.source_id,topic_id:topicId,expected_revision:revision})});}
  inspectMemory(operation,params,{signal}={}){return this.wire(`/memory/cognee/${operation}?${new URLSearchParams(params)}`,{signal});}
  async readMemory(source,kind,params=new URLSearchParams()){
    const search=params.get('q'),capabilities=source.capabilities||[];
    if(!search&&!capabilities.includes('list'))return {entries:[],next_cursor:null,no_list:true};
    const q=new URLSearchParams({source_id:source.source_id,limit:params.get('limit')||'50'});
    if(types[kind])q.set('type',types[kind]);
    if(search)q.set('text',search);else if(params.get('cursor'))q.set('cursor',params.get('cursor'));
    if(!search){for(const key of ['topic_id','unclassified'])if(params.has(key))q.set(key,params.get(key));}
    const data=await this.wire(`/memory/${search?'search':'records'}?${q}`);
    if(['failed','timeout'].includes(data.source_status?.[source.source_id])){
      const issue=data.issues?.[0],error=new Error(`记忆检索未完成${issue?.code?'：'+issue.code:''}，请检查服务后重试。`);
      error.code=issue?.code;error.issues=data.issues;error.effect='none';throw error;
    }
    return {...data,readonly:Boolean(search),entries:data.records.map(r=>({...r,id:r.record_id,event_timestamp:r.event_at,event_at:dateLabel(r.event_at)}))};
  }
  async writeMemory(source,kind,body){
    const operation={add:'write',replace:'update',remove:'delete'}[body.action];
    if(!source?.writable||!operation||!source.capabilities?.includes(operation)){const error=new Error('当前来源没有此操作的能力或写权限。');error.status=403;throw error;}
    const type=body.type||types[kind];
    if(body.action!=='remove'&&!type){const error=new Error('请选择新增记忆的类型。');error.status=400;throw error;}
    let metadata;
    try{metadata=body.action==='remove'?null:{content:body.content,type,subject:body.subject?.trim()||null,origin:body.origin?.trim()||null,
      event_at:type==='episode'?(body.event_timestamp&&dateLabel(body.event_timestamp)===body.event_at?body.event_timestamp:dateISO(body.event_at)):body.event_timestamp||null};}
    catch(error){error.status=400;throw error;}
    if(body.action==='add'){
      if(!body.topic_id){const error=new Error('请选择所属主题；没有合适主题时请先新建主题。');error.status=400;error.code='INVALID_REQUEST';throw error;}
      return memoryRecordReceipt(await this.wire('/memory/records',{method:'POST',body:memoryJSON({source_id:source.source_id,...metadata,topic_id:body.topic_id,request_key:body.request_key})}));
    }
    const result=await this.wire('/memory/changes',{method:'POST',body:memoryJSON({action:body.action,source_id:source.source_id,record_id:body.id,expected_version:body.expected_version,...(body.action==='replace'?{patch:metadata}:{})})});
    if(body.action==='remove'){if(result.deleted!==true)throw new Error('删除操作未返回完整回执，结果未确认，请刷新核对。');return result;}
    return memoryRecordReceipt(result);}
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
    await api.wire(`/interactions/${request.request_id}/answer`,{method:'POST',body:JSON.stringify({task_id:request.task_id,run_id:request.run_id,answer})});await api.interactions();return {answered:true};}catch(e){notify(e.message,true);await api.interactions().catch(()=>{});return {error:e.message};}},
};}
