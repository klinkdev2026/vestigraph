"""Stable, language-neutral service errors.

Every error carries a machine ``code`` (drives clients and translations), an
HTTP ``status`` for the web adapter, a readable English ``message`` for CLI and
diagnostics, and translation keys so the browser can localize without parsing
the message text.
"""
from __future__ import annotations


class ServiceError(Exception):
    def __init__(self, code, message, *, status=400, next_action="", retryable=False,
                 message_key=None, next_action_key=None, message_args=None, details=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        self.next_action = next_action
        self.retryable = retryable
        self.message_key = message_key or f"errors.{code.lower()}"
        self.next_action_key = next_action_key or f"errors.{code.lower()}.next"
        self.message_args = dict(message_args or {})
        self.details = details

    def to_dict(self):
        out = {
            "code": self.code, "message": self.message, "retryable": self.retryable,
            "next_action": self.next_action, "message_key": self.message_key,
            "next_action_key": self.next_action_key, "message_args": self.message_args,
        }
        if self.details is not None:
            out["details"] = self.details
        return out


def bad_request(message, next_action="Correct the request and retry.", **kw):
    return ServiceError("BAD_REQUEST", message, status=400, next_action=next_action, **kw)


def unauthorized(message="Sign in first.", **kw):
    return ServiceError("UNAUTHENTICATED", message, status=401,
                        next_action="Open the bootstrap link printed by the service.", **kw)


def forbidden(message, next_action="This action is not allowed for the current session.", **kw):
    return ServiceError("FORBIDDEN", message, status=403, next_action=next_action, **kw)


def not_found(kind, next_action="Refresh the list and pick an existing item.", **kw):
    return ServiceError("NOT_FOUND", f"{kind} not found in the current scope.", status=404,
                        next_action=next_action, message_args={"kind": kind}, **kw)


def conflict(code, message, next_action="", **kw):
    return ServiceError(code, message, status=409, next_action=next_action, **kw)


def unavailable(capability, message=None, next_action=None, **kw):
    return ServiceError(
        "CAPABILITY_UNAVAILABLE",
        message or f"Capability '{capability}' is not available in this service build.",
        status=503, retryable=False,
        next_action=next_action or "Install the optional dependency named in details, then restart the service.",
        message_args={"capability": capability}, **kw)


def too_large(message, next_action="Use a smaller request.", **kw):
    return ServiceError("PAYLOAD_TOO_LARGE", message, status=413, next_action=next_action, **kw)


def queue_full(message="The background queue is full.", **kw):
    return ServiceError("QUEUE_FULL", message, status=429, retryable=True,
                        next_action="Wait for running tasks to finish, then retry.", **kw)
