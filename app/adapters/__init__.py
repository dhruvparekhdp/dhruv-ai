"""Typed HTTP adapters for external feeds.

Every integration uses `AdapterClient`. Agents receive an `AdapterResponse`
and never an exception from a timeout, a 5xx, or an open circuit.

Credentials are read from the environment (or an injected secret source) at
call time and are not written to logs.
"""

from app.adapters.client import AdapterClient
from app.adapters.models import AdapterResponse, Outcome
from app.adapters.registry import registry

__all__ = ["AdapterClient", "AdapterResponse", "Outcome", "registry"]
