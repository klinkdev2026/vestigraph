"""Presentation metadata is independent of layout truth; all fixtures are synthetic."""


import base64


import hashlib


import struct


import uuid


import zlib


from types import SimpleNamespace


import pytest


from vestigraph.presentation import Presentation, PresentationError, LabelConflict, validate_png


from vestigraph.capture_thumbnail import capture_thumbnail


from tests.fixtures_web_api_history import web, data


from tests.fixtures_capture_pipeline import repo, enqueue, gds, OPTIONS


from vestigraph.capture_pipeline import CapturePipeline


from tests.fixtures_relocate_service import recording_stack


def png(width=64, height=32):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
    rows = b"".join(b"\0" + bytes([230, 40, 30]) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


__all__ = [name for name in globals() if not name.startswith("__")]
