#!/usr/bin/env python
"""Evaluation script for Modular RAG MCP Server.

Runs batch evaluation against a golden test set and outputs a metrics report.

Usage:
    # Run with default settings (custom evaluator)
    python scripts/evaluate.py

    # Specify a custom golden test set
    python scripts/evaluate.py --test-set path/to/golden.json

    # Use a specific collection
    python scripts/evaluate.py --collection technical_docs

    # JSON output
    python scripts/evaluate.py --json

Exit codes:
    0 - Success
    1 - Evaluation failure
    2 - Configuration error
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Set UTF-8 encoding for Windows console
if sys.platform == "win32" and __name__ == '__main__':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Run RAG evaluation against a golden test set."
    )
    parser.add_argument(
        "--test-set",
        default="tests/fixtures/golden_test_set.json",
        help="Path to golden test set JSON file (default: tests/fixtures/golden_test_set.json)",
    )
    parser.add_argument(
        "--collection",
        default=None,
        help="Collection name to search within.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=10,
        help="Number of chunks to retrieve per query (default: 10).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON instead of formatted text.",
    )
    parser.add_argument(
        "--no-search",
        action="store_true",
        help="Skip retrieval (evaluate with mock chunks for testing).",
    )
    parser.add_argument('--labels',help='Evidence labels JSON for structured evaluation')
    parser.add_argument('--build-id')
    parser.add_argument('--profile')
    parser.add_argument('--rerank',action='store_true')
    parser.add_argument('--min-grade',type=float,default=2)
    parser.add_argument('--output')
    return parser.parse_args()


def main() -> int:
    """Main entry point."""
    args = parse_args()
    from src.core.settings import resolve_path
    test_data=json.loads(resolve_path(args.test_set).read_text(encoding='utf-8'))
    if test_data.get('schema_version') == '2':
        if args.no_search:
            raise ValueError('Structured evidence evaluation requires actual retrieval')
        if not args.labels:
            raise ValueError('--labels is required for structured evidence evaluation')
        from scripts.evaluate_structured import run
        args.profile=args.profile or 'config/profiles/structured_v2.yaml'
        args.collection=args.collection or 'server'
        args.query_id=None
        args.resume=False
        report,path=run(args)
        print(json.dumps(report if args.json else {'report':str(path),'status':report['status'],
              'formal_quality_status':report['formal_quality_status']},ensure_ascii=False,indent=2))
        return 0

    try:
        from src.core.settings import load_settings
        from src.libs.evaluator.evaluator_factory import EvaluatorFactory
        from src.observability.evaluation.eval_runner import EvalRunner

        settings = load_settings(args.profile)
    except Exception as exc:
        print(f"❌ Configuration error: {exc}", file=sys.stderr)
        return 2

    # Create evaluator from config
    try:
        evaluator = EvaluatorFactory.create(settings)
        evaluator_name = type(evaluator).__name__
    except Exception as exc:
        print(f"❌ Failed to create evaluator: {exc}", file=sys.stderr)
        return 2

    from src.core.query_engine.query_entrypoint import StructuredSearchAdapter
    hybrid_search = None if args.no_search else StructuredSearchAdapter(settings, args.collection or 'default')

    # Create and run EvalRunner
    runner = EvalRunner(
        settings=settings,
        hybrid_search=hybrid_search,
        evaluator=evaluator,
    )

    try:
        print(f"\n🔍 Running evaluation with {evaluator_name}...")
        print(f"📄 Test set: {args.test_set}")
        print(f"🔢 Top-K: {args.top_k}\n")

        report = runner.run(
            test_set_path=args.test_set,
            top_k=args.top_k,
            collection=args.collection,
        )
    except Exception as exc:
        print(f"❌ Evaluation failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if isinstance(hybrid_search, StructuredSearchAdapter):
            hybrid_search.close()

    # Output results
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        _print_report(report)

    return 0


def _print_report(report) -> None:
    """Print formatted evaluation report."""
    print("=" * 60)
    print("  EVALUATION REPORT")
    print("=" * 60)
    print(f"  Evaluator: {report.evaluator_name}")
    print(f"  Test Set:  {report.test_set_path}")
    print(f"  Queries:   {len(report.query_results)}")
    print(f"  Time:      {report.total_elapsed_ms:.0f} ms")
    print()

    # Aggregate metrics
    print("─" * 60)
    print("  AGGREGATE METRICS")
    print("─" * 60)
    if report.aggregate_metrics:
        for metric, value in sorted(report.aggregate_metrics.items()):
            bar = "█" * int(value * 20) + "░" * (20 - int(value * 20))
            print(f"  {metric:<25s} {bar} {value:.4f}")
    else:
        print("  (no metrics computed)")
    print()

    # Per-query details
    print("─" * 60)
    print("  PER-QUERY RESULTS")
    print("─" * 60)
    for i, qr in enumerate(report.query_results, 1):
        print(f"\n  [{i}] {qr.query}")
        print(f"      Retrieved: {len(qr.retrieved_chunk_ids)} chunks")
        if qr.metrics:
            for metric, value in sorted(qr.metrics.items()):
                print(f"      {metric}: {value:.4f}")
        else:
            print("      (no metrics)")
        print(f"      Time: {qr.elapsed_ms:.0f} ms")

    print()
    print("=" * 60)


if __name__ == "__main__":
    sys.exit(main())
