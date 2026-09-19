<p align="right">
  <a href="CHANGELOG.md">English</a> | <a href="CHANGELOG.zh-CN.md">中文</a>
</p>

# Changelog

## 0.2.1

- Groups each continuous AI layout operation into one history checkpoint and records its bounded reason and method.
- Saves pending manual KLayout changes before an AI mutation so manual work remains visible in history.
- Lists 30 recent checkpoint summaries by default; agents only retrieve the complete history after an explicit user request.
- Adds modification and save times, operation notes, coverage, and restore provenance to checkpoint summaries.
- Improves unsaved-layout guidance and keeps ordinary history available without enabling experimental skill refinement.
- Adds compatibility handling for older Klink plugins while using synchronous event flushing with updated plugins.

## 0.2.0

- Complete KLayout history path uses compatible Klink as a required dependency for the KLayout plugin, HIST entry, companion service, and MCP tool discovery.
- Complete installation includes `vestigraph-scan-core`; scanning prefers the Rust backend and falls back to Python with diagnostics when a native wheel is unavailable. Supported wheel platforms do not require a local Rust toolchain.
- Local file history, GDS/OASIS previews, import, recovery, and byte export stay in local user storage.
- Restarting the existing Klink MCP process discovers Vestigraph tools; no separate Vestigraph MCP server is required.
- KLayout plugin installation remains the Klink command path (`klink plugin install`) for first install and upgrades.
- Local skill requests, frozen evidence, revisioned drafts, and exports are available behind an explicit experimental switch.
- Authenticated loopback calls use structured arguments, revision checks, and scoped validation reports.
