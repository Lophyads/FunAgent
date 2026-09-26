"""Gateway admission and optional reliable delivery."""

from .delivery import DeliveryAdapter, DeliveryBatch, DeliveryRuntime

__all__ = ["DeliveryAdapter", "DeliveryBatch", "DeliveryRuntime", "GatewayRuntime"]


def __getattr__(name: str):
    if name == "GatewayRuntime":
        from .gateway import GatewayRuntime
        return GatewayRuntime
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
