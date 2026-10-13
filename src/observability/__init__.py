"""Observation access; importing this package starts no resources."""
from .observation_mgr import ObservationMgr
from .protocols import ObservationAccess

__all__ = ["ObservationMgr", "ObservationAccess"]
