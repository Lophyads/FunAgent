"""Strict scalar-only read routes; authority remains in MemoryInspection."""
from ..frame.contracts import DomainError
from ..memory.inspection_contracts import InspectionQuery
from ..memory.memory_operations_mgr import MemoryOperationsMgr
from .request_validation import Text


def Handle(method, route, query, body):
    operation = route.removeprefix('/memory/cognee/')
    fields = {'health': {'source_id', 'refresh'}, 'datasets': {'source_id', 'refresh'},
              'dataset-summary': {'source_id', 'dataset_name', 'refresh'},
              'data': {'source_id', 'dataset_name', 'limit', 'cursor', 'refresh'},
              'raw': {'source_id', 'dataset_name', 'data_id'},
              'graph': {'source_id', 'dataset_name', 'max_nodes', 'depth', 'seed_node_id', 'refresh'}}
    if operation not in fields:
        raise DomainError('NOT_FOUND', 'Inspection route not found')
    if method != 'GET':
        raise DomainError('METHOD_NOT_ALLOWED', 'Inspection is read-only')
    required = {'source_id'} | ({'dataset_name'} if operation not in {'health', 'datasets'} else set()) | ({'data_id'} if operation == 'raw' else set())
    if query.keys() - fields[operation] or required - query.keys() or any(len(v) != 1 for v in query.values()):
        raise DomainError('INVALID_REQUEST', 'Invalid inspection parameters')
    values = {k: Text(v[0], 1024 if k == 'cursor' else 512) for k, v in query.items()}
    if 'refresh' in values:
        if values['refresh'] != '1':
            raise DomainError('INVALID_REQUEST', 'Invalid refresh')
        values['refresh'] = True
    for field in ('limit', 'max_nodes', 'depth'):
        if field not in values:
            continue
        if not values[field].isascii() or not values[field].isdigit():
            raise DomainError('INVALID_REQUEST', 'Invalid limit')
        values[field] = int(values[field])
    return 200, MemoryOperationsMgr.GetInstance().Inspect(operation, InspectionQuery(**values))
