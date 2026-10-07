#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
批量摄入（优化版）：复用 pipeline 的 加载→切分→变换→编码 每一环，
但把存储改成「一次性 BM25 构建 + 批量 Chroma upsert」，消除逐文件
replace_chunks 导致的 O(n²) 瓶颈。

用法（在项目根目录）：
  python scripts/ingest_mmmarco_batch.py --docs-dir data/mmmarco_eval_docs --collection mmmarco_eval
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_REPO_ROOT))

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from src.core.settings import load_settings
from src.ingestion.chunking.document_chunker import DocumentChunker
from src.ingestion.transform.chunk_refiner import ChunkRefiner
from src.ingestion.transform.metadata_enricher import MetadataEnricher
from src.ingestion.embedding.sparse_encoder import SparseEncoder
from src.ingestion.storage.bm25_indexer import BM25Indexer
from src.ingestion.storage.vector_upserter import VectorUpserter
from src.libs.loader.loader_factory import LoaderFactory
from src.libs.loader.file_integrity import SQLiteIntegrityChecker
from src.libs.embedding.embedding_factory import EmbeddingFactory


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs-dir", default=str(_REPO_ROOT / "data" / "mmmarco_eval_docs"))
    ap.add_argument("--collection", default="mmmarco_eval")
    ap.add_argument("--embed-batch", type=int, default=16)
    ap.add_argument("--store-batch", type=int, default=1000)
    ap.add_argument("--clear", action="store_true", help="摄入前清空 collection")
    ap.add_argument("--config", default=str(_REPO_ROOT / "config" / "settings.yaml"))
    args = ap.parse_args()

    settings = load_settings(args.config)

    # 组件（与 pipeline 相同）
    chunker = DocumentChunker(settings)
    refiner = ChunkRefiner(settings)
    enricher = MetadataEnricher(settings)
    sparse_encoder = SparseEncoder()
    embedding = EmbeddingFactory.create(settings)
    vector_upserter = VectorUpserter(settings, collection_name=args.collection)
    bm25_indexer = BM25Indexer(index_dir=str(_REPO_ROOT / f"data/db/bm25/{args.collection}"))
    integrity = SQLiteIntegrityChecker(db_path=str(_REPO_ROOT / "data/db/ingestion_history.db"))

    if args.clear:
        try:
            vector_upserter.vector_store.clear()
            print("[clear] 已清空 collection")
        except Exception as e:
            print(f"[warn] 清空失败（可忽略）: {e}")

    # ---- 阶段 1：load -> chunk -> transform，收集所有 chunk ----
    files = sorted(Path(args.docs_dir).glob("*.md"))
    print(f"[阶段1] 加载/切分/变换 {len(files)} 个文件 ...")
    t0 = time.time()
    all_chunks = []
    file_records = []  # (file_hash, file_path) 供写入 integrity history
    failed = 0
    for i, f in enumerate(files, 1):
        try:
            loader = LoaderFactory.create_for(f)
            doc = loader.load(str(f))
            doc.metadata["source_path"] = str(f.resolve())
            chunks = chunker.split_document(doc)
            chunks = refiner.transform(chunks)
            chunks = enricher.transform(chunks)
            all_chunks.extend(chunks)
            file_records.append((integrity.compute_sha256(str(f)), str(f.resolve())))
        except Exception as e:
            failed += 1
            print(f"      [FAIL] {f.name}: {e}")
        if i % 500 == 0:
            print(f"      {i}/{len(files)} ...")
    print(f"      完成：{len(all_chunks)} chunks（失败 {failed} 文件），用时 {time.time() - t0:.0f}s")

    # ---- 阶段 2：批量编码（dense + sparse） ----
    print(f"[阶段2] 编码 {len(all_chunks)} 个 chunk ...")
    t0 = time.time()
    sparse_stats = sparse_encoder.encode(all_chunks)
    dense_vectors = []
    texts = [c.text for c in all_chunks]
    for start in range(0, len(texts), args.embed_batch):
        batch = texts[start:start + args.embed_batch]
        dense_vectors.extend(embedding.embed(batch))
        done = min(start + args.embed_batch, len(texts))
        if (start // args.embed_batch + 1) % 50 == 0:
            print(f"      {done}/{len(texts)} ...")
    print(f"      完成，用时 {time.time() - t0:.0f}s")

    # ---- 阶段 3：批量存储（chroma 分批 upsert + BM25 一次性 build） ----
    print("[阶段3] 存储 ...")
    t0 = time.time()
    vector_ids = []
    for start in range(0, len(all_chunks), args.store_batch):
        bch = all_chunks[start:start + args.store_batch]
        bvec = dense_vectors[start:start + args.store_batch]
        vector_ids.extend(vector_upserter.upsert(bch, bvec))
    # BM25 chunk_id 对齐到 chroma 的 vector id
    for stat, vid in zip(sparse_stats, vector_ids):
        stat["chunk_id"] = vid
    bm25_indexer.build(sparse_stats, collection=args.collection)
    print(f"      完成：{len(vector_ids)} chunks 入库，用时 {time.time() - t0:.0f}s")

    # 写 integrity history（dashboard 的文档列表据此显示）
    for file_hash, file_path in file_records:
        integrity.mark_success(file_hash, file_path, args.collection)
    print(f"      integrity history 已写入 {len(file_records)} 条记录")

    print(f"\n[完成] 共 {len(all_chunks)} chunks，collection='{args.collection}'")


if __name__ == "__main__":
    main()
