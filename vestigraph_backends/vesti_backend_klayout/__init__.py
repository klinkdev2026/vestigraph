"""KLayout integration; no editor connection or plugin registration at import."""
BACKEND_ID = "klayout"
DISPLAY_NAME = "KLayout"
API_VERSION = 1


def create_backend():
    from vestigraph_backends.vesti_backend_klayout.adapter import KLayoutBackend
    return KLayoutBackend()
