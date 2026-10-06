"""Isolated one-shot DDGS search worker; invoked only by web_search."""

from __future__ import annotations

import json
import re
import sys


def main() -> int:
    try:
        from ddgs import DDGS

        request = json.load(sys.stdin)
        results = list(DDGS(proxy=request.get("proxy")).text(
            request["query"], max_results=request["limit"]
        ))
        sanitized = [
            {
                "position": index + 1,
                "title": str(item.get("title", ""))[:500],
                "url": str(item.get("href") or item.get("url") or "")[:2048],
                "description": str(item.get("body") or item.get("description") or "")[:1500],
            }
            for index, item in enumerate(results[:request["limit"]])
        ]
        json.dump({"results": sanitized}, sys.stdout, ensure_ascii=False)
        return 0
    except BaseException as exc:
        # Bound diagnostics and avoid exposing proxy credentials in tool results.
        message = re.sub(r"([a-z][a-z0-9+.-]*://)[^/\s@]+@",
                         r"\1[redacted]@", str(exc), flags=re.IGNORECASE)
        json.dump({"error": type(exc).__name__, "message": message[:1000]},
                  sys.stdout, ensure_ascii=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
