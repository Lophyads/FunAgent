"""One-time cleanup of legacy Cognee soft-deleted documents; dry-run by default."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.frame.contracts import DomainError
from src.memory.cognee_documents import NAME, _unpack
from src.memory.common import validate_content


def cleanup(client, dataset_id, source_id, *, apply=False):
    """Delete only owned, explicitly marked documents in the selected dataset."""
    dataset_id = str(UUID(dataset_id))
    found, offset = [], 0
    # Collect before deleting so pagination offsets cannot skip shifted rows.
    while True:
        response = client.get(f'/datasets/{dataset_id}/data', params={'limit': 100, 'offset': offset})
        response.raise_for_status()
        page = response.json()
        if not isinstance(page, list) or len(page) > 100:
            raise ValueError('Invalid dataset page')
        for row in page:
            if not NAME.fullmatch(row.get('name', '')):
                continue
            if str(UUID(row.get('datasetId', row.get('dataset_id')))) != dataset_id:
                raise ValueError('Document outside requested dataset')
            identifier = str(UUID(row['id']))
            response = client.get(f'/datasets/{dataset_id}/data/{identifier}/raw')
            response.raise_for_status()
            raw = response.content
            try:
                content, metadata = _unpack(raw)
            except DomainError:
                continue
            if (metadata.get('source_id') != source_id or metadata.get('type') != 'note' or
                    not isinstance(metadata.get('write_key'), str) or not metadata['write_key'].strip() or
                    metadata.get('deleted') is not True):
                continue
            validate_content(content, metadata.get('evidence'), 'note')
            found.append((identifier, sha256(raw).hexdigest()))
        if len(page) < 100:
            break
        offset += len(page)
    if apply:
        for identifier, expected in found:
            path = f'/datasets/{dataset_id}/data/{identifier}'
            response = client.get(path + '/raw')
            response.raise_for_status()
            if sha256(response.content).hexdigest() != expected:
                raise ValueError('Document changed during cleanup; rerun the dry-run')
            client.delete(path).raise_for_status()
            response = client.get(path + '/raw')
            if response.status_code != 404:
                response.raise_for_status()
                raise ValueError('Deletion could not be verified')
    return {'dataset_id': dataset_id, 'source_id': source_id, 'applied': apply,
            'data_ids': [identifier for identifier, _ in found]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', required=True, help='Cognee API base URL including /api/v1')
    parser.add_argument('--dataset-id', required=True, type=UUID)
    parser.add_argument('--source-id', required=True)
    parser.add_argument('--credential-env', default='COGNEE_MEMORY_TOKEN')
    parser.add_argument('--apply', action='store_true', help='Delete the listed legacy documents')
    args = parser.parse_args()
    import httpx
    token = os.environ.get(args.credential_env)
    if not token:
        parser.error('The credential environment variable is missing')
    with httpx.Client(base_url=args.endpoint.rstrip('/') + '/',
                      headers={'Authorization': 'Bearer ' + token}, timeout=120, trust_env=False) as client:
        # Relative paths preserve the configured /api/v1 prefix.
        class API:
            def get(self, path, **kwargs):
                return client.get(path.lstrip('/'), **kwargs)

            def delete(self, path):
                return client.delete(path.lstrip('/'))

        print(json.dumps(cleanup(API(), str(args.dataset_id), args.source_id, apply=args.apply)))


if __name__ == '__main__':
    main()
