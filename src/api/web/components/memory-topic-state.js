import {ROOT, UNCLASSIFIED, mergeRecords} from './memory-topic-model.js';

/** @typedef {{source_id:string,capabilities:string[],writable:boolean}} MemorySource */
/** @typedef {{memoryTopics:(source:MemorySource)=>Promise<{source_id:string,revision:number,topics:import('./memory-topic-model.js').Topic[]}>,
 * readMemory:(source:MemorySource,kind:string,params:URLSearchParams)=>Promise<{entries:import('./memory-topic-model.js').MemoryRecord[],next_cursor:string|null}>}} TopicReadApi */

/**
 * Read coordinator, independent of DOM and storage. API is injected by the page.
 * @param {{api:TopicReadApi,onChange:Function,currentNode:()=>string}} options
 * Each page exposes entries, next_cursor, loading and error; request IDs stay private.
 */
export function createTopicState({api, onChange, currentNode}) {
  let generation = 0, metadataId = 0, searchId = 0, disposed = false;
  let source = null, kind = 'all', topics = [], revision = null, pages = {};
  let loading = false, error = null, results = null;
  const requests = new Map(), stale = new Set();
  const snapshot = () => ({source, kind, topics, revision, pages, loading, error, results});
  const emit = () => {if (!disposed) onChange(snapshot());};
  const valid = gen => !disposed && gen === generation;

  async function loadNode(key, append = false) {
    if (!source || key === ROOT || !source.capabilities?.includes('list')) return;
    const previous = pages[key];
    if (append && previous?.loading) return;
    const ownSource = source, ownKind = kind, gen = generation;
    const id = (requests.get(key) || 0) + 1;
    requests.set(key, id);
    append = append && !stale.has(key) && Boolean(previous?.next_cursor);
    const params = new URLSearchParams({limit:'50'});
    if (key === UNCLASSIFIED) params.set('unclassified', '1');
    else params.set('topic_id', key);
    if (append) params.set('cursor', previous.next_cursor);
    pages = {...pages, [key]:{entries:previous?.entries || [], next_cursor:previous?.next_cursor || null, loading:true, error:null}};
    emit();
    try {
      const value = await api.readMemory(ownSource, ownKind, params);
      if (!valid(gen) || requests.get(key) !== id) return;
      pages = {...pages, [key]:{entries:mergeRecords(ownSource.source_id, append ? previous.entries : [], value.entries),
        next_cursor:value.next_cursor, loading:false, error:null}};
      stale.delete(key);
    } catch (failure) {
      if (!valid(gen) || requests.get(key) !== id) return;
      pages = {...pages, [key]:{...pages[key], loading:false, error:failure.message}};
    }
    emit();
  }

  async function refresh() {
    if (!source) return;
    const ownSource = source, gen = generation, id = ++metadataId;
    loading = true; error = null; emit();
    try {
      const value = await api.memoryTopics(ownSource);
      if (!valid(gen) || id !== metadataId) return;
      if (revision !== value.revision) {
        // Invalidate in-flight pages too: an older read must not publish after this snapshot.
        pages = {...pages};
        for (const key of Object.keys(pages)) {
          stale.add(key); requests.set(key, (requests.get(key) || 0) + 1);
          pages[key] = {...pages[key], loading:false};
        }
      }
      topics = value.topics; revision = value.revision;
      const existing = new Set(topics.map(topic => topic.topic_id));
      pages = Object.fromEntries(Object.entries(pages).filter(([key]) => key === UNCLASSIFIED || existing.has(key)));
      loading = false; emit();
      const key = currentNode();
      if (key === UNCLASSIFIED || existing.has(key)) await loadNode(key);
    } catch (failure) {
      if (!valid(gen) || id !== metadataId) return;
      loading = false; error = failure.message; emit();
    }
  }

  async function search(text) {
    const gen = generation, id = ++searchId, ownSource = source, ownKind = kind;
    if (!text) {results = null; emit(); return;}
    results = {entries:[], loading:true, error:null, text}; emit();
    try {
      const value = await api.readMemory(ownSource, ownKind, new URLSearchParams({q:text, limit:'50'}));
      if (!valid(gen) || id !== searchId) return;
      results = {...value, loading:false, error:null, text};
    } catch (failure) {
      if (!valid(gen) || id !== searchId) return;
      results = {entries:[], loading:false, error:failure.message, text, issues:failure.issues, code:failure.code};
    }
    emit();
  }

  return {
    snapshot, refresh, search, loadNode,
    expand(key) {
      if (pages[key]?.error) return loadNode(key, Boolean(pages[key].next_cursor));
      if (!pages[key] || stale.has(key)) return loadNode(key);
    },
    setScope(nextSource, nextKind) {
      const sameSource = source?.source_id === nextSource.source_id;
      generation++; metadataId++; searchId++; requests.clear(); stale.clear();
      source = nextSource; kind = nextKind; pages = {}; results = null;
      if (!sameSource) {topics = []; revision = null;}
      loading = false; error = null; emit();
      return refresh();
    },
    dispose() {disposed = true; generation++;}
  };
}
