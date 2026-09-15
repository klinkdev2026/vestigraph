"""On-demand images for saved files without capture-time screenshots."""
import base64
import json
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from ..presentation import validate_png, PresentationError
from ..preview.budgets import DEFAULT
from .errors import ServiceError, unavailable
from . import queries

IMAGE_BUDGETS = replace(DEFAULT, max_source_bytes=256*1024**2,
                        max_response_bytes=3*1024**2, timeout_s=30)
RENDERER_VERSION = "saved-layout-image-v1"


class SavedThumbnails:
    def __init__(self, app):
        self.app = app
        app.runner.register("saved_thumbnail", "preview", self.run)

    def paths(self, sha256):
        folder = self.app.state.cache_dir / "thumbnails" / sha256
        return folder / (RENDERER_VERSION + ".png"), folder / (RENDERER_VERSION + ".json")

    def get(self, record):
        try:
            return self._get(record)
        except (OSError, ValueError, TypeError, KeyError, PresentationError):
            return None  # Derived cache damage is a miss; the saved file remains authoritative.

    def _get(self, record):
        png_path, info_path = self.paths(record["sha256"])
        if not png_path.is_file() or not info_path.is_file():
            return None
        if png_path.stat().st_size > 2*1024**2 or info_path.stat().st_size > 65536:
            raise PresentationError("Oversized saved-layout image.")
        png = png_path.read_bytes()
        info = json.loads(info_path.read_text(encoding="utf-8"))
        validate_png(png)
        if not isinstance(info, dict):
            raise PresentationError("Invalid saved-layout image metadata.")
        import hashlib
        if info.get("raw_sha256") != record["sha256"] or info.get("sha256") != hashlib.sha256(png).hexdigest():
            raise PresentationError("Saved-layout image checksum mismatch.")
        return png, info

    def request(self, did, cid, request_key=None):
        document, store = self.app._store(did)
        record = queries.get_checkpoint(store, cid)
        fmt = record.get("format") or "GDS2"
        if not self.app.services.formats.has_reader(fmt, "thumbnail"):
            raise unavailable("thumbnail", "No image renderer is registered for this file format.")
        if record["size"] > IMAGE_BUDGETS.max_source_bytes:
            raise ServiceError("PREVIEW_TOO_LARGE", "This saved layout exceeds the image rendering limit.",
                               status=413, next_action="Open this version in the editor.")
        job, _ = self.app.runner.submit(document["project_id"], "saved_thumbnail", "checkpoint", cid,
                                        {"document_id": did}, request_key=request_key)
        return job

    def run(self, job):
        # Relocation holds this same lock; the source history cannot move during export.
        with self.app._presentation_lock:
            document, store = self.app._store(job["payload"]["document_id"])
            record = queries.get_checkpoint(store, job["target_id"])
            if self.get(record):
                return {"checkpoint_id": record["id"], "cached": True}, None
            reader = self.app.services.formats.reader(record.get("format") or "GDS2", "thumbnail")
            if reader is None:
                raise unavailable("thumbnail", "No saved-layout image renderer is available.")
            cache = self.app.state.cache_dir / "thumbnails"
            cache.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="working-", dir=cache) as temporary:
                path = store.export(record["id"], Path(temporary) / "source.bin")
                outcome = reader.render({"op": "thumbnail", "path": str(path),
                                         "format": record.get("format") or "GDS2"}, IMAGE_BUDGETS)
            if not outcome.get("ok"):
                raise ServiceError(outcome.get("code", "PREVIEW_WORKER_FAILED"),
                                   outcome.get("message", "Saved-layout image generation failed."), status=409)
            result = outcome["thumbnail"]
            png = base64.b64decode(result["png_base64"], validate=True)
            width, height = validate_png(png)
            import hashlib
            info = {k: v for k, v in result.items() if k != "png_base64"}
            info.update(source="saved_layout_render", raw_sha256=record["sha256"],
                        sha256=hashlib.sha256(png).hexdigest(), width=width, height=height,
                        generated_at=datetime.now(timezone.utc).isoformat())
            png_path, info_path = self.paths(record["sha256"])
            png_path.parent.mkdir(parents=True, exist_ok=True)
            from .application import _write_atomic
            temporary = png_path.with_suffix(".tmp")
            temporary.write_bytes(png)
            temporary.replace(png_path)
            _write_atomic(info_path, json.dumps(info, ensure_ascii=False))
            return {"checkpoint_id": record["id"], "cached": False}, None
