"""Dataset-grade telemetry engine -- the fine-tuning flywheel.

Every conversation turn, mood check-in, tool call, and human reward signal is
recorded in a structured, training-ready shape. The point is not observability
for its own sake: these rows export directly to ShareGPT/Axolotl JSONL so a
local model can later be fine-tuned on how *you* actually use Jarvis.

Captured per the design doc's "Standardized Logged Parameters":
  1. System metadata -- session/trajectory IDs, timestamps, provider, exact
     model ID, token counts, end-to-end latency, time-to-first-token.
  2. Context snapshots -- the system prompt actually used for that turn.
  3. Reasoning trajectories -- the router's rationale and classification.
  4. Tool execution traces -- name, args, stdout/stderr, exit code, duration.
  5. Human feedback -- thumbs up/down, for later DPO/RLHF pairs.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.core.db import ddl_types, execute, execute_script, fetch_all, get_conn


def _now() -> str:
    """UTC timestamp with fixed-width microseconds.

    Written by the application rather than left to the column default:
    CURRENT_TIMESTAMP has only second granularity, so several check-ins made in
    the same second tie, and ORDER BY falls through to a random UUID -- which
    silently reverses the mood trend chart. `timespec="microseconds"` keeps the
    string fixed-width so SQLite's lexicographic sort stays chronological.
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")

# Turns whose reply the user explicitly rated bad are excluded from exports;
# we keep the row (it is useful as a negative for preference tuning) but do not
# feed it into supervised fine-tuning as if it were a good answer.
_SFT_FILTER = "fine_tune_eligible = {true} AND user_reward_rating >= 0"


class TelemetryEngine:
    """Owns the operational database and the dataset exporter."""

    def __init__(self) -> None:
        self._initialised = False

    # -- schema ----------------------------------------------------------

    def init_db(self) -> None:
        """Create tables if absent. Safe to call repeatedly."""
        t = ddl_types()
        script = """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id   TEXT PRIMARY KEY,
            created_at   {ts} DEFAULT {now},
            client_type  TEXT,
            user_agent   TEXT
        );

        CREATE TABLE IF NOT EXISTS agent_trajectories (
            trajectory_id          TEXT PRIMARY KEY,
            session_id             TEXT NOT NULL,
            created_at             {ts} DEFAULT {now},
            intent_category        TEXT NOT NULL,
            user_prompt            TEXT NOT NULL,
            system_prompt_snapshot TEXT NOT NULL,
            model_provider         TEXT NOT NULL,
            model_name             TEXT NOT NULL,
            agent_reasoning        TEXT,
            raw_llm_response       TEXT NOT NULL,
            prompt_tokens          INTEGER DEFAULT 0,
            completion_tokens      INTEGER DEFAULT 0,
            latency_ms             INTEGER DEFAULT 0,
            ttft_ms                INTEGER,
            execution_success      BOOLEAN DEFAULT {true},
            error_message          TEXT,
            user_reward_rating     INTEGER DEFAULT 0,
            fine_tune_eligible     BOOLEAN DEFAULT {true}
        );

        CREATE TABLE IF NOT EXISTS tool_executions (
            execution_id   TEXT PRIMARY KEY,
            trajectory_id  TEXT NOT NULL,
            created_at     {ts} DEFAULT {now},
            tool_name      TEXT NOT NULL,
            tool_arguments {json} NOT NULL,
            raw_output     TEXT,
            error_output   TEXT,
            exit_code      INTEGER DEFAULT 0,
            duration_ms    INTEGER DEFAULT 0,
            hitl_approved  BOOLEAN DEFAULT {true}
        );

        CREATE TABLE IF NOT EXISTS mood_checkins (
            checkin_id    TEXT PRIMARY KEY,
            session_id    TEXT NOT NULL,
            created_at    {ts} DEFAULT {now},
            mood_score    INTEGER NOT NULL,
            mood_label    TEXT NOT NULL,
            note          TEXT,
            trajectory_id TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_traj_session ON agent_trajectories(session_id);
        CREATE INDEX IF NOT EXISTS idx_tool_traj    ON tool_executions(trajectory_id);
        CREATE INDEX IF NOT EXISTS idx_mood_created ON mood_checkins(created_at);
        """.format(**t)

        with get_conn() as conn:
            execute_script(conn, script)
        self._initialised = True

    # -- sessions --------------------------------------------------------

    def start_session(self, client_type: str = "WEB_PWA", user_agent: str = "") -> str:
        session_id = f"sess_{uuid.uuid4()}"
        with get_conn() as conn:
            execute(
                conn,
                "INSERT INTO sessions (session_id, created_at, client_type, user_agent)"
                " VALUES (?, ?, ?, ?)",
                (session_id, _now(), client_type, user_agent[:500]),
            )
        return session_id

    def session_exists(self, session_id: str) -> bool:
        with get_conn() as conn:
            rows = fetch_all(
                conn, "SELECT session_id FROM sessions WHERE session_id = ?", (session_id,)
            )
        return bool(rows)

    # -- trajectories ----------------------------------------------------

    def log_trajectory(
        self,
        *,
        session_id: str,
        intent_category: str,
        user_prompt: str,
        system_prompt: str,
        model_provider: str,
        model_name: str,
        agent_reasoning: str,
        raw_llm_response: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: int = 0,
        ttft_ms: int | None = None,
        execution_success: bool = True,
        error_message: str | None = None,
    ) -> str:
        """Record one complete agent turn. Returns the trajectory id."""
        trajectory_id = f"traj_{uuid.uuid4()}"
        with get_conn() as conn:
            execute(
                conn,
                """
                INSERT INTO agent_trajectories (
                    trajectory_id, session_id, created_at, intent_category, user_prompt,
                    system_prompt_snapshot, model_provider, model_name,
                    agent_reasoning, raw_llm_response, prompt_tokens,
                    completion_tokens, latency_ms, ttft_ms, execution_success,
                    error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trajectory_id, session_id, _now(), intent_category, user_prompt,
                    system_prompt, model_provider, model_name, agent_reasoning,
                    raw_llm_response, prompt_tokens, completion_tokens,
                    latency_ms, ttft_ms, execution_success, error_message,
                ),
            )
        return trajectory_id

    def log_tool_execution(
        self,
        *,
        trajectory_id: str,
        tool_name: str,
        tool_arguments: dict[str, Any],
        raw_output: str = "",
        error_output: str = "",
        exit_code: int = 0,
        duration_ms: int = 0,
        hitl_approved: bool = True,
    ) -> str:
        """Record a single tool call. Unused in Phase 1 (no tools yet) but the
        coding/OS/trading subsystems write through this path."""
        execution_id = f"exec_{uuid.uuid4()}"
        with get_conn() as conn:
            execute(
                conn,
                """
                INSERT INTO tool_executions (
                    execution_id, trajectory_id, created_at, tool_name, tool_arguments,
                    raw_output, error_output, exit_code, duration_ms, hitl_approved
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id, trajectory_id, _now(), tool_name,
                    json.dumps(tool_arguments), raw_output, error_output,
                    exit_code, duration_ms, hitl_approved,
                ),
            )
        return execution_id

    # -- mood ------------------------------------------------------------

    def log_mood(
        self,
        *,
        session_id: str,
        mood_score: int,
        mood_label: str,
        note: str = "",
        trajectory_id: str | None = None,
    ) -> str:
        checkin_id = f"mood_{uuid.uuid4()}"
        with get_conn() as conn:
            execute(
                conn,
                """
                INSERT INTO mood_checkins (
                    checkin_id, session_id, created_at, mood_score, mood_label,
                    note, trajectory_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (checkin_id, session_id, _now(), mood_score, mood_label, note, trajectory_id),
            )
        return checkin_id

    def mood_history(self, limit: int = 30) -> list[dict[str, Any]]:
        with get_conn() as conn:
            rows = fetch_all(
                conn,
                """
                SELECT checkin_id, created_at, mood_score, mood_label, note
                FROM mood_checkins
                ORDER BY created_at DESC, checkin_id DESC
                LIMIT ?
                """,
                (limit,),
            )
        for row in rows:
            row["created_at"] = str(row["created_at"])
        return rows

    # -- feedback --------------------------------------------------------

    def record_feedback(self, trajectory_id: str, rating: int) -> bool:
        """Attach a -1/+1 reward to a turn. Returns False if the id is unknown."""
        if rating not in (-1, 0, 1):
            raise ValueError("rating must be -1, 0 or 1")
        with get_conn() as conn:
            rows = fetch_all(
                conn,
                "SELECT trajectory_id FROM agent_trajectories WHERE trajectory_id = ?",
                (trajectory_id,),
            )
            if not rows:
                return False
            execute(
                conn,
                "UPDATE agent_trajectories SET user_reward_rating = ? WHERE trajectory_id = ?",
                (rating, trajectory_id),
            )
        return True

    # -- stats -----------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        t = ddl_types()
        with get_conn() as conn:
            totals = fetch_all(
                conn,
                """
                SELECT
                    COUNT(*)                                              AS turns,
                    COALESCE(SUM(prompt_tokens + completion_tokens), 0)   AS tokens,
                    COALESCE(AVG(latency_ms), 0)                          AS avg_latency_ms
                FROM agent_trajectories
                """,
            )[0]
            moods = fetch_all(conn, "SELECT COUNT(*) AS n FROM mood_checkins")[0]
            eligible = fetch_all(
                conn,
                f"SELECT COUNT(*) AS n FROM agent_trajectories WHERE {_SFT_FILTER.format(**t)}",
            )[0]
        return {
            "turns_logged": int(totals["turns"]),
            "total_tokens": int(totals["tokens"]),
            "avg_latency_ms": round(float(totals["avg_latency_ms"]), 1),
            "mood_checkins": int(moods["n"]),
            "training_examples_ready": int(eligible["n"]),
        }

    # -- dataset export --------------------------------------------------

    def build_sharegpt_dataset(self) -> list[dict[str, Any]]:
        """Assemble logged trajectories into ShareGPT records.

        Shape is directly ingestible by Unsloth / Axolotl / LLaMA-Factory.
        """
        t = ddl_types()
        dataset: list[dict[str, Any]] = []

        with get_conn() as conn:
            trajectories = fetch_all(
                conn,
                f"""
                SELECT * FROM agent_trajectories
                WHERE {_SFT_FILTER.format(**t)}
                ORDER BY created_at ASC, trajectory_id ASC
                """,
            )
            for traj in trajectories:
                tools = fetch_all(
                    conn,
                    """
                    SELECT * FROM tool_executions
                    WHERE trajectory_id = ?
                    ORDER BY created_at ASC, execution_id ASC
                    """,
                    (traj["trajectory_id"],),
                )
                dataset.append(_to_sharegpt(traj, tools))

        return dataset

    def export_to_sharegpt_jsonl(self, output_file: str | Path) -> int:
        """Write the dataset to newline-delimited JSON. Returns record count."""
        path = Path(output_file)
        if path.parent != Path(""):
            path.parent.mkdir(parents=True, exist_ok=True)
        dataset = self.build_sharegpt_dataset()
        with path.open("w", encoding="utf-8") as fh:
            for record in dataset:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return len(dataset)

    def iter_sharegpt_jsonl(self) -> Iterable[str]:
        """Stream the dataset as JSONL lines, for HTTP download without
        buffering the whole thing in memory."""
        for record in self.build_sharegpt_dataset():
            yield json.dumps(record, ensure_ascii=False) + "\n"


def _to_sharegpt(traj: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert one trajectory (+ its tool calls) into a ShareGPT record."""
    conversations: list[dict[str, Any]] = [
        {"from": "human", "value": traj["user_prompt"]}
    ]

    for tool in tools:
        args = tool["tool_arguments"]
        if isinstance(args, str):  # SQLite stores JSON as TEXT
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"_raw": args}
        conversations.append(
            {"from": "tool_call", "name": tool["tool_name"], "arguments": args}
        )
        conversations.append(
            {
                "from": "tool_response",
                "name": tool["tool_name"],
                "value": tool["raw_output"] if tool["exit_code"] == 0 else tool["error_output"],
            }
        )

    final: dict[str, Any] = {"from": "gpt", "value": traj["raw_llm_response"]}
    if traj.get("agent_reasoning"):
        final["thought"] = traj["agent_reasoning"]
    conversations.append(final)

    return {
        "id": traj["trajectory_id"],
        "system": traj["system_prompt_snapshot"],
        "conversations": conversations,
        "metadata": {
            "intent": traj["intent_category"],
            "provider": traj["model_provider"],
            "model": traj["model_name"],
            "total_latency_ms": traj["latency_ms"],
            "prompt_tokens": traj["prompt_tokens"],
            "completion_tokens": traj["completion_tokens"],
            "user_reward": traj["user_reward_rating"],
        },
    }


# Module-level singleton, mirroring the design doc's `telemetry` global.
telemetry = TelemetryEngine()
