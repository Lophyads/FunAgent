#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
mMARCO 中文端到端召回评测：检索已摄入的 mmmarco_eval collection，算召回指标。

通过 chunk 的 metadata.source_path（文件路径）反查段落 id，与 qrels 对齐。

用法（先 prepare + ingest，再 eval）：
  python scripts/prepare_mmmarco_docs.py --data-dir /path/to/dataset --num-queries 300 --pool-size 2000
  python scripts/ingest.py --path data/mmmarco_eval_docs --collection mmmarco_eval --force
  python scripts/eval_mmmarco_e2e.py --collection mmmarco_eval --ks 1,3,5,10
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_REPO_ROOT))

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from src.core.settings import load_settings

DEFAULT_META_PATH = _REPO_ROOT / "data" / "mmmarco_eval_meta.json"


def load_meta(meta_path=DEFAULT_META_PATH):
    with open(meta_path, encoding="utf-8") as f:
        return json.load(f)


def build_search(settings, collection: str, top_k: int):
    from dataclasses import replace
    from src.core.query_engine.query_entrypoint import StructuredSearchAdapter
    settings = replace(settings, retrieval=replace(settings.retrieval,
        dense_top_k=top_k, sparse_top_k=top_k, fusion_top_k=top_k))
    return StructuredSearchAdapter(settings, collection)


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
    names = ["dense", "sparse", "hybrid"]
    agg = {n: {} for n in names}
    cnt = 0
    t0 = time.time()
    for qid, text in queries.items():
        rel = qrels.get(qid)
        if not rel:
            continue
        try:
            detail = hybrid.service.query(text,top_k=max(ks),use_rerank=False,return_details=True)
        except Exception as e:
            print(f"      [WARN] query {qid} 检索失败: {e}")
            continue
        def unit_documents(rows, key):
            units = hybrid.service.evidence.get_units([row[key] for row in rows])
            paths = [hybrid.service.evidence.get_document(u.doc_id).source_path for u in units]
            return list(dict.fromkeys(Path(path).stem for path in paths))
        ranking = {
            'dense':unit_documents(detail.diagnostics['dense_results'],'id'),
            'sparse':unit_documents(detail.diagnostics['sparse_results'],'chunk_id'),
            'hybrid':list(dict.fromkeys(Path(row['metadata']['source_path']).stem for row in detail.evidence_results)),
        }
        rel_set = set(rel)
        for n in names:
            r = _score_one(ranking[n], rel_set, ks)
            if r:
                for key, val in r.items():
                    agg[n].setdefault(key, []).append(val)
        cnt += 1
        if cnt % 50 == 0:
            print(f"      已评测 {cnt} 条，用时 {time.time() - t0:.0f}s")
    result = {}
    for n in names:
        result[n] = {k: float(np.mean(v)) for k, v in agg[n].items()}
        result[n]["num_queries"] = cnt
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collection", default="mmmarco_eval")
    ap.add_argument("--meta", default=str(DEFAULT_META_PATH), help="meta.json 路径")
    ap.add_argument("--ks", type=str, default="1,3,5,10")
    ap.add_argument("--config", default=str(_REPO_ROOT / "config" / "settings.yaml"))
    args = ap.parse_args()

    ks = tuple(int(x) for x in args.ks.split(","))
    meta = load_meta(args.meta)
    queries = meta["queries"]
    qrels = meta["qrels"]
    print(f"查询 {len(queries)} 条 / 标注 query {len(qrels)} 条")

    settings = load_settings(args.config)
    hybrid = build_search(settings, args.collection, max(ks))

    try:
        result = evaluate(hybrid, queries, qrels, ks)
    finally:
        hybrid.close()

    names = ["dense", "sparse", "hybrid"]
    print("\n" + "=" * 66)
    print(f"  端到端召回对比（有效 query {result['hybrid']['num_queries']}）")
    print("=" * 66)
    for m in ["recall", "ndcg", "mrr", "hit"]:
        for k in ks:
            key = f"{m}@{k}"
            vals = [result[n].get(key, 0.0) for n in names]
            print(f"  {key:<9}" + "".join(f"{v:>10.4f}" for v in vals))
    print("-" * 66)


    out = _REPO_ROOT / "logs" / f"{args.collection}_result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out}")


if __name__ == "__main__":
    main()
