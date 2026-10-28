"""Reporting devices: phones and anything else that tells Jarvis about itself.

Separate from `app/nodes/` on purpose. A node holds a WebSocket and executes
work. An iPhone can do neither — iOS suspends background sockets, and it can
never execute on Jarvis's behalf (D-013). It can only report, in short bursts,
when the app or a Shortcuts automation runs. Different shape, different module.
"""
