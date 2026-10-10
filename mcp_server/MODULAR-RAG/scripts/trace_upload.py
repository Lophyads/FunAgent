#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
用 strace 风格追踪 Ingestion Manager 的 upload 链路。

模拟 dashboard 的上传流程（写临时文件 → IngestionPipeline.run），
用 sys.settrace 追踪 src 模块的函数调用，输出「缩进调用树 + 时间戳」。

用法（在项目根目录）：
  python scripts/trace_upload.py                    # 用内置演示文档
  python scripts/trace_upload.py --doc 某文件.md    # 追踪指定文档
"""
from __future__ import annotations

import argparse
import linecache
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

DEMO_DOC = (
    "## 演示文档\n\n"
    "这是用于追踪 upload 链路的测试文本。\n\n"
    "第二段：摄入时这段会被切分成 chunk，然后向量化，最后写入数据库。\n"
)


def _short(obj) -> str:
    """返回值的简短摘要。"""
    if obj is None:
        return ""
    s = repr(obj)
    if len(s) > 48:
        s = s[:48] + "…"
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description="追踪 Ingestion Manager upload 链路")
    ap.add_argument("--doc", help="要追踪的文档路径（默认用内置演示文本）")
    ap.add_argument("--collection", default="trace_demo")
    ap.add_argument("--max-depth", type=int, default=8, help="最大展示深度")
    args = ap.parse_args()

    # 1) 模拟 upload：写临时文件（同 dashboard 的 NamedTemporaryFile）
    import tempfile

    if args.doc:
        doc_path = Path(args.doc)
        tmp_obj = None
    else:
        tmp_obj = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8")
        tmp_obj.write(DEMO_DOC)
        tmp_obj.close()
        doc_path = Path(tmp_obj.name)

    # 2) 初始化 pipeline
    from src.core.settings import load_settings
    from src.ingestion.pipeline import IngestionPipeline

    settings = load_settings(str(_REPO_ROOT / "config" / "settings.yaml"))
    pipeline = IngestionPipeline(settings, collection=args.collection, force=True)

    # 3) strace 风格追踪
    t0 = time.monotonic()
    depth = 0
    lines: list[str] = []

    def tracer(frame, event, arg):
        nonlocal depth
        code = frame.f_code
        fn = code.co_filename
        # 只追踪项目源码（src 模块），跳过第三方库
        if "src" not in fn or "site-packages" in fn:
            return tracer
        rel = fn.split("src", 1)[-1]
        func = code.co_name
        ts = (time.monotonic() - t0) * 1000.0
        if event == "call":
            if depth <= args.max_depth:
                lineno = code.co_firstlineno
                src_line = linecache.getline(fn, lineno).strip()
                lines.append(
                    f"{ts:9.2f}ms {'  ' * depth}→ {func}()  [{rel}:{lineno}]"
                )
                if src_line and len(src_line) <= 78:
                    lines.append(f"{'':10s}{'  ' * depth}    {src_line}")
            depth += 1
        elif event == "return":
            depth -= 1
            if depth <= args.max_depth:
                lines.append(f"{ts:9.2f}ms {'  ' * depth}← {func}() = {_short(arg)}")
        return tracer

    sys.settrace(tracer)
    try:
        result = pipeline.run(str(doc_path), source_path=doc_path.name if args.doc else "uploaded_demo.md")
    finally:
        sys.settrace(None)
        pipeline.close()

    # 4) 输出
    print("=" * 72)
    print("  Ingestion Manager upload 链路追踪（strace 风格）")
    print(f"  文档: {doc_path.name}   collection: {args.collection}")
    print("=" * 72)
    for line in lines:
        print(line)
    print("-" * 72)
    print(f"结果: success={result.success}  chunks={result.chunk_count}  images={result.image_count}")
    if result.error:
        print(f"错误: {result.error}")

    if tmp_obj is not None:
        doc_path.unlink(missing_ok=True)
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
