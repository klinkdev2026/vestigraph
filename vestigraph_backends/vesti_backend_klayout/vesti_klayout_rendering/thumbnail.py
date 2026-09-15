"""Offline saved-layout image, never a screenshot of the current editor."""
import base64
import tempfile
from pathlib import Path

FORMATS = {"GDS2", "OASIS"}


def render(path, budgets):
    import klayout.lay as lay
    from .geometry import PreviewRefused
    source = Path(path)
    if source.stat().st_size > budgets.max_source_bytes:
        raise PreviewRefused("PREVIEW_TOO_LARGE", "Saved layout exceeds the image rendering limit.")
    view = lay.LayoutView()
    try:
        cv = view.load_layout(str(source), False)
        layout = view.cellview(cv).layout()
        tops = list(layout.top_cells())
        selected = max(tops, key=lambda c: c.bbox().area()) if tops else None
        if selected is not None:
            view.select_cell(selected.cell_index(), cv)
        view.add_missing_layers()
        view.max_hier()
        view.zoom_fit()
        with tempfile.TemporaryDirectory(prefix="vestigraph-thumbnail-") as folder:
            target = Path(folder) / "view.png"
            view.save_image(str(target), 1280, 800)
            if target.stat().st_size > 2 * 1024**2:
                raise PreviewRefused("PREVIEW_TOO_LARGE", "Rendered image exceeds the image size limit.")
            png = target.read_bytes()
        return {"png_base64": base64.b64encode(png).decode("ascii"),
                "top_cell": selected.name if selected else None,
                "top_cell_count": len(tops), "scope": "selected_top_cell",
                "renderer": "klayout.saved-layout-image.v1"}
    finally:
        view.destroy()
