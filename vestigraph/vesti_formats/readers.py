"""Format-selected offline readers; implementations own optional SDKs and worker isolation."""
from .registry import FORMATS

def vesti_run_preview(request, budgets, *, formats=None):
    formats = formats if formats is not None else FORMATS.copy(frozen=True)
    fmt = str(request.get("format") or "GDS2").upper()  # legacy API default
    reader = formats.reader(fmt, "preview")
    if reader is None:
        return {"ok": False, "code": "PREVIEW_UNSUPPORTED_FORMAT",
                "message": f"Preview is only available for {list(formats.reader_formats('preview'))} in this version; "
                           f"this version is {fmt}. Download it or open it in KLayout instead."}
    return reader.render(request, budgets)

def vesti_run_compare(request, budgets, *, formats=None):
    formats = formats if formats is not None else FORMATS.copy(frozen=True)
    before = str(request.get("format_before") or "GDS2").upper()
    after = str(request.get("format_after") or "GDS2").upper()
    # Cross-format geometry equivalence is a separate contract, never inferred from two parsers.
    if before != after or not formats.has_reader(before, "compare"):
        return {"ok": False, "code": "PREVIEW_UNSUPPORTED_FORMAT",
                "message": f"Comparison is only available for {list(formats.reader_formats('compare'))} in this version."}
    return formats.reader(before, "compare").compare(request, budgets)
