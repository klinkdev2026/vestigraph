"""Read-only KLayout session discovery through klink's session registry.

The registry is klink's: each plugin-enabled KLayout writes a heartbeat JSON.
We only read it. The registry object is injectable so tests use a fake.
"""
from __future__ import annotations

import ipaddress


def default_registry():
    try:
        from klink.mcp.session_registry import SessionRegistry
    except ImportError:
        return None
    return SessionRegistry()


def _loopback(host) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        return False


def candidates(registry, *, include_stale=False) -> list:
    """Online, loopback sessions with a numeric port, normalized for the service."""
    if registry is None:
        return []
    out = []
    for record in registry.list_sessions(include_stale=include_stale):
        if not isinstance(record, dict):
            continue
        port, host = record.get("rpc_port"), record.get("host") or "127.0.0.1"
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            continue
        if not _loopback(host):
            continue
        out.append({
            "session_id": str(record.get("session_id") or f"klayout-{port}"),
            "host": "127.0.0.1" if host == "localhost" else str(host),
            "port": port,
            "pid": record.get("pid"),
            "layout_path": record.get("layout_path"),
            "active_cell": record.get("active_cell"),
            "age_s": record.get("age_s"),
            "stale": bool(record.get("stale", False)),
            "loopback": True,
        })
    return out


def session_instance(candidate: dict) -> dict:
    """What we can observe about process identity; never treat the port as continuity."""
    return {"session_id": candidate["session_id"], "host": candidate["host"],
            "port": candidate["port"], "pid": candidate.get("pid")}
