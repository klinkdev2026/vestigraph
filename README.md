<p align="right">
  <a href="./README.md">English</a> | <a href="./README.zh-CN.md">中文</a>
</p>

# Vestigraph

Vestigraph is local file and layout history for KLayout users. It stores versions on the user's machine, shows a browser timeline, previews GDS/OASIS content, imports older files, and exports saved versions for recovery.

Vestigraph requires Python 3.10 or newer, KLayout desktop 0.30.x, `klayout-klink>=0.6.0,<0.7`, and the `vestigraph-scan-core` scanner package. The normal installation resolves Klink and the scanner as dependencies. Install the Klink KLayout plugin with the Klink command, restart MCP so Vestigraph registers its local companion for the active Python environment, then restart KLayout and open **HIST**.

## What it does

- Saves file checkpoints and history data in local user storage.
- Runs a loopback-only browser service for browsing, naming, importing, and exporting history.
- Previews GDS/OASIS versions with the Python `klayout` package.
- Uses Klink's KLayout plugin and companion-service path to record saved GDS/OASIS documents automatically.
- Exposes local history and skill-refinement tools through the existing Klink MCP extension registry.
- Keeps history, evidence, drafts, revisions, exports, login links, and control files on the user's machine.

Vestigraph does not provide cloud sync, remote collaboration, hosted storage, a model service, or automatic chat-client configuration.

## Install

Vestigraph 0.2.3 is available on [PyPI](https://pypi.org/project/vestigraph/0.2.3/).

```console
python -m pip install vestigraph
klink plugin install
# restart the MCP client that runs klink-mcp
# restart KLayout, open a saved GDS/OASIS layout, then click HIST
python -m vestigraph doctor --integration
```

`pip install vestigraph` installs the compatible `klayout-klink` and `vestigraph-scan-core` dependencies. Rust scanning is selected automatically when the native module is available; if it cannot be imported, Vestigraph falls back to the Python scanner with a diagnostic reason. Supported Linux, macOS, and Windows wheel platforms do not need a local Rust toolchain.

`klink plugin install` installs or upgrades the KLayout plugin. Restarting the MCP client lets the existing Klink MCP server discover Vestigraph and register the local companion for that Python environment. No separate MCP server or `vestigraph setup` step is needed. Installing Python packages does not configure a chat client.

After KLayout restarts, open a saved GDS/OASIS layout and click **HIST** to open the local history web UI. Confirm that recording is active before editing.

## Checkpoints and restore

Vestigraph groups one continuous AI drawing operation into one checkpoint instead of recording every shape separately. Before an AI mutation, pending manual KLayout changes are saved as their own checkpoint. Checkpoint summaries include save and observed modification times, the recorded reason, coverage, and restore provenance.

The agent history tool returns the 30 most recent summaries by default. It retrieves the complete checkpoint list only when the user explicitly asks for all of it; use HIST for detailed events and comparisons.

After the user explicitly selects a checkpoint, `vestigraph.restore` restores it into the active saved KLayout document. Restore is additive: it appends a new checkpoint with `restore_of` and the user's reason, and does not delete any earlier or later history. Pending editor changes are checkpointed first. See [Recovery and data locations](docs/RECOVERY.md).

## Upgrade

Stop the old Vestigraph service if one is running. Upgrade packages, upgrade the Klink plugin, then restart MCP and KLayout:

```console
python -m pip install --upgrade "vestigraph>=0.2,<0.3"
klink plugin install
python -m vestigraph doctor --integration
```

The underlying storage CLI can still save and export local file versions, but it is not the main product installation path. Full KLayout history, HIST, and local agent tools require Klink.

## Local skills and agents

Skill refinement is experimental and disabled by default. When enabled, users can select a history range, save a request, freeze evidence, let a chosen local agent submit a draft, review validation feedback, save revisions, and export files. The package does not include private skills and does not call a model by itself.

After installation and MCP restart, use the existing Klink MCP server:

```json
{"tool":"klink.find_tools","arguments":{"domain":"vestigraph"}}
```

Then call `vestigraph.guide` and follow `next_action`. See [Local agents and skills](docs/AGENT_LOCAL.md).

## Documentation

- [Installation and upgrades](docs/INSTALLATION.md)
- [Files and history](docs/HISTORY.md)
- [Recovery and data locations](docs/RECOVERY.md)
- [Command line](docs/CLI.md)
- [Local agents and skills](docs/AGENT_LOCAL.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Release scope](docs/PUBLIC_RELEASE.md)

Vestigraph is Apache-2.0. See [Security and local access](SECURITY.md), [third-party notices](THIRD_PARTY_NOTICES.md), and [changelog](CHANGELOG.md).
