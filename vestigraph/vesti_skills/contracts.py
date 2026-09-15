"""Replaceable skill stages. No editor, model vendor, or agent file format here."""
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

API_VERSION = 1

class EvidenceSource(Protocol):
    id: str
    api_version: int
    def collect(self, app, document, selection: dict) -> dict: ...

class Generator(Protocol):
    id: str
    api_version: int
    def generate(self, evidence: dict, intent: dict) -> dict: ...

class Validator(Protocol):
    id: str
    api_version: int
    def validate(self, skill: dict) -> dict: ...

class Exporter(Protocol):
    id: str
    api_version: int
    def export(self, skill: dict) -> tuple[bytes, str, str]: ...


def providers(values, method):
    result = {}
    for provider in values:
        if (type(getattr(provider, "api_version", None)) is not int
                or provider.api_version != API_VERSION
                or not isinstance(getattr(provider, "id", None), str)
                or not provider.id or provider.id in result
                or not callable(getattr(provider, method, None))):
            raise ValueError("Invalid or duplicate skill provider: " + method)
        result[provider.id] = provider
    return MappingProxyType(result)


@dataclass(frozen=True)
class SkillServices:
    sources: tuple = ()
    generators: tuple = ()
    validators: tuple = ()
    exporters: tuple = ()

    def __post_init__(self):
        for name, method in (("sources", "collect"), ("generators", "generate"),
                             ("validators", "validate"), ("exporters", "export")):
            object.__setattr__(self, name, providers(getattr(self, name), method))

    def describe(self):
        return {name: [{"id": p.id, "label": getattr(p, "label", p.id)}
                       for p in getattr(self, name).values()]
                for name in ("sources", "generators", "validators", "exporters")}


def default_services():
    from .evidence import HistoryWindow
    from .draft import EvidenceDraft, ContractValidator
    from .exporters import MarkdownSkill, JsonSkill
    return SkillServices(sources=(HistoryWindow(),), generators=(EvidenceDraft(),),
                         validators=(ContractValidator(),), exporters=(MarkdownSkill(), JsonSkill()))
