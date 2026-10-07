#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
诊断脚本：找出「加载后没有文本内容」的文档（会导致
"Document xxx has no text content to split" 报错）。

用法（在项目根目录）：
  python scripts/find_empty_docs.py --path documents
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_REPO_ROOT))

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from src.libs.loader.loader_factory import LoaderFactory


def main() -> int:
    ap = argparse.ArgumentParser(description="找出没有文本内容的文档")
    ap.add_argument("--path", "-p", required=True, help="文档目录")
    args = ap.parse_args()

    root = Path(args.path)
    if not root.exists():
        print(f"[FAIL] 目录不存在: {root}")
        return 2

    exts = {".pdf", ".md", ".markdown"}
    files = [f for f in root.rglob("*") if f.suffix.lower() in exts]
    print(f"扫描 {len(files)} 个文件 ...\n")

    empty, ok, failed = [], 0, []
    for f in files:
        try:
            doc = LoaderFactory.create_for(f).load(str(f))
            text = (doc.text or "").strip()
            if not text:
                empty.append(f)
            else:
                ok += 1
        except Exception as e:
            failed.append((f, str(e)))

    print(f"正常: {ok} 个")
    print(f"文本为空: {len(empty)} 个")
    if empty:
        print("\n=== 以下文件没有文本内容（需要处理）===")
        for f in empty:
            size = f.stat().st_size
            print(f"  - {f}  ({size} 字节)")
    if failed:
        print("\n=== 以下文件加载失败 ===")
        for f, e in failed:
            print(f"  - {f}: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
