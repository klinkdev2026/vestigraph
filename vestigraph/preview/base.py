"""Offline renderer contract, independent of an active editor or vendor SDK.

Implementations must honour budgets and use the existing killable worker for
vendor parsing. B1 establishes the contract; production worker migration is B3.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from .budgets import Budgets


@dataclass(frozen=True)
class RendererCapabilities:
    available: bool
    formats: tuple[str, ...]
    reason_code: str | None = None


class Renderer(ABC):
    api_version = 1

    @abstractmethod
    def capabilities(self) -> RendererCapabilities: ...

    @abstractmethod
    def render(self, request: dict, budgets: Budgets) -> dict: ...

    @abstractmethod
    def compare(self, request: dict, budgets: Budgets) -> dict: ...

