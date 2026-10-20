"""Pure, deterministic selection of memory records before context budgeting."""
import re

from .common import RecordIdentity
from .contracts import MemoryRecord


def SelectByTopic(records, snapshots, allowed):
    """Use stored classification only; missing or stale assignments never pass."""
    selected, outside, unclassified = [], [], []
    for record in records:
        source = record.ref.source_id
        snapshot = snapshots.get(source)
        topic = snapshot.assignments.get(record.ref.record_id) if snapshot is not None else None
        if snapshot is None or topic not in snapshot.topics:
            unclassified.append(record)
        elif topic in allowed.get(source, ()):
            selected.append(record)
        else:
            outside.append(record)
    return tuple(selected), tuple(outside), tuple(unclassified)


def AutoCutCount(scores, cut_off):
    """Cut a descending score curve at its Nth normalized local maximum.

    Adapted from Weaviate entities/autocut (BSD-3-Clause; see autocut.LICENSE.txt).
    https://github.com/weaviate/weaviate/blob/main/entities/autocut/autocut.go
    Scores are already sorted and validated by the caller. A tiny comparison
    tolerance prevents roundoff on a straight line from fabricating an elbow.
    """
    count = len(scores)
    if cut_off <= 0 or count <= 2 or scores[0] == scores[-1]:
        return count
    span = scores[-1] - scores[0]
    differences = [(score - scores[0]) / span - index / (count - 1)
                   for index, score in enumerate(scores)]
    extrema = 0
    for index in range(1, count):
        following = differences[index + 1] if index + 1 < count else differences[index - 2]
        if (differences[index] - differences[index - 1] > 1e-12 and
                differences[index] - following > 1e-12):
            extrema += 1
            if extrema >= cut_off:
                return index
    return count


def _ImportIdentity(record: MemoryRecord) -> tuple | None:
    if (record.truncated or record.origin is None or
            re.fullmatch(r"import:[0-9a-fA-F]{64}", record.origin) is None):
        return None
    # Cross-source copies may differ in reference and version, but not in body or semantics.
    return (record.origin, *RecordIdentity(record)[2:])


def DeduplicateRecords(records: tuple[MemoryRecord, ...]) -> tuple[
    tuple[MemoryRecord, ...], tuple[tuple[MemoryRecord, ...], ...],
]:
    """Return representatives in input order and groups of original duplicate objects.

    Exact identities share a group. Import groups accept at most one distinct
    identity per source, preserving same-source version and content conflicts.
    """
    groups: list[list[MemoryRecord]] = []
    identities: dict[tuple, int] = {}
    imports: dict[tuple, tuple[list[int], dict[str, int]]] = {}
    for record in records:
        identity = RecordIdentity(record)
        if identity not in identities:
            index = len(groups)
            import_identity = _ImportIdentity(record)
            if import_identity is not None:
                slots, source_counts = imports.setdefault(import_identity, ([], {}))
                source = record.ref.source_id
                position = source_counts.get(source, 0)
                # Each slot accepts only one distinct identity from a given source.
                # This prevents an imported copy from bridging same-source versions.
                if position == len(slots):
                    slots.append(index)
                else:
                    index = slots[position]
                source_counts[source] = position + 1
            identities[identity] = index
            if index == len(groups):
                groups.append([])
        groups[identities[identity]].append(record)
    return (tuple(group[0] for group in groups),
            tuple(tuple(group) for group in groups if len(group) > 1))


def SelectByScore(
    records: tuple[MemoryRecord, ...], scores: tuple[float, ...], min_score: float | None = None,
) -> tuple[tuple[MemoryRecord, ...], tuple[MemoryRecord, ...]]:
    """Stably sort model-validated scores, keeping scores >= the optional threshold.

    No threshold means sorting only. Incomplete score alignment is a call error;
    score values and configured thresholds are validated by their owning layers.
    """
    if len(records) != len(scores):
        raise ValueError("Each record requires exactly one score")
    ranked = sorted(zip(records, scores), key=lambda item: item[1], reverse=True)
    selected, rejected = [], []
    for record, score in ranked:
        (selected if min_score is None or score >= min_score else rejected).append(record)
    return tuple(selected), tuple(rejected)
