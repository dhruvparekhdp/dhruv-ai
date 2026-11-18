"""Agency domain.

A separate surface from the orchestrator: its own tables (prefixed ``agy_``),
its own migrations, and an API mounted at ``/api/agency``. Jarvis missions,
agents and tools do not share these tables.

This package records brands, clients and the work planned for them. It does
not publish content, send messages, or change a campaign on any external
network. Status fields are bookkeeping for the owner.
"""
