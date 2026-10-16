import {el,button,errorBox} from '../components/ui.js';
import {buildTrace,dateTime,seconds,shortId,resultText,collectionText} from './observability-model.js';
import {metric,traceDetail,savedInfo,diagnosticsView} from '../components/trace-detail.js';

const reviewCache=new Map(),viewStates=new Map(),sessionStates=new Map();
const validId=v=>/^[A-Za-z0-9_-]{1,128}$/.test(v||'');
const sessionKey=sid=>sid||'@unassigned';
function stored(key,fallback){try{return JSON.parse(sessionStorage.getItem(`funagent.obs.${key}`))||fallback;}catch{return fallback;}}
function save(key,value){try{sessionStorage.setItem(`funagent.obs.${key}`,JSON.stringify(value));}catch{}}
function remember(task,review){reviewCache.delete(task);reviewCache.set(task,review);while(reviewCache.size>5){const oldest=reviewCache.keys().next().value;reviewCache.delete(oldest);viewStates.delete(oldest);}}
function href(sid,task,span,offset=0){const q=new URLSearchParams();if(sid)q.set('session',sid);else q.set('unassigned','1');if(task)q.set('task',task);if(span)q.set('span',span);q.set('offset',offset);return `#observability?${q}`;}
const navigate=(sid,task,span,offset)=>{location.hash=href(sid,task,span,offset);};

export function mount(root,{api,routeParams=new URLSearchParams()}) {
  root.classList.add('observability-page');const shell=root.closest('.shell'),previousLive=root.getAttribute('aria-live');root.setAttribute('aria-live','off');
  const taskId=routeParams.get('task'),spanId=routeParams.get('span'),requestedSession=routeParams.get('session');
  let selectedSpan=spanId;
  if(!sessionStates.has(sessionKey(requestedSession)))sessionStates.set(sessionKey(requestedSession),stored(`session.${sessionKey(requestedSession)}`,{}));
  let selectedSession=requestedSession||null,unassigned=routeParams.get('unassigned')==='1',disposed=false,flight=null,generation=0,sessions=[],nextSessions=null,detailView=!!taskId;
  let offset=Math.max(0,routeParams.has('offset')?Number(routeParams.get('offset')):sessionStates.get(sessionKey(selectedSession))?.offset||0);offset=Number.isFinite(offset)?Math.floor(offset):0;
  const invalid=(taskId&&!validId(taskId))||(requestedSession&&!validId(requestedSession))||(spanId&&!/^[0-9a-f]{16}$/.test(spanId))||(unassigned&&requestedSession);
  const content=el('div',{class:'obs-content'}),live=el('div',{class:'obs-live',role:'status','aria-live':'polite'}),select=el('select',{'aria-label':'选择会话'});
  const refresh=button('刷新数据',()=>load(true),'obs-refresh'),moreSessions=button('更多会话',loadMoreSessions,'obs-refresh');moreSessions.hidden=true;
  const exports=el('div',{class:'obs-export-actions'}),toolbar=el('div',{class:'obs-toolbar'},select,moreSessions,refresh,exports);
  root.replaceChildren(el('header',{class:'obs-page-header'},el('div',{},el('div',{class:'obs-eyebrow',text:'TRACE REVIEW'}),el('h1',{text:'Observability'}),el('p',{text:'从已保存轨迹回看每一轮会话的执行过程。'})),toolbar),live,content);
  function setDetailMode(active){detailView=active;root.classList.toggle('obs-detail-view',active);shell?.classList.toggle('obs-detail-wide',active);}
  setDetailMode(detailView);
  select.addEventListener('change',()=>{const sid=select.value==='@unassigned'?null:select.value;navigate(sid);});
  function updateSessions(){
    select.replaceChildren(...sessions.map(s=>el('option',{value:sessionKey(s.session_id),text:s.session_id?`会话 ${shortId(s.session_id)} · ${s.turn_count} 轮`:`未关联会话 · ${s.turn_count} 条`})));
    const key=sessionKey(selectedSession);
    if(!sessions.some(s=>sessionKey(s.session_id)===key)&&selectedSession)select.append(el('option',{value:key,text:`会话 ${shortId(selectedSession)}`}));
    select.value=key;select.hidden=select.options.length<2;moreSessions.hidden=nextSessions==null;
  }
  async function loadMoreSessions(){
    moreSessions.disabled=true;
    try{const page=await api.traceSessions({offset:nextSessions,signal:flight?.signal});if(disposed)return;sessions.push(...page.items);nextSessions=page.next_offset;updateSessions();}
    catch(error){if(!disposed&&error.name!=='AbortError')live.textContent=`会话列表读取失败：${error.message}`;}
    finally{moreSessions.disabled=false;}
  }
  async function load(force=false){
    if(invalid){content.replaceChildren(errorBox('链接中的会话、任务或节点标识无效。'));return;}
    flight?.abort();flight=new AbortController();const signal=flight.signal,current=++generation;refresh.disabled=true;live.textContent=detailView?'正在读取执行详情…':'正在读取会话概览…';
    try{
      const sessionsPromise=api.traceSessions({signal});
      const detailPromise=taskId?(!force&&reviewCache.has(taskId)?Promise.resolve(reviewCache.get(taskId)):api.traceReview(taskId,{signal})):null;
      const [sessionResult,detailResult]=await Promise.allSettled([sessionsPromise,detailPromise]);
      if(disposed||current!==generation)return;
      if(sessionResult.status==='fulfilled'){sessions=sessionResult.value.items;nextSessions=sessionResult.value.next_offset;}
      else if(!taskId&&!requestedSession&&!unassigned)throw sessionResult.reason;
      if(taskId){
        if(detailResult.status==='rejected')throw detailResult.reason;
        const review=detailResult.value;selectedSession=review.session_id;unassigned=!selectedSession;remember(taskId,review);updateSessions();history.replaceState(null,'',href(selectedSession,taskId,selectedSpan,offset));renderDetail(review,force);
      }else{
        if(!requestedSession&&!unassigned){selectedSession=sessions[0]?.session_id??null;unassigned=!selectedSession;}
        if(!sessionStates.has(sessionKey(selectedSession)))sessionStates.set(sessionKey(selectedSession),stored(`session.${sessionKey(selectedSession)}`,{}));
        history.replaceState(null,'',href(selectedSession,null,null,offset));
        updateSessions();const page=await api.traceOverview({sessionId:selectedSession,unassigned,offset,signal});if(disposed||current!==generation)return;
        if(page.total&&offset>=page.total){offset=Math.floor((page.total-1)/50)*50;navigate(selectedSession,null,null,offset);return;}
        renderOverview(page,sessionResult.status==='fulfilled'?sessionResult.value.diagnostics:[]);
      }
      if(!live.textContent.startsWith('指定节点'))live.textContent=sessionResult.status==='rejected'?'会话列表暂时不可用，已显示所选记录。':force?'已读取最新保存版本。':'';
    }catch(error){if(!disposed&&current===generation&&error.name!=='AbortError'){content.replaceChildren(errorBox(error.message,()=>load(true)));live.textContent='读取失败，可重试。';}}
    finally{if(!disposed&&current===generation)refresh.disabled=false;}
  }
  function renderOverview(page,globalDiagnostics){
    setDetailMode(false);exports.replaceChildren();
    const summary=page.session_summary||{},cards=el('section',{class:'obs-turns','aria-label':'会话轮次'},el('div',{class:'obs-section-heading'},el('h2',{text:'会话轮次'}),el('span',{text:`共 ${page.total} 轮`})));
    for(const item of page.items){
      if(reviewCache.get(item.task_id)?.saved_seq!==item.saved_seq)reviewCache.delete(item.task_id);
      const v=item.overview||{},count=(o,k='success')=>`${o?.[k]??0}/${o?.total??0}`;
      const process=`记忆 ${count(v.retrieval)}${v.rerank?.total?` · 重排 ${count(v.rerank)}`:''} · Loop ${count(v.loops,'completed')} · Tool ${count(v.tools)} · ${seconds(v.duration_ms)} · ${resultText(item.task_status)}`;
      const card=button('',()=>navigate(selectedSession,item.task_id,null,offset),'obs-turn card');card.dataset.taskId=item.task_id;
      card.append(el('span',{class:'obs-turn-number',text:`${selectedSession?'TURN':'记录'} ${String(item.turn_number).padStart(2,'0')}`}),el('span',{class:'obs-turn-main'},el('span',{class:'obs-turn-line'},el('b',{text:'问题'}),el('span',{text:(v.user_preview||'未记录用户输入')+(v.user_preview_truncated?'…':'')})),el('span',{class:'obs-turn-line obs-turn-process'},el('b',{text:'过程'}),el('span',{text:process})),item.collection_state!=='complete'||!item.io_complete?el('span',{class:'obs-completeness',text:`${collectionText(item.collection_state)}${!item.io_complete?' · 内容有缺口':''}`}):null),el('span',{class:'obs-turn-arrow','aria-hidden':'true',text:'↗'}));cards.append(card);
    }
    if(!page.items.length)cards.append(el('div',{class:'obs-empty card'},el('h2',{text:'暂无轨迹'}),el('p',{text:'当前会话没有可读取的已保存轨迹。'})));
    const overview=el('section',{class:'obs-summary card','aria-label':'会话概览'},el('div',{class:'obs-section-heading'},el('h2',{text:'会话概览'}),el('span',{text:`最近记录 ${dateTime(summary.latest_started_at)}`})),el('div',{class:'obs-summary-grid'},metric('会话轮次',String(summary.turn_count??page.total).padStart(2,'0'),'条已保存轨迹'),metric('平均耗时',seconds(summary.average_duration_ms),`${summary.duration_sample_count||0} 轮已结束且有耗时记录`)));
    const pagination=el('div',{class:'obs-pagination'},button('上一页',()=>navigate(selectedSession,null,null,Math.max(0,offset-50))),el('span',{text:page.total?`${offset+1}–${Math.min(offset+50,page.total)} / ${page.total}`:'0 / 0'}),button('下一页',()=>navigate(selectedSession,null,null,page.next_offset)));
    pagination.firstChild.disabled=offset===0;pagination.lastChild.disabled=page.next_offset==null;pagination.hidden=page.total<=50;
    const diagnostics=diagnosticsView(page.diagnostics?.length?page.diagnostics:globalDiagnostics);
    content.replaceChildren(overview,cards,pagination,...(diagnostics?[diagnostics]:[]));
    const state=sessionStates.get(sessionKey(selectedSession));root.scrollTop=state?.scrollTop||0;
    if(state?.taskId){const target=[...cards.querySelectorAll('[data-task-id]')].find(e=>e.dataset.taskId===state.taskId);target?.focus({preventScroll:true});}
  }
  function renderDetail(review,refreshing){
    setDetailMode(true);const model=buildTrace(review),saved=stored(`task.${taskId}`,{}),state=viewStates.get(taskId)||{expanded:new Set(saved.expanded||[]),scrollTop:saved.scrollTop||0};viewStates.set(taskId,state);
    if(refreshing)state.scrollTop=root.scrollTop;
    for(const id of state.expanded)if(!model.nodes.has(id))state.expanded.delete(id);
    exports.replaceChildren(...['json','markdown'].filter(type=>type!=='json'||review.trace).map(type=>el('a',{href:`/tasks/${encodeURIComponent(taskId)}/trace/${type}`,download:`${taskId}.${type==='json'?'json':'md'}`,text:`导出 ${type==='json'?'JSON':'Markdown'}`,title:'导出服务端当前保存文件'})));
    const title=el('h2',{text:'执行详情',tabindex:'-1'}),detail=el('div',{class:'obs-detail'},button('← 返回会话概览',()=>navigate(selectedSession,null,null,sessionStates.get(sessionKey(selectedSession))?.offset||offset),'obs-back'),el('header',{class:'obs-detail-header'},el('div',{},el('span',{class:'obs-eyebrow',text:`TASK ${shortId(taskId)}`}),title),el('span',{class:'obs-detail-time',text:dateTime(model.root?.started_at)})),el('section',{class:'obs-conversation card'},el('span',{class:'obs-field-label',text:'用户问题'}),el('p',{text:model.user})));
    if(!review.trace)detail.append(el('div',{class:'obs-empty card'},el('h2',{text:review.collection_state==='not_collected'?'此任务未采集轨迹':'保存的轨迹无法解析'}),el('p',{text:'可查看下方诊断与文件信息。'})));
    else{
      const all=[...model.nodes.values()],loops=all.filter(n=>n.name==='agent.iteration'&&(n.purpose||'main')==='main'),tools=all.filter(n=>(n.name==='tool.call'||n.kind==='tool')&&(n.purpose||'main')==='main');
      const reranks=all.filter(n=>n.name==='model.attempt'&&n.kind==='rerank'),rankTime=review.summary?.rerank;
      detail.append(el('section',{class:'obs-detail-metrics card','aria-label':'本轮指标'},metric('总耗时',seconds(model.root?.duration_ms),resultText(review.task_status)),metric('工具调用',`${tools.filter(n=>['ok','completed','succeeded'].includes(n.status)).length} / ${tools.length}`,'成功 / 总数'),metric('模型循环',`${loops.filter(n=>['completed','succeeded'].includes(n.status)).length} / ${loops.length}`,'完成 / 总数'),...(reranks.length?[metric('重排评分',`${reranks.filter(n=>n.status==='accepted').length} / ${reranks.length}`,`成功 / 总数 · 已知耗时 ${seconds(rankTime?.duration_ms)}${rankTime?.unknown_duration_count?` · ${rankTime.unknown_duration_count} 次耗时未知`:''}`)]:[])));
      if(review.collection_state!=='complete'||!review.io_complete)detail.append(el('p',{class:'obs-notice',text:`${collectionText(review.collection_state)}${!review.io_complete?' · 交互内容有缺口':''}，详情展示已保存的事实。`}));
      const stages=traceDetail(model,{expanded:state.expanded,onExpanded:(id,open)=>{open?state.expanded.add(id):state.expanded.delete(id);save(`task.${taskId}`,{expanded:[...state.expanded],scrollTop:root.scrollTop});},onLocate:id=>{selectedSpan=id;history.replaceState(null,'',href(selectedSession,taskId,id,offset));}});detail.append(stages.section,savedInfo(model));content.replaceChildren(detail);
      title.focus({preventScroll:true});root.scrollTop=state.scrollTop;
      if(selectedSpan&&!stages.locate(selectedSpan)){live.textContent='指定节点不存在，已显示本轮执行轨迹。';selectedSpan=null;history.replaceState(null,'',href(selectedSession,taskId,null,offset));}
      return;
    }
    detail.append(savedInfo(model));content.replaceChildren(detail);title.focus({preventScroll:true});root.scrollTop=state.scrollTop;
  }
  load();
  return ()=>{disposed=true;++generation;flight?.abort();if(detailView&&taskId){const state=viewStates.get(taskId);if(state){state.scrollTop=root.scrollTop;save(`task.${taskId}`,{expanded:[...state.expanded],scrollTop:state.scrollTop});}const s=sessionStates.get(sessionKey(selectedSession))||{};s.taskId=taskId;sessionStates.set(sessionKey(selectedSession),s);}else sessionStates.set(sessionKey(selectedSession),{offset,scrollTop:root.scrollTop,taskId:sessionStates.get(sessionKey(selectedSession))?.taskId});save(`session.${sessionKey(selectedSession)}`,sessionStates.get(sessionKey(selectedSession)));setDetailMode(false);root.classList.remove('observability-page');if(previousLive==null)root.removeAttribute('aria-live');else root.setAttribute('aria-live',previousLive);};
}
