"""Telemetry engine tests — the fine-tuning flywheel.

Covers what training data actually depends on: turns are recorded with their
system-prompt snapshot, rewards attach to the right turn, and the ShareGPT
export is shaped the way Unsloth/Axolotl expect.
"""

from __future__ import annotations

import json

import pytest


def _log_turn(telemetry, session_id: str, **overrides):
    kwargs = dict(
        session_id=session_id,
        intent_category="PERSONAL_CHAT",
        user_prompt="hi",
        system_prompt="You are Jarvis.",
        model_provider="ECHO",
        model_name="jarvis-local-echo",
        agent_reasoning="Routed to local fallback.",
        raw_llm_response="Hey, good to see you.",
        prompt_tokens=11,
        completion_tokens=7,
        latency_ms=42,
    )
    kwargs.update(overrides)
    return telemetry.log_trajectory(**kwargs)


def test_session_roundtrip(telemetry) -> None:
    session_id = telemetry.start_session(client_type="CLI", user_agent="pytest")
    assert session_id.startswith("sess_")
    assert telemetry.session_exists(session_id)
    assert not telemetry.session_exists("sess_does-not-exist")


def test_trajectory_is_logged_with_full_metadata(telemetry) -> None:
    session_id = telemetry.start_session()
    trajectory_id = _log_turn(telemetry, session_id)

    assert trajectory_id.startswith("traj_")
    stats = telemetry.stats()
    assert stats["turns_logged"] == 1
    assert stats["total_tokens"] == 18
    assert stats["avg_latency_ms"] == 42.0
    assert stats["training_examples_ready"] == 1


def test_failed_turns_are_still_logged(telemetry) -> None:
    """A failed turn is training signal too — it must not vanish."""
    session_id = telemetry.start_session()
    _log_turn(
        telemetry, session_id,
        raw_llm_response="", execution_success=False, error_message="upstream 503",
    )
    assert telemetry.stats()["turns_logged"] == 1


def test_feedback_updates_reward_and_reports_unknown_ids(telemetry) -> None:
    session_id = telemetry.start_session()
    trajectory_id = _log_turn(telemetry, session_id)

    assert telemetry.record_feedback(trajectory_id, 1) is True
    assert telemetry.record_feedback("traj_nope", 1) is False

    with pytest.raises(ValueError):
        telemetry.record_feedback(trajectory_id, 5)


def test_negative_reward_excludes_turn_from_training_export(telemetry) -> None:
    session_id = telemetry.start_session()
    good = _log_turn(telemetry, session_id)
    bad = _log_turn(telemetry, session_id, raw_llm_response="unhelpful answer")

    telemetry.record_feedback(good, 1)
    telemetry.record_feedback(bad, -1)

    dataset = telemetry.build_sharegpt_dataset()
    exported_ids = {record["id"] for record in dataset}
    assert good in exported_ids
    assert bad not in exported_ids
    assert telemetry.stats()["training_examples_ready"] == 1


def test_mood_checkin_and_history(telemetry) -> None:
    session_id = telemetry.start_session()
    for score, label in ((2, "low"), (4, "good"), (5, "great")):
        telemetry.log_mood(session_id=session_id, mood_score=score, mood_label=label, note=f"note {score}")

    history = telemetry.mood_history(limit=10)
    assert len(history) == 3
    assert {entry["mood_score"] for entry in history} == {2, 4, 5}
    assert telemetry.stats()["mood_checkins"] == 3


def test_mood_history_is_newest_first_within_the_same_second(telemetry) -> None:
    """Regression: check-ins made inside one second must still order correctly.

    CURRENT_TIMESTAMP is second-granular, so a burst of check-ins used to tie
    and fall through to ordering by random UUID — which silently reversed the
    trend chart. Timestamps are now application-generated with microseconds.
    """
    session_id = telemetry.start_session()
    for score in (1, 2, 3, 4, 5):        # written well within one second
        telemetry.log_mood(session_id=session_id, mood_score=score, mood_label=str(score))

    history = telemetry.mood_history(limit=10)
    assert [entry["mood_score"] for entry in history] == [5, 4, 3, 2, 1]


def test_mood_history_respects_limit(telemetry) -> None:
    session_id = telemetry.start_session()
    for _ in range(8):
        telemetry.log_mood(session_id=session_id, mood_score=3, mood_label="okay")
    assert len(telemetry.mood_history(limit=5)) == 5


def test_sharegpt_export_shape(telemetry) -> None:
    """The export must match what Unsloth / Axolotl / LLaMA-Factory ingest."""
    session_id = telemetry.start_session()
    trajectory_id = _log_turn(telemetry, session_id)
    telemetry.log_tool_execution(
        trajectory_id=trajectory_id,
        tool_name="execute_shell",
        tool_arguments={"command": "pytest -q"},
        raw_output="3 passed",
        exit_code=0,
        duration_ms=120,
    )

    dataset = telemetry.build_sharegpt_dataset()
    assert len(dataset) == 1
    record = dataset[0]

    assert record["id"] == trajectory_id
    assert record["system"] == "You are Jarvis."

    turns = record["conversations"]
    assert [turn["from"] for turn in turns] == ["human", "tool_call", "tool_response", "gpt"]
    assert turns[0]["value"] == "hi"
    # Arguments must deserialise back to a dict, not stay a JSON string.
    assert turns[1]["arguments"] == {"command": "pytest -q"}
    assert turns[2]["value"] == "3 passed"
    assert turns[3]["value"] == "Hey, good to see you."
    assert turns[3]["thought"] == "Routed to local fallback."

    meta = record["metadata"]
    assert meta["intent"] == "PERSONAL_CHAT"
    assert meta["model"] == "jarvis-local-echo"
    assert meta["total_latency_ms"] == 42


def test_failed_tool_export_uses_stderr(telemetry) -> None:
    session_id = telemetry.start_session()
    trajectory_id = _log_turn(telemetry, session_id)
    telemetry.log_tool_execution(
        trajectory_id=trajectory_id,
        tool_name="execute_shell",
        tool_arguments={"command": "false"},
        raw_output="",
        error_output="command failed",
        exit_code=1,
    )

    turns = telemetry.build_sharegpt_dataset()[0]["conversations"]
    tool_response = next(turn for turn in turns if turn["from"] == "tool_response")
    assert tool_response["value"] == "command failed"


def test_jsonl_export_is_valid_ndjson(telemetry, tmp_path) -> None:
    session_id = telemetry.start_session()
    _log_turn(telemetry, session_id)
    _log_turn(telemetry, session_id, user_prompt="hello again")

    out = tmp_path / "dataset.jsonl"
    count = telemetry.export_to_sharegpt_jsonl(out)

    assert count == 2
    lines = out.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    for line in lines:
        record = json.loads(line)          # each line must parse independently
        assert record["conversations"][0]["from"] == "human"


def test_init_db_is_idempotent(telemetry) -> None:
    session_id = telemetry.start_session()
    telemetry.init_db()
    telemetry.init_db()
    assert telemetry.session_exists(session_id)
