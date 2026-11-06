"""Process entry point: `python -m app`.

uvicorn's own default host is 127.0.0.1, but a copied unit file that passes
`--host 0.0.0.0` is how a personal assistant ends up on the LAN. This launcher
binds `JARVIS_HOST` (default 127.0.0.1) and refuses to start unless the
passcode and bind policy in `enforce_startup_policy` both pass. The lifespan
hook checks again, so `uvicorn app.main:app --host ...` cannot skip it.
"""

from __future__ import annotations

import logging

import uvicorn

from app.core.config import get_settings
from app.core.security import (
    StartupRefused,
    effective_bind_host,
    enforce_startup_policy,
)


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    )
    host = effective_bind_host(settings)
    try:
        enforce_startup_policy(settings, bind_host=host)
    except StartupRefused as exc:
        logging.getLogger("jarvis").error("%s", exc)
        raise SystemExit(1) from exc
    uvicorn.run(
        "app.main:app",
        host=host,
        port=settings.port,
        log_level="debug" if settings.debug else "info",
    )


if __name__ == "__main__":
    main()
