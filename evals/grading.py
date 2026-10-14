"""Source-group metrics and bounded context, without gold-based reranking."""
from .dataset import Encode


GRADING_VERSION = 1


def Fit(evidence, budget):
    selected = []
    for record in evidence:
        if len(Encode(selected + [record])) <= budget:
            selected.append(dict(record))
            continue
        low, high = 0, len(record["text"])
        while low < high:
            middle = (low + high + 1) // 2
            clipped = record | {"text": record["text"][:middle], "truncated": True}
            if len(Encode(selected + [clipped])) <= budget:
                low = middle
            else:
                high = middle - 1
        if low:
            selected.append(record | {"text": record["text"][:low], "truncated": True})
        break
    return selected


def Coverage(groups, records, full=False):
    if not groups:
        return None
    ids = {i for r in records if not r.get("unmapped") and (not full or not r.get("truncated")) for i in r["item_ids"]}
    return sum(bool(set(group) & ids) for group in groups) / len(groups)


def Grade(result, gold):
    eligible = (result["status"] == "completed" or result.get("failed_stage") == "answer") and result["backend"] not in {"none", "oracle"}
    candidate = Coverage(gold["evidence_groups"], result["candidates"]) if eligible else None
    context = Coverage(gold["evidence_groups"], result["context"], full=True) if eligible else None
    return {"case_id": result["case_id"], "question_id": result["question_id"], "repeat": result["repeat"],
            "variant": result["variant"], "candidate_coverage": candidate, "context_coverage": context,
            "candidate_complete": None if candidate is None else candidate == 1,
            "context_complete": None if context is None else context == 1,
            "abstain": gold["abstain"], "answer_status": "ungraded", "reason": None}


def Key(row):
    return row["case_id"], row["question_id"], row["repeat"], row["variant"]
