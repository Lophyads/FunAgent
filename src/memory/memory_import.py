"""Strict import protocol and storage-independent, sequential execution."""
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Callable

from jsonschema import Draft202012Validator
from ..frame.contracts import DomainError
from .common import NormalizeTopicName
from .contracts import MemoryDraft, MemoryFailure, MemoryListQuery
from .failure import CODES, FailureData
from .protocols import MemoryOperationsAccess

MAX_FILE = 2 * 1024 * 1024
_validator = Draft202012Validator(json.loads(Path(__file__).with_name('memory_import.schema.json').read_text('utf-8')))


class ImportValidationError(DomainError):
    def __init__(self, path, message):
        super().__init__('INVALID_REQUEST', message)
        self.path = path


@dataclass(frozen=True)
class ImportDocument:
    dataset_id: str
    topics: tuple
    memories: tuple


def Parse(raw: bytes) -> ImportDocument:
    def fail(path, message):
        raise ImportValidationError(path, message)
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail(key, '重复的 JSON 字段。')
            result[key] = value
        return result
    if len(raw) > MAX_FILE:
        raise DomainError('PAYLOAD_TOO_LARGE', '文件超过 2 MiB。')
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=lambda _: fail('', '不支持非标准 JSON 值。'))
    except (ValueError, UnicodeError, RecursionError):
        fail('', '文件必须是严格的 UTF-8 JSON。')
    error = next(_validator.iter_errors(data), None)
    if error:
        path = ''.join(f'[{part}]' if isinstance(part, int) else ('.' if i else '') + part
                       for i, part in enumerate(error.absolute_path))
        fail(path, '字段缺失、类型、长度或取值不符合导入协议。')
    def unicode_check(value):
        if isinstance(value, str) and any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            fail('', '文件包含非法 Unicode 字符。')
        if isinstance(value, dict):
            for item in value.values():
                unicode_check(item)
        elif isinstance(value, list):
            for item in value:
                unicode_check(item)
    unicode_check(data)
    if type(data['schema_version']) is not int:
        fail('schema_version', '版本必须是整数 1。')
    topics, seen, siblings = {}, set(), set()
    for i, topic in enumerate(data['topics']):
        for field in ('name', 'description'):
            value = topic[field]
            if not value.strip() or value != value.strip():
                fail(f'topics[{i}].{field}', '主题名称和说明不得为空或包含首尾空白。')
        if topic['key'] in topics:
            fail(f'topics[{i}].key', '主题键重复。')
        sibling = (topic['parent_key'], NormalizeTopicName(topic['name']))
        if sibling in siblings:
            fail(f'topics[{i}].name', '同级主题名称重复。')
        siblings.add(sibling)
        topics[topic['key']] = topic
    ordered, done = [], set()
    for i, topic in enumerate(data['topics']):
        chain, active, current = [], set(), topic['key']
        while current is not None and current not in done:
            if current not in topics:
                fail(f'topics[{i}].parent_key', '父主题不存在。')
            if current in active:
                fail(f'topics[{i}].parent_key', '主题目录存在循环。')
            active.add(current)
            chain.append(current)
            current = topics[current]['parent_key']
        for item in reversed(chain):
            done.add(item)
            ordered.append(topics[item])
    for i, memory in enumerate(data['memories']):
        if memory['key'] in seen:
            fail(f'memories[{i}].key', '记忆键重复。')
        seen.add(memory['key'])
        if memory['topic_key'] not in topics:
            fail(f'memories[{i}].topic_key', '所属主题不存在。')
        if not memory['content'].strip():
            fail(f'memories[{i}].content', '记忆正文不能为空。')
        if any(0xD800 <= ord(c) <= 0xDFFF for c in memory['content']):
            fail(f'memories[{i}].content', '正文包含非法 Unicode 字符。')
    return ImportDocument(data['dataset_id'], tuple(ordered), tuple(data['memories']))


def CheckSource(operations, source_id, *, write=True):
    source = next((s for s in operations.Sources() if s['source_id'] == source_id), None)
    if source is None:
        raise DomainError('DENIED', '来源不可管理。')
    if write and not source.get('writable'):
        raise DomainError('DENIED', '来源不可写入。')
    if not {'list', *(['write'] if write else [])} <= set(source.get('capabilities', ())):
        raise DomainError('UNSUPPORTED', '来源不支持导入所需能力。')
    return source


def Identity(dataset_id, key):
    return 'import:' + hashlib.sha256((dataset_id + '\n' + key).encode('utf-8')).hexdigest()


def Run(document: ImportDocument, source_id: str, operations: MemoryOperationsAccess, *,
        on_progress: Callable[[dict], None] | None = None,
        should_stop: Callable[[], bool] | None = None) -> dict:
    """Return a complete result; progress callbacks carry only new ID mappings."""
    result = dict(source_id=source_id, dataset_id=document.dataset_id, status='running',
                  progress=dict(topics_done=0, topics_total=len(document.topics), memories_done=0,
                                memories_total=len(document.memories)), topics=[], memories=[], error=None)
    stage, key = 'validation', None
    published_topics, published_memories = 0, 0
    def check():
        if should_stop and should_stop():
            raise DomainError('STOPPING', '导入已停止。')
    def publish():
        nonlocal published_topics, published_memories
        if on_progress:
            # Copy each confirmed mapping once, rather than copying the growing result.
            on_progress(deepcopy(result | {
                'topics': result['topics'][published_topics:],
                'memories': result['memories'][published_memories:]}))
            published_topics = len(result['topics'])
            published_memories = len(result['memories'])
    def read_topics():
        check()
        return operations.Topics(source_id)['topics']
    wanted = {Identity(document.dataset_id, m['key']) for m in document.memories}
    def records():
        found, cursor, visited = {}, None, set()
        while True:
            check()
            page = operations.List(MemoryListQuery(source_id, limit=100, cursor=cursor))
            if page.get('source_status', {}).get(source_id) != 'ok':
                raise DomainError('MEMORY_STORAGE', '来源读取失败。')
            for record in page['records']:
                if record.get('origin') in wanted:
                    found.setdefault(record['origin'], []).append(record)
            cursor = page['next_cursor']
            if cursor is None:
                return found
            if cursor in visited:
                raise DomainError('MEMORY_STORAGE', '分页游标重复。')
            visited.add(cursor)
    def match_topic(topic, parent, directory):
        matches = [t for t in directory if t['parent_id'] == parent and
                   NormalizeTopicName(t['name']) == NormalizeTopicName(topic['name'])]
        if len(matches) > 1 or (matches and matches[0]['description'] != topic['description']):
            raise DomainError('CONFLICT', '主题说明冲突。')
        return matches[0]['topic_id'] if matches else None
    def verify(memory, existing, topic_id, *, preflight=False):
        if len(existing) != 1:
            raise DomainError('CONFLICT', '记忆身份不唯一或记录不存在。')
        record = existing[0]
        if (not preflight and topic_id is None or record.get('source_id') != source_id or record.get('content') != memory['content'] or
            record.get('type') != memory['type'] or
            (record.get('topic_id') != topic_id and not (preflight and record.get('topic_id') is None)) or
            record.get('origin') != Identity(document.dataset_id, memory['key']) or
            not record.get('record_id') or record.get('truncated')):
            raise DomainError('CONFLICT', '记忆正文、类型或所属主题冲突。')
        return record
    try:
        check()
        CheckSource(operations, source_id)
        directory = read_topics()
        existing = records()
        mapping = {}
        # Resolve every existing path and origin before the first mutation.
        for topic in document.topics:
            key = topic['key']
            parent = mapping.get(topic['parent_key'])
            mapping[key] = (match_topic(topic, parent, directory)
                            if topic['parent_key'] is None or parent is not None else None)
        for memory in document.memories:
            key = memory['key']
            matches = existing.get(Identity(document.dataset_id, key), [])
            if matches:
                verify(memory, matches, mapping[memory['topic_key']], preflight=True)
        stage = 'topic'
        for topic in document.topics:
            check()
            key = topic['key']
            parent = mapping.get(topic['parent_key'])
            topic_id = match_topic(topic, parent, read_topics())
            if topic_id is None:
                check()
                try:
                    operations.CreateTopic(source_id, parent, topic['name'], topic['description'])
                except DomainError as error:
                    if error.code != 'CONFLICT':
                        raise
                topic_id = match_topic(topic, parent, read_topics())
                if topic_id is None:
                    raise DomainError('CONFLICT', '主题创建未确认。')
            mapping[key] = topic_id
            result['topics'].append(dict(key=key, topic_id=topic_id))
            result['progress']['topics_done'] += 1
            publish()
        stage = 'memory'
        for memory in document.memories:
            check()
            key = memory['key']
            identity = Identity(document.dataset_id, key)
            topic_id = mapping[memory['topic_key']]
            matches = existing.get(identity, [])
            check()
            receipt = operations.Write(MemoryDraft(source_id, memory['content'], identity, (),
                                                   type=memory['type'], origin=identity),
                                       request_key=identity, topic_id=topic_id)
            if matches and receipt.get('record_id') != matches[0].get('record_id'):
                raise DomainError('CONFLICT', '重复导入的记录身份发生变化。')
            matches = existing[identity] = [receipt]
            record = verify(memory, matches, topic_id)
            result['memories'].append(dict(key=key, record_id=record['record_id'], topic_id=topic_id,
                **({'index_status': record['index_status']} if record.get('index_status') is not None else {})))
            result['progress']['memories_done'] += 1
            publish()
        stage, key = 'verification', None
        directory, existing = read_topics(), records()
        for topic in document.topics:
            check()
            key = topic['key']
            if match_topic(topic, mapping.get(topic['parent_key']), directory) != mapping[key]:
                raise DomainError('CONFLICT', '最终目录核验失败。')
        for memory, receipt in zip(document.memories, result['memories']):
            check()
            key = memory['key']
            record = verify(memory, existing.get(Identity(document.dataset_id, key), []), mapping[memory['topic_key']])
            if record['record_id'] != receipt['record_id']:
                raise DomainError('CONFLICT', '最终记录核验失败。')
        result['status'] = 'completed'
    except Exception as error:
        code = error.code if isinstance(error, DomainError) else 'MEMORY_STORAGE'
        allowed = {'CONFLICT', 'STOPPING', 'DENIED', 'UNSUPPORTED', 'NOT_FOUND', 'NOT_READY',
                   'MEMORY_STORAGE', 'TOPIC_SYNC_FAILED', 'TIMEOUT', 'DEADLINE', 'TRANSPORT',
                   'EMBEDDING_SOURCE', 'EMBEDDING_PROTOCOL', 'CONFIG_CREDENTIAL', 'CANCELLED'} | CODES
        code = code if code in allowed else 'MEMORY_STORAGE'
        result['status'] = 'stopped'
        result['error'] = dict(stage=stage, key=key, code=code,
                               message='导入停止；请核对正文和主题目录后重新导入。' if code != 'CONFLICT'
                               else '已有数据与导入内容冲突，请核对正文、类型、主题和说明。')
        if isinstance(error, MemoryFailure):
            # Keep the import cursor stage; the exact driver stage lives in issues.
            result['error'].update(FailureData(error))
    publish()
    return result
