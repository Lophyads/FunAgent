"""Bounded read-only projections of the seven application tables."""
import re

from ..frame.contracts import DomainError

TABLES = ("sessions", "tasks", "messages", "tool_calls", "review_watermarks",
          "review_observed_turns", "review_triggers")


def _columns(db, table):
    if table not in TABLES:
        raise DomainError("NOT_FOUND", "Table not found")
    return [dict(row) for row in db.execute(f'PRAGMA table_info("{table}")')]


def Schema(db):
    return {"tables": [{"name": table, "columns": _columns(db, table)} for table in TABLES]}


def _integer(value, low, high):
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,9}", value):
        raise DomainError("INVALID_REQUEST", "Invalid data pagination")
    number = int(value)
    if not low <= number <= high:
        raise DomainError("INVALID_REQUEST", "Invalid data pagination")
    return number


def Read(db, table, query, *, detail=False):
    columns = _columns(db, table)
    names = {c["name"] for c in columns}
    primary = [c["name"] for c in sorted(columns, key=lambda c: c["pk"]) if c["pk"]]
    permitted = set(primary) if detail else {"limit", "offset", "sort", "direction", "column", "op", "value"}
    if query.keys() - permitted or any(not isinstance(v, str) or len(v) > 4096 for v in query.values()):
        raise DomainError("INVALID_REQUEST", "Invalid table query")
    # Project hidden protocol state before filtering, sorting, measuring or clipping.
    # A projected subquery also prevents filter/count side channels over replay.
    source = f'"{table}"'
    if table == "messages":
        projected = ["json_remove(payload, '$.replay') AS payload" if c["name"] == "payload" else f'"{c["name"]}"' for c in columns]
        source = '(SELECT ' + ','.join(projected) + ' FROM messages)'
    conditions, parameters = [], []
    if detail:
        if set(query) != set(primary):
            raise DomainError("INVALID_REQUEST", "Complete row primary key required")
        for key in primary:
            conditions.append(f'"{key}"=?')
            parameters.append(query[key])
    elif query.get("column"):
        column, operation = query["column"], query.get("op", "eq")
        if column not in names or operation not in {"eq", "contains", "is_null"}:
            raise DomainError("INVALID_REQUEST", "Invalid data filter")
        if operation == "is_null":
            conditions.append(f'"{column}" IS NULL')
        elif operation == "eq":
            conditions.append(f'"{column}"=?')
            parameters.append(query.get("value", ""))
        else:
            value = query.get("value", "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append(f'CAST("{column}" AS TEXT) LIKE ? ESCAPE \'\\\'')
            parameters.append(f"%{value}%")
    elif "op" in query or "value" in query:
        raise DomainError("INVALID_REQUEST", "Filter column required")
    sort, direction = query.get("sort", primary[0]), query.get("direction", "asc")
    if sort not in names or direction not in {"asc", "desc"}:
        raise DomainError("INVALID_REQUEST", "Invalid data sorting")
    limit = 1 if detail else _integer(query.get("limit", "50"), 1, 200)
    offset = 0 if detail else _integer(query.get("offset", "0"), 0, 10_000_000)
    cap = 65536 if detail else 512
    selections = []
    for name in (c["name"] for c in columns):
        selections.extend((f'CASE WHEN typeof("{name}") IN (\'text\',\'blob\') THEN substr("{name}",1,{cap}) '
                           f'ELSE "{name}" END AS "{name}"', f'length("{name}") AS "__len_{name}"'))
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    order = f'"{sort}" {direction}' + ''.join(f',"{key}" {direction}' for key in primary if key != sort)
    total = db.execute(f'SELECT count(*) FROM {source}{where}', parameters).fetchone()[0]
    rows = []
    for row in db.execute(f'SELECT {",".join(selections)} FROM {source}{where} ORDER BY {order} LIMIT ? OFFSET ?',
                          (*parameters, limit, offset)):
        values, truncated, blobs = {}, [], []
        remaining = cap
        for column in columns:
            name, value = column["name"], row[column["name"]]
            if isinstance(value, bytes):
                blobs.append(name)
                value = value.decode("utf8", errors="replace")
            if isinstance(value, str):
                clipped = value.encode("utf8")[:remaining].decode("utf8", errors="ignore") if detail else value[:cap]
                if clipped != value or (row[f"__len_{name}"] or 0) > cap:
                    truncated.append(name)
                value = clipped
                if detail:
                    remaining -= len(value.encode('utf8'))
            values[name] = value
        rows.append({"values": values, "truncated": truncated, "blobs": blobs,
                     "pk": {key: row[key] for key in primary}})
    if detail:
        if not rows:
            raise DomainError("NOT_FOUND", "Row not found")
        return rows[0]
    return {"table": table, "columns": columns, "rows": rows, "total": total, "limit": limit, "offset": offset}
