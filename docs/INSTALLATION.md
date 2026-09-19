<p align="right">
  <a href="INSTALLATION.md">English</a> | <a href="INSTALLATION.zh-CN.md">中文</a>
</p>

# Installation and upgrades

## Requirements

Vestigraph requires Python 3.10 or newer, KLayout desktop 0.30.x, `klayout-klink>=0.6.0,<0.7`, and `vestigraph-scan-core`. The normal `vestigraph` package installs the compatible Klink and scanner dependencies. The Python `klayout` package is used for offline preview; it does not install the KLayout desktop application.

Klink owns the KLayout plugin installation. Use `klink plugin install` for first install and upgrades.

## Install from PyPI

Vestigraph 0.2.1 is available on [PyPI](https://pypi.org/project/vestigraph/0.2.1/).

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
