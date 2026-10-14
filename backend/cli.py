"""Command-line entry point for the production HTTP/WebSocket service."""

from __future__ import annotations

import argparse
import os
from importlib.metadata import PackageNotFoundError, version

from backend.config.network import NetworkSettings


def main() -> int:
    try:
        package_version = version("mpv-web-suite")
    except PackageNotFoundError:
        package_version = "0.0.1"
    parser = argparse.ArgumentParser(
        prog="mpv-webd",
        description="Run the local mpv web-control service.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"mpv-web-suite {package_version}",
    )
    parser.add_argument("--startup-idle-state", metavar="PATH",
                        help="Start idle using a private captured options/queue handoff; suppress historical recovery.")
    parser.add_argument("--startup-idle-sha256", metavar="SHA256",
                        help="Required SHA-256 binding of --startup-idle-state.")
    parser.add_argument("--startup-persisted", action="store_true",
                        help="Require current durable settings/output intent and normal media recovery.")
    arguments = parser.parse_args()
    if bool(arguments.startup_idle_state) != bool(arguments.startup_idle_sha256):
        parser.error("idle startup requires both --startup-idle-state and --startup-idle-sha256")
    if arguments.startup_persisted and arguments.startup_idle_state:
        parser.error("durable normal startup and administrative idle handoff are separate modes")

    settings = NetworkSettings.from_environment()
    raw_port = os.environ.get("MPV_WEBD_PORT", "8000")
    try:
        port = int(raw_port, 10)
    except (TypeError, ValueError) as error:
        parser.error("MPV_WEBD_PORT must be an integer from 1 through 65535")
        raise AssertionError from error
    if not 1 <= port <= 65535:
        parser.error("MPV_WEBD_PORT must be an integer from 1 through 65535")

    try:
        import uvicorn
    except ImportError as error:
        raise RuntimeError("Install mpv-web-suite with its runtime dependencies first") from error
    application = "backend.api.app:app"
    if arguments.startup_idle_state:
        from backend.api.idle_startup import IdleStartupHandoff
        from backend.api.service import PlayerService
        from backend.api.app import create_app
        handoff = IdleStartupHandoff.read(arguments.startup_idle_state, arguments.startup_idle_sha256)
        service = PlayerService.from_environment(mpv_options=handoff.mpv_options,
                                                startup_idle=True, startup_handoff=handoff)
        application = create_app(service)
    elif arguments.startup_persisted:
        from backend.api.service import PlayerService
        from backend.api.app import create_app
        application = create_app(PlayerService.from_environment(require_startup_configuration=True))
    uvicorn.run(
        application,
        host=settings.bind_host,
        port=port,
        # The service binds directly to its LAN address. Do not let Uvicorn
        # replace the ASGI peer IP with an untrusted forwarded header before
        # TrustedLanSourceMiddleware enforces the subnet boundary.
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
