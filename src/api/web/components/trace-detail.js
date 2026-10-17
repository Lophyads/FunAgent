import {el,button,notify} from './ui.js';
import {formatValue,payload,parseJSON,interactionText,traceName,resultText,collectionText,seconds,dateTime,eventText,descendants,externalNodes,eventsFor,stageFor} from '../pages/observability-model.js';

export function metric(label,value,note) {return el('div',{class:'obs-metric'},el('span',{text:label}),el('strong',{text:value}),el('small',{text:note}));}
function textBlock(text) {
  const pre=el('pre',{text:text.length>5000?`${text.slice(0,5000)}\n…（界面预览已省略）`:text});
  const wrap=el('div',{},pre);
  if(text.length>5000)wrap.append(button('展开全文',e=>{pre.textContent=text;e.currentTarget.remove();},'obs-text-action'));
  return wrap;
}
function rawDetails(label,value) {return el('details',{class:'obs-payload-raw'},el('summary',{text:label}),textBlock(formatValue(value)));}
function copyButton(text) {return button('复制',async e=>{try{await navigator.clipboard.writeText(text);e.currentTarget.textContent='已复制';}catch{notify('复制失败，请展开原始内容后选择文本。',true);}},'obs-copy');}
function memoryRecords(value) {
  if(!Array.isArray(value?.records))return null;
  const list=el('div',{class:'obs-memory-results'},el('p',{class:'obs-memory-summary',text:`${value.records.length} 条结果 · ${Object.entries(value.source_status||{}).map(([k,v])=>`${k}：${resultText(v)}`).join(' · ')||'来源状态未记录'}`}));
  if(value.warnings?.length)list.append(el('p',{class:'obs-notice',text:value.warnings.join(' · ')}));
  if(value.issues?.length)list.append(rawDetails('检索诊断',value.issues));
  if(!value.records.length)list.append(el('p',{class:'obs-memory-summary',text:'没有检索到记录'}));
  for(const [i,r] of value.records.entries()){
    const ref=r.ref||r;
    list.append(el('details',{class:'obs-memory-record'},el('summary',{text:`${i+1}. ${ref.source_id||'来源未知'} · ${r.type||'记录'} · ${ref.record_id||'ID 未记录'}`}),
      textBlock(formatValue(r.content)),el('p',{class:'obs-memory-summary',text:`引用：${r.citation||'未记录'}${r.score!=null?` · 来源内评分：${r.score}`:''}`}),rawDetails('记录原始 JSON',r)));
  }
  return list;
}
function payloadCard(trace,node,side) {
  const ref=node[`${side}_ref`],saved=ref?trace.payloads?.[ref]:null;
  const decoded=side==='output'&&node.name==='model.attempt'&&node.decoded_reply;
  const value=saved?.data;
  const raw=!ref?'未记录':!saved?'引用内容缺失':formatValue(value);
  const text=saved||decoded?interactionText(node,side,value):raw;
  const title=side==='input'?'请求 / 输入':'返回 / 输出';
  const head=el('div',{class:'obs-payload-head'},el('span',{text:title}),el('div',{class:'obs-payload-actions'},saved?.truncated?el('small',{text:'采集时已截断'}):null,copyButton(text)));
  const card=el('div',{class:'obs-payload'},head);
  const records=side==='output'&&node.name.startsWith('memory.')?memoryRecords(value):null;
  card.append(records||textBlock(text));
  if(records||text!==raw)card.append(rawDetails('原始返回 / JSON',saved?value:undefined));
  return card;
}
function exchange(trace,node) {
  if(!node.input_ref&&!node.output_ref&&!node.decoded_reply)return null;
  return el('div',{class:'obs-exchange'},payloadCard(trace,node,'input'),payloadCard(trace,node,'output'));
}
function nodeFields(node) {return Object.fromEntries(Object.entries(node).filter(([k])=>!['children','decoded_reply'].includes(k)));}

function traceBranch(model,node,options={}) {
  const {trace}=model,{navigate,owner,references=new Set(),expanded,onExpanded}=options;
  const line=el('span',{class:'obs-trace-line'},el('span',{class:'obs-trace-name',text:traceName(trace,node)}),el('span',{class:'obs-trace-result',text:resultText(node.status)}),el('span',{class:'obs-trace-time',text:seconds(node.duration_ms)}));
  const targetStage=stageFor(model,node.span_id);
  const canonical=targetStage&&targetStage!==owner;
  if(canonical||references.has(node.span_id))return el('div',{class:'obs-trace-leaf'},line,button(canonical?'查看阶段详情':'查看交互',()=>navigate(node.span_id),'obs-inline-link'));
  const branch=el('details',{class:'obs-trace-branch','data-trace-span':node.span_id},el('summary',{},line));
  let loaded=false;
  const load=()=>{
    if(loaded)return;loaded=true;
    const body=el('div',{class:'obs-trace-body'}),io=exchange(trace,node);
    if(io)body.append(io);
    body.append(rawDetails('节点信息',nodeFields(node)));
    if(node.children?.length)body.append(el('div',{class:'obs-trace-children'},...node.children.map(child=>traceBranch(model,child,options))));
    branch.append(body);
  };
  branch.addEventListener('toggle',()=>{onExpanded?.(node.span_id,branch.open);if(branch.open)load();});
  if(expanded?.has(node.span_id)){branch.open=true;load();}
  // Only create the next level when its parent is expanded.
  branch.loadBody=load;
  return branch;
}
function eventList(model,node) {
  const events=eventsFor(model,node);if(!events.length)return null;
  const details=el('details',{class:'obs-events'},el('summary',{text:`事件序列 · ${events.length} 条`}));let loaded=false;
  details.addEventListener('toggle',()=>{
    if(!details.open||loaded)return;loaded=true;
    const list=el('div',{class:'obs-event-list'});let count=0;
    const more=button('显示更多事件',append,'obs-text-action');
    function append(){
      for(const event of events.slice(count,count+100)){
        const name=model.nodes.get(event.span_id)?.name||event.name||event.span_id;
        const row=el('details',{class:'obs-event-row'},el('summary',{class:'obs-event'},el('span',{text:`#${event.seq}`}),el('span',{text:eventText(event.operation)}),el('span',{text:`${name}${event.status?` · ${resultText(event.status)}`:''}`}),el('span',{text:seconds(node.started_at==null?null:(event.occurred_at-node.started_at)*1000)})));
        row.addEventListener('toggle',()=>{if(!row.open||row.dataset.loaded)return;row.dataset.loaded='true';const snapshot={kind:model.nodes.get(event.span_id)?.kind,...event,name};const io=exchange(model.trace,snapshot);if(io)row.append(el('div',{class:'obs-event-body'},io));row.append(rawDetails('事件字段',event));});
        list.append(row);
      }
      count+=100;more.hidden=count>=events.length;
    }
    append();details.append(list,more);
  });return details;
}

export function traceDetail(model,{expanded=new Set(),onExpanded=()=>{},onLocate=()=>{}}={}) {
  const section=el('section',{class:'obs-stages card'},el('div',{class:'obs-section-heading'},el('div',{},el('h2',{text:'执行轨迹'}),el('p',{class:'obs-stage-hint',text:'展开阶段，查看与模型、工具及记忆来源的交互。'})),el('span',{text:`${model.rows.length} 个阶段`})));
  const list=el('div',{class:'obs-stage-list'}),stages=new Map();section.append(list);
  const navigate=id=>{locate(id);onLocate(id);};
  for(const row of model.rows){
    const {node}=row,stage=el('details',{class:'obs-stage','data-stage-span':node.span_id},el('summary',{class:'obs-stage-summary'},el('span',{class:'obs-stage-kind',text:row.kind}),el('span',{class:'obs-stage-name',text:row.name},row.note?el('small',{text:row.note}):null),el('span',{class:'obs-stage-status',text:resultText(node.status)}),el('span',{class:'obs-stage-time',text:seconds(node.duration_ms)})));
    let loaded=false;
    const load=()=>{
      if(loaded)return;loaded=true;
      const body=el('div',{class:'obs-stage-body'}),io=exchange(model.trace,node);
      if(io)body.append(el('div',{class:'obs-stage-subhead',text:'交互内容'}),io);
      const external=externalNodes(node),externalIds=new Set(external.map(n=>n.span_id));
      if(external.length)body.append(el('div',{class:'obs-stage-subhead',text:`外部交互 · ${external.length} 次`}),el('div',{class:'obs-interaction-list'},...external.map(n=>traceBranch(model,n,{navigate,owner:node.span_id,expanded,onExpanded,references:new Set(externalIds.has(n.parent_span_id)?[n.span_id]:[])}))));
      if(node.children?.length)body.append(el('div',{class:'obs-stage-subhead',text:'子步骤轨迹'}),el('div',{class:'obs-trace-children'},...node.children.map(n=>traceBranch(model,n,{navigate,owner:node.span_id,references:externalIds,expanded,onExpanded}))));
      body.append(rawDetails('阶段信息',nodeFields(node)));
      const events=eventList(model,node);if(events)body.append(events);stage.append(body);
    };
    stage.addEventListener('toggle',()=>{onExpanded(node.span_id,stage.open);if(stage.open)load();});
    if(expanded.has(node.span_id)){stage.open=true;load();}
    stages.set(node.span_id,{stage,load});list.append(stage);
  }
  if(!model.rows.length)list.append(el('p',{class:'obs-muted',text:'该快照没有阶段记录，可查看下方保存信息。'}));
  function locate(id){
    const owner=stageFor(model,id),entry=stages.get(owner);if(!entry)return false;
    entry.stage.open=true;entry.load();onExpanded(owner,true);
    const chain=[];let current=model.nodes.get(id);
    while(current&&current.span_id!==owner){chain.unshift(current.span_id);current=model.nodes.get(current.parent_span_id);}
    for(const span of chain){for(const branch of entry.stage.querySelectorAll('[data-trace-span]'))if(branch.dataset.traceSpan===span){branch.open=true;branch.loadBody?.();}}
    const target=[...entry.stage.querySelectorAll('[data-trace-span]')].find(e=>e.dataset.traceSpan===id)||entry.stage;
    target.scrollIntoView({block:'nearest',behavior:'auto'});target.classList.add('obs-located');return true;
  }
  return {section,locate};
}

const diagnosisLabels={unsupported_schema:'保存格式暂不支持',invalid_json:'JSON 无法解析',invalid_trace:'轨迹结构异常',payload_missing:'交互内容引用缺失',events_dropped:'部分事件未采集',parent_missing:'原父节点缺失',interrupted:'执行已中断',task_status_reconciled:'任务状态已校准',task_not_found:'主库中未找到任务',task_lookup_failed:'任务状态读取失败',trace_directory_unreadable:'轨迹目录无法读取',trace_read_failed:'文件读取失败',collection_inconsistent:'封存信息不一致'};
export function diagnosticText(code){return `${diagnosisLabels[code]||code}${diagnosisLabels[code]?` (${code})`:''}`;}
export function diagnosticsView(diagnostics) {
  if(!diagnostics?.length)return null;
  return el('details',{class:'obs-diagnostics card'},el('summary',{text:`读取诊断 · ${diagnostics.length} 项`}),...diagnostics.map(d=>el('p',{text:typeof d==='string'?diagnosticText(d):`${d.trace_id} · ${(d.codes||[]).map(diagnosticText).join('；')}`})));
}
export function savedInfo(model) {
  const {review,trace}=model,summary=review.summary||{},box=el('details',{class:'obs-saved card'},el('summary',{text:`保存信息 · ${collectionText(review.collection_state)} · 序号 ${review.saved_seq??'未知'}`}));
  box.append(el('p',{text:`业务状态：${resultText(review.task_status)} · 文件记录：${resultText(review.recorded_task_status)} · 交互内容：${review.io_complete?'完整':'有缺口'}`}));
  if(review.task_status!==review.recorded_task_status)box.append(el('p',{class:'obs-notice',text:'当前任务状态与文件记录不同，展示的是所选保存快照。'}));
  if(review.collection_state==='pending')box.append(el('p',{class:'obs-notice',text:'运行中的已保存版本，可通过刷新读取最新快照。'}));
  if(!review.io_complete)box.append(el('p',{class:'obs-notice',text:'交互内容有缺口，请结合截断标记和读取诊断查看。'}));
  box.append(el('p',{text:`已知 Token：${summary.known_total_tokens??'未知'} · 主模型：${summary.main_model_tokens??'未知'} · 用量未知调用：${summary.unknown_usage_calls??'未知'}`}));
  if(summary.rerank?.count)box.append(el('p',{text:`重排评分：${summary.rerank.count} 次 · 已知耗时 ${seconds(summary.rerank.duration_ms)} · 耗时未知 ${summary.rerank.unknown_duration_count} 次`}));
  const waitLabels={'task.queue_wait':'排队','model.retry_wait':'模型重试','interaction.wait':'人工等待'};
  for(const [name,value] of Object.entries(summary.waits||{}))box.append(el('p',{text:`${waitLabels[name]||name}：${value.count} 次 · 已知耗时 ${seconds(value.duration_ms)} · 耗时未知 ${value.unknown_duration_count} 次`}));
  if(model.root){
    const task=el('details',{class:'obs-payload-raw'},el('summary',{text:'任务输入与保存结果'})),io=exchange(trace,model.root);
    if(io)task.append(io);task.append(rawDetails('任务节点信息',nodeFields(model.root)));
    const events=eventList(model,model.root);if(events)task.append(events);box.append(task);
  }
  box.append(diagnosticsView(review.diagnostics)||el('p',{class:'obs-muted',text:'没有已知读取诊断'}),rawDetails('保存检查点',trace.checkpoint),el('p',{class:'obs-file-path',text:`文件：${review.path||'未记录'}`}));return box;
}
