"""Startup capability registration with bounded discovery and typed local invocation.

This is not an MCP transport or an authorization layer. Adapters decide exposure.
The initial contract deliberately accepts flat scalar arguments; richer operations
need a versioned contract rather than silently bypassing argument validation.
"""
from dataclasses import dataclass
from types import MappingProxyType
import json
import re


@dataclass(frozen=True)
class VestiArgument:
    name: str
    kind: str
    description: str = ""
    required: bool = False
    default: object = None
    minimum: int | None = None
    maximum: int | None = None
    max_length: int = 4096

    def __post_init__(self):
        if (not re.fullmatch(r"[a-z][a-z0-9_]*", self.name)
                or self.kind not in ("string", "integer", "boolean")):
            raise ValueError("Invalid capability argument")
        if not self.required and self.default is not None:
            self.validate(self.default)

    def validate(self, value):
        expected = {"string": str, "integer": int, "boolean": bool}[self.kind]
        if type(value) is not expected:
            raise ValueError("Invalid type for " + self.name)
        if self.kind == "string" and len(value) > self.max_length:
            raise ValueError("Argument too long: " + self.name)
        if self.kind == "integer" and ((self.minimum is not None and value < self.minimum)
                                      or (self.maximum is not None and value > self.maximum)):
            raise ValueError("Argument outside bounds: " + self.name)
        return value

    def schema(self):
        result = {"type": self.kind, "description": self.description}
        if self.kind == "string":
            result["maxLength"] = self.max_length
        if self.kind == "integer":
            if self.minimum is not None:
                result["minimum"] = self.minimum
            if self.maximum is not None:
                result["maximum"] = self.maximum
        if not self.required and self.default is not None:
            result["default"] = self.default
        return result


@dataclass(frozen=True)
class VestiCapability:
    name: str
    summary: str
    details: str
    arguments: tuple[VestiArgument, ...] = ()
    tags: tuple[str, ...] = ()
    version: int = 1
    read_only: bool = False

    def __post_init__(self):
        object.__setattr__(self, "arguments", tuple(self.arguments))
        object.__setattr__(self, "tags", tuple(self.tags))
        if (not re.fullmatch(r"vesti_[a-z0-9_]+", self.name)
                or type(self.version) is not int or self.version != 1
                or not self.summary or type(self.read_only) is not bool
                or len({a.name for a in self.arguments}) != len(self.arguments)):
            raise ValueError("Invalid capability descriptor")

    def brief(self):
        return {"name": self.name, "summary": self.summary, "version": self.version,
                "tags": list(self.tags), "read_only": self.read_only}

    def describe(self):
        return {**self.brief(), "details": self.details, "inputSchema": {
            "type": "object", "properties": {a.name: a.schema() for a in self.arguments},
            "required": [a.name for a in self.arguments if a.required],
            "additionalProperties": False}, "outputSchema": {"type": "object"}}

    def bind(self, arguments):
        if not isinstance(arguments, dict) or set(arguments) - {a.name for a in self.arguments}:
            raise ValueError("Expected a JSON object with registered arguments only")
        result = {}
        for arg in self.arguments:
            if arg.name in arguments:
                result[arg.name] = arg.validate(arguments[arg.name])
            elif arg.required:
                raise ValueError("Missing argument: " + arg.name)
            elif arg.default is not None:
                result[arg.name] = arg.default
        return result


class VestiCapabilityRegistry:
    def __init__(self):
        self._entries = {}
        self._frozen = False

    def register(self, capability, handler):
        if (self._frozen or not isinstance(capability, VestiCapability)
                or capability.name in self._entries or not callable(handler)):
            raise ValueError("Invalid, duplicate or frozen capability registration")
        self._entries[capability.name] = (capability, handler)

    def freeze(self):
        if not self._frozen:
            self._entries = MappingProxyType(dict(self._entries))
            self._frozen = True
        return self

    def _entry(self, name):
        if not isinstance(name, str) or name not in self._entries:
            raise ValueError("Unknown capability")
        return self._entries[name]

    def search(self, query="", limit=20):
        VestiArgument("query", "string", max_length=256).validate(query)
        VestiArgument("limit", "integer", minimum=1, maximum=50).validate(limit)
        words = query.casefold().split()
        entries = [cap.brief() for _, (cap, _) in sorted(self._entries.items())
                   if all(word in " ".join((cap.name, cap.summary, *cap.tags)).casefold() for word in words)]
        return {"items": entries[:limit], "matched": len(entries), "truncated": len(entries) > limit}

    def describe(self, name):
        return self._entry(name)[0].describe()

    def invoke(self, name, arguments=None):
        cap, handler = self._entry(name)
        result = handler(**cap.bind({} if arguments is None else arguments))
        if not isinstance(result, dict):
            raise ValueError("Capability result must be a JSON object")
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Capability result exceeds 1 MiB; use bounded evidence access")
        return json.loads(encoded)

    def mcp_tools(self, names=None):
        """Project an explicit exposure profile. No server, sessions or list-change notifications."""
        selected = sorted(self._entries if names is None else names)
        result = []
        for name in selected:
            cap, _ = self._entry(name)
            detail = cap.describe()
            description = cap.summary + ("\n\n" + cap.details if cap.details else "")
            result.append({"name": name, "description": description,
                           "inputSchema": detail["inputSchema"], "outputSchema": detail["outputSchema"],
                           "annotations": {"readOnlyHint": cap.read_only}})
        return result
