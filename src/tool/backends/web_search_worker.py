"""Optional keyless search lives in a cancellable isolated process."""
import json
import sys


def main():
    from ddgs import DDGS
    args = json.loads(sys.argv[1])
    results = list(DDGS(timeout=args["timeout"]).text(args["query"], max_results=args["limit"]))
    print(json.dumps({"results": [{"title": r["title"], "url": r["href"], "snippet": r.get("body", "")} for r in results]}))


if __name__ == "__main__":
    main()
