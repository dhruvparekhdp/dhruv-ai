"""Provider credentials the Keys page is allowed to manage.

Groq, Gemini, Tavily, and Ollama are the integrations this process can
actually call. Anything else is absent here on purpose, so the UI cannot
present a planned provider as if it were wired up.

An environment variable that is set and non-empty wins. A saved value never
replaces it, and the API says so instead of appearing to succeed. Saved
secrets are sealed before they touch the database. Callers that only need
status receive a source label, never the secret. Reveal is a separate call.

The seal key is `JARVIS_SETTINGS_KEY` when that is set, otherwise the
passcode. Losing both makes a saved secret unreadable; the row stays, and the
status says to enter it again. The plaintext is not logged on that path.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings, get_settings
from app.core.db import execute, execute_script, fetch_all, fetch_one, get_conn

Source = Literal["environment", "saved", "default", "unset", "unreadable"]

_SEAL_PREFIX = "seal:"
_PLAIN_PREFIX = "plain:"
_SALT = b"jarvis-provider-settings-v1"
_KEY_ITERATIONS = 200_000


class ProviderSettingsError(Exception):
    """Base for refusals the route turns into an HTTP error. No secret text."""


class UnknownProviderError(ProviderSettingsError):
    def __init__(self, provider_id: str) -> None:
        super().__init__(f"Unknown provider '{provider_id}'.")
        self.provider_id = provider_id


class UnknownFieldError(ProviderSettingsError):
    def __init__(self, provider_id: str, field_name: str) -> None:
        super().__init__(f"Unknown setting '{provider_id}.{field_name}'.")


class ManagedByEnvironmentError(ProviderSettingsError):
    def __init__(self, provider_id: str, field_name: str) -> None:
        super().__init__(
            f"{provider_id}.{field_name} is set from the environment and cannot be changed here."
        )


class InvalidSettingError(ProviderSettingsError):
    pass


@dataclass(frozen=True)
class FieldSpec:
    name: str
    secret: bool
    env: str
    kind: Literal["str", "bool"]
    default: str = ""
    max_len: int = 200


@dataclass(frozen=True)
class ResolvedField:
    name: str
    secret: bool
    source: Source
    managed: bool
    configured: bool
    value: str


# Order is the order the Keys page renders. Defaults match config.py so an
# untouched install behaves the way it did before this table existed.
_CATALOG: dict[str, tuple[str, tuple[FieldSpec, ...]]] = {
    "groq": (
        "Groq",
        (
            FieldSpec("api_key", True, "GROQ_API_KEY", "str", max_len=512),
            FieldSpec("model", False, "GROQ_MODEL", "str", "openai/gpt-oss-120b"),
        ),
    ),
    "gemini": (
        "Gemini",
        (
            FieldSpec("api_key", True, "GEMINI_API_KEY", "str", max_len=512),
            FieldSpec("model", False, "GEMINI_MODEL", "str", "gemini-2.5-flash"),
        ),
    ),
    "tavily": (
        "Tavily",
        (FieldSpec("api_key", True, "SEARCH_API_KEY", "str", max_len=512),),
    ),
    "ollama": (
        "Ollama",
        (
            FieldSpec("base_url", False, "OLLAMA_BASE_URL", "str", "http://127.0.0.1:11434"),
            FieldSpec("model", False, "OLLAMA_MODEL", "str", "qwen2.5:7b-instruct"),
            FieldSpec("enabled", False, "JARVIS_OLLAMA", "bool", "false", max_len=8),
        ),
    ),
}


def init_db() -> None:
    script = """
    CREATE TABLE IF NOT EXISTS provider_settings (
        provider     TEXT NOT NULL,
        field_name   TEXT NOT NULL,
        stored_value TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        PRIMARY KEY (provider, field_name)
    );
    """
    with get_conn() as conn:
        execute_script(conn, script)


def provider_ids() -> tuple[str, ...]:
    return tuple(_CATALOG)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _spec(provider_id: str, field_name: str) -> FieldSpec:
    label_fields = _CATALOG.get(provider_id)
    if label_fields is None:
        raise UnknownProviderError(provider_id)
    for spec in label_fields[1]:
        if spec.name == field_name:
            return spec
    raise UnknownFieldError(provider_id, field_name)


def _fields(provider_id: str) -> tuple[FieldSpec, ...]:
    found = _CATALOG.get(provider_id)
    if found is None:
        raise UnknownProviderError(provider_id)
    return found[1]


def _env_explicit(spec: FieldSpec) -> str | None:
    """The environment value when the variable is set and non-empty.

    An unset variable and a blank one are the same: the UI may supply a
    value. A present value, including `JARVIS_OLLAMA=0`, is managed and the
    UI must not overwrite it.
    """
    raw = os.environ.get(spec.env)
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if spec.kind == "bool":
        return "true" if value.lower() in {"1", "true", "yes", "on"} else "false"
    return value


def _master_key() -> bytes:
    """Key material for the seal. Cached so a request does not re-run PBKDF2.

    The cache key is the material itself. Tests that change the passcode or
    the settings key call `reset_key_cache`.
    """
    material = os.environ.get("JARVIS_SETTINGS_KEY", "").strip()
    if not material:
        material = os.environ.get("JARVIS_PASSCODE", "").strip()
    if not material:
        # Dev insecure boot has no passcode. The database is still local, and
        # a later passcode makes these rows unreadable rather than silently
        # reused under a new key.
        material = "jarvis-dev-insecure"
    cached = _key_cache.get(material)
    if cached is not None:
        return cached
    derived = hashlib.pbkdf2_hmac("sha256", material.encode(), _SALT, _KEY_ITERATIONS, dklen=32)
    _key_cache.clear()
    _key_cache[material] = derived
    return derived


_key_cache: dict[str, bytes] = {}


def reset_key_cache() -> None:
    _key_cache.clear()


def _seal(plaintext: str) -> str:
    key = _master_key()
    nonce = secrets.token_bytes(16)
    data = plaintext.encode()
    stream = _keystream(key, nonce, len(data))
    cipher = bytes(left ^ right for left, right in zip(data, stream, strict=True))
    mac = hmac.new(key, nonce + cipher, hashlib.sha256).digest()
    blob = base64.urlsafe_b64encode(nonce + mac + cipher).decode("ascii")
    return _SEAL_PREFIX + blob


def _open(stored: str) -> str:
    if not stored.startswith(_SEAL_PREFIX):
        raise InvalidSettingError("unreadable")
    try:
        raw = base64.urlsafe_b64decode(stored[len(_SEAL_PREFIX) :])
    except (binascii.Error, ValueError) as exc:
        raise InvalidSettingError("unreadable") from exc
    if len(raw) < 16 + 32:
        raise InvalidSettingError("unreadable")
    nonce, mac, cipher = raw[:16], raw[16:48], raw[48:]
    key = _master_key()
    expected = hmac.new(key, nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise InvalidSettingError("unreadable")
    stream = _keystream(key, nonce, len(cipher))
    data = bytes(left ^ right for left, right in zip(cipher, stream, strict=True))
    try:
        return data.decode()
    except UnicodeDecodeError as exc:
        raise InvalidSettingError("unreadable") from exc


def _keystream(key: bytes, nonce: bytes, size: int) -> bytes:
    blocks = bytearray()
    counter = 0
    while len(blocks) < size:
        block = hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        blocks.extend(block)
        counter += 1
    return bytes(blocks[:size])


def _load_rows() -> dict[tuple[str, str], str]:
    try:
        with get_conn() as conn:
            rows = fetch_all(
                conn,
                "SELECT provider, field_name, stored_value FROM provider_settings",
            )
    except Exception:  # noqa: BLE001 - sqlite and postgres raise different errors before the table exists
        # A call before boot has to look like "nothing saved" so startup still works.
        return {}
    return {(str(row["provider"]), str(row["field_name"])): str(row["stored_value"]) for row in rows}


def _db_revision() -> str:
    try:
        with get_conn() as conn:
            row = fetch_one(conn, "SELECT MAX(updated_at) AS rev FROM provider_settings")
    except Exception:  # noqa: BLE001 - sqlite and postgres raise different errors before the table exists
        return "0"
    if row is None or not row["rev"]:
        return "0"
    return str(row["rev"])


def revision() -> str:
    """Changes when a saved row changes or a provider env var changes.

    The env portion is a hash. The stamp is compared in memory and is not
    written to a log, but it still must not be the secret itself.
    """
    parts: list[str] = []
    for fields in _CATALOG.values():
        for spec in fields[1]:
            raw = os.environ.get(spec.env)
            parts.append("" if raw is None else raw)
    env_hash = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]
    return f"{_db_revision()}|{env_hash}"


def _decode_saved(spec: FieldSpec, stored: str | None) -> tuple[Source, str]:
    if stored is None:
        if spec.default and not spec.secret:
            return "default", spec.default
        return "unset", ""
    if spec.secret:
        try:
            return "saved", _open(stored)
        except InvalidSettingError:
            return "unreadable", ""
    if stored.startswith(_PLAIN_PREFIX):
        return "saved", stored[len(_PLAIN_PREFIX) :]
    return "unreadable", ""


def resolve(provider_id: str) -> dict[str, ResolvedField]:
    rows = _load_rows()
    resolved: dict[str, ResolvedField] = {}
    for spec in _fields(provider_id):
        explicit = _env_explicit(spec)
        if explicit is not None:
            configured = explicit in {"true", "false"} if spec.kind == "bool" else bool(explicit)
            resolved[spec.name] = ResolvedField(
                name=spec.name,
                secret=spec.secret,
                source="environment",
                managed=True,
                configured=configured,
                value=explicit,
            )
            continue
        source, value = _decode_saved(spec, rows.get((provider_id, spec.name)))
        if spec.kind == "bool":
            configured = value in {"true", "false"} and source not in {"unset", "unreadable"}
        else:
            configured = bool(value)
        resolved[spec.name] = ResolvedField(
            name=spec.name,
            secret=spec.secret,
            source=source,
            managed=False,
            configured=configured,
            value=value,
        )
    return resolved


def _public_field(field: ResolvedField) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": field.name,
        "secret": field.secret,
        "source": field.source,
        "managed": field.managed,
        "configured": field.configured,
    }
    if not field.secret:
        payload["value"] = field.value
    return payload


def _provider_configured(provider_id: str, fields: dict[str, ResolvedField]) -> bool:
    if provider_id == "ollama":
        return fields["enabled"].value == "true" and bool(fields["base_url"].value)
    return bool(fields["api_key"].value)


def public_status() -> list[dict[str, object]]:
    """Status for the Keys page and the AI gateway. Secrets are omitted."""
    items: list[dict[str, object]] = []
    for provider_id, (label, _specs) in _CATALOG.items():
        fields = resolve(provider_id)
        items.append(
            {
                "id": provider_id,
                "label": label,
                "configured": _provider_configured(provider_id, fields),
                "fields": [_public_field(fields[spec.name]) for spec in _specs],
            }
        )
    return items


def public_provider(provider_id: str) -> dict[str, object]:
    for item in public_status():
        if item["id"] == provider_id:
            return item
    raise UnknownProviderError(provider_id)


def effective_settings() -> Settings:
    """Env settings with the saved overlay applied where the env is silent."""
    base = get_settings()
    groq = resolve("groq")
    gemini = resolve("gemini")
    tavily = resolve("tavily")
    ollama = resolve("ollama")
    return replace(
        base,
        groq_api_key=groq["api_key"].value,
        gemini_api_key=gemini["api_key"].value,
        groq_model=groq["model"].value or base.groq_model,
        gemini_model=gemini["model"].value or base.gemini_model,
        search_api_key=tavily["api_key"].value,
        ollama_base_url=ollama["base_url"].value or base.ollama_base_url,
        ollama_model=ollama["model"].value or base.ollama_model,
        ollama_enabled=ollama["enabled"].value == "true",
    )


def search_api_key() -> str:
    return resolve("tavily")["api_key"].value


def scrub(text: str) -> str:
    """Remove any currently configured secret from a string before it is logged."""
    from app.core.redact import redact

    cleaned = redact(text)
    for provider_id in _CATALOG:
        for field in resolve(provider_id).values():
            if field.secret and len(field.value) >= 8 and field.value in cleaned:
                cleaned = cleaned.replace(field.value, "[REDACTED]")
    return cleaned


def _validate_base_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise InvalidSettingError("base_url must be an http(s) address.")
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise InvalidSettingError("base_url must not include credentials.")


def _normalise(spec: FieldSpec, raw: str) -> str:
    value = raw.strip()
    if spec.kind == "bool":
        lowered = value.lower()
        if lowered in {"1", "true", "yes", "on"}:
            return "true"
        if lowered in {"0", "false", "no", "off"}:
            return "false"
        raise InvalidSettingError(f"{spec.name} must be true or false.")
    if not value:
        raise InvalidSettingError(f"{spec.name} is empty.")
    if len(value) > spec.max_len:
        raise InvalidSettingError(f"{spec.name} is too long.")
    if any(ord(ch) < 32 for ch in value):
        raise InvalidSettingError(f"{spec.name} contains control characters.")
    if spec.name == "base_url":
        _validate_base_url(value)
    return value


def save(provider_id: str, updates: dict[str, str]) -> None:
    if not updates:
        raise InvalidSettingError("Nothing to save.")
    normalised: list[tuple[FieldSpec, str]] = []
    for name, raw in updates.items():
        spec = _spec(provider_id, name)
        if _env_explicit(spec) is not None:
            raise ManagedByEnvironmentError(provider_id, name)
        normalised.append((spec, _normalise(spec, raw)))
    stamp = _now()
    with get_conn() as conn:
        for spec, value in normalised:
            stored = _seal(value) if spec.secret else _PLAIN_PREFIX + value
            execute(
                conn,
                """
                INSERT INTO provider_settings (provider, field_name, stored_value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (provider, field_name) DO UPDATE SET
                    stored_value = excluded.stored_value,
                    updated_at = excluded.updated_at
                """,
                (provider_id, spec.name, stored, stamp),
            )


def clear(provider_id: str, field_name: str) -> None:
    spec = _spec(provider_id, field_name)
    if _env_explicit(spec) is not None:
        raise ManagedByEnvironmentError(provider_id, field_name)
    with get_conn() as conn:
        execute(
            conn,
            "DELETE FROM provider_settings WHERE provider = ? AND field_name = ?",
            (provider_id, field_name),
        )


def reveal(provider_id: str, field_name: str) -> str:
    spec = _spec(provider_id, field_name)
    if not spec.secret:
        raise InvalidSettingError(f"{field_name} is not a secret. It is already visible.")
    return resolve(provider_id)[field_name].value


def http_client() -> httpx.AsyncClient:
    """A short-lived client. Tests replace this so a check never dials out."""
    return httpx.AsyncClient(timeout=8.0)


async def check(provider_id: str) -> dict[str, object]:
    """Probe one provider. The result is a status word, never a body or a key."""
    if provider_id not in _CATALOG:
        raise UnknownProviderError(provider_id)
    fields = resolve(provider_id)
    if provider_id == "groq":
        return await _check_bearer(
            fields["api_key"].value,
            "https://api.groq.com/openai/v1/models",
        )
    if provider_id == "gemini":
        return await _check_bearer(
            fields["api_key"].value,
            "https://generativelanguage.googleapis.com/v1beta/models",
            header="x-goog-api-key",
        )
    if provider_id == "tavily":
        return await _check_tavily(fields["api_key"].value)
    return await _check_ollama(fields["base_url"].value, fields["model"].value, fields["enabled"].value == "true")


async def _check_bearer(key: str, url: str, header: str = "Authorization") -> dict[str, object]:
    if not key:
        return {"ok": False, "status": "missing"}
    headers = {"Authorization": f"Bearer {key}"} if header == "Authorization" else {header: key}
    return await _status_only("GET", url, headers=headers)


async def _check_tavily(key: str) -> dict[str, object]:
    if not key:
        return {"ok": False, "status": "missing"}
    # A one-result search is the only liveness signal Tavily exposes. The
    # body is discarded; the caller sees a status word.
    return await _status_only(
        "POST",
        "https://api.tavily.com/search",
        json_body={"api_key": key, "query": "ping", "max_results": 1},
    )


async def _check_ollama(base_url: str, model: str, enabled: bool) -> dict[str, object]:
    if not enabled and not base_url:
        return {"ok": False, "status": "disabled"}
    try:
        _validate_base_url(base_url)
    except InvalidSettingError:
        return {"ok": False, "status": "invalid"}
    url = base_url.rstrip("/") + "/api/tags"
    try:
        async with http_client() as client:
            response = await client.get(url)
    except httpx.HTTPError:
        return {"ok": False, "status": "unreachable", "enabled": enabled}
    if response.status_code != 200:
        return _from_status(response.status_code) | {"enabled": enabled}
    present: bool | None = None
    try:
        present = _model_present(response.json(), model)
    except ValueError:
        present = None
    return {"ok": True, "status": "reachable", "enabled": enabled, "model_present": present}


def _model_present(payload: object, model: str) -> bool | None:
    if not isinstance(payload, dict):
        return None
    models = payload.get("models")
    if not isinstance(models, list):
        return None
    names: list[str] = []
    for item in models:
        if isinstance(item, dict):
            name = item.get("name")
            if isinstance(name, str):
                names.append(name)
    return model in names


async def _status_only(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    json_body: dict[str, object] | None = None,
) -> dict[str, object]:
    try:
        async with http_client() as client:
            response = await client.request(method, url, headers=headers, json=json_body)
    except httpx.HTTPError:
        return {"ok": False, "status": "unreachable"}
    # Do not read response.text. A provider error body can echo the key.
    return _from_status(response.status_code)


def _from_status(code: int) -> dict[str, object]:
    if 200 <= code < 300:
        return {"ok": True, "status": "reachable"}
    if code in {401, 403}:
        return {"ok": False, "status": "auth_failed"}
    return {"ok": False, "status": "unreachable"}
