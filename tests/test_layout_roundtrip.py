"""Optional real GDS/OAS byte roundtrip, headless and synthetic only."""
import pytest

from vestigraph.store import Repository

db = pytest.importorskip("klayout.db")


@pytest.mark.parametrize("extension", ["gds", "oas"])
def test_hierarchical_layout_and_numbering_roundtrip(tmp_path, extension):
    repo = Repository.init(tmp_path / "history")
    layout = db.Layout()
    layout.dbu = 0.001
    layer = layout.layer(1, 0)
    child = layout.create_cell("UNIT")
    child.shapes(layer).insert(db.Box(0, 0, 1000, 2000))
    top = layout.create_cell("TOP")
    top.insert(db.CellInstArray(child.cell_index(), db.Trans(2000, 0)))
    source = tmp_path / ("numbering." + extension)
    layout.write(str(source))
    before = repo.checkpoint(source, title="Before numbering", metadata={"synthetic_test": True})
    before_bytes = source.read_bytes()
    for n in range(3):
        top.shapes(layer).insert(db.Text(str(n + 1), db.Trans(n * 2000, 3000)))
    layout.write(str(source))
    after = repo.checkpoint(source, title="Numbered", source="automation", metadata={"synthetic_test": True})
    after_bytes = source.read_bytes()
    first_path = repo.export(before["id"], tmp_path / ("before." + extension))
    second_path = repo.export(after["id"], tmp_path / ("after." + extension))
    assert first_path.read_bytes() == before_bytes
    assert second_path.read_bytes() == after_bytes
    restored = db.Layout()
    restored.read(str(second_path))
    assert restored.dbu == 0.001
    assert restored.cell("UNIT") is not None
    assert restored.cell("TOP").child_instances() == 1
    texts = [shape.text.string for shape in restored.cell("TOP").shapes(restored.layer(1, 0)).each() if shape.is_text()]
    assert sorted(texts) == ["1", "2", "3"]
