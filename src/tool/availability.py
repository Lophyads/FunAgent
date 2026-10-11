"""One backend availability validator for startup, dispatch, and diagnostics."""
from ..frame.contracts import DomainError
from .contracts import Availability


def Probe(backend):
    try:
        probe = getattr(backend, "Probe", None)
        availability = probe() if callable(probe) else Availability(
            getattr(backend, "available", True), getattr(backend, "capabilities", ()))
        if (not isinstance(availability, Availability) or type(availability.available) is not bool
                or any(not isinstance(capability, str) or not capability for capability in availability.capabilities)):
            raise ValueError
        return availability
    except Exception:
        raise DomainError("CONFIG_INVALID", "Invalid tool backend availability") from None


