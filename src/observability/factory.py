"""Application-only assembly using LogSession's already resolved file paths."""
from pathlib import Path

from .hub import ObservabilityHub
from .json_store import JsonStore
from .observation_mgr import ObservationMgr


def Create(conf, logs, conf_dir):
    files = logs.Files()
    path = files.get('normal', files.get('error'))
    logs_dir = Path(path).parent if path is not None else Path(conf_dir).resolve() / 'logs'
    store = JsonStore(logs_dir.parent / 'trace_json')
    hub = ObservabilityHub(store, redact_text=logs.Redact, **conf.Log().Observability())
    try:
        return ObservationMgr(hub)
    except Exception:
        hub.Close()
        raise
