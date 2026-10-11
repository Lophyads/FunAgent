"""Pure Unicode tokenization and source-local retrieval ranking."""
import re
from datetime import datetime
from .common import normalize


def Terms(value):
    normalized = normalize(value).strip()
    words = re.findall(r"[\u3400-\u9fff]+|[^\W_\u3400-\u9fff]+", normalized)
    terms = [piece for word in words for piece in
             ([word[i:i + 2] for i in range(len(word) - 1)] if re.fullmatch(r"[\u3400-\u9fff]{2,}", word) else [word])]
    return tuple(dict.fromkeys(terms)) or ((normalized,) if normalized else ())


def Rank(row, terms, bm25=0):
    coverage = sum(term in row["search_text"] for term in terms)
    event = datetime.fromisoformat(row["event_at"]).timestamp() if row["event_at"] else 0
    return (-coverage, bm25, -event, row["seq"])
