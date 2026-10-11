"""Stream real memory pages into an exclusively created JSONL artifact."""
from pathlib import Path
import json
import os
from ..frame.contracts import DomainError
from ..memory.memory_operations_mgr import MemoryOperationsMgr
from ..memory.contracts import MemoryListQuery


def Export(source_id, filename):
    target = Path(filename).expanduser().absolute()
    operations = MemoryOperationsMgr.GetInstance()
    page = operations.List(MemoryListQuery(source_id, limit=100))
    try:
        stream = target.open("x", encoding="utf8", newline="\n")
    except FileExistsError:
        raise DomainError("CONFLICT", "Export target already exists") from None
    except OSError:
        raise DomainError("EXPORT_FAILED", "Export target could not be created") from None
    identity = os.fstat(stream.fileno())
    count = 0
    try:
        with stream:
            while True:
                for record in page["records"]:
                    stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                    count += 1
                cursor = page["next_cursor"]
                if cursor is None:
                    break
                page = operations.List(MemoryListQuery(source_id, limit=100, cursor=cursor))
    except BaseException as error:
        try:
            actual = target.stat()
            if (identity.st_dev, identity.st_ino) == (actual.st_dev, actual.st_ino):
                target.unlink()
        except OSError:
            pass
        if isinstance(error, (DomainError, KeyboardInterrupt, SystemExit)):
            raise
        raise DomainError("EXPORT_FAILED", "Memory export failed") from None
    return {"path": str(target), "records": count, "format": "jsonl"}
