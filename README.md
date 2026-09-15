<p align="right">
  <a href="./README.md">English</a> | <a href="./README.zh-CN.md">中文</a>
</p>

# Vestigraph

Vestigraph is local file and layout history for KLayout users. It stores versions on the user's machine, shows a browser timeline, previews GDS/OASIS content, imports older files, and exports saved versions for recovery.

Vestigraph requires Python 3.10 or newer, KLayout desktop 0.30.x, and `klayout-klink>=0.6.0,<0.7`. The normal installation installs Klink as a dependency. Install the Klink KLayout plugin with the Klink command, restart MCP so Vestigraph registers its local companion for the active Python environment, then restart KLayout and open **HIST**.

## What it does

- Saves file checkpoints and history data in local user storage.
- Runs a loopback-only browser service for browsing, naming, importing, and exporting history.
- Previews GDS/OASIS versions with the Python `klayout` package.
- Uses Klink's KLayout plugin and companion-service path to record saved GDS/OASIS documents automatically.
- Exposes local history and skill-refinement tools through the existing Klink MCP extension registry.
- Keeps history, evidence, drafts, revisions, exports, login links, and control files on the user's machine.

Vestigraph does not provide cloud sync, remote collaboration, hosted storage, a model service, or automatic chat-client configuration.

## Install

The first public release is pending PyPI publication. Until it is published, download the build-only wheel and sdist artifacts from [GitHub Actions](https://github.com/klinkdev2026/vestigraph/actions/workflows/release.yml). After PyPI publication, use the package name directly.

```console
python -m pip install vestigraph
klink plugin install
# restart the MCP client that runs klink-mcp
# restart KLayout, open a saved GDS/OASIS layout, then click HIST
python -m vestigraph doctor --integration
```

`pip install vestigraph` installs the compatible `klayout-klink` dependency. `klink plugin install` is still the Klink command that installs or upgrades the KLayout plugin. Restarting the MCP client lets the Klink extension registry discover Vestigraph and register the companion for that Python environment. No separate Vestigraph MCP server is needed. Installing Python packages does not configure arbitrary chat clients.

After KLayout restarts, open a saved GDS/OASIS layout and click **HIST**. The panel opens the local web history UI and shows recording status. Confirm that recording is active before editing.

## Release artifacts before PyPI

Download the Vestigraph artifact archive from the [release workflow](https://github.com/klinkdev2026/vestigraph/actions/workflows/release.yml), extract it, and install the wheel from that directory:

```console
python -m pip install ./wheels/vestigraph-0.2.0-py3-none-any.whl
klink plugin install
```

Before both projects are on PyPI, put the matching platform wheels in one local directory: the two Klink Rust wheels, the `klayout_klink` core wheel, and the Vestigraph wheel. Then install from that directory:

```console
python -m pip install --find-links ./wheels "klayout-klink>=0.6.0,<0.7" "vestigraph>=0.2,<0.3"
klink plugin install
```

Then restart the MCP client, restart KLayout, open a saved layout, and click **HIST**.

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
