<p align="right">
  <a href="INSTALLATION.md">English</a> | <a href="INSTALLATION.zh-CN.md">中文</a>
</p>

# Installation and upgrades

## Requirements

Vestigraph requires Python 3.10 or newer, KLayout desktop 0.30.x, and `klayout-klink>=0.6.0,<0.7`. The normal `vestigraph` package installs the compatible Klink dependency. The Python `klayout` package is used for offline preview; it does not install the KLayout desktop application.

Klink owns the KLayout plugin installation. Use `klink plugin install` for first install and upgrades.

## Install from PyPI

The commands below install published PyPI releases. For a version not yet on PyPI, use the [GitHub Actions release artifacts](https://github.com/klinkdev2026/vestigraph/actions/workflows/release.yml) described below.

```console
python -m pip install vestigraph
klink plugin install
```

Restart the MCP client that runs `klink-mcp`. The Klink extension registry discovers Vestigraph in that Python environment and registers the local companion. No separate Vestigraph MCP server is needed.

Restart KLayout, open a saved GDS/OASIS layout, and click **HIST**. The panel opens the local history UI and shows live recording status. Run the integration doctor when you need an installation check:

```console
python -m vestigraph doctor --integration
```

Installing Python packages does not configure arbitrary chat clients. Configure or restart the MCP client you actually use.

## Release artifacts before PyPI

For unreleased versions, use the wheel and sdist artifacts created by the [GitHub Actions release workflow](https://github.com/klinkdev2026/vestigraph/actions/workflows/release.yml). Do not assume unreleased packages already exist on PyPI.

Vestigraph artifact install:

```console
python -m pip install ./wheels/vestigraph-0.2.0-py3-none-any.whl
klink plugin install
```

Before both projects are on PyPI, download artifacts built from matching reviewed Klink and Vestigraph revisions. Put the two Klink Rust wheels, the `klayout_klink` core wheel, and the Vestigraph wheel in one local directory, then run:

```console
python -m pip install --find-links ./wheels "klayout-klink>=0.6.0,<0.7" "vestigraph>=0.2,<0.3"
klink plugin install
```

Then restart MCP, restart KLayout, open a saved layout, and click **HIST**.

## Upgrade

Stop the old Vestigraph service if one is running. Upgrade packages and the Klink plugin, then restart MCP and KLayout:

```console
python -m pip install --upgrade "vestigraph>=0.2,<0.3"
klink plugin install
python -m vestigraph doctor --integration
```

Keep the same Python environment for Klink MCP, Vestigraph, and KLayout companion registration. `KLAYOUT_HOME` selects the KLayout configuration directory. `KLINK_REGISTRY_ROOT` selects the local session registry. Data locations are described in [Recovery](RECOVERY.md).

## Underlying storage CLI

Vestigraph still includes a local storage CLI for explicit file checkpoints and exports. It is useful for recovery tasks and tests, but the main user install path is the full Klink-backed KLayout history flow.

## Disable automatic companion startup

```console
python -m vestigraph companion status
python -m vestigraph companion unregister
```

Unregistering does not delete history and does not immediately stop an already running service. Close all KLayout windows and allow the companion service to exit, or stop a foreground service with Ctrl+C.
