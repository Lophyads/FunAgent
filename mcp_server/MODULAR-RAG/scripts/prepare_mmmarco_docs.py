#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
准备 mMARCO 中文评测文档：把段落写成 .md 文件，供 ingest.py 走完整链路摄入。

每个段落写成一个 .md 文件，文件名 = 段落 id（如 2026790.md），
摄入后 chunk 的 metadata.source_path 就是该文件路径，评测时用文件名反查段落 id。

产出：
  - data/mmmarco_eval_docs/*.md   段落文件
  - data/mmmarco_eval_meta.json   {"queries": {qid: text}, "qrels": {qid: [doc_id,...]}}

用法：
  python scripts/prepare_mmmarco_docs.py --data-dir /path/to/dataset --num-queries 300 --pool-size 2000
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import pandas as pd

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_REPO_ROOT))

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

DOCS_DIR = _REPO_ROOT / "data" / "mmmarco_eval_docs"
META_PATH = _REPO_ROOT / "data" / "mmmarco_eval_meta.json"


def load_raw(data_dir: Path):
    corpus_df = pd.read_parquet(data_dir / "corpus.parquet")
    queries_df = pd.read_parquet(data_dir / "queries.parquet")
    corpus = {str(r["id"]): r["text"] for r in corpus_df.to_dict("records")}
    queries = {str(r["id"]): r["text"] for r in queries_df.to_dict("records")}

    qids_set = set(queries.keys())
    qrels: dict[str, list[str]] = {}
    with open(data_dir / "qrels.dev.tsv", encoding="utf-8") as f:
        for line in f:
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            qid, _, doc_id, score = parts[0], parts[1], parts[2], parts[3]
            if qid in qids_set and int(score) > 0:
                qrels.setdefault(qid, []).append(doc_id)
    return corpus, queries, qrels


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--num-queries", type=int, default=300, help="评测查询数")
    ap.add_argument("--pool-size", type=int, default=2000, help="语料池大小（正例+随机负例）")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    corpus, queries, qrels = load_raw(args.data_dir)
    print(f"全量语料 {len(corpus)} 段 / 查询 {len(queries)} 条 / 标注 query {len(qrels)} 条")

    # 1) 选 query（有有效标注的优先，按原顺序取）
    qids_all = [q for q in queries if q in qrels and qrels[q]]
    qids = qids_all[: args.num_queries]
    selected_queries = {q: queries[q] for q in qids}

    # 2) 池子抽样：正例全保留 + 随机负例补足
    random.seed(args.seed)
    pos = {d for q in qids for d in qrels[q] if d in corpus}
    neg_pool = [d for d in corpus if d not in pos]
    n_neg = max(0, args.pool_size - len(pos))
    neg = set(random.sample(neg_pool, min(n_neg, len(neg_pool))))
    selected_docs = sorted(pos | neg)

    # 3) 写 .md 文件
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    for old in DOCS_DIR.glob("*.md"):
        old.unlink()
    for did in selected_docs:
        (DOCS_DIR / f"{did}.md").write_text(corpus[did], encoding="utf-8")

    # 4) 保存 meta（只保留 doc 在池子里的 qrels）
    meta_qrels = {
        q: [d for d in qrels[q] if d in pos] for q in qids
    }
    meta = {"queries": selected_queries, "qrels": meta_qrels}
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n[完成] 写 {len(selected_docs)} 个 .md 文件到 {DOCS_DIR}")
    print(f"       查询 {len(selected_queries)} 条 / 正例 {len(pos)} 段 + 负例 {len(neg)} 段")
    print(f"       meta 已保存到 {META_PATH}")
    print(f"\n下一步摄入：")
    print(f"  python scripts/ingest.py --path {DOCS_DIR} --collection mmmarco_eval --force")


if __name__ == "__main__":
    main()
