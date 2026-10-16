/** @typedef {{topic_id:string,parent_id:string|null,name:string,description:string}} Topic */
/** @typedef {{kind:'root'}|{kind:'topic',topicId:string}|{kind:'unclassified'}|{kind:'record',recordId:string,topicId:string|null}} Selection */
/** @typedef {{id?:string,record_id:string,content:string,type:string,subject?:string,origin?:string,version?:string,topic_id:string|null,topic_path:string[]}} MemoryRecord */
/** @typedef {{entries:MemoryRecord[],next_cursor:string|null,loading:boolean,error:string|null}} NodePage */
export const ROOT = '$root';
export const UNCLASSIFIED = '$unclassified';
export const recordKey = (sourceId, recordId) => JSON.stringify([sourceId, recordId]);
export const nodeKey = selection => selection?.kind === 'topic' ? selection.topicId
  : selection?.kind === 'record' ? selection.topicId || UNCLASSIFIED
  : selection?.kind === 'unclassified' ? UNCLASSIFIED : ROOT;

/** Build child indexes without changing server order. @param {Topic[]} topics */
export function topicChildren(topics) {
  const children = new Map([[ROOT, []]]);
  for (const topic of topics) {
    const parent = topic.parent_id || ROOT;
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(topic);
  }
  return children;
}
/** @param {Topic[]} topics @param {string|null} topicId */
export function topicPath(topics, topicId) {
  const index = new Map(topics.map(topic => [topic.topic_id, topic]));
  const path = [], seen = new Set();
  while (topicId && index.has(topicId) && !seen.has(topicId)) {
    seen.add(topicId);
    const topic = index.get(topicId);
    path.unshift(topic.name);
    topicId = topic.parent_id;
  }
  return path;
}
export function mergeRecords(sourceId, previous, next) {
  return [...new Map([...previous, ...next].map(record =>
    [recordKey(sourceId, record.id || record.record_id), record])).values()];
}
