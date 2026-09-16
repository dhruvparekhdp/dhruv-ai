"""Jarvis host engine -- FastAPI gateway.

Serves both the JSON API and the PWA from a single origin, which is what makes
a sub-30-minute cloud deploy possible: one service, one HTTPS certificate, no
CORS negotiation, and an iPhone-installable app at the root URL.

The API contract here is frozen -- the Phase 2 Ionic + Angular dashboard is
built against exactly these routes, so the vanilla PWA in app/static can be
replaced without touching the backend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent import prompts
from app.agent.orchestrator import orchestrator
from app.agents.registry import registry as agent_registry
from app.core.config import get_settings
from app.core.security import check_passcode, require_passcode
from app.core.telemetry import telemetry
from app.missions import store as mission_store
from app.missions.models import ApprovalDecision, Budget
from app.memory import search as memory_search
from app.memory import store as memory_store
from app.missions.scheduler import scheduler
from app.nodes import store as node_store
from app.nodes.gateway import NoNodeAvailable, gateway
from app.nodes.models import NodeStatus, parse_capabilities
from app.services.websocket_manager import ws_manager
from app.todos import reminders as todo_reminders
from app.todos import seed as todo_seed
from app.todos import store as todo_store
from app.todos.models import TodoError
from app.tools import cloud as _cloud_tools  # noqa: F401 - import registers the tools
from app.tools import node as _node_tools    # noqa: F401 - import registers the tools
from app.tools.registry import registry as tool_registry

logging.basicConfig(
    level=logging.DEBUG if get_settings().debug else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)
log = logging.getLogger("jarvis")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    telemetry.init_db()
    mission_store.init_db()
    memory_store.init_db()
    node_store.init_db()
    todo_store.init_db()

    # Only writes on a genuinely empty table, so a restart neither duplicates
    # the backlog nor resurrects finished work.
    seeded = todo_seed.seed_if_empty()
    if seeded:
        log.info("Seeded %d starting todos.", seeded)

    # Tools are registered by the import above; agent definitions are validated
    # against them here, so a bad definition fails loudly at boot rather than
    # halfway through a mission.
    agent_registry.load()

    # Nothing is connected until it re-announces itself; otherwise the fleet
    # view would show pre-crash state and dispatch into dead sockets.
    was_online = node_store.mark_all_offline()
    if was_online:
        log.info("marked %d node(s) offline pending reconnect", was_online)

    # Recover anything a crash, restart, or redeploy left mid-flight before
    # accepting new work.
    recovered = mission_store.reconcile_orphaned_tasks()
    if recovered["tasks_requeued"] or recovered["missions_resumed"]:
        log.warning(
            "recovered from unclean shutdown: %d task(s) requeued, %d mission(s) resumed",
            recovered["tasks_requeued"], recovered["missions_resumed"],
        )

    log.info(
        "Jarvis up | storage=%s | engines=%s | auth=%s | agents=%s | tools=%d",
        settings.storage_backend,
        ",".join(orchestrator.engines.available()),
        "on" if settings.auth_enabled else "OFF",
        ",".join(agent_registry.ids()),
        len(tool_registry.names()),
    )
    if orchestrator.engines.is_degraded:
        log.warning("No GROQ_API_KEY or GEMINI_API_KEY set - running on local echo engine.")
    if not settings.auth_enabled:
        log.warning("No JARVIS_PASSCODE set - the API is unauthenticated.")
    if settings.storage_backend == "sqlite":
        log.warning(
            "Using SQLite. On an ephemeral host this database is wiped on every "
            "restart; set DATABASE_URL to a Postgres instance to keep telemetry."
        )
    yield


app = FastAPI(
    title="Jarvis Host Engine",
    version="0.1.0",
    description="Phase 1: conversation, mood check-ins, and dataset-grade telemetry.",
    lifespan=lifespan,
)

# The PWA is same-origin, so CORS is not needed for it. It stays open for the
# Phase 2 Ionic dashboard during local development (ionic serve on :8100).
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("JARVIS_CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

Guarded = Annotated[None, Depends(require_passcode)]


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------


class SessionStartRequest(BaseModel):
    client_type: str = Field(default="WEB_PWA", max_length=32)
    user_agent: str = Field(default="", max_length=500)


class CommandRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=8000)
    intent_override: str | None = Field(default=None, max_length=32)


class MoodRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    score: int = Field(ge=prompts.MIN_MOOD, le=prompts.MAX_MOOD)
    note: str = Field(default="", max_length=2000)


class FeedbackRequest(BaseModel):
    trajectory_id: str = Field(min_length=1, max_length=100)
    rating: int = Field(ge=-1, le=1)


class MissionRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=8000)
    session_id: str = Field(default="", max_length=100)
    budget: dict[str, int] | None = None
    #: Optionally block for up to N seconds so simple missions can be awaited
    #: inline instead of polling.
    wait_seconds: float = Field(default=0, ge=0, le=120)


class ApprovalRequest(BaseModel):
    approve: bool


class MemoryWriteRequest(BaseModel):
    scope: str = Field(default="user", max_length=16)
    key: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=4000)


class MemoryUpdateRequest(BaseModel):
    value: str = Field(min_length=1, max_length=4000)


class TodoCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    track: str = Field(min_length=1, max_length=32)
    priority: int = Field(default=2, ge=1, le=3)
    status: str = Field(default="inbox", max_length=16)
    notes: str = Field(default="", max_length=4000)
    due_at: str | None = None
    remind_at: str | None = None
    # Defaults to the human. An agent filing work must say so, so a backlog of
    # unasked-for suggestions stays visible as exactly that.
    source: str = Field(default="me", max_length=16)


class TodoUpdateRequest(BaseModel):
    """Every field optional — `exclude_unset` is what makes this a real PATCH.

    Without it, omitting a field would send None and blank it, so clearing a
    due date and not mentioning one would be indistinguishable.
    """

    title: str | None = Field(default=None, min_length=1, max_length=300)
    track: str | None = Field(default=None, max_length=32)
    status: str | None = Field(default=None, max_length=16)
    priority: int | None = Field(default=None, ge=1, le=3)
    notes: str | None = Field(default=None, max_length=4000)
    due_at: str | None = None
    remind_at: str | None = None


class TrackActiveRequest(BaseModel):
    is_active: bool
    note: str = Field(default="", max_length=500)


class EnrolmentRequest(BaseModel):
    label: str = Field(default="", max_length=100)


class NodeTrustRequest(BaseModel):
    untrusted_ok: bool


class NodeDispatchRequest(BaseModel):
    tool: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)
    node_id: str | None = Field(default=None, max_length=100)
    timeout: float = Field(default=60, ge=1, le=300)


# --------------------------------------------------------------------------
# Health & metadata
# --------------------------------------------------------------------------


@app.get("/health", tags=["system"])
async def health() -> dict[str, Any]:
    """Liveness plus an honest report of how the service is actually running."""
    settings = get_settings()
    payload: dict[str, Any] = {
        "status": "ok",
        "service": "jarvis-host-engine",
        "version": app.version,
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "storage": settings.storage_backend,
        "storage_durable": settings.storage_backend == "postgres",
        "auth_enabled": settings.auth_enabled,
        "engines": orchestrator.engines.describe(),
    }
    try:
        payload["telemetry"] = telemetry.stats()
    except Exception as exc:  # noqa: BLE001 - health must never 500
        payload["status"] = "degraded"
        payload["telemetry_error"] = str(exc)
    return payload


@app.get("/api/v1/greeting", tags=["chat"])
async def greeting(_: Guarded) -> dict[str, Any]:
    """Time-aware greeting and the mood scale, so the client has no hardcoded copy."""
    return {
        "greeting": prompts.greeting_for(),
        "mood_scale": [
            {"score": score, "label": label, "emoji": emoji}
            for score, (label, emoji) in sorted(prompts.MOOD_SCALE.items())
        ],
        "degraded": orchestrator.engines.is_degraded,
    }


# --------------------------------------------------------------------------
# Core API
# --------------------------------------------------------------------------


@app.post("/api/v1/session/start", tags=["chat"], status_code=status.HTTP_201_CREATED)
async def start_session(req: SessionStartRequest, _: Guarded) -> dict[str, Any]:
    session_id = telemetry.start_session(client_type=req.client_type, user_agent=req.user_agent)
    await ws_manager.broadcast("session", f"Session started: {session_id}", session_id=session_id)
    return {"status": "success", "session_id": session_id}


@app.get("/api/v1/session/{session_id}", tags=["chat"])
async def get_session(session_id: str, _: Guarded) -> dict[str, Any]:
    """Cheap existence check so the client can reuse a stored session across
    reloads without spending an LLM call to find out whether it still exists."""
    return {"status": "success", "session_id": session_id, "exists": telemetry.session_exists(session_id)}


@app.post("/api/v1/command", tags=["chat"])
async def execute_command(req: CommandRequest, _: Guarded) -> dict[str, Any]:
    """Main turn endpoint: route, complete, log, return."""
    if not telemetry.session_exists(req.session_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown session_id. Start a session first.")

    try:
        turn = await orchestrator.run_turn(
            session_id=req.session_id,
            user_prompt=req.prompt,
            intent_override=req.intent_override,
        )
    except Exception as exc:  # noqa: BLE001 - already logged as a failed trajectory
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Engine failure: {exc}") from exc

    return {
        "status": "success",
        "trajectory_id": turn.trajectory_id,
        "intent": turn.intent,
        "response": turn.response,
        "provider": turn.provider,
        "model": turn.model,
        "latency_ms": turn.latency_ms,
        "degraded": turn.degraded,
    }


@app.post("/api/v1/mood", tags=["mood"])
async def mood_checkin(req: MoodRequest, _: Guarded) -> dict[str, Any]:
    """Record a mood check-in and reply to it."""
    if not telemetry.session_exists(req.session_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown session_id. Start a session first.")

    try:
        turn, checkin_id = await orchestrator.run_mood_checkin(
            session_id=req.session_id, score=req.score, note=req.note
        )
    except Exception as exc:  # noqa: BLE001 - already logged as a failed trajectory
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Engine failure: {exc}") from exc

    return {
        "status": "success",
        "checkin_id": checkin_id,
        "trajectory_id": turn.trajectory_id,
        "score": req.score,
        "label": prompts.mood_label(req.score),
        "emoji": prompts.mood_emoji(req.score),
        "response": turn.response,
        "provider": turn.provider,
        "latency_ms": turn.latency_ms,
        "degraded": turn.degraded,
    }


@app.get("/api/v1/mood/history", tags=["mood"])
async def mood_history(
    _: Guarded, limit: int = Query(default=30, ge=1, le=365)
) -> dict[str, Any]:
    entries = telemetry.mood_history(limit=limit)
    scores = [e["mood_score"] for e in entries]
    return {
        "status": "success",
        "count": len(entries),
        "average": round(sum(scores) / len(scores), 2) if scores else None,
        "entries": entries,
    }


@app.post("/api/v1/feedback", tags=["telemetry"])
async def record_feedback(req: FeedbackRequest, _: Guarded) -> dict[str, Any]:
    """Attach a reward signal to a turn -- the preference data for later tuning."""
    if not telemetry.record_feedback(req.trajectory_id, req.rating):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown trajectory_id.")
    await ws_manager.broadcast(
        "feedback",
        f"Reward {req.rating:+d} recorded for {req.trajectory_id}",
        trajectory_id=req.trajectory_id,
        rating=req.rating,
    )
    return {"status": "success", "trajectory_id": req.trajectory_id, "rating": req.rating}


# --------------------------------------------------------------------------
# Missions
# --------------------------------------------------------------------------


def _task_payload(task: Any) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "agent_id": task.agent_id,
        "objective": task.objective,
        "status": task.status.value,
        "depends_on": task.depends_on,
        "result": task.result,
        "error": task.error,
        "attempts": task.attempts,
        "tokens": task.total_tokens,
        "tool_calls": task.tool_calls,
        "created_at": task.created_at,
    }


def _mission_payload(mission: Any, tasks: list[Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mission_id": mission.mission_id,
        "goal": mission.goal,
        "status": mission.status.value,
        "summary": mission.summary,
        "error": mission.error,
        "budget": mission.budget.as_dict(),
        "created_at": mission.created_at,
        "updated_at": mission.updated_at,
    }
    if tasks is not None:
        done = sum(1 for t in tasks if t.is_terminal)
        payload["tasks"] = [_task_payload(t) for t in tasks]
        payload["progress"] = {
            "total": len(tasks),
            "done": done,
            "percent": round(100 * done / len(tasks)) if tasks else 0,
        }
    return payload


@app.post("/api/v1/missions", tags=["missions"], status_code=status.HTTP_201_CREATED)
async def create_mission(req: MissionRequest, _: Guarded) -> dict[str, Any]:
    """Create a mission and start it in the background."""
    mission = await scheduler.create_mission(
        goal=req.goal,
        session_id=req.session_id,
        budget=Budget.from_dict(req.budget) if req.budget else None,
    )
    scheduler.start(mission.mission_id)

    if req.wait_seconds:
        try:
            await scheduler.wait_for(mission.mission_id, timeout=req.wait_seconds)
        except asyncio.TimeoutError:
            pass   # still running; the client polls or watches the WebSocket

    refreshed = mission_store.get_mission(mission.mission_id) or mission
    return {"status": "success", **_mission_payload(refreshed, mission_store.list_tasks(mission.mission_id))}


@app.get("/api/v1/missions", tags=["missions"])
async def list_missions(_: Guarded, limit: int = Query(default=25, ge=1, le=200)) -> dict[str, Any]:
    missions = mission_store.list_missions(limit=limit)
    return {"status": "success", "count": len(missions), "missions": [_mission_payload(m) for m in missions]}


@app.get("/api/v1/missions/{mission_id}", tags=["missions"])
async def get_mission(mission_id: str, _: Guarded) -> dict[str, Any]:
    mission = mission_store.get_mission(mission_id)
    if mission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown mission_id.")
    return {"status": "success", **_mission_payload(mission, mission_store.list_tasks(mission_id))}


@app.get("/api/v1/missions/{mission_id}/messages", tags=["missions"])
async def get_mission_messages(mission_id: str, _: Guarded) -> dict[str, Any]:
    """The audited message bus for one mission -- who asked what of whom."""
    if mission_store.get_mission(mission_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown mission_id.")
    messages = mission_store.list_messages(mission_id)
    return {"status": "success", "count": len(messages), "messages": messages}


@app.post("/api/v1/missions/{mission_id}/cancel", tags=["missions"])
async def cancel_mission(mission_id: str, _: Guarded) -> dict[str, Any]:
    if not await scheduler.cancel(mission_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown or already-finished mission.")
    return {"status": "success", "mission_id": mission_id}


# --------------------------------------------------------------------------
# Agents, tools, approvals
# --------------------------------------------------------------------------


@app.get("/api/v1/agents", tags=["agents"])
async def list_agents(_: Guarded) -> dict[str, Any]:
    agents = [a.describe() for a in agent_registry.all()]
    return {"status": "success", "count": len(agents), "agents": agents}


@app.get("/api/v1/tools", tags=["agents"])
async def list_tools(_: Guarded) -> dict[str, Any]:
    tools = tool_registry.describe()
    return {"status": "success", "count": len(tools), "tools": tools}


@app.get("/api/v1/approvals", tags=["agents"])
async def list_approvals(_: Guarded) -> dict[str, Any]:
    pending = mission_store.list_pending_approvals()
    return {"status": "success", "count": len(pending), "approvals": pending}


@app.post("/api/v1/approvals/{approval_id}", tags=["agents"])
async def decide_approval(approval_id: str, req: ApprovalRequest, _: Guarded) -> dict[str, Any]:
    """Approve or deny a parked dangerous action."""
    decision = ApprovalDecision.APPROVED if req.approve else ApprovalDecision.DENIED
    if not mission_store.decide_approval(approval_id, decision):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Unknown approval, or it was already decided."
        )
    return {"status": "success", "approval_id": approval_id, "decision": decision.value}


# --------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------


@app.get("/api/v1/memory", tags=["memory"])
async def list_memory(
    _: Guarded,
    scope: str = Query(default="", max_length=16),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    """Everything Jarvis believes, for the Memory panel.

    A confidently recalled wrong fact is worse than no memory, so this is
    deliberately browsable and editable rather than opaque.
    """
    try:
        entries = memory_store.list_all(scope=scope, limit=limit)
    except memory_store.MemoryError_ as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"status": "success", "count": len(entries), "entries": entries,
            "stats": memory_store.stats()}


@app.get("/api/v1/memory/search", tags=["memory"])
async def search_memory(
    _: Guarded,
    q: str = Query(min_length=1, max_length=1000),
    scope: str = Query(default="", max_length=16),
    limit: int = Query(default=8, ge=1, le=50),
) -> dict[str, Any]:
    outcome = memory_search.search(q, scope=scope, limit=limit)
    return {"status": "success", "query": q, **outcome}


@app.post("/api/v1/memory", tags=["memory"], status_code=status.HTTP_201_CREATED)
async def write_memory(req: MemoryWriteRequest, _: Guarded) -> dict[str, Any]:
    try:
        entry = memory_store.write(req.scope, req.key, req.value, source="user")
    except memory_store.MemoryError_ as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"status": "success", "entry": entry}


@app.put("/api/v1/memory/{entry_id}", tags=["memory"])
async def update_memory(entry_id: str, req: MemoryUpdateRequest, _: Guarded) -> dict[str, Any]:
    try:
        entry = memory_store.update_by_id(entry_id, req.value)
    except memory_store.MemoryError_ as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown entry_id.")
    return {"status": "success", "entry": entry}


@app.delete("/api/v1/memory/{entry_id}", tags=["memory"])
async def delete_memory(entry_id: str, _: Guarded) -> dict[str, Any]:
    """Direct deletion by the user.

    No approval gate here: the gate exists to hold *agents* accountable to the
    human, and the human is the one calling this.
    """
    if not memory_store.delete_by_id(entry_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown entry_id.")
    return {"status": "success", "entry_id": entry_id}


# --------------------------------------------------------------------------
# Todos
#
# Owner-facing, unlike missions: these are what Dhruv is doing, not what Jarvis
# is doing. Agents may file them (source="agent") but the focus rules in
# todos/store.py apply identically no matter who calls.
# --------------------------------------------------------------------------


@app.get("/api/v1/todos", tags=["todos"])
async def list_todos(
    _: Guarded,
    track: str = Query(default="", max_length=32),
    todo_status: str = Query(default="", max_length=16, alias="status"),
    active_only: bool = Query(default=False),
    include_done: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    try:
        todos = todo_store.list_todos(
            track=track or None,
            status=todo_status or None,
            active_only=active_only,
            include_done=include_done,
            limit=limit,
        )
    except TodoError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"status": "success", "count": len(todos), "todos": [t.to_dict() for t in todos]}


@app.get("/api/v1/todos/next", tags=["todos"])
async def next_todo(_: Guarded) -> dict[str, Any]:
    """The one thing to do now, or an honest nothing.

    Deliberately singular. A ranked list of ten is still a decision, and making
    that decision repeatedly is the tax this endpoint removes.
    """
    todo = todo_store.next_todo()
    summary = todo_store.track_summary()
    return {
        "status": "success",
        "todo": todo.to_dict() if todo else None,
        "active_tracks": summary["active_tracks"],
        "reason": summary["verdict"] if todo is None else "",
    }


@app.get("/api/v1/todos/summary", tags=["todos"])
async def todo_summary(_: Guarded) -> dict[str, Any]:
    return {"status": "success", **todo_store.track_summary()}


@app.post("/api/v1/todos", tags=["todos"], status_code=status.HTTP_201_CREATED)
async def create_todo(req: TodoCreateRequest, _: Guarded) -> dict[str, Any]:
    try:
        todo = todo_store.add(
            req.title,
            req.track,
            priority=req.priority,
            status=req.status,
            notes=req.notes,
            due_at=req.due_at,
            remind_at=req.remind_at,
            source=req.source,
        )
    except TodoError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"status": "success", "todo": todo.to_dict()}


@app.patch("/api/v1/todos/{todo_id}", tags=["todos"])
async def patch_todo(todo_id: str, req: TodoUpdateRequest, _: Guarded) -> dict[str, Any]:
    fields = req.model_dump(exclude_unset=True)
    try:
        todo = todo_store.update(todo_id, **fields)
    except TodoError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if todo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown todo_id.")
    return {"status": "success", "todo": todo.to_dict()}


@app.delete("/api/v1/todos/{todo_id}", tags=["todos"])
async def remove_todo(todo_id: str, _: Guarded) -> dict[str, Any]:
    if not todo_store.delete(todo_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown todo_id.")
    return {"status": "success", "todo_id": todo_id}


@app.post("/api/v1/todos/tracks/{track}", tags=["todos"])
async def set_track(track: str, req: TrackActiveRequest, _: Guarded) -> dict[str, Any]:
    """Activate or park a track.

    Returns 400 when the active limit is already reached. That refusal is the
    product, not an error — it names what is active so a real trade gets made.
    """
    try:
        result = todo_store.set_track_active(track, req.is_active, note=req.note)
    except TodoError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"status": "success", **result}


@app.post("/api/v1/todos/reminders/sweep", tags=["todos"])
async def sweep_reminders(_: Guarded) -> dict[str, Any]:
    """Send any reminders that have come due. Idempotent; safe on a timer."""
    return {"status": "success", **await todo_reminders.sweep()}


# --------------------------------------------------------------------------
# Fleet
# --------------------------------------------------------------------------


@app.get("/api/v1/nodes", tags=["fleet"])
async def list_nodes(_: Guarded) -> dict[str, Any]:
    """Every enrolled node and what it can do."""
    nodes = node_store.list_nodes()
    connected = set(gateway.connected_ids())
    payload = []
    for node in nodes:
        item = node.describe()
        item["connected"] = node.node_id in connected
        payload.append(item)
    return {
        "status": "success",
        "count": len(payload),
        "online": sum(1 for n in payload if n["connected"]),
        "nodes": payload,
    }


@app.post("/api/v1/nodes/enrol", tags=["fleet"], status_code=status.HTTP_201_CREATED)
async def create_enrolment(req: EnrolmentRequest, _: Guarded) -> dict[str, Any]:
    """Mint a one-time join token. Shown once, expires in 30 minutes."""
    token = node_store.create_enrolment_token(req.label)
    return {
        "status": "success",
        "enrolment_token": token,
        "expires_in_minutes": node_store.ENROLMENT_TTL_MINUTES,
        "note": "Shown once. Give it to the node agent on first start.",
    }


@app.post("/api/v1/nodes/{node_id}/trust", tags=["fleet"])
async def set_node_trust(node_id: str, req: NodeTrustRequest, _: Guarded) -> dict[str, Any]:
    """Designate which node may run untrusted, model-generated code.

    Deliberately explicit: a node never becomes the sandbox by accident.
    """
    if not node_store.set_untrusted_ok(node_id, req.untrusted_ok):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown node_id.")
    return {"status": "success", "node_id": node_id, "untrusted_ok": req.untrusted_ok}


@app.post("/api/v1/nodes/dispatch", tags=["fleet"])
async def dispatch_to_node(req: NodeDispatchRequest, _: Guarded) -> dict[str, Any]:
    """Run a node tool directly, as the user.

    This is what the Files and System panels call, and it is the fastest way to
    check a node is actually working.

    No approval gate here, deliberately: the gate exists to hold *agents*
    accountable to the human, and the human is the one making this call. The
    node still enforces its own path jail and shell allowlist, so "direct" does
    not mean "unbounded".
    """
    tool = tool_registry.get(req.tool)
    if tool is None or not tool.requires_node:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"'{req.tool}' is not a node tool. Node tools: "
            + ", ".join(t["name"] for t in tool_registry.describe() if t["requires_node"]),
        )

    try:
        outcome = await gateway.dispatch(
            tool=tool.name,
            arguments=req.arguments,
            capability=tool.node_capability,
            untrusted=tool.untrusted,
            node_id=req.node_id,
            timeout=req.timeout,
        )
    except NoNodeAvailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    return {"status": "success", "tool": tool.name, **outcome}


@app.delete("/api/v1/nodes/{node_id}", tags=["fleet"])
async def revoke_node(node_id: str, _: Guarded) -> dict[str, Any]:
    """Remove a node. Its secret stops working immediately."""
    if not node_store.revoke(node_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown node_id.")
    await gateway.detach(node_id)
    return {"status": "success", "node_id": node_id}


@app.get("/api/v1/telemetry/stats", tags=["telemetry"])
async def telemetry_stats(_: Guarded) -> dict[str, Any]:
    return {"status": "success", **telemetry.stats()}


@app.get("/api/v1/dataset/export", tags=["telemetry"])
async def export_dataset(_: Guarded) -> StreamingResponse:
    """Download logged trajectories as ShareGPT JSONL.

    Feeds straight into Unsloth / Axolotl / LLaMA-Factory.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        telemetry.iter_sharegpt_jsonl(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="jarvis-dataset-{stamp}.jsonl"'},
    )


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------


@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket, passcode: str | None = Query(default=None)) -> None:
    """Live telemetry stream.

    Browsers cannot set headers on a WebSocket handshake, so the passcode
    arrives as a query parameter here. Rejected before accept() so an
    unauthorised client never joins the broadcast set.
    """
    if not check_passcode(passcode):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await ws_manager.connect(websocket)
    await ws_manager.broadcast("system", "Log stream connected.")
    try:
        while True:
            # Inbound messages are unused in Phase 1; the read keeps the
            # connection alive and detects client disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
    except Exception:  # noqa: BLE001 - never let one socket kill the server
        log.exception("websocket error")
        await ws_manager.disconnect(websocket)


@app.websocket("/ws/node")
async def websocket_node(websocket: WebSocket) -> None:
    """Node connection endpoint.

    A node joins with a one-time enrolment token, or reconnects with the
    node_id and secret it was issued. Authentication happens in the first
    message rather than the handshake, because a node agent can set neither
    headers nor cookies reliably across platforms.
    """
    await websocket.accept()
    node_id: str | None = None

    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=15)
        hello = json.loads(raw)
    except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    if hello.get("type") != "hello":
        await websocket.send_text(json.dumps({"type": "error", "error": "expected hello"}))
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    name = str(hello.get("name") or "unnamed-node")[:100]
    platform = str(hello.get("platform") or "linux")[:32]
    capabilities = parse_capabilities(hello.get("capabilities"))
    ram_mb = int(hello.get("ram_mb") or 0)
    cores = int(hello.get("cores") or 0)

    if hello.get("node_id") and hello.get("secret"):
        # Returning node.
        if not node_store.authenticate(hello["node_id"], hello["secret"]):
            await websocket.send_text(json.dumps({"type": "error", "error": "authentication failed"}))
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        node_id = hello["node_id"]
        node_store.update_presence(
            node_id, status=NodeStatus.ONLINE, capabilities=capabilities,
            ram_mb=ram_mb, cores=cores,
        )
        await websocket.send_text(json.dumps({"type": "welcome", "node_id": node_id}))

    elif hello.get("enrolment_token"):
        # New node joining for the first time.
        if not node_store.redeem_enrolment_token(hello["enrolment_token"]):
            await websocket.send_text(json.dumps({
                "type": "error", "error": "enrolment token invalid, expired, or already used",
            }))
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        node, secret = node_store.enrol(
            name=name, platform=platform, capabilities=capabilities,
            ram_mb=ram_mb, cores=cores,
        )
        node_id = node.node_id
        # The secret is shown exactly once; the node stores it for reconnects.
        await websocket.send_text(json.dumps({
            "type": "welcome", "node_id": node_id, "secret": secret,
        }))
        log.info("enrolled node %s (%s)", node.name, node_id)

    else:
        await websocket.send_text(json.dumps({
            "type": "error", "error": "hello needs either enrolment_token, or node_id and secret",
        }))
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await gateway.attach(node_id, websocket)
    await ws_manager.broadcast("node", f"Node online: {name}", node_id=node_id, name=name)

    try:
        while True:
            message = json.loads(await websocket.receive_text())
            reply = await gateway.handle_message(node_id, message)
            if reply is not None:
                await websocket.send_text(json.dumps(reply))
    except WebSocketDisconnect:
        pass
    except json.JSONDecodeError:
        await websocket.send_text(json.dumps({"type": "error", "error": "malformed JSON"}))
    except Exception:  # noqa: BLE001 - one node must never take the server down
        log.exception("node socket error for %s", node_id)
    finally:
        await gateway.detach(node_id)
        await ws_manager.broadcast("node", f"Node offline: {name}", node_id=node_id, name=name)


# --------------------------------------------------------------------------
# PWA
# --------------------------------------------------------------------------

# Mounted last so it never shadows an API route.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "icons" / "icon-192.png", media_type="image/png")


@app.get("/manifest.json", include_in_schema=False)
async def manifest() -> FileResponse:
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
async def service_worker() -> FileResponse:
    # Must be served from the origin root to control the whole scope.
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )
