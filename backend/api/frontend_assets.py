from __future__ import annotations

import json
import re
import sysconfig
from pathlib import Path, PurePosixPath
from typing import Any

_BUILD_INFO_NAME = "build-info.json"
_BUILD_ID_RE = re.compile(r"^[a-f0-9]{64}$")
_HASHED_ASSET_RE = re.compile(r"(?:^|/)[^/]+-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$")
_NO_STORE_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _read_build_info(root: Path) -> dict[str, int | str] | None:
    try:
        payload = json.loads((root / "assets" / _BUILD_INFO_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "client_protocol_version",
        "build_id",
    }:
        return None
    schema_version = payload.get("schema_version")
    protocol_version = payload.get("client_protocol_version")
    build_id = payload.get("build_id")
    if (
        type(schema_version) is not int
        or schema_version != 1
        or type(protocol_version) is not int
        or protocol_version < 1
        or not isinstance(build_id, str)
        or _BUILD_ID_RE.fullmatch(build_id) is None
    ):
        return None
    return {
        "schema_version": schema_version,
        "client_protocol_version": protocol_version,
        "build_id": build_id,
    }


def _cache_controlled_static_files(static_files_type: type, directory: str) -> Any:
    """Set long-lived caching only for content-hashed production assets."""

    class CacheControlledStaticFiles(static_files_type):
        async def get_response(self, path: str, scope: Any) -> Any:
            response = await super().get_response(path, scope)
            if response.status_code == 200:
                if path == _BUILD_INFO_NAME:
                    response.headers.update(_NO_STORE_HEADERS)
                elif _HASHED_ASSET_RE.search(path):
                    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
                else:
                    response.headers["Cache-Control"] = "no-cache"
            return response

    return CacheControlledStaticFiles(directory=directory)


def frontend_distribution_root() -> Path | None:
    """Find the built UI in a checkout or an installed package data directory."""

    checkout_root = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    installed_root = (
        Path(sysconfig.get_path("data"))
        / "share"
        / "mpv-web-suite"
        / "frontend"
    )
    for candidate in (checkout_root, installed_root):
        try:
            root = candidate.resolve(strict=True)
        except OSError:
            continue
        if (root / "index.html").is_file() and (root / "assets").is_dir():
            return root
    return None


def install_frontend_routes(app: Any) -> None:
    """Serve the built single-page UI without swallowing API or asset 404s."""

    root = frontend_distribution_root()
    # Import only when an API app is actually created. The control tooling can
    # still import this module in dependency-light environments.
    from fastapi import HTTPException
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    @app.get("/_ui/version", include_in_schema=False)
    def frontend_version() -> JSONResponse:
        current_root = frontend_distribution_root()
        build_info = _read_build_info(current_root) if current_root is not None else None
        if build_info is None:
            raise HTTPException(status_code=503, detail="frontend build metadata unavailable")
        return JSONResponse(content=build_info, headers=_NO_STORE_HEADERS)

    if root is None:
        return

    index = root / "index.html"
    app.mount(
        "/assets",
        _cache_controlled_static_files(StaticFiles, directory=str(root / "assets")),
        name="frontend-assets",
    )

    @app.get("/", include_in_schema=False)
    def frontend_index() -> FileResponse:
        if _read_build_info(root) is None:
            raise HTTPException(status_code=503, detail="frontend build metadata unavailable")
        return FileResponse(index, headers=_NO_STORE_HEADERS)

    @app.get("/{ui_path:path}", include_in_schema=False)
    def frontend_route(ui_path: str) -> FileResponse:
        parts = PurePosixPath(ui_path).parts
        if (
            not parts
            or parts[0] in {"api", "assets"}
            or any(part in {".", ".."} or part.startswith(".") for part in parts)
            or "." in parts[-1]
        ):
            raise HTTPException(status_code=404)
        if _read_build_info(root) is None:
            raise HTTPException(status_code=503, detail="frontend build metadata unavailable")
        return FileResponse(index, headers=_NO_STORE_HEADERS)
