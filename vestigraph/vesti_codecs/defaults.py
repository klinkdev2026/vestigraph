"""Default codecs are composed here; the registry and pack framing know no algorithms."""
from .raw import VestiRawCodec
from .zlib import VestiZlibCodec
from .bsdiff import VestiBsdiffCodec
from .registry import VestiCodecRegistry
from .services import VestiCodecServices


def vesti_default_codecs():
    return VestiCodecServices(VestiCodecRegistry((VestiRawCodec(), VestiZlibCodec(), VestiBsdiffCodec())))
