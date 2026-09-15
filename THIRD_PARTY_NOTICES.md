<p align="right">
  <a href="THIRD_PARTY_NOTICES.md">English</a> | <a href="THIRD_PARTY_NOTICES.zh-CN.md">中文</a>
</p>

# Third-party notices

Vestigraph source is distributed under Apache-2.0. Its installation resolves separately distributed dependencies including FastAPI, Uvicorn, Pydantic, the KLayout Python package, compatible Klink packages, and the `vestigraph-scan-core` scanner package. Each dependency retains its own copyright notices and license; installing it does not relicense it under Vestigraph's license.

KLayout and its Python bindings have their own distribution and licensing terms. Klink is distributed separately. The scanner package is distributed as a separate native wheel on supported platforms; users normally do not need a local Rust toolchain. If the native scanner is unavailable, Vestigraph reports the reason and falls back to the Python scanner.

Optional delta encoding packages such as `bsdiff4` are resolved only when that feature is requested. Consult the metadata and license files in each installed distribution for its terms. The native scanner dependency list is documented in `native/scan_core/THIRD_PARTY.md` in the public release tree.

The wheel contains Vestigraph's own web assets and requires no CDN or remote asset loading.
