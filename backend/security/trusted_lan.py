from __future__ import annotations

import ipaddress
import json
from typing import Any


class TrustedLanSourceMiddleware:
    """Restrict HTTP and WebSocket clients to the configured LAN source CIDR.

    The source address comes only from the ASGI server's peer address. Forwarded
    headers are deliberately ignored because this service has no trusted proxy.
    """

    def __init__(self, app: Any, *, network: ipaddress.IPv4Network) -> None:
        self.app = app
        self.network = network

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        scope_type = scope.get("type") if isinstance(scope, dict) else None
        if scope_type not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        host = client[0] if isinstance(client, (list, tuple)) and client else None
        try:
            source = ipaddress.ip_address(host) if isinstance(host, str) else None
        except ValueError:
            source = None
        if source is None or source not in self.network:
            if scope_type == "websocket":
                await send(
                    {
                        "type": "websocket.close",
                        "code": 1008,
                        "reason": "source not allowed",
                    }
                )
                return
            body = json.dumps(
                {
                    "code": "source_not_allowed",
                    "message": "client source is outside the trusted home LAN",
                },
                separators=(",", ":"),
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send(
                {"type": "http.response.body", "body": body, "more_body": False}
            )
            return

        await self.app(scope, receive, send)
