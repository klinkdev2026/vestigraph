"""Loopback web authentication: one-shot bootstrap token -> HttpOnly session cookie + CSRF token.

All material lives in memory of the service process; nothing is written to
disk or logs. Tokens are compared in constant time.
"""
from __future__ import annotations

import hmac
import secrets
import threading
import time

BOOTSTRAP_TTL_S = 300.0
SESSION_TTL_S = 12 * 3600.0
COOKIE_NAME = "vestigraph_session"


def cookie_name(port) -> str:
    """Session cookie name for the service on ``port``.

    Browsers scope cookies by host, not by port, so two Vestigraph services on 8787 and 8788
    would otherwise overwrite each other's session cookie and sign each other out. The
    per-port name only keeps instances apart; it is NOT isolation -- another service on the
    same host still receives every cookie for the host (docs/THREAT_MODEL_LOCAL_WEB.md)."""
    return f"{COOKIE_NAME}_{int(port)}"


class AuthManager:
    def __init__(self, *, bootstrap_ttl=BOOTSTRAP_TTL_S, session_ttl=SESSION_TTL_S, clock=time.time):
        self.bootstrap_ttl = bootstrap_ttl
        self.session_ttl = session_ttl
        self.clock = clock
        self.lock = threading.Lock()
        self._bootstrap = {}       # token -> expires_at
        self._sessions = {}        # session id -> {"csrf": str, "expires_at": float}

    # ---------------------------------------------------------------- bootstrap --
    def issue_bootstrap(self) -> str:
        token = secrets.token_urlsafe(32)      # 256 bits
        with self.lock:
            self._bootstrap[token] = self.clock() + self.bootstrap_ttl
        return token

    def exchange(self, presented) -> dict | None:
        """Consume a bootstrap token; returns session dict or None."""
        if not isinstance(presented, str) or not presented or not presented.isascii():
            return None
        now = self.clock()
        with self.lock:
            match = None
            for token in list(self._bootstrap):
                if self._bootstrap[token] < now:
                    del self._bootstrap[token]
                    continue
                if hmac.compare_digest(token, presented):
                    match = token
            if match is None:
                return None
            del self._bootstrap[match]           # single use
            return self._create_session_locked()

    def _create_session_locked(self):
        session_id = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        expires_at = self.clock() + self.session_ttl
        self._sessions[session_id] = {"csrf": csrf, "expires_at": expires_at}
        return {"session_id": session_id, "csrf_token": csrf, "expires_at": expires_at}

    # ----------------------------------------------------------------- sessions --
    def session(self, session_id) -> dict | None:
        if not isinstance(session_id, str) or not session_id or not session_id.isascii():
            return None
        now = self.clock()
        with self.lock:
            for sid in list(self._sessions):
                if self._sessions[sid]["expires_at"] < now:
                    del self._sessions[sid]
            for sid, record in self._sessions.items():
                if hmac.compare_digest(sid, session_id):
                    return {"session_id": sid, "csrf_token": record["csrf"], "expires_at": record["expires_at"]}
        return None

    def check_csrf(self, session: dict, presented) -> bool:
        return isinstance(presented, str) and bool(presented) and presented.isascii() and hmac.compare_digest(session["csrf_token"], presented)

    def revoke(self, session_id):
        with self.lock:
            self._sessions.pop(session_id, None)
