"""Run with an installed wheel: python -I tools/check_installed.py. Synthetic and local only."""
from pathlib import Path
import hashlib
import json
from importlib.resources import files
import tempfile

import klayout.db as db
from vestigraph.store import Repository
from vestigraph.web.app import create_app
from vestigraph_backends.registry import default_registry


def check():
    assert files("vestigraph.web").joinpath("static/index.html").is_file()
    assert default_registry().backend_ids == ("klayout",)
    result = []
    with tempfile.TemporaryDirectory(prefix="vestigraph-wheel-") as temporary:
        root = Path(temporary)
        repository = Repository.init(root / "history")
        for extension in ("gds", "oas"):
            layout = db.Layout()
            layout.dbu = 0.001
            layer = layout.layer(1, 0)
            unit = layout.create_cell("UNIT")
            unit.shapes(layer).insert(db.Box(0, 0, 1000, 2000))
            top = layout.create_cell("TOP")
            top.insert(db.CellInstArray(unit.cell_index(), db.ICplxTrans(2, 45, False, 3000, 0)))
            path = root / ("sample." + extension)
            for step in range(2):
                top.shapes(layer).insert(db.Text(str(step), db.Trans(step * 2000, 0)))
                layout.write(str(path))
                content = path.read_bytes()
                record = repository.checkpoint(path, title=str(step))
                restored = Repository(root / "history", readonly=True).export(record["id"], root / f"restored-{step}.{extension}")
                assert restored.read_bytes() == content
                readback = db.Layout(); readback.read(str(restored))
                assert readback.cell("UNIT") is not None
                result.append({"format": extension, "step": step, "sha256": hashlib.sha256(content).hexdigest()})
    print(json.dumps({"ok": True, "checks": result}))


if __name__ == "__main__":
    check()
