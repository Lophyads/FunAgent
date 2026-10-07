#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
准备 MLDR-zh 长文档评测数据：选 N 个 query 的正例文档 + 负例文档，写 .md 文件。

每个长文档写成一个 .md 文件，文件名 = docid（如 doc-zh-0.md）。
摄入后 chunk 的 source_path 反查 docid，实现「文档级」召回（命中该文档任意 chunk 算对）。

产出：
  - data/mldr_eval_docs/*.md
  - data/mldr_eval_meta.json  {"queries": {qid: text}, "qrels": {qid: [docid]}}

用法：
  python scripts/prepare_mldr_docs.py --num-queries 10 --max-negatives 20
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_REPO_ROOT))

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

DEFAULT_DATA = Path(r"C:/Users/ql136/WorkBuddy/2026-10-05-21-22-48/rag_eval/data/mldr/dev.jsonl.gz")
DOCS_DIR = _REPO_ROOT / "data" / "mldr_eval_docs"
META_PATH = _REPO_ROOT / "data" / "mldr_eval_meta.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(DEFAULT_DATA), help="MLDR dev.jsonl.gz 路径")
    ap.add_argument("--num-queries", type=int, default=10, help="选几个 query（= 正例文档数）")
    ap.add_argument("--max-negatives", type=int, default=20, help="负例文档数上限")
    args = ap.parse_args()

    # 解析 dev.jsonl.gz
    queries = {}
    pos_docs = {}      # docid -> text
    neg_docs = {}      # docid -> text
    qrels = {}         # qid -> [docid]
    with gzip.open(args.data, "rt", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= args.num_queries:
                break
            d = json.loads(line)
            qid = d["query_id"]
            queries[qid] = d["query"]
            for p in d.get("positive_passages", []):
                pos_docs[p["docid"]] = p["text"]
                qrels.setdefault(qid, []).append(p["docid"])
            for n in d.get("negative_passages", []):
                if n["docid"] not in pos_docs:
                    neg_docs[n["docid"]] = n["text"]

    # 负例去重 + 限数
    neg_ids = list(neg_docs.keys())[: args.max_negatives]
    neg_docs = {k: neg_docs[k] for k in neg_ids}

    # 正例文档里若也有被用作负例的，去掉（保持干净）
    all_docs = dict(pos_docs)
    for k, v in neg_docs.items():
        if k not in all_docs:
            all_docs[k] = v

    # 写 .md 文件（文件名 = docid）
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    for old in DOCS_DIR.glob("*.md"):
        old.unlink()
    for docid, text in all_docs.items():
        (DOCS_DIR / f"{docid}.md").write_text(text, encoding="utf-8")

    # 写 meta（文档级 qrels）
    meta = {"queries": queries, "qrels": qrels}
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[完成] 正例文档 {len(pos_docs)} 个 + 负例文档 {len(neg_docs)} 个 = 共 {len(all_docs)} 个长文档")
    print(f"       查询 {len(queries)} 条")
    lens = [len(t) for t in all_docs.values()]
    print(f"       文档长度: 最小 {min(lens)} / 最大 {max(lens)} / 平均 {sum(lens)//len(lens)} 字")
    print(f"       写入 {DOCS_DIR}")
    print(f"下一步摄入：")
    print(f"  python scripts/ingest_mmmarco_batch.py --docs-dir {DOCS_DIR} --collection mldr_eval")


if __name__ == "__main__":
    main()
