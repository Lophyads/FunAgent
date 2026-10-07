#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
mMARCO 中文混合检索召回质量评测脚本。

用你项目的真实检索组件（jieba BM25 + 本地 Qwen3-Embedding + RRF 融合）评测
召回质量，输出 Recall@k / MRR@k / nDCG@k / Hit@k，并对比
Dense-only / Sparse-only / Hybrid 三种方式。

原理：
  - 把 mMARCO 的每个段落直接当作一个 Chunk 注入（id = 段落 id），
    绕开 loader / splitter / transform，因此不受"只支持 pdf/md"限制，
    也不需要 LLM（Transform 已关，检索本身也不用 LLM）。
  - 检索返回的 chunk_id 就是段落 id，可直接与 qrels 对齐算召回。

用法（在项目根目录，用项目 .venv 跑）：
  # 快速冒烟（小规模，几分钟）
  python scripts/eval_mmmarco.py --max-corpus 5000 --num-queries 100

  # 全量（10.6 万段，embedding 需几分钟到十几分钟）
  python scripts/eval_mmmarco.py --num-queries 500

  # 全量 + 全部 6980 查询
  python scripts/eval_mmmarco.py

  # 已经摄入过，跳过摄入直接评测
  python scripts/eval_mmmarco.py --skip-ingest --num-queries 500
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# 路径 / 编码
# --------------------------------------------------------------------------- #
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_REPO_ROOT))

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from src.core.settings import load_settings
from src.core.types import Chunk
from src.core.query_engine.query_processor import QueryProcessor
from src.core.query_engine.hybrid_search import create_hybrid_search
from src.core.query_engine.dense_retriever import create_dense_retriever
from src.core.query_engine.sparse_retriever import create_sparse_retriever
from src.ingestion.storage.bm25_indexer import BM25Indexer
from src.ingestion.embedding.sparse_encoder import SparseEncoder
from src.libs.embedding.embedding_factory import EmbeddingFactory
from src.libs.vector_store.vector_store_factory import VectorStoreFactory

# 默认数据目录（已下载好的 mMARCO 中文 MTEB 抽样版三件套）
DEFAULT_DATA_DIR = Path(r"C:/Users/ql136/WorkBuddy/2026-10-05-21-22-48/rag_eval/data")


# --------------------------------------------------------------------------- #
# 数据加载
# --------------------------------------------------------------------------- #
def load_data(data_dir: Path, max_corpus: int | None, num_queries: int | None):
    """读取 corpus/queries/qrels，返回 (corpus, queries, qrels)。

    corpus: {doc_id: text}      queries: {qid: text}
    qrels:  {qid: {doc_id: score}}
    """
    corpus_df = pd.read_parquet(data_dir / "corpus.parquet")
    queries_df = pd.read_parquet(data_dir / "queries.parquet")

    corpus = {str(r["id"]): r["text"] for r in corpus_df.to_dict("records")}
    queries = {str(r["id"]): r["text"] for r in queries_df.to_dict("records")}

    # 读 qrels.dev.tsv，只保留中文 query 的部分
    qids_set = set(queries.keys())
    qrels: dict[str, dict[str, int]] = {}
    with open(data_dir / "qrels.dev.tsv", encoding="utf-8") as f:
        for line in f:
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            qid, _, doc_id, score = parts[0], parts[1], parts[2], parts[3]
            if qid in qids_set:
                qrels.setdefault(qid, {})[doc_id] = int(score)

    # 抽样 corpus（冒烟用；抽样会排除部分正例，指标仅作链路验证）
    if max_corpus and max_corpus < len(corpus):
        keys = list(corpus.keys())[:max_corpus]
        corpus = {k: corpus[k] for k in keys}

    # 抽样 query
    if num_queries and num_queries < len(queries):
        keys = list(queries.keys())[:num_queries]
        queries = {k: queries[k] for k in keys}

    # 只保留 doc 在语料中的 qrels 对，且 query 有有效标注
    qrels_clean: dict[str, dict[str, int]] = {}
    for qid, docs in qrels.items():
        if qid not in queries:
            continue
        d = {doc: s for doc, s in docs.items() if doc in corpus}
        if d:
            qrels_clean[qid] = d

    return corpus, queries, qrels_clean


# --------------------------------------------------------------------------- #
# 摄入：段落 -> Chunk -> Sparse(BM25) + Dense(Embedding) -> 存储
# --------------------------------------------------------------------------- #
def ingest(corpus: dict, settings, collection: str, embed_batch: int):
    """把段落写入 BM25 索引 + ChromaDB（id = 段落 id，检索后可直接对齐 qrels）。"""
    doc_ids = list(corpus.keys())
    texts = list(corpus.values())
    print(f"      摄入 {len(doc_ids)} 段到 collection='{collection}'")

    # 1) 稀疏：SparseEncoder（jieba 分词）-> BM25Indexer
    print("      [1/3] BM25 索引（jieba 分词）...")
    t0 = time.time()
    chunks = [
        Chunk(id=did, text=txt, metadata={"source_path": did, "chunk_index": 0})
        for did, txt in corpus.items()
    ]
    term_stats = SparseEncoder().encode(chunks)
    bm25_indexer = BM25Indexer(index_dir=str(_REPO_ROOT / f"data/db/bm25/{collection}"))
    bm25_indexer.build(term_stats, collection=collection)
    print(f"            完成，{len(term_stats)} 篇，用时 {time.time() - t0:.0f}s")

    # 2) 稠密：本地 embedding -> ChromaDB
    print("      [2/3] 稠密向量（本地 Qwen3-Embedding）...")
    t0 = time.time()
    embedding = EmbeddingFactory.create(settings)
    vector_store = VectorStoreFactory.create(settings, collection_name=collection)
    for start in range(0, len(doc_ids), embed_batch):
        batch_ids = doc_ids[start:start + embed_batch]
        batch_texts = texts[start:start + embed_batch]
        vectors = embedding.embed(batch_texts)
        records = [
            {"id": did, "vector": vec, "metadata": {"text": txt, "source_path": did}}
            for did, vec, txt in zip(batch_ids, vectors, batch_texts)
        ]
        vector_store.upsert(records)
        if (start // embed_batch + 1) % 20 == 0:
            print(f"            {start + len(batch_ids)}/{len(doc_ids)} ...")
    print(f"            完成，用时 {time.time() - t0:.0f}s")

    print("      [3/3] 存储就绪")
    return bm25_indexer, vector_store, embedding


# --------------------------------------------------------------------------- #
# 检索组件组装（与 scripts/query.py 一致）
# --------------------------------------------------------------------------- #
def build_search(settings, collection: str, bm25_indexer, vector_store, embedding, top_k: int):
    dense_retriever = create_dense_retriever(
        settings=settings, embedding_client=embedding, vector_store=vector_store,
    )
    sparse_retriever = create_sparse_retriever(
        settings=settings, bm25_indexer=bm25_indexer, vector_store=vector_store,
    )
    sparse_retriever.default_collection = collection
    query_processor = QueryProcessor()
    hybrid = create_hybrid_search(
        settings=settings,
        query_processor=query_processor,
        dense_retriever=dense_retriever,
        sparse_retriever=sparse_retriever,
    )
    # 让 dense/sparse/fusion 都返回足够多，便于对比各 k 的召回
    hybrid.config.dense_top_k = top_k
    hybrid.config.sparse_top_k = top_k
    hybrid.config.fusion_top_k = top_k
    return hybrid


# --------------------------------------------------------------------------- #
# 指标
# --------------------------------------------------------------------------- #
def _dcg(rels, k):
    return sum(r / math.log2(i + 2) for i, r in enumerate(rels[:k]))


def _score_one(retrieved_ids, rel_set, ks):
    if not rel_set:
        return None
    hits = [1 if d in rel_set else 0 for d in retrieved_ids]
    out = {}
    for k in ks:
        top = hits[:k]
        out[f"recall@{k}"] = sum(top) / len(rel_set)
        out[f"hit@{k}"] = 1.0 if sum(top) > 0 else 0.0
        out[f"ndcg@{k}"] = _dcg(hits, k) / _dcg([1] * len(rel_set), k)
        mrr = 0.0
        for i, h in enumerate(top, start=1):
            if h:
                mrr = 1.0 / i
                break
        out[f"mrr@{k}"] = mrr
    return out


def evaluate(hybrid, queries, qrels, ks):
    """一次检索拿到 dense-only / sparse-only / hybrid 三组结果，分别算指标。"""
    names = ["dense", "sparse", "hybrid"]
    agg = {n: {} for n in names}
    cnt = 0
    t0 = time.time()
    for qid, text in queries.items():
        rel = qrels.get(qid)
        if not rel:
            continue
        try:
            detail = hybrid.search(query=text, top_k=max(ks), return_details=True)
        except Exception as e:
            print(f"      [WARN] query {qid} 检索失败: {e}")
            continue
        ranking = {
            "dense": [r.chunk_id for r in (detail.dense_results or [])],
            "sparse": [r.chunk_id for r in (detail.sparse_results or [])],
            "hybrid": [r.chunk_id for r in detail.results],
        }
        rel_set = set(rel.keys())
        for n in names:
            r = _score_one(ranking[n], rel_set, ks)
            if r:
                for key, val in r.items():
                    agg[n].setdefault(key, []).append(val)
        cnt += 1
        if cnt % 100 == 0:
            print(f"      已评测 {cnt} 条，用时 {time.time() - t0:.0f}s")
    result = {}
    for n in names:
        result[n] = {k: float(np.mean(v)) for k, v in agg[n].items()}
        result[n]["num_queries"] = cnt
    return result


def print_report(result, ks):
    names = ["dense", "sparse", "hybrid"]
    print("\n" + "=" * 66)
    print(f"  召回质量对比（有效 query 数 {result['hybrid']['num_queries']}）")
    print("=" * 66)
    header = "  metric    " + "".join(f"{'dense':>10}{'sparse':>10}{'hybrid':>10}" for _ in [0])
    print(header)
    for m in ["recall", "ndcg", "mrr", "hit"]:
        for k in ks:
            key = f"{m}@{k}"
            vals = [result[n].get(key, 0.0) for n in names]
            print(f"  {key:<9}" + "".join(f"{v:>10.4f}" for v in vals))
    print("-" * 66)
    print("  说明：hybrid 的 recall@k 高于单独 dense/sparse，说明混合检索有效")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="mMARCO 中文混合检索召回评测")
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--collection", default="mmmarco_eval", help="独立 collection，不污染现有数据")
    ap.add_argument("--max-corpus", type=int, default=None, help="抽样语料段数（冒烟用）")
    ap.add_argument("--num-queries", type=int, default=None, help="评测查询数（默认全量 6980）")
    ap.add_argument("--ks", type=str, default="1,3,5,10")
    ap.add_argument("--embed-batch", type=int, default=32)
    ap.add_argument("--skip-ingest", action="store_true", help="已摄入过则跳过摄入")
    ap.add_argument("--config", default=str(_REPO_ROOT / "config" / "settings.yaml"))
    args = ap.parse_args()

    ks = tuple(int(x) for x in args.ks.split(","))

    if not (args.data_dir / "corpus.parquet").exists():
        print(f"[FAIL] 数据目录不存在或缺少文件: {args.data_dir}")
        print("       需要 corpus.parquet / queries.parquet / qrels.dev.tsv")
        return 2

    settings = load_settings(args.config)
    corpus, queries, qrels = load_data(args.data_dir, args.max_corpus, args.num_queries)
    print(f"语料 {len(corpus)} 段 / 查询 {len(queries)} 条 / 有效标注 query {len(qrels)} 条")

    top_k = max(ks)

    if args.skip_ingest:
        print("\n[跳过摄入] 直接复用已有索引 ...")
        embedding = EmbeddingFactory.create(settings)
        vector_store = VectorStoreFactory.create(settings, collection_name=args.collection)
        bm25_indexer = BM25Indexer(index_dir=str(_REPO_ROOT / f"data/db/bm25/{args.collection}"))
    else:
        print("\n[摄入] ...")
        bm25_indexer, vector_store, embedding = ingest(corpus, settings, args.collection, args.embed_batch)

    print("\n[检索评测] ...")
    hybrid = build_search(settings, args.collection, bm25_indexer, vector_store, embedding, top_k)
    result = evaluate(hybrid, queries, qrels, ks)
    print_report(result, ks)

    # 存一份 JSON 结果
    out = str(_REPO_ROOT / "logs" / "mmmarco_eval_result.json")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
