"""Cell-level comparison of two saved layout versions (Cell Diff JSON v1).

Runs inside the worker subprocess (needs ``klayout``). Answers "what changed":
which cells were added/removed/changed, which shapes and instances inside a
changed cell, and how the hierarchy (parent -> child references) moved.
It is a GEOMETRY diff on expanded content, not a statement of intent.

Renames are never asserted: a removed cell whose content fingerprint equals an
added cell's is reported under ``renamed`` with ``inferred: true``.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

from vestigraph.preview import RENDERER_VERSION
from vestigraph.preview.budgets import DEFAULT, Budgets
from vestigraph_backends.vesti_backend_klayout.vesti_klayout_rendering.geometry import PreviewRefused

DIFF_VERSION = 1
MAX_EXAMPLES = 20


def _load(db, path, budgets):
    path = Path(path)
    size = path.stat().st_size
    if size > budgets.max_source_bytes:
        raise PreviewRefused("PREVIEW_TOO_LARGE", "A compared file is larger than the preview limit.",
                             size=size, limit=budgets.max_source_bytes)
    layout = db.Layout()
    try:
        layout.read(str(path))
    except Exception as exc:
        raise PreviewRefused("PREVIEW_PARSE_FAILED", f"A compared file could not be parsed: {exc}") from exc
    return layout


INT32_MAX = 2 ** 31 - 1


def _unit_factors(db, before, after):
    """Integer factors that bring both layouts to their COMMON finest database unit.

    Two files with different DBU but the same integer coordinates describe different
    physical geometry, so coordinates must be compared in one unit. That unit is the finer
    of the two DBUs, and each layout is scaled by an exact integer -- never rounded into
    nanometres, which collapsed distinct sub-nanometre coordinates onto one key. A DBU
    ratio that is not an integer, or a scaled coordinate outside 32-bit range, is refused
    rather than compared approximately.
    """
    unit = min(before.dbu, after.dbu)
    factors = []
    for layout in (before, after):
        ratio = layout.dbu / unit
        factor = int(round(ratio))
        if factor < 1 or abs(ratio - factor) > 1e-6 * factor:
            raise PreviewRefused("PREVIEW_DBU_INCOMPATIBLE",
                                 "The two versions use database units that are not integer multiples "
                                 "of each other; they cannot be compared exactly.",
                                 dbu_before_um=before.dbu, dbu_after_um=after.dbu)
        if factor == 1:
            factors.append(None)
            continue
        extent = 0
        for index in layout.each_top_cell():
            box = layout.cell(index).bbox()
            if not box.empty():
                extent = max(extent, abs(box.left), abs(box.bottom), abs(box.right), abs(box.top))
        if extent * factor > INT32_MAX:
            raise PreviewRefused("PREVIEW_DBU_OVERFLOW",
                                 "Scaling the coarser version to the finer database unit exceeds the "
                                 "32-bit coordinate range; the versions cannot be compared exactly.",
                                 dbu_before_um=before.dbu, dbu_after_um=after.dbu, factor=factor)
        factors.append(factor)
    return factors


def _scaled_vector(db, vector, factor):
    return db.Vector(vector.x * factor, vector.y * factor)


def _shape_key(shape, scale):
    """Canonical text for a shape's geometry in the common unit (``scale`` = ICplxTrans or None)."""
    if shape.is_box():
        box = shape.box if scale is None else shape.box.transformed(scale)
        return "B" + str(box)
    if shape.is_path():
        path = shape.path if scale is None else shape.path.transformed(scale)
        return "P" + str(path)
    if shape.is_polygon():
        polygon = shape.polygon if scale is None else shape.polygon.transformed(scale)
        return "G" + str(polygon)
    if shape.is_text():
        text = shape.text if scale is None else shape.text.transformed(scale)
        return "T" + str(text)
    return "O" + str(shape)


def _instance_key(db, layout, inst, factor):
    """(child name, transform text) with the displacement in the common unit.

    Rebuilt with KEYWORD arguments: ``Trans(rot, Vector)`` binds the vector to the
    ``mirrx`` parameter (mirror always on, displacement lost), which silently hid every
    instance move and mirror whenever the layouts had to be rescaled.
    """
    child = layout.cell(inst.cell_index).name
    if inst.is_complex():
        trans = inst.cplx_trans
        if factor is not None:   # displacement into the common unit; rotation/mirror/mag unchanged
            trans = db.ICplxTrans(trans.mag, trans.angle, trans.is_mirror(), _scaled_vector(db, trans.disp, factor))
    else:
        trans = inst.trans
        if factor is not None:
            trans = db.Trans(trans.angle, trans.is_mirror(), _scaled_vector(db, trans.disp, factor))
    key = str(trans)
    if inst.is_regular_array():
        a, b = (inst.a, inst.b) if factor is None else (_scaled_vector(db, inst.a, factor),
                                                        _scaled_vector(db, inst.b, factor))
        key += f"|{a}|{b}|{inst.na}x{inst.nb}"
    return child, key


def _cell_model(db, layout, cell, budgets, counters, factor):
    """Per-cell content: shapes per layer (as multisets of keys) and instances."""
    scale = None if factor is None else db.ICplxTrans(float(factor))
    layers = {}
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        keys = Counter()
        for shape in cell.shapes(layer_index).each():
            counters["shapes"] += 1
            if counters["shapes"] > budgets.max_diff_shapes:
                counters["truncated"] = True
                break
            keys[_shape_key(shape, scale)] += 1
        if keys:
            layers[f"{info.layer}/{info.datatype}"] = keys
    instances = Counter()
    for inst in cell.each_inst():
        counters["instances"] += 1
        if counters["instances"] > budgets.max_diff_instances:
            counters["truncated"] = True
            break
        instances[_instance_key(db, layout, inst, factor)] += 1
    box = cell.bbox()
    return {"layers": layers, "instances": instances,
            "bbox": None if box.empty() else [box.left, box.bottom, box.right, box.top]}


def _fingerprint(model, child_fingerprints):
    digest = hashlib.sha256()
    for layer in sorted(model["layers"]):
        digest.update(layer.encode())
        for key in sorted(model["layers"][layer]):
            digest.update(f"{key}x{model['layers'][layer][key]};".encode())
    for (child, trans), count in sorted(model["instances"].items()):
        digest.update(f"I{child_fingerprints.get(child, child)}@{trans}x{count};".encode())
    return digest.hexdigest()


def _models(db, layout, budgets, counters, factor):
    """Cells by name with content models; fingerprints computed bottom-up."""
    models = {}
    order = []
    for index in layout.each_cell_bottom_up():
        cell = layout.cell(index)
        if len(models) >= budgets.max_diff_cells:
            counters["truncated"] = True
            break
        models[cell.name] = _cell_model(db, layout, cell, budgets, counters, factor)
        order.append(cell.name)
    fingerprints = {}
    for name in order:                      # bottom-up: children first
        fingerprints[name] = _fingerprint(models[name], fingerprints)
    return models, fingerprints


def _layer_diff(before, after):
    out = []
    for layer in sorted(set(before) | set(after)):
        a, b = before.get(layer, Counter()), after.get(layer, Counter())
        added = sum((b - a).values())
        removed = sum((a - b).values())
        if added or removed:
            out.append({"layer": int(layer.split("/")[0]), "datatype": int(layer.split("/")[1]),
                        "added": added, "removed": removed, "before": sum(a.values()), "after": sum(b.values())})
    return out


def _instance_diff(before, after):
    """Multiset difference; same child gone here / appearing there pairs up as "moved" BY COUNT.

    3 instances at P and 1 at Q afterwards = 1 moved + 2 removed, never "1 moved".
    """
    added_c, removed_c = Counter(after - before), Counter(before - after)
    moved, moved_total = [], 0
    for (child, old_trans), old_n in sorted(removed_c.items()):
        if old_n <= 0:
            continue
        for (new_child, new_trans), new_n in sorted(added_c.items()):
            if new_child != child or new_n <= 0 or old_n <= 0:
                continue
            n = min(old_n, new_n)
            moved.append({"child": child, "from": old_trans, "to": new_trans, "count": n})
            moved_total += n
            removed_c[(child, old_trans)] -= n
            added_c[(new_child, new_trans)] -= n
            old_n -= n
    remaining_added = [{"child": c, "trans": t, "count": n} for (c, t), n in sorted(added_c.items()) if n > 0]
    remaining_removed = [{"child": c, "trans": t, "count": n} for (c, t), n in sorted(removed_c.items()) if n > 0]
    return {"added": remaining_added[:MAX_EXAMPLES], "removed": remaining_removed[:MAX_EXAMPLES],
            "moved": moved[:MAX_EXAMPLES],
            "counts": {"added": sum(i["count"] for i in remaining_added),
                       "removed": sum(i["count"] for i in remaining_removed), "moved": moved_total}}


def _edges(models):
    edges = set()
    for parent, model in models.items():
        for (child, _), _ in model["instances"].items():
            edges.add((parent, child))
    return edges


def diff(path_before, path_after, *, budgets: Budgets = DEFAULT, from_id=None, to_id=None) -> dict:
    try:
        import klayout.db as db
    except ImportError as exc:  # pragma: no cover
        raise PreviewRefused("PREVIEW_UNAVAILABLE", "The klayout Python module is not installed.") from exc
    counters_before = {"shapes": 0, "instances": 0, "truncated": False}
    counters_after = {"shapes": 0, "instances": 0, "truncated": False}
    before_layout = _load(db, path_before, budgets)
    after_layout = _load(db, path_after, budgets)
    factor_before, factor_after = _unit_factors(db, before_layout, after_layout)
    before, fp_before = _models(db, before_layout, budgets, counters_before, factor_before)
    after, fp_after = _models(db, after_layout, budgets, counters_after, factor_after)
    counters = {"shapes": counters_before["shapes"] + counters_after["shapes"],
                "instances": counters_before["instances"] + counters_after["instances"],
                "truncated": counters_before["truncated"] or counters_after["truncated"]}

    if counters["truncated"]:
        # Partial inventories cannot establish absence or equal fingerprints, even
        # for a parent whose own shapes were scanned. Suppress unproven changes.
        before, after, fp_before, fp_after = {}, {}, {}, {}
    names_before, names_after = set(before), set(after)
    added = sorted(names_after - names_before)
    removed = sorted(names_before - names_after)
    common = sorted(names_before & names_after)
    changed, unchanged = [], []
    for name in common:
        if fp_before[name] == fp_after[name]:
            unchanged.append(name)
            continue
        own_layers = _layer_diff(before[name]["layers"], after[name]["layers"])
        own_instances = _instance_diff(before[name]["instances"], after[name]["instances"])
        own_changed = bool(own_layers) or any(own_instances["counts"].values())
        changed.append({
            "name": name, "layers": own_layers, "instances": own_instances,
            "bbox_before": before[name]["bbox"], "bbox_after": after[name]["bbox"],
            # A cell can change only because a child changed; say so instead of blaming its own content.
            "own_content_changed": own_changed,
            "children_changed": not own_changed,
        })
    # Rename inference by identical fingerprint (content-equal), never asserted.
    by_fp_added = {fp_after[n]: n for n in added}
    renamed = []
    for name in removed:
        target = by_fp_added.get(fp_before[name])
        if target is not None:
            renamed.append({"from": name, "to": target, "inferred": True})
    renamed_from = {r["from"] for r in renamed}
    renamed_to = {r["to"] for r in renamed}

    edges_before, edges_after = _edges(before), _edges(after)
    edges_added = sorted(edges_after - edges_before)
    edges_removed = sorted(edges_before - edges_after)
    parents_before, parents_after = {}, {}
    for p, c in edges_before:
        parents_before.setdefault(c, set()).add(p)
    for p, c in edges_after:
        parents_after.setdefault(c, set()).add(p)
    reparented = []
    for child in sorted(set(parents_before) & set(parents_after)):
        if parents_before[child] != parents_after[child]:
            reparented.append({"child": child, "from_parents": sorted(parents_before[child]),
                               "to_parents": sorted(parents_after[child])})
    tops_before = sorted(before_layout.cell(i).name for i in before_layout.each_top_cell())
    tops_after = sorted(after_layout.cell(i).name for i in after_layout.each_top_cell())

    def total(items, key):
        return sum(c["instances"]["counts"][key] for c in items)

    summary = {
        "cells_added": len([n for n in added if n not in renamed_to]),
        "cells_removed": len([n for n in removed if n not in renamed_from]),
        "cells_renamed": len(renamed),
        "cells_changed": len([c for c in changed if c["own_content_changed"]]),
        "cells_changed_via_children": len([c for c in changed if c["children_changed"]]),
        "cells_unchanged": len(unchanged),
        "shapes_added": sum(l["added"] for c in changed for l in c["layers"]),
        "shapes_removed": sum(l["removed"] for c in changed for l in c["layers"]),
        "instances_added": total(changed, "added"),
        "instances_removed": total(changed, "removed"),
        "instances_moved": total(changed, "moved"),
        "hierarchy_edges_added": len(edges_added),
        "hierarchy_edges_removed": len(edges_removed),
        "top_cells_before": tops_before, "top_cells_after": tops_after,
        "dbu_before_um": before_layout.dbu, "dbu_after_um": after_layout.dbu,
        "units_changed": abs(before_layout.dbu - after_layout.dbu) > 1e-15,
        # A comparison that stopped at its budget has NOT seen everything: it can report
        # differences it found, but it can never vouch for the two versions being identical.
        "identical": not counters["truncated"] and not (
            added or removed or changed or renamed or edges_added or edges_removed
            or reparented or abs(before_layout.dbu - after_layout.dbu) > 1e-15),
        "comparison_complete": not counters["truncated"],
    }
    warnings = []
    if counters["truncated"]:
        warnings.append({"code": "diff_budget", "note": "comparison stopped at the budget; change assertions are withheld; zero counts do not mean equality"})
    return {
        "format_version": DIFF_VERSION, "renderer_version": RENDERER_VERSION,
        "from_checkpoint_id": from_id, "to_checkpoint_id": to_id,
        "dbu_um": after_layout.dbu, "units": "dbu",
        "summary": summary,
        "cells": {"added": [n for n in added if n not in renamed_to][:budgets.max_candidates * 10],
                  "removed": [n for n in removed if n not in renamed_from][:budgets.max_candidates * 10],
                  "renamed": renamed[:MAX_EXAMPLES], "changed": changed[:budgets.max_candidates * 10]},
        "hierarchy": {"edges_added": [list(e) for e in edges_added[:200]],
                      "edges_removed": [list(e) for e in edges_removed[:200]],
                      "reparented": reparented[:MAX_EXAMPLES]},
        "highlight_bboxes_dbu": [c["bbox_after"] for c in changed if c["bbox_after"]][:200]
                                 + [after[n]["bbox"] for n in added if after[n]["bbox"]][:200],
        "completeness": "partial" if counters["truncated"] else "complete",
        "warnings": warnings, "truncated": counters["truncated"],
    }
