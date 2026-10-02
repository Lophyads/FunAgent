import {applyEvent, terminal} from './store.js';
import {notify} from './components/ui.js';
export class ApiClient {
  constructor(store) {this.store = store; this.source = null; this.generations = {}; this.historyTimers = new Map(); this.historyFlights = new Map(); this.seq = -1; this.epoch = null; this.pending = [];
    this.expiryTimer = setInterval(() => {if (this.store.get().server.interactions.length) this.interactions().catch(() => {});}, 5000);
  }
  async request(path, options = {}) {
    const response = await fetch(path, {...options, headers: {'Content-Type':'application/json', ...options.headers}});
    const data = await response.json();
    if (!response.ok) {const error = new Error(data.error?.message || `HTTP ${response.status}`); error.status = response.status; error.code = data.error?.code; throw error;}
    return data;
  }
  async resource(name, path, key = name) {
    const generation = this.generations[key] = (this.generations[key] || 0) + 1;
    const value = await this.request(path);
    if (generation === this.generations[key]) this.store.server(name, value); return value;
  }
  async sessions() {const value = await this.request('/api/sessions'); this.store.server('sessions', value.sessions);}
  async history(sid, options = {}) {
    const params = new URLSearchParams(options);
    const data = await this.request(`/api/sessions/${encodeURIComponent(sid)}/messages?${params}`);
    this.store.update(s => {
      const old = s.server.messagesBySession[sid] || {messages: []};
      const merged = new Map(old.messages.map(m => [m.id, m])); for (const m of data.messages) merged.set(m.id, m);
      const value = {...old, messages: [...merged.values()].sort((a,b) => a.id - b.id)};
      if (!options.after) {value.has_more = data.has_more; value.before = data.before;}
      return {...s, server: {...s.server, messagesBySession: {...s.server.messagesBySession, [sid]: value}}};
    }); return data;
  }
  scheduleHistory(sid) {
    if (this.historyTimers.has(sid)) return;
    this.historyTimers.set(sid, setTimeout(async () => {
      this.historyTimers.delete(sid);
      if (this.historyFlights.has(sid)) {this.scheduleHistory(sid); return;}
      const messages = this.store.get().server.messagesBySession[sid]?.messages || [];
      this.historyFlights.set(sid, true);
      try {const data=await this.history(sid, messages.length ? {after: messages.at(-1).id, limit:200} : {});
        if(messages.length&&data.has_more)this.scheduleHistory(sid);
      }
      catch (e) {notify(e.message, true);} finally {this.historyFlights.delete(sid);}
    }, 180));
  }
  async interactions() {
    const sid = this.store.get().ui.activeSessionId;
    const data = await this.request(`/api/interactions?session_id=${encodeURIComponent(sid)}`);
    if (this.store.get().ui.activeSessionId === sid) this.store.server('interactions', data.interactions);
  }
  selectSession(sid) {
    if (this.frame) {cancelAnimationFrame(this.frame); this.frame = null;}
    this.source?.close(); this.pending = []; this.seq = -1; this.epoch = null;
    this.store.ui('activeSessionId', sid); sessionStorage.setItem('funagent.session', sid);
    this.store.server('interactions', []); this.store.ui('connectionStatus', 'connecting');
    this.history(sid).catch(e => notify(e.message, true)); this.interactions().catch(e => notify(e.message, true));
    const source = this.source = new EventSource(`/api/events?session_id=${encodeURIComponent(sid)}`);
    source.onopen = () => {if (source === this.source) {this.store.ui('connectionStatus', 'connected'); this.interactions().catch(() => {});}};
    source.onerror = () => {if (source === this.source) this.store.ui('connectionStatus', 'reconnecting');};
    source.addEventListener('update', event => {
      if (source !== this.source) return;
      const e = JSON.parse(event.data);
      if (!['snapshot','reset'].includes(e.kind) && e.epoch === this.epoch && e.seq <= this.seq) return;
      this.epoch = e.epoch; this.seq = e.seq; this.pending.push(e);
      if (!this.frame) this.frame = requestAnimationFrame(() => {this.frame = null;
        for (const item of this.pending.splice(0)) {
          applyEvent(this.store, item);
          if (['snapshot','reset'].includes(item.kind)) {this.history(sid).catch(err => notify(err.message,true)); this.interactions().catch(() => {});}
          if (['turn.finished', 'tool.succeeded','tool.failed','tool.denied','tool.interrupted'].includes(item.kind) ||
              item.kind === 'turn.phase_finished' && item.payload.phase === 'prepare_conversation') this.scheduleHistory(sid);
          if (item.kind.startsWith('interaction.') || item.kind === 'turn.finished') this.interactions().catch(() => {});
          if (item.kind === 'turn.finished') this.sessions().catch(() => {});
        }
      });
    });
  }
  close() {this.source?.close(); clearInterval(this.expiryTimer); for (const timer of this.historyTimers.values()) clearTimeout(timer); if (this.frame) cancelAnimationFrame(this.frame);}
}
export function createCommands(store, api) {
  return {
    newSession() {api.selectSession(crypto.randomUUID());},
    async send() {
      const sid = store.get().ui.activeSessionId, draft = store.get().drafts.chatBySession[sid] || {text:''};
      if (draft.submitting || !draft.text.trim()) return;
      const text = draft.uncertain ? draft.pendingText : draft.text;
      const requestId = draft.requestId || crypto.randomUUID();
      store.draft('chatBySession', sid, {...draft, requestId, pendingText:text, submitting:true});
      try {
        const dto = await api.request('/api/turns', {method:'POST', body:JSON.stringify({session_id:sid, text, client_request_id:requestId})});
        const turn = await api.request(`/api/turns/${encodeURIComponent(dto.turn_id)}`);
        turn.input = text; turn.client_request_id = requestId;
        store.update(s => ({...s, server:{...s.server, turns:{...s.server.turns, [dto.turn_id]:turn}}}));
        const current = store.get().drafts.chatBySession[sid];
        store.draft('chatBySession', sid, {text:current.text === text ? '' : current.text});
        api.scheduleHistory(sid); api.sessions().catch(() => {});
      } catch (e) {
        const current = store.get().drafts.chatBySession[sid];
        store.draft('chatBySession', sid, {...current, submitting:false, uncertain:!e.status,
          requestId:!e.status ? requestId : null});
        notify(!e.status ? '提交结果不确定。重试将使用同一请求 ID；也可先检查历史。' : e.message, true);
      }
    },
    async stop(tid) {const sid=store.get().ui.activeSessionId; const turns=Object.values(store.get().server.turns).filter(t => t.session_id === sid && !terminal(t.status)).sort((a,b)=>a.created_at-b.created_at);
      const t=tid?turns.find(t=>t.turn_id===tid):turns.find(t=>t.status!=='queued')||turns[0];
      if(t) await api.request(`/api/turns/${encodeURIComponent(t.turn_id)}/cancel`, {method:'POST',body:'{}'});
    },
    async resolve(request, response) {try {await api.request(`/api/interactions/${encodeURIComponent(request.request_id)}/resolve`, {method:'POST',body:JSON.stringify({session_id:request.session_id,response})}); await api.interactions();}
      catch(e) {notify(e.message,true); await api.interactions();}},
  };
}
