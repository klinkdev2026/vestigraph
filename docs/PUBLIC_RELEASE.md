<p align="right">
  <a href="PUBLIC_RELEASE.md">English</a> | <a href="PUBLIC_RELEASE.zh-CN.md">中文</a>
</p>

# Release scope and compatibility

## Complete product path

The public package installs local file and KLayout history. With compatible Klink in the same Python environment, Vestigraph records saved GDS/OASIS documents from KLayout, provides the HIST entry, supports pause/resume controls, opens old versions in new KLayout tabs, and exposes local tools through the existing Klink MCP server.

## Underlying storage features

The storage layer includes file checkpoints, version queries, change records, byte export, local browser UI, GDS/OASIS preview, naming, old-file import, history integrity checks, and index recovery into a new directory. These pieces remain local and are used by the complete KLayout flow.

## Experimental skill features

When explicitly enabled, Vestigraph supports range requests, frozen evidence, draft submission, document-structure validation, revisions, local published state, and file export. It does not run scripts, install skills, call models, or contact hosted services by itself.

## Compatibility

Vestigraph requires Python 3.10 or newer, KLayout desktop 0.30.x, Klink 0.6.0 or a compatible later 0.6.x release, and the `vestigraph-scan-core` scanner package. Klink 0.5.x does not provide the 0.6 companion and tool-discovery contract. The Python preview engine is installed with the base package. The scanner prefers the Rust backend when it is importable and falls back to Python with a diagnostic reason if a native wheel is unavailable.

Delta encoder packages such as `bsdiff4` remain optional; basic restore can still use stored bytes without them.

GitHub Actions builds and tests the public package across the configured Python and operating-system matrix. Release publication uses the repository CI/CD path from a reviewed tag with OIDC trusted publishing.

## Boundaries

The service is for a trusted local user. It does not provide remote collaboration, cloud sync, automatic merge, or full editor-environment recovery. GDS can provide structured changes; OASIS is saved and previewed, but does not have dedicated structured history analysis. Large files are limited by capture time, storage, and preview budgets.

Operating-system and optional-component support is determined by actual CI and environment checks. A local environment may still fail if a required native or browser dependency is unavailable.
