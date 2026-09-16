"""Cloud-side tools.

Importing this package registers every tool with the global registry. Agent
definitions are validated against that registry at startup, so this import must
happen before `agent_registry.load()`.
"""

from app.tools.cloud import memory, web  # noqa: F401

__all__ = ["memory", "web"]
