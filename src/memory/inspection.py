"""Frozen management scope, bounded single-flight cache and request budgets."""
from collections import OrderedDict
from copy import deepcopy
from dataclasses import replace
from threading import Condition, BoundedSemaphore
from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import json
import time

from ..frame.contracts import DomainError, freeze, thaw
from .cognee_inspection import now


class MemoryInspection:
    capabilities = ('health', 'datasets', 'raw_data', 'graph')

    def __init__(self, sources, adapters):
        self._sources = freeze(sources)
        self._adapters = dict(adapters)
        self._slots = {name: BoundedSemaphore(2) for name in adapters}
        self._condition = Condition()
        self._cache, self._flights = OrderedDict(), {}
        self._epochs = {name: 0 for name in adapters}
        self._closed = False
        self._fingerprints = {name: hashlib.sha256(json.dumps(thaw(source), sort_keys=True).encode()).hexdigest()
                              for name, source in self._sources.items()}

    def Supports(self, source):
        return source in self._adapters

    def Invalidate(self, source):
        with self._condition:
            if source in self._epochs:
                self._epochs[source] += 1
            for key in list(self._cache):
                if key[0] == source:
                    del self._cache[key]

    def _authorize(self, query, context, operation):
        context.CheckCancelled()
        source = self._sources.get(query.source_id)
        if source is None:
            raise DomainError('NOT_FOUND', 'Source not found')
        if context.purpose != 'management' or source.get('read_access', f'memory.{query.source_id}.read') not in context.access:
            raise DomainError('DENIED', 'Inspection access denied')
        if source.get('store') != 'cognee' or not self.Supports(query.source_id):
            raise DomainError('UNSUPPORTED', 'Inspection unavailable')
        if operation not in {'health', 'datasets', 'dataset-summary', 'data', 'raw', 'graph'}:
            raise DomainError('NOT_FOUND', 'Inspection route not found')
        if operation not in {'health', 'datasets'} and query.dataset_name not in source['datasets']:
            raise DomainError('NOT_FOUND', 'Dataset not found')
        if type(query.limit) is not int or not 1 <= query.limit <= 100 or type(query.refresh) is not bool:
            raise DomainError('INVALID_REQUEST', 'Invalid inspection query')
        if operation == 'graph' and (type(query.max_nodes) is not int or query.max_nodes not in (100, 300, 500) or type(query.depth) is not int or query.depth not in (1, 2)):
            raise DomainError('INVALID_REQUEST', 'Invalid graph limits')
        return source

    def _cursor(self, query):
        if query.cursor is None:
            return 0
        try:
            if not isinstance(query.cursor, str) or len(query.cursor) > 1024:
                raise ValueError
            source, dataset, limit, offset = json.loads(base64.b64decode(query.cursor, altchars=b'-_', validate=True))
            if (source, dataset, limit) != (query.source_id, query.dataset_name, query.limit) or type(limit) is not int or type(offset) is not int or not 0 < offset <= 10000000 or offset % limit:
                raise ValueError
            return offset
        except (ValueError, TypeError, UnicodeError):
            raise DomainError('INVALID_REQUEST', 'Invalid inspection cursor') from None

    def Read(self, operation, query, context):
        try:
            return self._Read(operation, query, context)
        except DomainError as error:
            if error.code == 'DEADLINE':
                raise DomainError('INSPECTION_TIMEOUT', 'Inspection timed out') from None
            raise

    def _Read(self, operation, query, context):
        source = self._authorize(query, context, operation)
        context = replace(context, deadline=min(context.deadline or float('inf'),
            time.monotonic() + min(source.get('timeout', 2), 3 if operation == 'health' else 10 if operation == 'graph' else 5)))
        offset = self._cursor(query)
        if operation == 'raw' and (not isinstance(query.data_id, str) or not query.data_id or len(query.data_id) > 128
                                  or any(c in query.data_id for c in '/\\\x00')):
            raise DomainError('INVALID_REQUEST', 'Invalid data reference')
        with self._condition:
            if self._closed:
                raise DomainError('NOT_READY', 'Inspection closed')
            if operation == 'graph' and query.seed_node_id is not None:
                if not isinstance(query.seed_node_id, str) or not any(
                    k[0] == query.source_id and k[3] == 'graph' and k[4] == query.dataset_name and v[0] > time.monotonic()
                    and any(n['id'] == query.seed_node_id for n in v[1]['nodes']) for k, v in self._cache.items()):
                    raise DomainError('NOT_FOUND', 'Node not in current authorized snapshot; refresh graph')
            epoch = self._epochs[query.source_id]
            key = (query.source_id, self._fingerprints[query.source_id], epoch, operation,
                   query.dataset_name, query.limit, offset, query.data_id, query.seed_node_id, query.depth, query.max_nodes)
            cached = self._cache.get(key)
            if operation != 'raw' and not query.refresh and cached and cached[0] > time.monotonic():
                self._cache.move_to_end(key)
                value = deepcopy(cached[1]); value['cached'] = True
                return value
            flight = self._flights.get(key)
            if flight is not None:
                while not flight['done']:
                    context.CheckCancelled()
                    self._condition.wait(min(.05, context.Remaining()))
                if flight.get('error') is not None:
                    raise flight['error']
                return deepcopy(flight['value'])
            flight = self._flights[key] = {'done': False}
        slot = self._slots[query.source_id]
        borrowed = False
        try:
            while not borrowed:
                context.CheckCancelled()
                borrowed = slot.acquire(timeout=min(.05, context.Remaining()))
            context.CheckCancelled()
            # Summary fields borrow the same source slots independently. Release
            # this aggregate's slot before starting those bounded reads.
            def release_slot():
                nonlocal borrowed
                slot.release()
                borrowed = False
            value = self._read(operation, query, offset, context, release_slot=release_slot if operation == 'dataset-summary' else None)
            value['cached'] = False
            if operation not in {'health', 'dataset-summary'}:
                context.CheckCancelled()
            with self._condition:
                flight['value'] = deepcopy(value)
                if operation != 'raw' and epoch == self._epochs[query.source_id]:
                    size = len(json.dumps(value).encode())
                    if size <= 16 * 1024 * 1024:
                        self._cache[key] = (time.monotonic() + (30 if operation == 'graph' else 15), deepcopy(value), size)
                        while len(self._cache) > 32 or sum(v[2] for v in self._cache.values()) > 16 * 1024 * 1024:
                            self._cache.popitem(last=False)
            return value
        except DomainError as error:
            if error.code == 'DEADLINE':
                error = DomainError('INSPECTION_TIMEOUT', 'Inspection timed out')
            with self._condition:
                flight['error'] = error
            raise error
        except BaseException as error:
            with self._condition:
                flight['error'] = error
            raise
        finally:
            if borrowed:
                slot.release()
            with self._condition:
                flight['done'] = True
                self._flights.pop(key, None)
                self._condition.notify_all()

    def _read(self, operation, query, offset, context, release_slot=None):
        adapter = self._adapters[query.source_id]
        base = {'source_id': query.source_id}
        if operation == 'health':
            start = time.monotonic()
            service = {'state': 'unknown', 'ready': None, 'version': None}
            access, scope, warnings = {'state': 'unknown'}, {'state': 'unknown'}, []
            try:
                service = adapter.HealthService(context)
            except DomainError as error:
                service['state'] = 'unreachable' if error.code == 'INSPECTION_SOURCE' else 'unknown'
                warnings.append(error.code)
            try:
                datasets = adapter.Datasets(context)
                access['state'], scope['state'] = 'ok', 'available' if datasets else 'missing'
            except DomainError as error:
                access['state'] = {'CONFIG_CREDENTIAL': 'auth_failed', 'DENIED': 'denied'}.get(error.code, 'unknown')
                warnings.append(error.code)
            return base | {'service': service, 'access': access, 'scope': scope, 'warnings': warnings,
                           'latency_ms': round((time.monotonic() - start) * 1000), 'checked_at': now()}
        datasets = adapter.Datasets(context)
        if operation == 'datasets':
            return base | {'items': datasets, 'fetched_at': now()}
        if not any(r['dataset_name'] == query.dataset_name for r in datasets):
            raise DomainError('NOT_FOUND', 'Dataset not found or unreadable')
        base['dataset_name'] = query.dataset_name
        if operation == 'dataset-summary':
            release_slot()
            fields = {'total': None, 'processing': {'state': 'unknown', 'raw_status': None},
                      'graph': {'nodes': None, 'edges': None, 'computed_at': None}, 'warnings': []}
            def read_field(method):
                slot = self._slots[query.source_id]
                acquired = False
                try:
                    while not acquired:
                        context.CheckCancelled()
                        acquired = slot.acquire(timeout=min(.05, context.Remaining()))
                    return method(query.dataset_name, context)
                finally:
                    if acquired:
                        slot.release()
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {field: pool.submit(read_field, method) for field, method in
                           [('total', adapter.Count), ('processing', adapter.Status), ('graph', adapter.Statistics)]}
                for field, future in futures.items():
                    try:
                        fields[field] = future.result()
                    except DomainError as error:
                        fields['warnings'].append({'field': field, 'code': 'INSPECTION_TIMEOUT' if error.code == 'DEADLINE' else error.code})
            return base | fields | {'checked_at': now()}
        if operation == 'graph':
            return base | adapter.Graph(query.dataset_name, query.max_nodes, query.depth, query.seed_node_id, context)
        if operation == 'raw':
            return base | adapter.Raw(query.dataset_name, query.data_id, context)
        rows = adapter.Rows(query.dataset_name, query.limit, offset, context)
        total, checked = None, None
        try:
            total, checked = adapter.Count(query.dataset_name, context), now()
        except DomainError:
            pass
        cursor = base64.urlsafe_b64encode(json.dumps([query.source_id, query.dataset_name, query.limit, offset + query.limit]).encode()).decode() if len(rows) == query.limit else None
        return base | {'items': rows, 'next_cursor': cursor, 'total': total, 'total_checked_at': checked}

    def Close(self):
        with self._condition:
            self._closed = True
            while self._flights:
                self._condition.wait(.05)
            self._cache.clear()
