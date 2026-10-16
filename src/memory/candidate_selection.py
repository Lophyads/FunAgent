"""Pure, deterministic selection of memory records before context budgeting."""
import re

from .common import RecordIdentity
from .contracts import MemoryRecord


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
