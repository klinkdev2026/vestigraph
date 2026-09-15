"""Run with an installed wheel: python -I tools/check_installed.py. Synthetic and local only."""
from pathlib import Path
import hashlib
import json
import os
import sys
from importlib import metadata
from importlib.resources import files
import tempfile

import klayout.db as db
import vestigraph_scan_core
from vestigraph.store import Repository
from vestigraph.web.app import create_app
from vestigraph_backends.registry import default_registry


def _assert_installed_klink_contract():
    requirements = metadata.requires("vestigraph") or []
    normalized = [req.lower().replace(" ", "") for req in requirements]
    base_requirements = [req for req in normalized if ";" not in req or "extra==" not in req]
    assert any(req.startswith("klayout-klink") and ">=0.6.0" in req and "<0.7" in req for req in base_requirements), requirements
    assert any(req.startswith("vestigraph-scan-core") and ">=0.2" in req and "<0.3" in req for req in base_requirements), requirements

    entry_points = list(metadata.entry_points(group="klink.plugins"))
    assert any(ep.name == "vestigraph" and ep.value == "vestigraph.klink_extension:register" for ep in entry_points), entry_points

    from klink import ext
    from klink.mcp.bridge import KLinkMCPBridge

    original_env = {key: os.environ.get(key) for key in ("VESTIGRAPH_HOME", "KLINK_REGISTRY_ROOT")}
    with tempfile.TemporaryDirectory(prefix="vestigraph-installed-roots-") as temporary:
        root = Path(temporary)
        home = root / "vestigraph-home"
        klink_registry = root / "klink-registry"
        mcp_context = root / "mcp-context"
        mcp_registry = root / "mcp-registry"
        os.environ["VESTIGRAPH_HOME"] = str(home)
        os.environ["KLINK_REGISTRY_ROOT"] = str(klink_registry)
        try:
            registry = ext.discover(force=True)
            assert "vestigraph" in registry.domains
            assert "vestigraph.guide" in registry.tools
            assert not any(failure.get("package") == "vestigraph" for failure in registry.failures)
            summary = ext.status_summary()
            assert any("vestigraph.guide" in plugin["tools"] for plugin in summary["installed"]), summary

            descriptor = klink_registry / "companions" / "vestigraph.json"
            assert descriptor.is_file(), descriptor
            descriptor_data = json.loads(descriptor.read_text(encoding="utf-8"))
            assert descriptor_data["name"] == "vestigraph"
            assert descriptor_data["label"] == "HIST"
            assert descriptor_data["autostart"] is True
            assert descriptor_data["command"][0] == sys.executable
            assert descriptor_data["env"].get("VESTIGRAPH_HOME") == str(home)

            from vestigraph import klink_extension
            companion = klink_extension.companion_status()
            assert companion["registered"] is True, companion
            assert companion["descriptor"] == str(descriptor), companion
            assert companion["python"] == sys.executable, companion

            bridge = KLinkMCPBridge(context_root=mcp_context, registry_root=mcp_registry)
            assert "vestigraph.guide" in {tool["name"] for tool in bridge.list_tools()["tools"]}
        finally:
            ext.reset_for_tests()
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def check():
    _assert_installed_klink_contract()
    assert vestigraph_scan_core.__name__ == "vestigraph_scan_core"
    original_scan_backend = os.environ.pop("VESTIGRAPH_SCAN_BACKEND", None)
    try:
        from vestigraph.vesti_formats.vesti_format_gds import native as scan_backend
        cap = scan_backend.capability()
        assert cap["available"], cap
        assert scan_backend.select_backend("auto")[0] == "rust"
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
                    if extension == "gds" and record["manifest"]["format_analysis"].get("status") == "complete":
                        assert record["scan"]["backend"] == "rust", record["scan"]
                    restored = Repository(root / "history", readonly=True).export(record["id"], root / f"restored-{step}.{extension}")
                    assert restored.read_bytes() == content
                    readback = db.Layout(); readback.read(str(restored))
                    assert readback.cell("UNIT") is not None
                    result.append({"format": extension, "step": step, "sha256": hashlib.sha256(content).hexdigest()})
        print(json.dumps({"ok": True, "checks": result}))
    finally:
        if original_scan_backend is not None:
            os.environ["VESTIGRAPH_SCAN_BACKEND"] = original_scan_backend


if __name__ == "__main__":
    check()
