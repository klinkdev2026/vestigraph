"""KLayout SDK parsing stays in the killable subprocess, not the service."""
from vestigraph.preview.base import Renderer, RendererCapabilities
from vestigraph.preview.budgets import SUPPORTED_FORMATS
from vestigraph_backends.vesti_backend_klayout.vesti_klayout_rendering.worker import (
    klayout_available,
    run_preview,
    run_diff,
)


class KLayoutRenderer(Renderer):
    def capabilities(self):
        available = klayout_available()
        return RendererCapabilities(available, tuple(sorted(SUPPORTED_FORMATS)),
                                    None if available else "dependency_unavailable")

    def render(self, request, budgets):
        return run_preview(request, budgets)

    def compare(self, request, budgets):
        return run_diff(request, budgets)
