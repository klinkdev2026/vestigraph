# Third-party notices

Vestigraph source is distributed under Apache-2.0. Its base installation resolves separately distributed dependencies including FastAPI, Uvicorn, Pydantic and the KLayout Python package. The optional klink extra also installs klayout-klink and its dependencies. Each dependency retains its own copyright notices and license; installing it does not relicense it under Vestigraph's license.

KLayout and its Python bindings have their own distribution and licensing terms. KLink is Apache-2.0. Optional acceleration and encoding packages, including bsdiff4 and vestigraph_scan_core, are not bundled in the pure-Python Vestigraph wheel. Consult the metadata and license files in each installed distribution for its terms.

The wheel contains Vestigraph's own web assets and requires no CDN or remote asset loading.
