"""Read-only Cognee protocol adapter with a separate bounded HTTP lease."""
from datetime import datetime, timezone
from urllib.parse import quote
import json
import math
import re
import hashlib

from ..frame.contracts import ControlError, DomainError
from .cognee import Headers
from .cognee_datasets import CogneeDataset
from .cognee_documents import _unpack, _deleted
from .common import validate_content, validate_metadata
from .transport import JSONHTTP


def now():
    return datetime.now(timezone.utc).isoformat()


def protocol():
    raise DomainError('INSPECTION_PROTOCOL', 'Invalid inspection response')


def safe_metadata(value):
    result = _safe_metadata(value)
    return result if len(json.dumps(result, ensure_ascii=False).encode()) <= 8192 else {'truncated': True}


def _safe_metadata(value, depth=0):
    if depth > 3:
        return None
    if isinstance(value, dict):
        return {str(k)[:100]: _safe_metadata(v, depth + 1) for k, v in list(value.items())[:24]
                if not re.search(r'path|location|token|secret|password|credential|owner|user|hash|pipeline|provenance|authorization|api.?key', str(k), re.I)}
    if isinstance(value, list):
        return [_safe_metadata(v, depth + 1) for v in value[:16]]
    if isinstance(value, str):
        if re.search(r'(?:[A-Za-z]:[\\/]|/(?:home|Users|tmp|etc)/|Bearer\s|https?://[^\s]*[?@])', value, re.I):
            return '[filtered]'
        return value[:512]
    return value if value is None or type(value) in (bool, int) or type(value) is float and math.isfinite(value) else None


class CogneeInspection:
    def __init__(self, settings, source_id, *, transport=None):
        self.source_id = source_id
        self.datasets = tuple(settings['datasets'])
        self.timeout = settings.get('timeout', 2)
        endpoint = settings['endpoint'].rstrip('/')
        self._health_supported = endpoint.endswith('/api/v1')
        self._http = JSONHTTP(endpoint, Headers(settings), self.timeout, transport=transport,
                              protocol='INSPECTION_PROTOCOL', source='INSPECTION_SOURCE')
        # A second endpoint would duplicate pools; an absolute URL override is unnecessary:
        # HTTPX normalizes ../ segments while retaining a reverse-proxy prefix.
        self._health_path = '/../../health'

    def request(self, path, context, **kwargs):
        return self._http.Request('GET', path, context,
            status_codes={401: 'CONFIG_CREDENTIAL', 403: 'DENIED', 404: 'NOT_FOUND', 405: 'UNSUPPORTED'}, **kwargs)

    def HealthService(self, context):
        if not self._health_supported:
            return {'state': 'unknown', 'ready': None, 'version': None}
        value = self.request(self._health_path, context)
        if not isinstance(value, dict):
            protocol()
        ready = value.get('status') == 'ready'
        healthy = value.get('health') == 'healthy'
        return {'state': 'healthy' if ready and healthy else 'degraded', 'ready': ready,
                'version': value.get('version')[:128] if isinstance(value.get('version'), str) else None}

    def Datasets(self, context):
        values = self.request('/datasets', context)
        if not isinstance(values, list) or any(not isinstance(v, dict) or not isinstance(v.get('id'), str)
            or not isinstance(v.get('name'), str) for v in values):
            protocol()
        result = []
        for name in self.datasets:
            registered = CogneeDataset.Find(name)
            if registered is None:
                continue
            row = next((v for v in values if v['name'] == name and v['id'] == registered.dataset_id), None)
            if row is not None:
                for field in ('createdAt', 'updatedAt'):
                    if row.get(field) is not None and (not isinstance(row[field], str) or len(row[field]) > 512):
                        protocol()
                result.append({'dataset_name': name, 'created_at': row.get('createdAt'), 'updated_at': row.get('updatedAt')})
        return result

    def dataset_id(self, name):
        selected = CogneeDataset.Find(name)
        if name not in self.datasets or selected is None:
            raise DomainError('NOT_FOUND', 'Dataset not found')
        return selected.dataset_id

    def Rows(self, name, limit, offset, context):
        identifier = self.dataset_id(name)
        values = self.request(f'/datasets/{identifier}/data', context, params={'limit': limit, 'offset': offset})
        if not isinstance(values, list) or len(values) > limit:
            protocol()
        result = []
        for v in values:
            if (not isinstance(v, dict) or not isinstance(v.get('id'), str) or not v['id'] or len(v['id']) > 128
                or not isinstance(v.get('name'), str) or v.get('datasetId', v.get('dataset_id')) != identifier):
                protocol()
            size = v.get('dataSize')
            if size is not None and (type(size) is not int or size < 0):
                protocol()
            for field in ('extension', 'mimeType', 'createdAt', 'updatedAt'):
                if v.get(field) is not None and (not isinstance(v[field], str) or len(v[field]) > 512):
                    protocol()
            result.append({'data_id': v['id'], 'name': v['name'][:512], 'extension': v.get('extension'),
                'mime_type': v.get('mimeType'), 'size': size, 'created_at': v.get('createdAt'),
                'updated_at': v.get('updatedAt'), 'tags': safe_metadata(v.get('label')),
                'metadata': safe_metadata(v.get('externalMetadata'))})
        # This is a timestamped observation for browsing, never write evidence.
        completed, issue = {}, None
        try:
            payload = self.request(f'/datasets/{identifier}/processing-status', context)
            if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
                protocol()
            for item in payload['items']:
                if (not isinstance(item, dict) or not isinstance(item.get('id'), str) or
                        type(item.get('completed')) is not bool or item['id'] in completed):
                    protocol()
                completed[item['id']] = item['completed']
        except ControlError:
            raise
        except DomainError as error:
            completed, issue = {}, error.code
        checked = now()
        for row in result:
            row.update(completed=completed.get(row['data_id']), checked_at=checked)
            if issue:
                row['completion_issue'] = issue
        return result

    def Count(self, name, context):
        value = self.request(f'/datasets/{self.dataset_id(name)}/data/count', context)
        if not isinstance(value, dict) or type(value.get('count')) is not int or value['count'] < 0:
            protocol()
        return value['count']

    def Status(self, name, context):
        identifier = self.dataset_id(name)
        value = self.request('/datasets/status', context, params={'dataset': identifier, 'pipeline': 'cognify_pipeline'})
        if not isinstance(value, dict):
            protocol()
        raw = value.get(identifier)
        if raw is not None and not isinstance(raw, str):
            protocol()
        states = {'DATASET_PROCESSING_'+k: v for k,v in {'INITIATED': 'pending', 'STARTED': 'running', 'COMPLETED': 'completed', 'ERRORED': 'failed'}.items()}
        return {'state': states.get(raw, 'unknown'), 'raw_status': raw[:128] if raw else raw}

    def Statistics(self, name, context):
        identifier = self.dataset_id(name)
        value = self.request('/datasets/graph-summary', context, params={'dataset_ids': identifier})
        if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
            protocol()
        row = next((v for v in value if v.get('datasetId', v.get('dataset_id')) == identifier), {})
        for k in ('numNodes', 'numEdges'):
            if row.get(k) is not None and (type(row[k]) is not int or row[k] < 0):
                protocol()
        return {'nodes': row.get('numNodes'), 'edges': row.get('numEdges'), 'computed_at': row.get('computedAt')}

    def Raw(self, name, data_id, context):
        # Membership is proved by this dataset's unfiltered pages before downloading bytes.
        offset = 0
        while True:
            rows = self.Rows(name, 100, offset, context)
            row = next((r for r in rows if r['data_id'] == data_id), None)
            if row is not None:
                break
            if len(rows) < 100:
                raise DomainError('NOT_FOUND', 'Data not found or unreadable')
            offset += 100
        raw = self.request(f'/datasets/{self.dataset_id(name)}/data/{quote(data_id, safe="")}/raw', context, response_mode='bytes')
        mime = (row['mime_type'] or '').lower().split(';')[0]
        ext = (row['extension'] or '').lower().lstrip('.')
        textual = mime.startswith('text/') or mime in {'application/json', 'application/xml'} or (not mime and ext in {'txt', 'md', 'json', 'csv', 'xml'})
        content, owned, deleted = None, False, False
        if textual:
            try:
                content = raw.decode('utf8')
            except UnicodeError:
                textual = False
            if textual:
                try:
                    body, metadata = _unpack(raw)
                    if metadata.get('source_id') == self.source_id and metadata.get('type') == 'note' and isinstance(metadata.get('write_key'), str) and metadata['write_key'].strip():
                        validate_content(body, metadata.get('evidence'), 'note')
                        validate_metadata(metadata.get('subject'), metadata.get('event_at'), metadata.get('origin'))
                        content, owned, deleted = body, True, _deleted(metadata)
                except DomainError:
                    pass
        truncated = textual and len(content.encode('utf8')) > 65536
        preview = content.encode('utf8')[:65536].decode('utf8', errors='ignore') if textual else None
        return {'item': row, 'preview': preview, 'textual': textual, 'truncated': bool(truncated),
                'funagent_note': owned, 'deleted': deleted, 'fetched_at': now()}

    def Graph(self, name, max_nodes, depth, seed, context):
        identifier = self.dataset_id(name)
        warnings = []
        totals = {'nodes': None, 'edges': None, 'computed_at': None}
        try:
            totals = self.Statistics(name, context)
        except DomainError as error:
            warnings.append(error.code)
        params = {'dataset_id': identifier, 'full': 'false', 'stream': 'false',
                  'max_nodes': max_nodes, 'neighborhood_depth': depth}
        if seed is not None:
            params['seed_node_ids'] = seed
        coverage = 'bounded'
        try:
            value = self.request('/visualize/json', context, params=params)
        except DomainError as error:
            if error.code not in {'NOT_FOUND', 'UNSUPPORTED'}:
                raise
            if seed is not None or totals['nodes'] is None or totals['edges'] is None or totals['nodes'] > 500 or totals['edges'] > 1000:
                raise DomainError('UNSUPPORTED', 'Bounded graph unavailable; safe fallback not established') from None
            value = self.request(f'/datasets/{identifier}/graph', context)
            coverage = 'validated-small-fallback'
            warnings.append('BOUNDED_UNSUPPORTED')
        if not isinstance(value, dict) or not isinstance(value.get('nodes'), list):
            protocol()
        links = value.get('links', value.get('edges'))
        if not isinstance(links, list):
            protocol()
        if coverage != 'bounded' and (len(value['nodes']) > 500 or len(links) > 1000):
            raise DomainError('INSPECTION_TOO_LARGE', 'Fallback graph exceeded validated limits')
        nodes, identifiers = [], set()
        for row in value['nodes']:
            if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'] or len(row['id']) > 512 or row['id'] in identifiers:
                protocol()
            identifiers.add(row['id'])
            label, kind = row.get('name', row.get('label')), row.get('type')
            if label is not None and not isinstance(label, str) or kind is not None and not isinstance(kind, str):
                protocol()
            # Only explicitly declared public properties are exposed; provenance,
            # document names and unverified document IDs are never associations.
            properties = row.get('properties', {})
            if not isinstance(properties, dict):
                protocol()
            nodes.append({'id': row['id'], 'label': (label or row['id'])[:512], 'type': (kind or 'unknown')[:128],
                          'properties': safe_metadata({k: v for k, v in properties.items() if k in {'description', 'category', 'value', 'created_at', 'updated_at'}})})
        # Associations require explicit data ID + matching dataset UUID, followed
        # by membership verification. Names/document_id alone carry no authority.
        candidates = {r['id']: r['data_id'] for r in value['nodes']
                      if r.get('dataset_id') == identifier and isinstance(r.get('data_id'), str)}
        if candidates:
            remaining = set(candidates.values()); found = set(); offset = 0
            while remaining:
                rows = self.Rows(name, 100, offset, context)
                matches = {r['data_id'] for r in rows} & remaining
                found.update(matches); remaining -= matches
                if len(rows) < 100:
                    break
                offset += 100
            for node in nodes:
                if candidates.get(node['id']) in found:
                    node['data_id'] = candidates[node['id']]
        truncated = value.get('truncated') if type(value.get('truncated')) is bool else None
        if len(nodes) > max_nodes:
            nodes = nodes[:max_nodes]; truncated = True; warnings.append('NODE_LIMIT')
        kept = {n['id'] for n in nodes}
        edges = []
        for index, row in enumerate(links):
            if not isinstance(row, dict) or not isinstance(row.get('source'), str) or not isinstance(row.get('target'), str):
                protocol()
            if row['source'] not in identifiers or row['target'] not in identifiers:
                warnings.append('DANGLING_EDGE'); continue
            label = row.get('relation', row.get('label'))
            properties = row.get('properties', {})
            if label is not None and not isinstance(label, str) or not isinstance(properties, dict):
                protocol()
            if row['source'] not in kept or row['target'] not in kept:
                continue
            edges.append({'id': f'edge-{index}', 'source': row['source'], 'target': row['target'],
                          'label': (label or 'unknown')[:512], 'properties': safe_metadata({k:v for k,v in properties.items() if k in {'description', 'weight', 'value'}}),
                          'properties_provided': 'properties' in row})
        if len(edges) > 1000:
            edges = edges[:1000]; truncated = True; warnings.append('EDGE_LIMIT')
        snapshot = hashlib.sha256(json.dumps([nodes, edges], sort_keys=True).encode()).hexdigest()
        return {'snapshot_id': snapshot, 'nodes': nodes, 'edges': edges, 'coverage': coverage,
                'limits': {'max_nodes': max_nodes, 'max_edges': 1000, 'depth': depth}, 'seed_node_id': seed,
                'shown': {'nodes': len(nodes), 'edges': len(edges)}, 'dataset_totals': totals,
                'truncated': truncated, 'warnings': sorted(set(warnings)), 'fetched_at': now()}

    def Close(self):
        self._http.Close()
