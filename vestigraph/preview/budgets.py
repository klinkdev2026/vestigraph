"""Refusal/degradation thresholds for previews. These are limits, not performance promises.

Parser memory still depends on the input's structure (deep hierarchies,
huge arrays); the subprocess timeout is the backstop.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Budgets:
    max_source_bytes: int = 16 * 1024 * 1024
    max_vertices: int = 100_000
    max_shapes: int = 10_000            # shapes visited (instances expanded), a proxy for instance traversal
    max_depth: int = 64
    # Cell-level comparison walks the hierarchy once (no instance expansion), so it can afford more.
    max_diff_shapes: int = 200_000        # shapes visited per side
    max_diff_instances: int = 50_000      # instances visited per side
    max_diff_cells: int = 20_000          # cells per side
    max_response_bytes: int = 2 * 1024 * 1024
    timeout_s: float = 10.0
    max_candidates: int = 20

    def to_dict(self):
        return asdict(self)


DEFAULT = Budgets()
SUPPORTED_FORMATS = {"GDS2"}     # OASIS: no verified parser memory bound yet -> unsupported (metadata/export only)
