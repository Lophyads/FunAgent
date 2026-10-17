"""Response validation independent of SDK and of the consuming application."""
from math import isfinite


class EmbeddingValidationError(RuntimeError):
    def __init__(self, code='EMBEDDING_PROTOCOL', *, expected=None, actual=None):
        self.issue = {'code': code, 'stage': 'embedding'}
        if expected is not None:
            self.issue.update(expected_dimensions=expected, actual_dimensions=actual)
        super().__init__(code)


class EmbeddingSourceError(RuntimeError):
    """Provider failure; its message is never part of the public diagnostic."""


def validate_vector(vector, dimensions):
    if not isinstance(vector, (list, tuple)):
        raise EmbeddingValidationError()
    if dimensions is not None and len(vector) != dimensions:
        raise EmbeddingValidationError('EMBEDDING_DIMENSION_MISMATCH', expected=dimensions, actual=len(vector))
    if (not vector or any(type(v) not in (int, float) or not isfinite(v) for v in vector) or not any(vector)):
        raise EmbeddingValidationError()
    return [float(v) for v in vector]


def decode_batch(rows, count, dimensions):
    try:
        if not isinstance(rows, (list, tuple)) or len(rows) != count:
            raise EmbeddingValidationError()
        indexes = [row.index for row in rows]
        if any(type(i) is not int for i in indexes) or sorted(indexes) != list(range(count)):
            raise EmbeddingValidationError()
        vectors = [validate_vector(row.embedding, dimensions) for row in sorted(rows, key=lambda r: r.index)]
        if vectors and any(len(v) != len(vectors[0]) for v in vectors):
            raise EmbeddingValidationError()
        return vectors
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise EmbeddingValidationError() from None


def error_issue(error, stage='search'):
    """Only our own validation errors carry machine diagnostics across wrappers."""
    for _ in range(8):
        if isinstance(error, EmbeddingValidationError):
            return dict(error.issue)
        if isinstance(error, EmbeddingSourceError):
            return {'code': 'EMBEDDING_SOURCE', 'stage': 'embedding'}
        error = getattr(error, '__cause__', None)
        if error is None:
            break
    return {'code': 'MEMORY_SOURCE', 'stage': stage}
