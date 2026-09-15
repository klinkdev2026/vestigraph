# Vestigraph

Vestigraph is a local history service for layout files. It stores file versions on the user's machine, shows a browser timeline, previews GDS/OASIS content, imports older files, and exports saved versions for recovery.

Vestigraph requires Python 3.10 or newer. It can run on its own. For KLayout automatic recording, the HIST toolbar entry, and local agent tools through the existing klink MCP server, install it with a compatible klink in the same Python environment.

## What it does

- Saves explicit file checkpoints from the CLI.
- Runs a loopback-only browser service for browsing, naming, importing, and exporting history.
- Previews GDS/OASIS versions with the Python `klayout` package.
- Optionally registers as a local companion service for klink so KLayout can record saved GDS/OASIS documents automatically.
- Optionally exposes local skill-refinement tools through the existing klink MCP extension registry.
- Keeps history, evidence, drafts, revisions, exports, login links, and control files on the user's machine.

Vestigraph does not provide cloud sync, remote collaboration, hosted storage, a model service, or automatic chat-client configuration.

## Install standalone history

The first public release is pending PyPI publication. Until it is published, download the build-only wheel and sdist artifacts from [GitHub Actions](https://github.com/klinkdev2026/vestigraph/actions/workflows/release.yml). After PyPI publication, use the package name directly.

Use standalone mode when you only need local file history and the browser UI.

```console
python -m pip install vestigraph
python -m vestigraph doctor
python -m vestigraph serve --open-browser
```

Standalone mode does not require KLayout desktop, klink, Git, Rust, or an AI account. It does not run `setup`.

```console
python -m vestigraph --repo ./my-history init
python -m vestigraph --repo ./my-history checkpoint ./chip.gds --title initial
python -m vestigraph --repo ./my-history history
python -m vestigraph --repo ./my-history export CHECKPOINT_ID ./restored.gds
```

Replace `CHECKPOINT_ID` with the full id returned by `history` or `show`. The export destination must not already exist, its parent directory must exist, and it must be outside the history repository.

## Recommended KLayout integration

Install Vestigraph into the same Python environment that runs `klink-mcp`:

```console
python -m pip install "klayout-klink>=0.6.0,<0.7" "vestigraph[klink]>=0.2,<0.3"
python -m vestigraph setup
python -m vestigraph doctor --integration
```

Restart KLayout, open a saved GDS/OASIS layout, click **HIST**, and confirm the panel shows recording status before editing. Restart the MCP client after installing or upgrading packages. `klink.status` lists installed extensions, and `klink.find_tools` with `domain="vestigraph"` discovers the local tools.

Installing Python packages does not configure arbitrary chat clients and does not upload history.

## Release artifacts before PyPI

Download the Vestigraph artifact archive from the [release workflow](https://github.com/klinkdev2026/vestigraph/actions/workflows/release.yml), extract it, and install the wheel from that directory:

```console
python -m pip install ./wheels/vestigraph-0.2.0-py3-none-any.whl
```

For KLayout integration before both projects are on PyPI, put the matching platform wheels in one local directory: the two klink Rust wheels, the `klayout_klink` core wheel, and the Vestigraph wheel. Then install from that directory and run setup:

```console
python -m pip install --find-links ./wheels "klayout-klink>=0.6.0,<0.7" "vestigraph[klink]>=0.2,<0.3"
python -m vestigraph setup
```

## Upgrade

Stop the old Vestigraph service first. Then upgrade compatible packages, run setup again, and restart KLayout plus the MCP client:

```console
python -m pip install --upgrade "klayout-klink>=0.6.0,<0.7" "vestigraph[klink]>=0.2,<0.3"
python -m vestigraph setup
python -m vestigraph doctor --integration
```

Standalone mode only needs the Vestigraph package and service restarted. Keep using the same Python environment that ran setup.

## Local skills and agents

Skill refinement is experimental and disabled by default. When enabled, users can select a history range, save a request, freeze evidence, let a chosen local agent submit a draft, review validation feedback, save revisions, and export files. The package does not include private skills and does not call a model by itself.

After joint installation and MCP restart, use the existing klink MCP server:

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
