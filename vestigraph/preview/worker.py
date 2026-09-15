"""Service dispatch to registered format readers and isolated renderers."""
from .budgets import DEFAULT, Budgets
from vestigraph_backends.registry import default_renderer
from ..vesti_formats.readers import vesti_run_preview, vesti_run_compare


def klayout_available():
    return default_renderer().capabilities().available


def run_preview(request: dict, budgets: Budgets = DEFAULT, *, formats=None) -> dict:
    return vesti_run_preview(request, budgets, formats=formats)


def run_diff(request: dict, budgets: Budgets = DEFAULT, *, formats=None) -> dict:
    return vesti_run_compare(request, budgets, formats=formats)
