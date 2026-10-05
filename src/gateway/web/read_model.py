"""Resource-specific, read-only views; reads never attach sessions or probe backends."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

TABLES = ("sessions", "messages", "session_leases", "delivery_obligations")


class WebError(ValueError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def integer(value, name, low=0, high=2**63 - 1):
    try:
        if isinstance(value, bool):
            raise ValueError()
        result = int(value)
        if str(result) != str(value) or not low <= result <= high:
            raise ValueError()
        return result
    except (ValueError, TypeError, OverflowError):
        raise WebError(422, "invalid_input", f"invalid {name}") from None


def sanitized(value, key=""):
    """Only configuration DTOs use this filter; chat content belongs to the local user."""
    name = key.lower()
    if name in {"env", "headers"} and isinstance(value, dict):
        return {k: "[redacted]" for k in value}
    if name in {"args", "system_prompt", "credential_ref"} or any(
        fragment in name for fragment in ("api_key", "password", "secret", "authorization", "token")
    ):
        return "[redacted]"
    if isinstance(value, dict):
        return {k: sanitized(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitized(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str) and (name.endswith("url") or name == "endpoint"):
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    return value


class ReadModel:
    def __init__(self, config, services, registry, memory_settings=None):
        self.config, self.services, self.registry = config, services, registry
        self.memory_settings = memory_settings
        self.database = Path(config.database_path).resolve()

    @contextmanager
    def connection(self):
        try:
            db = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=2)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                yield db
            finally:
                db.close()
        except sqlite3.Error:
            raise WebError(503, "database_unavailable", "database read unavailable") from None

    @staticmethod
    def _columns(db, table):
        if table not in TABLES:
            raise WebError(404, "not_found", "unknown table")
        return [dict(row) for row in db.execute(f'PRAGMA table_info("{table}")')]

    def schema(self):
        with self.connection() as db:
            return {"tables": [{"name": table, "columns": self._columns(db, table)} for table in TABLES]}

    def sessions(self):
        with self.connection() as db:
            rows = db.execute("""SELECT s.id, s.workspace, s.created_at, s.updated_at, s.resume_pending,
                (SELECT substr(m.content,1,80) FROM messages m WHERE m.session_id=s.id AND m.role='user'
                 ORDER BY m.id LIMIT 1) AS title
                FROM sessions s ORDER BY s.updated_at DESC,s.id LIMIT 500""")
            return {"sessions": [dict(row) for row in rows]}

    def messages(self, sid, query):
        limit = integer(query.get("limit", "50"), "limit", 1, 200)
        before, after = query.get("before"), query.get("after")
        if before is not None and after is not None:
            raise WebError(422, "invalid_input", "choose before or after")
        where, params = "session_id=?", [sid]
        if before is not None or after is not None:
            cursor = integer(before if before is not None else after, "cursor")
            where += " AND id" + ("<?" if before is not None else ">?")
            params.append(cursor)
        order = "ASC" if after is not None else "DESC"
        with self.connection() as db:
            rows = [dict(row) for row in db.execute(
                f"SELECT * FROM messages WHERE {where} ORDER BY id {order} LIMIT ?", (*params, limit + 1))]
        more = len(rows) > limit
        rows = rows[:limit]
        rows.sort(key=lambda row: row["id"])
        for row in rows:
            for source, target in (("metadata_json", "metadata"), ("tool_calls_json", "tool_calls")):
                try:
                    row[target] = json.loads(row.pop(source) or ("{}" if target == "metadata" else "[]"))
                except (ValueError, TypeError):
                    row[target] = {} if target == "metadata" else []
        return {"session_id": sid, "messages": rows, "has_more": more,
                "before": rows[0]["id"] if rows else None, "after": rows[-1]["id"] if rows else None}

    @staticmethod
    def _select(columns, cap):
        # Preview in SQL so a large cell never enters Python unbounded.
        selections = []
        for col in columns:
            name = col["name"]
            selections.extend((f'CASE WHEN typeof("{name}") IN (\'text\',\'blob\') '
                               f'THEN substr("{name}",1,{cap}) ELSE "{name}" END AS "{name}"',
                               f'length("{name}") AS "__len_{name}"'))
        return ",".join(selections)

    @staticmethod
    def _row(row, columns, cap):
        values, truncated, blobs = {}, [], []
        for col in columns:
            name = col["name"]
            value = row[name]
            if isinstance(value, bytes):
                blobs.append(name)
                try:
                    value = value.decode("utf-8")
                except UnicodeDecodeError:
                    value = value.hex()
            if isinstance(value, str) and cap == 65536 and len(value.encode("utf-8")) > cap:
                value = value.encode("utf-8")[:cap].decode("utf-8", errors="ignore")
                truncated.append(name)
            if isinstance(value, str) and len(value) > cap:
                value = value[:cap]
                if name not in truncated:
                    truncated.append(name)
            values[name] = value
            if row[f"__len_{name}"] is not None and row[f"__len_{name}"] > cap and name not in truncated:
                truncated.append(name)
        return {"values": values, "truncated": truncated, "blobs": blobs,
                "pk": {col["name"]: values[col["name"]] for col in columns if col["pk"]}}

    def table(self, table, query, *, detail=False):
        with self.connection() as db:
            columns = self._columns(db, table)
            allowed = {col["name"] for col in columns}
            primary = [col["name"] for col in columns if col["pk"]]
            where, params = [], []
            if detail:
                if not all(key in query for key in primary):
                    raise WebError(422, "invalid_input", "row primary key required")
                for key in primary:
                    where.append(f'"{key}"=?')
                    params.append(query[key])
            elif query.get("column"):
                column, op = query["column"], query.get("op", "eq")
                if column not in allowed or op not in {"eq", "contains", "is_null"}:
                    raise WebError(422, "invalid_input", "invalid filter")
                if op == "is_null":
                    where.append(f'"{column}" IS NULL')
                elif op == "eq":
                    where.append(f'"{column}"=?')
                    params.append(query.get("value", ""))
                else:
                    where.append(f'CAST("{column}" AS TEXT) LIKE ? ESCAPE \'\\\'')
                    escaped = query.get("value", "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    params.append(f"%{escaped}%")
            sort = query.get("sort", primary[0])
            direction = query.get("direction", "asc").lower()
            if sort not in allowed or direction not in {"asc", "desc"}:
                raise WebError(422, "invalid_input", "invalid sort")
            clause = " WHERE " + " AND ".join(where) if where else ""
            cap = 65536 if detail else 512
            limit = 1 if detail else integer(query.get("limit", "50"), "limit", 1, 200)
            offset = 0 if detail else integer(query.get("offset", "0"), "offset", 0, 10_000_000)
            order = f'"{sort}" {direction}' + "".join(f',"{key}" {direction}' for key in primary if key != sort)
            total = db.execute(f'SELECT count(*) FROM "{table}"{clause}', params).fetchone()[0]
            rows = [self._row(row, columns, cap) for row in db.execute(
                f'SELECT {self._select(columns, cap)} FROM "{table}"{clause} ORDER BY {order} LIMIT ? OFFSET ?',
                (*params, limit, offset))]
        if detail:
            if not rows:
                raise WebError(404, "not_found", "row not found")
            return rows[0]
        return {"table": table, "columns": columns, "rows": rows, "total": total, "limit": limit, "offset": offset}

    def catalog(self, name):
        if name == "tools":
            return {"tools": [{"name": e.name, "tool_id": e.tool_id, "toolset": e.toolset,
                "implementation_version": e.implementation_version, "description": e.description,
                "parameters": e.args_model.model_json_schema(), "requires_approval": e.requires_approval,
                "required_permissions": sorted(e.required_permissions),
                "required_capabilities": sorted(e.required_capabilities), "side_effects": e.side_effects}
                for e in self.registry.entries()]}
        if name == "connections":
            return sanitized(self.services.status_snapshot())
        if name == "models":
            return sanitized({"routes": [route.model_dump(mode="json") for route in self.config.routes]})
        if name == "config":
            result = sanitized(self.config.model_dump(mode="json"))
            if self.memory_settings is not None:
                from dataclasses import asdict
                result["memory"] = sanitized(asdict(self.memory_settings))
            return result
        raise WebError(404, "not_found", "unknown resource")
