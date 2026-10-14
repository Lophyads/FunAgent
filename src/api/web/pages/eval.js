import {el,button,pageHeader,errorBox} from '../components/ui.js';

const statuses={queued:'排队中',running:'处理中',completed:'已完成',partial:'部分完成',failed:'失败',empty:'无召回',
  skipped:'未评分',timeout:'超时',interrupted:'已中断'};
const active=run=>run&&['queued','running'].includes(run.status);
const duration=value=>Number.isFinite(value)?`${Math.round(value)} ms`:'—';
const date=value=>value?new Date(value*1000).toLocaleString('zh-CN',{hour12:false}):'—';
const drivers={sqlite:'SQLite',supabase:'Supabase',cognee:'Cognee'};
const validId=value=>/^[a-f0-9]{32}$/.test(value||'');
function saved(){try{return JSON.parse(sessionStorage.getItem('funagent.eval.draft'))||{};}catch{return {};}}

export function mount(root,{api,routeParams=new URLSearchParams()}) {
  root.classList.add('eval-page');
  const draft=saved();
  let disposed=false,submitting=false,options=null,timer=null,flight=null,generation=0,selected=null,signature=null,pendingRequest=null;
  const choices=new Map(),refresh=button('刷新',()=>initialize(true));
  pageHeader(root,'Eval','输入一个问题，对比各记忆来源的召回内容、耗时与 LLM 检索质量评分。',refresh);
  const question=el('textarea',{rows:3,maxlength:8000,placeholder:'例如：项目最终采用了哪种缓存方案？','aria-label':'评测问题'});
  question.value=draft.question||'';
  const topK=el('input',{type:'number',min:1,max:20,value:draft.top_k||5,'aria-label':'每个来源的 Top-K'});
  const sources=el('div',{class:'eval-source-choices'}),model=el('p',{class:'muted eval-model'}),selectionHint=el('small',{class:'muted'});
  const submit=el('button',{type:'submit',class:'primary',text:'开始评测',disabled:true});
  const feedback=el('p',{class:'eval-feedback',role:'status','aria-live':'polite'});
  const form=el('form',{class:'card eval-form'},el('label',{for:'eval-question',text:'问题'}),question,
    el('div',{class:'eval-form-row'},el('strong',{text:'记忆来源'}),selectionHint),sources,
    el('div',{class:'eval-form-row'},el('label',{},'每个来源 Top-K',topK),submit),model,feedback);
  question.id='eval-question';
  const results=el('section',{class:'eval-results','aria-label':'评测结果'});
  const historyRows=el('div',{class:'eval-history'}),historyStatus=el('p',{class:'muted'});
  const historyCard=el('section',{class:'card eval-history-card'},el('h2',{text:'最近评测'}),historyStatus,historyRows);
  root.append(form,results,historyCard);

  function selectedSources(){return [...choices].filter(([,node])=>node.checked).map(([id])=>id);}
  function remember(){try{sessionStorage.setItem('funagent.eval.draft',JSON.stringify({question:question.value,top_k:Number(topK.value),source_ids:selectedSources()}));}catch{}}
  function updateSubmit(){const count=selectedSources().length,k=Number(topK.value);selectionHint.textContent=`已选 ${count} 个 · 最多 ${options?.max_sources||8} 个`;
    submit.disabled=submitting||!options||!question.value.trim()||!count||count>(options?.max_sources||8)||!Number.isInteger(k)||k<1||k>(options?.max_top_k||20);
    submit.textContent=submitting?'提交中…':'开始评测';}
  question.addEventListener('input',()=>{remember();updateSubmit();});
  topK.addEventListener('input',()=>{remember();updateSubmit();});
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(submit.disabled)return;
    const payload={question:question.value.trim(),source_ids:selectedSources(),top_k:Number(topK.value)},key=JSON.stringify(payload);
    if(pendingRequest?.key!==key)pendingRequest={key,request_key:crypto.randomUUID().replaceAll('-','')};
    submitting=true;updateSubmit();feedback.textContent='正在提交评测…';
    try{const value=await api.createEval({...payload,request_key:pendingRequest.request_key});if(disposed)return;
      if(!validId(value.run_id))throw new Error('响应缺少有效的评测标识。');
      pendingRequest=null;feedback.textContent='已提交。召回内容会先展示，LLM 评分随后更新。';
      await selectRun(value.run_id);await loadHistory();
    }catch(error){if(!disposed){feedback.textContent=error.status?`提交失败：${error.message}`:'提交结果未确认，请查看最近评测或原样重试。';if(error.status&&error.status<500)pendingRequest=null;}}
    finally{if(!disposed){submitting=false;updateSubmit();}}
  });

  async function loadHistory(){
    try{const data=await api.evalRuns();if(disposed)return;historyStatus.textContent=data.items.length?'最近 50 条已保存记录；刷新页面或重启服务后仍可查看。':'还没有评测记录。';
      historyRows.replaceChildren(...data.items.map(run=>{
        const node=button('',()=>selectRun(run.run_id).catch(()=>{}),'eval-history-item'+(selected===run.run_id?' selected':''));node.dataset.runId=run.run_id;
        node.append(el('span',{class:'eval-history-question',text:run.question}),el('small',{text:`${date(run.created_at)} · ${run.source_ids.length} 个来源 · ${statuses[run.status]||run.status}`}));return node;
      }));
    }catch(error){if(!disposed)historyStatus.textContent=`历史读取失败：${error.message}`;}
  }

  function badge(status){return el('span',{class:`badge eval-state ${status}`,text:statuses[status]||status});}
  function renderRun(run){
    const nextSignature=JSON.stringify(run);if(nextSignature===signature)return;signature=nextSignature;
    const title=el('div',{class:'eval-result-header'},el('h2',{text:'评测结果'}),badge(run.status));
    const meta=el('p',{class:'muted eval-run-meta',text:`${date(run.created_at)} · Top-K ${run.top_k} · ${run.judge_model.model} · 总耗时 ${duration(run.total_ms)}`});
    const note=el('p',{class:'muted eval-score-note',text:'LLM 评分表示可见记忆对回答的支持程度；每条相关性为 0–3 分，整体支持度为 0–4 分并转换为百分制。'});
    const table=el('table',{class:'table eval-comparison'},el('thead',{},el('tr',{},...['记忆来源','召回','检索耗时','LLM 评分','评测耗时','状态'].map(text=>el('th',{text})))),el('tbody'));
    for(const source of run.source_results){
      const judgment=source.judgment,score=judgment?`${judgment.score} / 100`:'—';
      const state=source.retrieval_status==='completed'?source.judge_status:source.retrieval_status;
      const row=el('tr',{'data-source-id':source.source_id},el('td',{text:source.source_id}),el('td',{text:String(source.records.length)}),
        el('td',{text:duration(source.retrieval_ms)}),el('td',{class:'eval-score',text:score}),el('td',{text:duration(source.judge_ms)}),el('td',{},badge(state)));
      table.querySelector('tbody').append(row);
    }
    const content=el('div',{class:'eval-source-results'});
    for(const source of run.source_results){
      const card=el('article',{class:'card eval-source-result','data-source-id':source.source_id});
      card.append(el('header',{class:'eval-source-header'},el('h3',{text:source.source_id}),el('small',{text:drivers[source.store]||source.store})));
      const judgment=source.judgment,grades=new Map((judgment?.record_judgments||[]).map(g=>[g.record_id,g]));
      if(judgment){
        card.append(el('div',{class:'eval-grade-summary'},el('strong',{text:`${judgment.score} / 100`}),el('span',{text:`回答支持度 ${judgment.answer_support}/4 · 平均相关性 ${judgment.relevance_score}/100`})),el('p',{class:'eval-judge-reason',text:judgment.reason}));
        if(judgment.missing_information.length)card.append(el('p',{class:'muted',text:'缺失信息：'+judgment.missing_information.join('；')}));
      }else if(source.judge_status==='running'||source.judge_status==='queued'&&source.retrieval_status==='completed')card.append(el('p',{class:'muted',text:'记忆已召回，等待 LLM 评分…'}));
      if(source.error_code||source.judge_error_code)card.append(el('p',{class:'eval-error',text:`${source.error_code?'检索':'评测'}失败：${source.error_code||source.judge_error_code}。${source.records.length?'已保留召回内容。':''}`}));
      if(source.warnings?.length)card.append(el('p',{class:'muted',text:source.warnings.join(' · ')}));
      if(!source.records.length)card.append(el('p',{class:'muted',text:source.retrieval_status==='empty'?'未召回记忆，本来源未评分。':statuses[source.retrieval_status]||source.retrieval_status}));
      source.records.forEach((record,index)=>{
        const grade=grades.get(record.record_id),judged=source.judge_input?.evidence.find(r=>r.record_id===record.record_id);
        const details=el('details',{class:'eval-record','data-record-id':record.record_id});
        // Preserve expanded records as scoring updates arrive.
        if(results.querySelector(`details[data-record-id="${CSS.escape(record.record_id)}"][open]`))details.open=true;
        details.append(el('summary',{},el('strong',{text:`${index+1}. ${record.subject||record.origin||record.type}`}),el('span',{text:grade?`相关性 ${grade.relevance}/3`:'待评分'})),
          el('small',{class:'eval-record-meta',text:`${record.record_id} · ${record.citation}${record.event_at?' · '+record.event_at:''}`}),
          el('pre',{class:'eval-record-content',text:record.content}));
        if(grade)details.append(el('p',{class:'eval-record-reason',text:grade.reason}));
        if(record.truncated||judged?.truncated)details.append(el('small',{class:'eval-truncation',text:record.truncated?'检索返回内容已截断。':'评测使用了截断内容，完整召回内容保留在上方。'}));
        card.append(details);
      });
      if(source.judge_input){card.append(el('details',{class:'eval-input'},el('summary',{text:'查看实际评测输入'}),el('pre',{text:JSON.stringify(source.judge_input,null,2)})));}
      content.append(card);
    }
    results.replaceChildren(...[title,el('p',{class:'eval-result-question',text:run.question}),meta,note,
      run.error_code&&el('p',{class:'eval-error',text:run.error_code}),el('div',{class:'eval-table-scroll'},table),content].filter(Boolean));
  }

  async function poll(id,current){
    try{const run=await api.evalRun(id,{signal:flight.signal});if(disposed||current!==generation)return;
      renderRun(run);if(active(run))timer=setTimeout(()=>poll(id,current),900);else await loadHistory();
    }catch(error){if(disposed||current!==generation||error.name==='AbortError')return;
      const previous=results.querySelector('.eval-poll-error');previous?.remove();
      const box=errorBox(`评测读取失败：${error.message}`,()=>selectRun(id));box.classList.add('eval-poll-error');results.prepend(box);
    }
  }

  async function selectRun(id){
    clearTimeout(timer);flight?.abort();flight=new AbortController();const current=++generation;selected=id;signature=null;
    for(const node of historyRows.children)node.classList.toggle('selected',node.dataset.runId===id);
    if(!validId(id)){results.replaceChildren(errorBox('评测链接中的标识无效。'));return;}
    history.replaceState(null,'','#eval?'+new URLSearchParams({run:id}));
    try{sessionStorage.setItem('funagent.eval.lastRun',id);}catch{}
    results.replaceChildren(el('p',{class:'empty',text:'读取评测结果…'}));await poll(id,current);
  }

  async function initialize(force=false){
    refresh.disabled=true;
    try{const value=await api.evalOptions();if(disposed)return;options=value;
      const old=choices.size?selectedSources():draft.source_ids,selectedNames=new Set(Array.isArray(old)?old:[]);
      choices.clear();sources.replaceChildren();
      value.sources.forEach((source,index)=>{
        const node=el('input',{type:'checkbox',value:source.source_id});node.checked=Array.isArray(old)?selectedNames.has(source.source_id):index<value.max_sources;
        node.addEventListener('change',()=>{remember();updateSubmit();});choices.set(source.source_id,node);
        sources.append(el('label',{class:'eval-source-choice'},node,el('span',{text:source.source_id}),el('small',{text:drivers[source.store]||source.store})));
      });
      if(!value.sources.length)sources.append(el('p',{class:'muted',text:'没有可搜索的记忆来源，请检查记忆配置和管理端读取权限。'}));
      model.textContent=`评测模型：${value.judge_model.model} · ${value.judge_model.profile_id}。每个来源独立评分。`;updateSubmit();
      await loadHistory();if(disposed)return;
      if(force&&selected)await selectRun(selected);
      else if(!selected){let last;try{last=sessionStorage.getItem('funagent.eval.lastRun');}catch{}const id=routeParams.get('run')||last;if(id)await selectRun(id);}
    }catch(error){if(!disposed)feedback.textContent=`初始化失败：${error.message}`;}
    finally{if(!disposed)refresh.disabled=false;}
  }
  initialize();
  return ()=>{disposed=true;clearTimeout(timer);flight?.abort();remember();root.classList.remove('eval-page');};
}
