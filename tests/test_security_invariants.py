"""Security invariants checked against the source itself.

Some properties cannot be observed from behaviour. A secret compared with `==`
returns exactly the same answers as one compared with `hmac.compare_digest` --
the difference is *how long it takes to say no*, which leaks the secret one
character at a time to anyone who can measure it.

Unit tests cannot see that. These read the source and assert the safe
construct is present, which is how security-critical code gets guarded in
practice.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _source(relative: str) -> str:
    return (ROOT / relative).read_text()


def test_node_secrets_are_compared_in_constant_time() -> None:
    source = _source("app/nodes/store.py")
    assert "hmac.compare_digest" in source, (
        "Node secrets must be compared with hmac.compare_digest. A plain == "
        "returns early on the first differing byte, which leaks the secret "
        "through response timing."
    )


def test_passcode_is_compared_in_constant_time() -> None:
    source = _source("app/core/security.py")
    assert "hmac.compare_digest" in source, (
        "The passcode must be compared with hmac.compare_digest, not ==."
    )


def test_secrets_are_never_stored_in_clear() -> None:
    source = _source("app/nodes/store.py")
    assert "secret_hash" in source
    assert "_hash(secret)" in source, "Node secrets must be hashed before storage."


def test_node_agent_resolves_paths_before_checking_them() -> None:
    """`../` must be collapsed *before* the jail check, or it escapes."""
    source = _source("nodes/pc/agent.py")
    resolve_at = source.find(".resolve()")
    check_at = source.find("path outside the allowed roots")
    assert resolve_at != -1, "the node agent must resolve paths"
    assert resolve_at < check_at, (
        "Paths must be resolved before the allowed-roots check, otherwise "
        "'~/workspace/../../etc/passwd' passes a prefix test and escapes."
    )
