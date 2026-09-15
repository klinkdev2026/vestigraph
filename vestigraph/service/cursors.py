"""Opaque pagination cursors bound to a query scope.

A cursor carries the snapshot upper bound and the last key of the previous
page, plus the scope (document/project, list kind, filters) it was issued for.
Using it with a different scope or filter is rejected instead of silently
returning another list.
"""
from __future__ import annotations

import base64
import json

from .errors import bad_request

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def encode(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode(token, scope: dict) -> dict:
    """Return the cursor payload after checking it matches ``scope`` exactly."""
    if token is None or token == "":
        return {}
    if not isinstance(token, str) or len(token) > 2048:
        raise bad_request("Cursor is malformed.", "Reload the list without a cursor.")
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise bad_request("Cursor is malformed.", "Reload the list without a cursor.") from None
    if not isinstance(payload, dict) or payload.get("scope") != scope:
        raise ServiceCursorMismatch()
    for key in ("upper", "before"):
        value = payload.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise bad_request("Cursor is malformed.", "Reload the list without a cursor.")
    return payload


def limit_of(value) -> int:
    if value is None:
        return DEFAULT_LIMIT
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_LIMIT:
        raise bad_request(f"limit must be an integer between 1 and {MAX_LIMIT}.")
    return value


def page(scope: dict, result: dict, items) -> dict:
    """Build the list envelope from a store page result."""
    next_cursor = None
    if result.get("next_before") is not None:
        next_cursor = encode({"scope": scope, "upper": result["upper"], "before": result["next_before"]})
    return {"items": list(items), "next_cursor": next_cursor,
            "window_id": encode({"scope": scope, "upper": result["upper"]})}


class ServiceCursorMismatch(Exception):
    """Raised by decode(); converted to a CURSOR_SCOPE_MISMATCH ServiceError by callers."""


def scope_error():
    from .errors import ServiceError
    return ServiceError("CURSOR_SCOPE_MISMATCH",
                        "Cursor belongs to a different list, filter or document.",
                        status=400, next_action="Reload the list without a cursor.")
