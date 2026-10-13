"""Credential redaction and bounded, JSON-compatible payload snapshots."""
from dataclasses import dataclass
from collections.abc import Mapping
import json
from math import isfinite
import re

from ..frame.contracts import freeze, thaw


_SENSITIVE = re.compile(r'(?:password|passwd|secret|credential|authorization|cookie|api[_-]?key|(?:access|refresh|auth|session|bearer)[_-]?token|^token$|^headers$|^env$)', re.I)
_KEY = r'(?:[\w.-]*[_-])?(?:password|passwd|secret|credential|api[_-]?key|access[_-]?token|refresh[_-]?token|token)'
_ASSIGNMENT = re.compile(rf'''(?i)(["']?\b{_KEY}["']?\s*[:=]\s*["']?)([^\s,;"'}}]+)''')
_QUOTED = re.compile(rf'''(?is)(["']?\b{_KEY}["']?\s*[:=]\s*)(["'])(.*?)(?:\2|$)''')
_AUTH = re.compile(r'''(?i)(\bauthorization["']?\s*[:=]\s*["']?)(?:Basic|Bearer)\s+[^\s,;"']+''')
_BEARER = re.compile(r'(?i)\bBearer\s+[^\s,;"\']+')
_USERINFO = re.compile(r'(https?://)[^/\s:@]+:[^/\s@]+@', re.I)


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf8'))


def _original_size(value, limit):
    """Exact size when a bounded traversal can measure it; unknown for unvisited tails."""
    visits = 0
    def measure(item, depth=0):
        nonlocal visits
        visits += 1
        if visits > 2048 or depth >= 16:
            raise ValueError
        if isinstance(item, str):
            if len(item) > limit:
                raise ValueError
            size = _bytes(item)
        elif isinstance(item, Mapping):
            if len(item) > 128:
                raise ValueError
            size = 2
            for index, (key, child) in enumerate(item.items()):
                if not isinstance(key, str):
                    raise ValueError
                size += measure(key, depth + 1) + 2 + measure(child, depth + 1) + (2 if index else 0)
                if size > limit:
                    raise ValueError
        elif isinstance(item, (list, tuple)):
            if len(item) > 128:
                raise ValueError
            size = 2
            for index, child in enumerate(item):
                size += measure(child, depth + 1) + (2 if index else 0)
                if size > limit:
                    raise ValueError
        elif item is None or type(item) in (bool, int, float):
            if type(item) is int and item.bit_length() > 256:
                raise ValueError
            size = _bytes(item)
        else:
            raise ValueError
        if size > limit:
            raise ValueError
        return size
    try:
        return measure(value)
    except (ValueError, TypeError, RecursionError):
        return None


@dataclass(frozen=True)
class PayloadSnapshot:
    data: object
    original_bytes: int | None = None
    captured_bytes: int = 0
    truncated: bool = False
    omitted_reason: str | None = None

    def Wire(self):
        return dict(data=thaw(self.data), original_bytes=self.original_bytes,
                    captured_bytes=self.captured_bytes, truncated=self.truncated,
                    omitted_reason=self.omitted_reason)

    def Omit(self, reason):
        return PayloadSnapshot(None, self.original_bytes, 0, True, reason)


class Redactor:
    def __init__(self, max_bytes, redact_text=None):
        self.max_bytes = max_bytes
        self.redact_text = redact_text
        redaction_owner = getattr(redact_text, '__self__', None)
        self._lookahead = getattr(redaction_owner, 'RedactionLookahead', lambda: 4096)()

    def Snapshot(self, value):
        original = _original_size(value, self.max_bytes)
        if self._lookahead > 65536:
            return PayloadSnapshot(None, original, 0, True, 'redaction_window_limit')
        remaining = self.max_bytes
        reasons = set()
        visits = 0

        def redact(text):
            text = _QUOTED.sub(r'\1\2[REDACTED]\2', text)
            text = _AUTH.sub(r'\1[REDACTED]', text)
            text = _BEARER.sub('Bearer [REDACTED]', text)
            text = _ASSIGNMENT.sub(r'\1[REDACTED]', text)
            text = _USERINFO.sub(r'\1[REDACTED]@', text)
            return self.redact_text(text) if self.redact_text else text

        def visit(item, depth=0):
            nonlocal remaining, visits
            visits += 1
            if depth >= 16:
                reasons.add('depth_limit')
                return None
            if visits > 2048 or remaining < 8:
                reasons.add('payload_bytes')
                return None
            if isinstance(item, str):
                # Include configured credentials crossing the capture boundary,
                # redact them completely, then apply the output byte limit.
                prefix = item[:max(0, remaining) + self._lookahead]
                if len(prefix) != len(item):
                    reasons.add('payload_bytes')
                if prefix.lstrip().startswith(('{', '[')) and len(prefix) == len(item):
                    try:
                        decoded = json.loads(prefix)
                    except (ValueError, RecursionError):
                        pass
                    else:
                        budget = remaining
                        prefix = json.dumps(visit(decoded, depth + 1), ensure_ascii=False)
                        remaining = budget
                prefix = redact(prefix)
                encoded = json.dumps(prefix, ensure_ascii=False).encode('utf8')
                if len(encoded) > remaining:
                    reasons.add('payload_bytes')
                    prefix = prefix.encode('utf8')[:max(0, remaining - 2)].decode('utf8', errors='ignore')
                    while prefix and _bytes(prefix) > remaining:
                        prefix = prefix[:len(prefix) // 2]
                remaining -= _bytes(prefix)
                return prefix
            if isinstance(item, Mapping):
                result = {}
                remaining -= 2
                for index, (key, child) in enumerate(item.items()):
                    if index >= 128 or remaining < 16:
                        reasons.add('collection_limit' if index >= 128 else 'payload_bytes')
                        break
                    if not isinstance(key, str):
                        reasons.add('unsupported_key')
                        continue
                    if len(key) > 256:
                        reasons.add('key_length_limit')
                        result[redact(key[:256 + self._lookahead])[:256]] = None
                        continue
                    key = redact(key)
                    remaining -= _bytes(key) + 3
                    result[key] = visit('[REDACTED]' if _SENSITIVE.search(key) else child, depth + 1)
                return result
            if isinstance(item, (list, tuple)):
                remaining -= 2
                result = []
                for index, child in enumerate(item):
                    if index >= 128 or remaining < 8:
                        reasons.add('collection_limit' if index >= 128 else 'payload_bytes')
                        break
                    remaining -= 2
                    result.append(visit(child, depth + 1))
                return result
            if item is None or type(item) in (bool, int, float):
                if type(item) is float and not isfinite(item):
                    reasons.add('non_json_value')
                    return None
                if type(item) is int and item.bit_length() > 256:
                    reasons.add('non_json_value')
                    return None
                remaining -= _bytes(item)
                return item
            reasons.add('unsupported_type')
            return None

        data = visit(value)
        size = _bytes(data)
        if size > self.max_bytes:
            data, size = None, 0
            reasons.add('payload_bytes')
        return PayloadSnapshot(freeze(data), original, size, bool(reasons), ','.join(sorted(reasons)) or None)
