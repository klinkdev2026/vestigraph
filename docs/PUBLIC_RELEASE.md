# Release scope and compatibility

## Standalone features

The public package includes file checkpoints, version queries, change records, byte export, local browser UI, GDS/OASIS preview, naming, old-file import, history integrity checks, and index recovery into a new directory.

## Optional klink integration

With compatible klink installed in the same Python environment, Vestigraph adds local KLayout session discovery, automatic recording, the HIST entry, pause/resume controls, opening old versions in new KLayout tabs, and local tools through the existing klink MCP server.

## Experimental skill features

When explicitly enabled, Vestigraph supports range requests, frozen evidence, draft submission, document-structure validation, revisions, local published state, and file export. It does not run scripts, install skills, call models, or contact hosted services by itself.

## Compatibility

Vestigraph requires Python 3.10 or newer. KLayout desktop 0.30.x is needed only for editor integration. The klink integration requires klink 0.6.0 or a compatible later 0.6.x release; 0.5.x does not provide the 0.6 companion and tool-discovery contract. The Python preview engine is installed with the base package. Optional scanner and delta encoder packages are not required for basic history or restore.

GitHub Actions builds and tests the public package across the configured Python and operating-system matrix. Release publication uses the repository CI/CD path from a reviewed tag with OIDC trusted publishing.

## Boundaries

The service is for a trusted local user. It does not provide remote collaboration, cloud sync, automatic merge, or full editor-environment recovery. GDS can provide structured changes; OASIS is saved and previewed, but does not have dedicated structured history analysis. Large files are limited by capture time, storage, and preview budgets.

Operating-system and optional-component support is determined by actual CI and environment checks. A local environment may still fail if a required native or browser dependency is unavailable.
