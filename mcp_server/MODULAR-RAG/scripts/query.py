"""Query original evidence in a current collection or completed staging build."""
import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--query', '-q', required=True)
    parser.add_argument('--collection', '-c', default='default')
    parser.add_argument('--top-k', type=int, default=10)
    parser.add_argument('--config', default=str(_REPO_ROOT/'config/settings.yaml'))
    parser.add_argument('--no-rerank', action='store_true')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--build-id')
    parser.add_argument('--json', action='store_true')
    return parser.parse_args()


def main():
    args = parse_args()
    from src.core.settings import load_settings
    from src.core.query_engine.query_entrypoint import query_collection
    try:
        bundle = query_collection(load_settings(args.config), args.query, args.collection,
            args.top_k, use_rerank=not args.no_rerank, build_id=args.build_id)
    except Exception as exc:
        print(f'Query failed: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
