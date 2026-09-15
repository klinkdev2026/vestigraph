"""Bounded BSDIFF40 reader (stdlib only) and the optional bsdiff4 encoder (M2, spec §5.5).

A delta-encoded object stores a BSDIFF40 patch against a base object. This
module never trusts a patch: every length is checked against the object
header before any allocation, the three bz2 streams are decompressed with
explicit output limits, the control block is validated triple by triple, and
the caller still verifies the SHA-256 of the result.

Format (Colin Percival's bsdiff 4, as written by the bsdiff4 package):

    "BSDIFF40" | int64 len(bz2 control) | int64 len(bz2 diff) | int64 newsize
    bz2(control)  triples of int64 (x, y, z): add x diff bytes to old, copy y extra bytes, seek z
    bz2(diff)     sum(x) bytes, each added (mod 256) to the base byte at the same position
    bz2(extra)    sum(y) bytes copied verbatim

int64 is sign-magnitude: little-endian magnitude, sign in the top bit of byte 7.
Base bytes outside the base object are 0 (bsdiff semantics); the stdlib path and
the optional native path (bsdiff4.core.patch on already-validated blocks) must agree
byte for byte, which the tests cross-check.
"""
from __future__ import annotations

import bz2
import os

MAGIC = b"BSDIFF40"
HEADER_SIZE = 32
MAX_CONTROL_TRIPLES = 65536
CONTROL_LIMIT = MAX_CONTROL_TRIPLES * 24
CODEC_ID = "bsdiff40-restricted-v1"


from .contract import VestiCodec, VestiCodecDescriptor, VestiCodecError, VestiPatchRejected


class DeltaError(VestiCodecError):
    """Base class: the object cannot be restored from this patch (any reason)."""


class PatchFormatError(DeltaError, VestiPatchRejected):
    """The patch is not a valid restricted BSDIFF40 patch for this object: framing, limits,
    control-block coverage or seek bounds. At encode time this means "not adoptable", never
    a fault (the encoder may legitimately emit something outside the restricted subset)."""


class NativePatchError(DeltaError):
    """The native bsdiff4 extension failed or produced the wrong size on ALREADY VALIDATED
    blocks. This is an internal failure, not a subset miss: a publisher must stop, not
    count it as a compression miss (spec P3)."""


class NativeUnavailable(DeltaError):
    """native=True was requested but bsdiff4.core is not importable."""


VERIFY_ENV = "VESTIGRAPH_DELTA_VERIFY"       # stdlib (default) | native


def verify_backend_preference(explicit=None):
    """Which reader checks a freshly encoded patch: 'stdlib' (independent of the encoder,
    the default until P3 has passed its gates) or 'native' (bsdiff4.core.patch on the
    validated blocks; falls back to stdlib only when the module is missing)."""
    value = explicit or os.environ.get(VERIFY_ENV) or "stdlib"
    if value not in ("stdlib", "native"):
        raise DeltaError("delta verify backend must be stdlib or native (got %r)" % value)
    return value


def decode_int64(buf: bytes) -> int:
    value = int.from_bytes(buf[:7], "little") | ((buf[7] & 0x7F) << 56)
    return -value if buf[7] & 0x80 else value


def encode_int64(value: int) -> bytes:
    magnitude = -value if value < 0 else value
    if magnitude >= 1 << 63:
        raise DeltaError("int64 out of range")
    buf = bytearray(magnitude.to_bytes(8, "little"))
    if value < 0:
        buf[7] |= 0x80
    return bytes(buf)


def _decompress(data: bytes, limit: int, what: str) -> bytes:
    decoder = bz2.BZ2Decompressor()
    try:
        out = decoder.decompress(data, limit + 1)
    except (OSError, ValueError) as exc:
        raise PatchFormatError("%s block is not a valid bz2 stream" % what) from exc
    if len(out) > limit:
        raise PatchFormatError("%s block exceeds its limit" % what)
    if not decoder.eof:
        raise PatchFormatError("%s block is truncated" % what)
    if decoder.unused_data:
        raise PatchFormatError("%s block has trailing data" % what)
    return out


def parse_patch(patch: bytes, expected_size: int, base_size: int | None = None):
    """Validate a patch for an object of `expected_size` bytes.
    Returns (control triples, diff block, extra block); raises DeltaError.
    With `base_size` the running base position is bounded to
    [-expected_size, base_size + expected_size] so no seek can reach an absurd
    offset (the native path computes in int64)."""
    if not isinstance(expected_size, int) or expected_size < 0:
        raise PatchFormatError("bad expected size")
    if base_size is not None and (not isinstance(base_size, int) or base_size < 0):
        raise PatchFormatError("bad base size")
    if len(patch) < HEADER_SIZE or patch[:8] != MAGIC:
        raise PatchFormatError("not a BSDIFF40 patch")
    len_control = decode_int64(patch[8:16])
    len_diff = decode_int64(patch[16:24])
    new_size = decode_int64(patch[24:32])
    if new_size != expected_size:
        raise PatchFormatError("patch target size disagrees with the object header")
    if len_control < 0 or len_diff < 0 or HEADER_SIZE + len_control + len_diff > len(patch):
        raise PatchFormatError("patch block lengths exceed the patch")
    control_raw = _decompress(patch[HEADER_SIZE:HEADER_SIZE + len_control], CONTROL_LIMIT, "control")
    if len(control_raw) % 24:
        raise PatchFormatError("control block is not a whole number of triples")
    diff = _decompress(patch[HEADER_SIZE + len_control:HEADER_SIZE + len_control + len_diff], expected_size, "diff")
    extra = _decompress(patch[HEADER_SIZE + len_control + len_diff:], expected_size, "extra")
    control = []
    new_pos = diff_pos = extra_pos = old_pos = 0
    lo_bound = -expected_size
    hi_bound = (base_size if base_size is not None else 0) + expected_size
    for i in range(0, len(control_raw), 24):
        x = decode_int64(control_raw[i:i + 8])
        y = decode_int64(control_raw[i + 8:i + 16])
        z = decode_int64(control_raw[i + 16:i + 24])
        if x < 0 or y < 0 or new_pos + x + y > expected_size:
            raise PatchFormatError("control triple runs past the target")
        new_pos += x + y
        diff_pos += x
        extra_pos += y
        old_pos += x + z
        if base_size is not None and not lo_bound <= old_pos <= hi_bound:
            raise PatchFormatError("control seek leaves the plausible base range")
        control.append((x, y, z))
    if new_pos != expected_size or diff_pos != len(diff) or extra_pos != len(extra):
        raise PatchFormatError("control block does not cover the target or its blocks exactly")
    return control, diff, extra


def _apply_python(base: bytes, expected_size: int, control, diff: bytes, extra: bytes) -> bytes:
    out = bytearray(expected_size)
    old_len = len(base)
    old_pos = new_pos = diff_pos = extra_pos = 0
    for x, y, z in control:
        if x:
            lo = max(old_pos, 0)
            hi = min(old_pos + x, old_len)
            segment = diff[diff_pos:diff_pos + x]
            if hi > lo:
                # bytes inside the base are added mod 256; bytes outside stay as the diff says
                start = lo - old_pos
                added = bytes((a + b) & 0xFF for a, b in zip(segment[start:start + (hi - lo)], base[lo:hi]))
                segment = segment[:start] + added + segment[start + (hi - lo):]
            out[new_pos:new_pos + x] = segment
            new_pos += x
            diff_pos += x
            old_pos += x
        if y:
            out[new_pos:new_pos + y] = extra[extra_pos:extra_pos + y]
            new_pos += y
            extra_pos += y
        old_pos += z
    return bytes(out)


def _native():
    try:
        import bsdiff4.core as core  # optional native extension, never required
    except Exception:  # noqa: BLE001 - any import problem means "not available"
        return None
    return core


def apply_patch(patch: bytes, base: bytes, expected_size: int, *, native: bool | None = None,
                base_size: int | None = None) -> bytes:
    """Restore an object from `base` + `patch`; every check of parse_patch applies first.
    `native=None` uses bsdiff4.core.patch when importable (only on validated blocks),
    `native=False` forces the stdlib path, `native=True` requires the extension.
    `base_size` defaults to len(base) so seeks are always bounded."""
    control, diff, extra = parse_patch(patch, expected_size, len(base) if base_size is None else base_size)
    core = _native() if native is not False else None
    if native is True and core is None:
        raise NativeUnavailable("native bsdiff4 requested but not importable")
    if core is not None:
        try:
            out = core.patch(base, expected_size, control, diff, extra)
        except Exception as exc:  # noqa: BLE001 - never let the extension's error escape unlabeled
            raise NativePatchError("native patch failed on validated blocks: %s" % exc) from exc
        if len(out) != expected_size:
            raise NativePatchError("native patch produced the wrong size (%d, expected %d)" % (len(out), expected_size))
        return bytes(out)
    return _apply_python(base, expected_size, control, diff, extra)


def encoder_available() -> bool:
    return _native() is not None


def encoder_version() -> str | None:
    if _native() is None:
        return None
    try:
        import bsdiff4
        return "bsdiff4/%s" % getattr(bsdiff4, "__version__", "unknown")
    except Exception:  # noqa: BLE001
        return "bsdiff4/unknown"


def make_patch(base: bytes, target: bytes) -> bytes:
    """BSDIFF40 patch via the bsdiff4 encoder (optional dependency)."""
    core = _native()
    if core is None:
        raise DeltaError("bsdiff4 encoder is not installed (optional extra storage-delta)")
    control, diff, extra = core.diff(base, target)
    return write_patch(len(target), control, diff, extra)


def write_patch(new_size: int, control, diff: bytes, extra: bytes) -> bytes:
    """Serialize blocks in BSDIFF40 layout (also used by tests to build hand-made patches).
    Refuses what the restricted reader would refuse anyway (too many control triples), so an
    encoder output outside the subset is reported at write time, not at restore time."""
    if len(control) > MAX_CONTROL_TRIPLES:
        raise PatchFormatError("patch has more control triples than the restricted reader accepts")
    raw_control = b"".join(encode_int64(x) + encode_int64(y) + encode_int64(z) for x, y, z in control)
    b_control = bz2.compress(raw_control)
    b_diff = bz2.compress(bytes(diff))
    b_extra = bz2.compress(bytes(extra))
    return (MAGIC + encode_int64(len(b_control)) + encode_int64(len(b_diff)) + encode_int64(new_size)
            + b_control + b_diff + b_extra)


def naive_patch(base: bytes, target: bytes) -> bytes:
    """Reference encoder without any dependency: one triple that adds a full-length diff.
    Not small, but a valid BSDIFF40 patch; used for tests and as a fallback cross-check."""
    padded = base[:len(target)] + bytes(max(0, len(target) - len(base)))
    diff = bytes((t - b) & 0xFF for t, b in zip(target, padded))
    return write_patch(len(target), [(len(target), 0, 0)], diff, b"")


class VestiBsdiffCodec(VestiCodec):
    descriptor = VestiCodecDescriptor(CODEC_ID, 2, "delta")

    def encoder_available(self):
        return encoder_available()

    def encoder_version(self):
        return encoder_version()

    def encode(self, payload, *, base=None, level=None):
        return make_patch(base, payload)

    def decode(self, stored, raw_size, limit, *, base=None, native=None):
        if not 0 <= raw_size <= limit:
            raise DeltaError("object larger than the caller's limit")
        return apply_patch(stored, base, raw_size, base_size=len(base), native=native)

    def verify_preference(self, preference=None):
        return verify_backend_preference(preference)
