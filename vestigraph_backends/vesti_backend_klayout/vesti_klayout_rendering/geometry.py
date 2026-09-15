"""Convert a verified layout file into Preview JSON v1 within budgets.

Runs inside the worker subprocess (needs ``klayout``). Pure function of
(file, options, budgets): no service state, no network, no GUI.
"""
from __future__ import annotations
import logging

from pathlib import Path

from vestigraph.preview import RENDERER_VERSION
from vestigraph.preview.budgets import DEFAULT, Budgets


class PreviewRefused(Exception):
    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code, self.extra = code, extra


def _layer_filter(layers):
    if not layers:
        return None
    wanted = set()
    for item in layers:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            wanted.add((int(item[0]), int(item[1])))
        elif isinstance(item, str) and "/" in item:
            a, b = item.split("/", 1)
            wanted.add((int(a), int(b)))
        else:
            raise PreviewRefused("BAD_REQUEST", "layers must be [layer, datatype] pairs or 'L/D' strings.")
    return wanted


def render(path, *, top_cell=None, viewport_dbu=None, layers=None, budgets: Budgets = DEFAULT,
           checkpoint_id=None) -> dict:
    """Return Preview JSON v1 for ``path``. Raises PreviewRefused for refusals."""
    path = Path(path)
    size = path.stat().st_size
    if size > budgets.max_source_bytes:
        raise PreviewRefused("PREVIEW_TOO_LARGE", "The saved file is larger than the preview limit.",
                             size=size, limit=budgets.max_source_bytes)
    try:
        import klayout.db as db
    except ImportError as exc:  # pragma: no cover - the worker checks first
        raise PreviewRefused("PREVIEW_UNAVAILABLE", "The klayout Python module is not installed.") from exc
    layout = db.Layout()
    try:
        layout.read(str(path))
    except Exception as exc:
        raise PreviewRefused("PREVIEW_PARSE_FAILED", f"The file could not be parsed: {exc}") from exc

    tops = [layout.cell(index) for index in layout.each_top_cell()]
    if top_cell is not None:
        cell = layout.cell(str(top_cell))
        if cell is None:
            raise PreviewRefused("TOP_CELL_NOT_FOUND", "No cell with that name in this version.",
                                 candidates=[c.name for c in tops][:budgets.max_candidates])
    elif len(tops) == 1:
        cell = tops[0]
    elif not tops:
        return _empty(layout, checkpoint_id, None, "The saved version has no cells.")
    else:
        raise PreviewRefused("TOP_CELL_REQUIRED", "This version has several top cells; choose one.",
                             candidates=[c.name for c in tops][:budgets.max_candidates])

    wanted = _layer_filter(layers)
    clip = None
    if viewport_dbu is not None:
        try:
            x1, y1, x2, y2 = (int(v) for v in viewport_dbu)
        except (TypeError, ValueError):
            raise PreviewRefused("BAD_REQUEST", "viewport_dbu must be [x1, y1, x2, y2] integers.") from None
        clip = db.Box(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))

    items, warnings = [], []
    vertices = shapes = unsupported = 0
    truncated = False
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        key = (info.layer, info.datatype)
        if wanted is not None and key not in wanted:
            continue
        iterator = (cell.begin_shapes_rec_overlapping(layer_index, clip) if clip is not None
                    else cell.begin_shapes_rec(layer_index))
        iterator.max_depth = budgets.max_depth
        while not iterator.at_end():
            shapes += 1
            if shapes > budgets.max_shapes:
                truncated = True
                warnings.append({"code": "shape_budget", "limit": budgets.max_shapes})
                break
            shape, trans = iterator.shape(), iterator.trans()
            item, count = _convert(db, shape, trans, key)
            if item is None:
                unsupported += 1
            else:
                if vertices + count > budgets.max_vertices:
                    truncated = True
                    warnings.append({"code": "vertex_budget", "limit": budgets.max_vertices})
                    break
                vertices += count
                items.append(item)
            iterator.next()
        if truncated:
            break
    try:
        if cell.hierarchy_levels() > budgets.max_depth:
            truncated = True
            warnings.append({"code": "depth_budget", "limit": budgets.max_depth,
                             "note": "cells deeper than the limit were not drawn"})
    except Exception:  # noqa: BLE001 - a diagnostic must not break rendering
        logging.getLogger(__name__).warning("Nonfatal callback or cleanup failure", exc_info=True)
    if unsupported:
        warnings.append({"code": "unsupported_shapes", "count": unsupported,
                         "note": "text/edge shapes are not drawn"})
    bbox = cell.bbox()
    out = {
        "format_version": 1, "renderer_version": RENDERER_VERSION, "checkpoint_id": checkpoint_id,
        "units": "dbu", "dbu_um": layout.dbu, "top_cell": cell.name,
        "bbox_dbu": None if bbox.empty() else [bbox.left, bbox.bottom, bbox.right, bbox.top],
        "viewport_dbu": [clip.left, clip.bottom, clip.right, clip.top] if clip is not None else None,
        "completeness": "partial" if (truncated or unsupported) else "complete",
        "warnings": warnings, "items": items, "truncated": truncated,
        "counts": {"items": len(items), "vertices": vertices, "shapes_visited": shapes},
        "top_cells": [c.name for c in tops][:budgets.max_candidates],
    }
    return out


def _empty(layout, checkpoint_id, name, note):
    return {"format_version": 1, "renderer_version": RENDERER_VERSION, "checkpoint_id": checkpoint_id,
            "units": "dbu", "dbu_um": layout.dbu, "top_cell": name, "bbox_dbu": None, "viewport_dbu": None,
            "completeness": "complete", "warnings": [{"code": "empty", "note": note}], "items": [],
            "truncated": False, "counts": {"items": 0, "vertices": 0, "shapes_visited": 0}, "top_cells": []}


def _convert(db, shape, trans, key):
    """Return (item, vertex_count) or (None, 0) for unsupported shape kinds."""
    layer, datatype = key
    if shape.is_box():
        if trans.is_ortho() and trans.is_unity() or (trans.is_ortho() and trans.mag == 1.0):
            box = shape.box.transformed(trans)
            return {"kind": "box", "layer": layer, "datatype": datatype,
                    "bbox_dbu": [box.left, box.bottom, box.right, box.top]}, 4
        polygon = db.Polygon(shape.box).transformed(trans)
        return _polygon_item(polygon, layer, datatype)
    if shape.is_path():
        path = shape.path.transformed(trans)
        points = [[p.x, p.y] for p in path.each_point()]
        return {"kind": "path", "layer": layer, "datatype": datatype, "points_dbu": points,
                "width_dbu": path.width, "begin_ext_dbu": path.bgn_ext, "end_ext_dbu": path.end_ext,
                "round_ends": bool(path.round)}, len(points)
    if shape.is_polygon():
        return _polygon_item(shape.polygon.transformed(trans), layer, datatype)
    return None, 0


def _polygon_item(polygon, layer, datatype):
    hull = [[p.x, p.y] for p in polygon.each_point_hull()]
    holes = []
    for index in range(polygon.holes()):
        holes.append([[p.x, p.y] for p in polygon.each_point_hole(index)])
    count = len(hull) + sum(len(h) for h in holes)
    return {"kind": "polygon", "layer": layer, "datatype": datatype, "hull_dbu": hull, "holes_dbu": holes}, count
