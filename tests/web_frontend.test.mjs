// Browser-independent contracts for the native store and safe Markdown.
// Run: node --test tests/web_frontend.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
globalThis.sessionStorage = {getItem:()=>null};
globalThis.crypto ||= (await import('node:crypto')).webcrypto;
const sourceModule = async path => import(`data:text/javascript;base64,${Buffer.from(await readFile(path)).toString('base64')}`);
const {createStore,applyEvent} = await sourceModule('src/gateway/web/static/store.js');
const {markdown} = await sourceModule('src/gateway/web/static/components/ui.js');
const {buildOverview,span,eventInfo} = await sourceModule('src/gateway/web/static/pages/overview-model.js');
const event=(kind,payload={},turn='t',session='s')=>({kind,payload,turn_id:turn,session_id:session,seq:1,epoch:'epoch',occurred_at:100});

test('unrelated updates preserve references and subscriptions',()=>{
  const store=createStore();const messages=store.get().server.messagesBySession;let called=0;
  store.subscribe(s=>s.server.messagesBySession,()=>called++);
  applyEvent(store,event('turn.queued'));
  assert.equal(store.get().server.messagesBySession,messages);assert.equal(called,1);
  store.draft('chatBySession','s',{text:'草稿'});
  assert.equal(called,1);assert.equal(store.get().drafts.chatBySession.s.text,'草稿');
});
test('attempts stay separate and terminal turns reject late deltas',()=>{
  const store=createStore(),p={attempt_id:'a',route:'r',iteration:1};
  applyEvent(store,event('model.delta',{...p,text:'partial'}));
  applyEvent(store,event('model.discarded',p));
  applyEvent(store,event('model.delta',{...p,attempt_id:'b',text:'result'}));
  applyEvent(store,event('model.accepted',{...p,attempt_id:'b',text:'complete result'}));
  applyEvent(store,event('turn.finished',{status:'completed',final_text:'complete result'}));
  applyEvent(store,event('model.delta',{...p,attempt_id:'b',text:'late'}));
  const t=store.get().server.turns.t;assert.equal(t.status,'completed');assert.equal(t.attempts.a.text,'partial');assert.equal(t.attempts.b.text,'complete result');assert.equal(t.attempts.a.status,'discarded');
});
test('snapshot replaces only one session and keeps drafts',()=>{
  const store=createStore();applyEvent(store,event('turn.queued',{},'other','other'));
  store.draft('chatBySession','s',{text:'保留'});
  applyEvent(store,{...event('reset'),payload:{turns:[{session_id:'s',turn_id:'restored',status:'running',attempts:{}}]}});
  assert.ok(store.get().server.turns.other);assert.ok(store.get().server.turns.restored);assert.equal(store.get().drafts.chatBySession.s.text,'保留');
});
test('model HTML is escaped and links restrict schemes',()=>{
  const result=markdown('<img src=x onerror=alert(1)> [bad](javascript:alert(1)) [ok](https://example.com)');
  assert.ok(result.includes('&lt;img'));assert.ok(!result.includes('<img'));
  assert.ok(!result.includes('href="javascript:'));assert.ok(result.includes('href="https://example.com"'));
});

test('Overview follows actual model, tool and human execution states',()=>{
  const turn={status:'running',iteration:1,phase:'provider_attempts',created_at:100,
    attempts:{a:{attempt_id:'a',status:'delta',iteration:1,route:'primary',text:'正文'}},tools:{}};
  let view=buildOverview(turn,'connected',103);
  assert.equal(view.activeNode,'model');assert.equal(view.nodeStates.model,'active');assert.equal(view.elapsed,3);
  const waiting={...turn,status:'waiting',phase:'tool_round',attempts:{a:{...turn.attempts.a,status:'accepted'}},
    tools:{call:{tool_call_id:'call',tool_name:'clarify',status:'running',iteration:1}}};
  view=buildOverview(waiting,'connected',104);
  assert.equal(view.activeNode,'human');assert.equal(view.nodeStates.human,'waiting');assert.equal(view.nodeStates.tools,'waiting');
  assert.equal(view.nodeStates.model,'done');assert.equal(view.nodeStates.memory,'support');
});

test('Overview final outcomes override stale tool diagnostics and stop the clock',()=>{
  const turn={status:'running',created_at:100,finished_at:105,iteration:1,phase:'tool_round',
    tools:{call:{tool_call_id:'call',tool_name:'write',status:'running',iteration:1}},
    result:{status:'completed',iterations:2,provider_attempts:3,usage:{total_tokens:0},tool_outcomes:[
      {tool_call_id:'call',tool_name:'write',state:'succeeded',effect_disposition:'confirmed'},
    ]}};
  const view=buildOverview(turn,'connected',1000);
  assert.equal(view.live,false);assert.equal(view.activeNode,null);assert.equal(view.elapsed,5);
  assert.equal(view.nodeStates.reply,'done');assert.equal(view.nodeStates.tools,'done');assert.equal(view.nodeStates.hub,'idle');
  assert.equal(view.items[0].status,'succeeded');assert.equal(view.items[0].iteration,1);
  assert.equal(view.items[0].seconds,null);assert.equal(view.tokens,0);assert.equal(view.attemptCount,3);
});

test('Overview measures retries separately and leaves truncated timing unknown',()=>{
  const timeline=[
    {kind:'model.started',occurred_at:100,payload:{attempt_id:'a'}},
    {kind:'model.discarded',occurred_at:102,payload:{attempt_id:'a'}},
    {kind:'model.started',occurred_at:103,payload:{attempt_id:'b'}},
    {kind:'model.accepted',occurred_at:107,payload:{attempt_id:'b'}},
  ];
  const turn={status:'completed',created_at:100,finished_at:108,timeline,attempts:{
    a:{attempt_id:'a',iteration:1,status:'discarded'},b:{attempt_id:'b',iteration:1,status:'accepted'},
    c:{attempt_id:'c',iteration:2,status:'delta',text:'未确认'},
  },result:{status:'completed'}};
  const view=buildOverview(turn,'connected',1000);
  assert.deepEqual(view.items.map(i=>i.seconds),[2,4,null]);assert.equal(view.items[2].status,'unknown');
  assert.equal(view.tokens,null);assert.deepEqual(view.iterations,[1,2]);
  assert.equal(span(timeline.slice(1),e=>e.payload.attempt_id==='a',1000,false),null);
  assert.equal(span([timeline[0]],()=>true,105,true),5);
  assert.equal(span([timeline[0]],()=>true,105,false),null);
});

test('Overview preserves uncertainty instead of reporting unobserved success',()=>{
  const turn={status:'failed',created_at:100,finished_at:110,
    attempts:{a:{attempt_id:'a',status:'discarded'}},
    result:{status:'failed',tool_outcomes:[{tool_call_id:'c',tool_name:'write',state:'unknown'}]}};
  const view=buildOverview(turn,'reconnecting',1000);
  assert.equal(view.nodeStates.context,'idle');assert.equal(view.nodeStates.tools,'unknown');
  assert.equal(view.nodeStates.model,'failed');assert.equal(view.nodeStates.reply,'failed');
  assert.equal(view.nodeStates.browser,'waiting');assert.equal(view.items.find(i=>i.id==='c').iteration,undefined);
});

test('Overview timeline labels and filters retain the underlying event semantics',()=>{
  const phase=eventInfo({kind:'turn.phase_failed',payload:{phase:'provider_attempts',iteration:2,error_type:'TimeoutError'}});
  assert.equal(phase.category,'model');assert.equal(phase.tone,'failed');assert.ok(phase.title.includes('模型推理'));
  const approval=eventInfo({kind:'interaction.pending',payload:{}});
  assert.equal(approval.category,'interaction');assert.equal(approval.tone,'waiting');
  const final=eventInfo({kind:'turn.finished',payload:{status:'interrupted'}});
  assert.equal(final.tone,'interrupted');assert.equal(final.category,'system');
});
