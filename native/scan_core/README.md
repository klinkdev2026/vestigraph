<p align="right">
  <a href="README.md">English</a> | <a href="README.zh-CN.md">中文</a>
</p>

# vestigraph-scan-core

`vestigraph-scan-core` is the native scanner package used by Vestigraph's complete installation. The Vestigraph runtime tries this Rust-backed scanner first and falls back to the Python scanner with a diagnostic reason if the native module cannot be imported.

## What it provides

- Bounded GDS record scanning for local history capture and preview metadata.
- Hashing and record offsets used by Vestigraph's local history pipeline.
- A Python extension module named `vestigraph_scan_core`.

## Installation model

Users normally install this package through `pip install vestigraph`, which resolves the compatible scanner wheel along with `klayout-klink`. Supported release platforms provide prebuilt wheels, so a local Rust toolchain is not required for normal installation.

When installing from GitHub Actions artifacts before PyPI publication, place the matching `vestigraph_scan_core` wheel in the same local wheel directory as the Vestigraph wheel and install with `--find-links`.

## Build and release

Public wheels are built by the Vestigraph GitHub Actions release workflow from the audited public release tree. Maintainers should verify the produced wheel metadata and third-party notices before publication. End users should not need to build this package locally.

License and dependency terms are reported through package metadata and the public `THIRD_PARTY.md` file in this directory.
