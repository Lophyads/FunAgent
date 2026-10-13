"""Local API producer measurement; no machine-dependent pass/fail thresholds."""
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from .hub import ObservabilityHub
from .json_store import JsonStore
from .observation_mgr import ObservationMgr
from .protocols import NullObservation


def _produce(manager, count):
    started = perf_counter()
    manager.StartTask('benchmark', input={'text': '基准输入'})
    with manager.UseTask('benchmark'):
        for _ in range(count):
            with manager.Scope('model.attempt', kind='generation', purpose='main', input={'text': '中文' * 128}):
                manager.Update(output={'content': '回复' * 128}, usage={'total_tokens': 120})
                manager.Complete(status='completed')
        manager.EndTask(status='completed')
    return (perf_counter() - started) * 1000


def Measure(count=200):
    baseline = _produce(NullObservation(), count)
    with TemporaryDirectory(prefix='funagent-observation-benchmark-') as temporary:
        hub = ObservabilityHub(JsonStore(Path(temporary)), queue_capacity=2048)
        manager = ObservationMgr(hub)
        try:
            observed = _produce(manager, count)
        finally:
            manager.CloseResource()
        health = hub.Health()
    return dict(calls=count, baseline_producer_ms=baseline, observed_producer_ms=observed,
                producer_overhead_us_per_call=(observed - baseline) * 1000 / count,
                writer_serialization_ms=health['last_serialization_ms'], writer_flush_ms=health['last_flush_ms'],
                peak_queue=health['peak_queue'], peak_bytes=health['peak_bytes'],
                max_persistence_lag_ms=health['max_persistence_lag_ms'], dropped_events=health['dropped'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--calls', type=int, default=200)
    args = parser.parse_args(argv)
    if not 1 <= args.calls <= 500:
        parser.error('--calls must be between 1 and 500')
    print(json.dumps(Measure(args.calls), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
