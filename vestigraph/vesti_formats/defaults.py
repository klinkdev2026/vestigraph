"""Default single-file providers and optional readers; imported only for composition."""


def _vesti_gds_factory():
    from .vesti_format_gds.handler import VestiGdsHandler
    return VestiGdsHandler()


def _vesti_gds_renderer_factory():
    from vestigraph_backends.vesti_backend_klayout.vesti_klayout_rendering.adapter import KLayoutRenderer
    return KLayoutRenderer()


def vesti_default_formats():
    from .registry import FormatRegistry, FormatSpec
    registry = FormatRegistry()
    registry.register(FormatSpec("GDS2", (".gds", ".gds2"), structured_changes=True), aliases=("GDS",))
    registry.register(FormatSpec("OASIS", (".oas", ".oasis")), aliases=("OAS",))
    registry.register(FormatSpec("DXF", (".dxf",)))
    registry.register(FormatSpec("TDB", (".tdb",), artifact_role="file_snapshot"))
    registry.register_storage("GDS2", _vesti_gds_factory,
                              signatures=(b"\x00\x06\x00\x02",),
                              recipe_keys=(("gds-record-cdc-v1", "gds-timestamp-zero-v1"),))
    registry.register_reader("GDS2", "preview", _vesti_gds_renderer_factory)
    registry.register_reader("GDS2", "compare", _vesti_gds_renderer_factory)
    registry.register_reader("GDS2", "thumbnail", _vesti_gds_renderer_factory)
    registry.register_reader("OASIS", "thumbnail", _vesti_gds_renderer_factory)
    return registry
