"""Read documents, original evidence and assets from published collections."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from src.core import index_registry, path_resolver
from src.core.structured_types import DocumentBundle
from src.ingestion.storage.evidence_store import EvidenceStore


@dataclass
class DeleteResult:
    success: bool
    errors: list[str] = field(default_factory=list)


class DataService:
    def __init__(self, project_root=None, settings=None):
        self.resolver = path_resolver.PathResolver(project_root)
        self.registry = index_registry.IndexRegistry(project_root=self.resolver.root)
        self.settings = settings

    def _structured_store(self, collection):
        target = self.resolver.component(collection or 'default', 'collection')
        if target not in self.registry._read()['active']:
            return None
        snapshot = self.registry.get_active(target)
        return snapshot, EvidenceStore(snapshot.path('evidence_db'), snapshot.build_id,
                                       snapshot.project_root, read_only=True)

    @staticmethod
    def _documents(store):
        with store.connection() as conn:
            rows = conn.execute('SELECT payload_json FROM documents ORDER BY source_path').fetchall()
        return [DocumentBundle.from_dict(json.loads(row[0])) for row in rows]

    @staticmethod
    def _info(document, snapshot):
        return {'doc_id':document.doc_id, 'source_path':document.source_path, 'source_hash':document.source_hash,
            'collection':snapshot.logical_collection, 'chunk_count':len(document.search_units),
            'image_count':sum(f.classification not in {'decorative','failed'} for f in document.figures),
            'processed_at':None, 'build_id':snapshot.build_id, 'can_delete':False}

    def close(self):
        """Connections are scoped to each read; no persistent clients are held."""

    def list_collections(self):
        return sorted(self.registry._read()['active'])

    def list_documents(self, collection=None):
        captured = self._structured_store(collection)
        if captured is None:
            return []
        snapshot, store = captured
        return [self._info(d, snapshot) for d in self._documents(store)]

    def get_document_bundle(self, doc_id, collection=None):
        captured = self._structured_store(collection)
        if captured is None:
            return None
        _, store = captured
        with store.connection() as conn:
            rows = conn.execute('SELECT doc_id FROM documents WHERE doc_id=? OR source_hash=?',
                                (doc_id,doc_id)).fetchall()
        if len(rows) > 1:
            raise ValueError('Ambiguous document identity')
        return store.get_document(rows[0][0]) if rows else None

    def get_document_detail(self, doc_id, collection=None):
        captured = self._structured_store(collection)
        if captured is None:
            return None
        snapshot, store = captured
        document = next((d for d in self._documents(store) if doc_id in {d.doc_id,d.source_hash}),None)
        if document is None:
            return None
        return {**self._info(document,snapshot), 'chunk_ids':[u.unit_id for u in document.search_units],
                'image_ids':[f.figure_id for f in document.figures if f.classification not in {'decorative','failed'}]}

    def get_chunks(self, source_hash, collection=None):
        captured = self._structured_store(collection)
        if captured is None:
            return []
        snapshot, store = captured
        return [{'id':u.unit_id, 'text':u.retrieval_text, 'metadata':{
            'doc_id':d.doc_id, 'source_path':d.source_path, 'build_id':snapshot.build_id,
            'granularity':u.granularity, 'content_type':u.content_type, 'evidence_ids':u.evidence_ids,
            'llm_metadata':u.attributes.get('llm_metadata',{})}}
            for d in self._documents(store) if d.source_hash==source_hash for u in d.search_units]

    def get_images(self, source_hash, collection=None):
        captured = self._structured_store(collection)
        if captured is None:
            return []
        _, store = captured
        results = []
        for document in self._documents(store):
            if document.source_hash != source_hash:
                continue
            assets = {a.asset_id:a for a in document.assets}
            for figure in document.figures:
                if figure.classification not in {'decorative','failed'}:
                    results.append({'image_id':figure.figure_id,
                        'file_path':str(self.resolver.within(assets[figure.asset_id].path)),
                        'page':figure.page, 'classification':figure.classification})
        return results

    def delete_document(self, source_path, collection=None, source_hash=None):
        return DeleteResult(False, ['已发布集合不可原地删除；应重建并发布新的来源集合。'])

    def get_collection_stats(self, collection=None):
        docs = self.list_documents(collection)
        return {'collection':collection or 'default', 'document_count':len(docs),
                'chunk_count':sum(d['chunk_count'] for d in docs), 'image_count':sum(d['image_count'] for d in docs)}
