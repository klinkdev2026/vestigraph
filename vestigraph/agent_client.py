"""Owner-local HTTP transport. No proxies, redirects, cloud requests or token output."""
import http.cookiejar
import ipaddress
from pydantic import ValidationError
import json
import os
from pathlib import Path
from urllib import request, error, parse

from .agent_contract import bind, next_call
from .service.paths import user_home

class LocalFailure(Exception):
    def __init__(self, message, next_action):
        super().__init__(message)
        self.next_action = next_action

class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def control_path(registry_root=None):
    configured = os.environ.get("VESTIGRAPH_CONTROL_FILE")
    if configured:
        return Path(configured).expanduser()
    from vestigraph_backends.vesti_backend_klayout.companion import descriptor_path
    descriptor = descriptor_path(registry_root)
    if descriptor.is_file():
        spec = json.loads(descriptor.read_text(encoding="utf-8"))
        path = Path(spec["control_file"]).expanduser()
        if not path.is_absolute():
            raise ValueError("Control file must be an absolute local path")
        return path
    return user_home() / "state" / "control.json"

class LocalClient:
    def __init__(self, registry_root=None):
        control = json.loads(control_path(registry_root).read_text(encoding="utf-8"))
        port, secret = control.get("port"), control.get("secret")
        if type(port) is not int or not 1 <= port <= 65535 or not isinstance(secret, str) or not secret or not secret.isascii():
            raise ValueError("Invalid local service registration")
        host = control.get("host", "127.0.0.1")
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("Only loopback services are supported")
        self.origin = "http://" + ("[" + host + "]" if ":" in host else host) + ":" + str(port)
        self.opener = request.build_opener(request.ProxyHandler({}), NoRedirect(),
                                          request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        link = self.request("/api/v1/auth/issue-link", {}, {"X-Control-Secret": secret})["link"]
        parsed = parse.urlsplit(link)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.port != port:
            raise ValueError("Invalid local bootstrap origin")
        token = parse.parse_qs(parsed.fragment).get("bootstrap", [None])[0]
        if not token:
            raise ValueError("No bootstrap token")
        self.csrf = self.request("/api/v1/auth/bootstrap", {}, {"Authorization": "Bearer " + token})["csrf_token"]

    def request(self, path, body, headers=None):
        payload = json.dumps(body, allow_nan=False).encode("utf-8")
        if len(payload) > 64 * 1024:
            raise LocalFailure("The request exceeds 64 KiB.", "Shorten the draft or supporting text files and retry.")
        headers = {"Content-Type": "application/json", "Origin": self.origin, **(headers or {})}
        req = request.Request(self.origin + path, payload, headers, method="POST")
        try:
            with self.opener.open(req, timeout=30) as response:
                raw = response.read(1024 * 1024 + 1)
        except error.HTTPError as exc:
            try:
                result = json.loads(exc.read(64 * 1024))
                problem = result["error"]
                raise LocalFailure(problem["message"], problem.get("next_action") or next_call("guide")) from None
            except (ValueError, KeyError, TypeError):
                raise LocalFailure("The local service rejected the request.", "Restart the matching Vestigraph service and call vestigraph.guide with {}.") from None
        if len(raw) > 1024 * 1024:
            raise LocalFailure("The local response exceeds 1 MiB.", "Choose a shorter evidence interval.")
        return json.loads(raw)["data"]

    def invoke(self, name, arguments):
        return self.request("/api/v1/agent/invoke", {"name": name, "arguments": arguments}, {"X-CSRF-Token": self.csrf})

    def close(self):
        try:
            self.request("/api/v1/auth/logout", {}, {"X-CSRF-Token": self.csrf})
        except Exception:
            pass


def call(name, arguments, registry_root=None):
    client = None
    try:
        # Reject invalid calls before contacting the service, let alone mutating it.
        bind(name, arguments)
        client = LocalClient(registry_root)
        return client.invoke(name, arguments)
    except ValidationError as exc:
        fields = [".".join(map(str, item["loc"])) for item in exc.errors(include_input=False)]
        return {"ok": False, "problems": ["Invalid tool fields: " + ", ".join(fields)],
                "next_action": "Correct these fields using the tool inputSchema and retry."}
    except LocalFailure as exc:
        return {"ok": False, "problems": [str(exc)], "next_action": exc.next_action}
    except Exception:
        # Never echo an HTTP header, bootstrap URL, control record, or private body.
        return {"ok": False, "problems": ["The local Vestigraph call could not complete."],
                "next_action": "Check tool arguments. Restart this MCP server so Vestigraph can register its KLink companion descriptor, then open or restart KLayout with the KLink plugin and click HIST. If you use KLINK_REGISTRY_ROOT, set the same root for MCP and KLayout. Read klink.status if the HIST button or local service is still unavailable."}
    finally:
        if client is not None:
            client.close()
