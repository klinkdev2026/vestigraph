# Installation and upgrades

## Choose a mode

| Mode | What it provides |
| --- | --- |
| `vestigraph` | Local file history, browser UI, GDS/OASIS preview, import, and export |
| `vestigraph[klink]` | Everything above plus compatible klink integration for automatic KLayout recording, HIST, and local MCP tools |

Vestigraph requires Python 3.10 or newer. The Python `klayout` package is used for offline preview; it does not install the KLayout desktop application. KLayout desktop integration uses KLayout 0.30.x plus compatible klink 0.6.x.

## Standalone mode

The first public release is pending PyPI publication. Until it is published, download the build-only wheel and sdist artifacts from [GitHub Actions](https://github.com/klinkdev2026/vestigraph/actions/workflows/release.yml). After PyPI publication, install by package name.

```console
python -m pip install vestigraph
python -m vestigraph doctor
python -m vestigraph serve --open-browser
```

Do not run `setup` for standalone mode. Stop the foreground service with Ctrl+C. Existing CLI history can be attached to the browser service; see [Files and history](HISTORY.md).

## KLayout integration

Install Vestigraph in the same Python environment that runs klink MCP:

```console
python -m pip install "klayout-klink>=0.6.0,<0.7" "vestigraph[klink]>=0.2,<0.3"
python -m vestigraph setup
python -m vestigraph doctor --integration
```

Restart KLayout, open a saved layout, and click **HIST**. `doctor --integration` checks packages, the plugin, and companion registration; the panel is the source of truth for live recording status.

The default browser service port is 8787, separate from editor RPC ports. Use `setup --port 8788` to change the preferred port; the companion service may choose a later free port at startup.

Use the same environment when running setup and starting KLayout. `KLAYOUT_HOME` selects the KLayout configuration directory. `KLINK_REGISTRY_ROOT` selects the local session registry. Data locations are described in [Recovery](RECOVERY.md).

## Release artifacts before PyPI

For unreleased versions, use the wheel and sdist artifacts created by the [GitHub Actions release workflow](https://github.com/klinkdev2026/vestigraph/actions/workflows/release.yml). Do not assume unreleased packages already exist on PyPI.

Standalone artifact install:

```console
python -m pip install ./wheels/vestigraph-0.2.0-py3-none-any.whl
```

For joint integration before both projects are on PyPI, download artifacts built from matching reviewed klink and Vestigraph revisions. Put the two klink Rust wheels, the `klayout_klink` core wheel, and the Vestigraph wheel in one local directory, then run:

```console
python -m pip install --find-links ./wheels "klayout-klink>=0.6.0,<0.7" "vestigraph[klink]>=0.2,<0.3"
python -m vestigraph setup
```

## Upgrade

Stop the old Vestigraph service before upgrading:

```console
python -m pip install --upgrade "klayout-klink>=0.6.0,<0.7" "vestigraph[klink]>=0.2,<0.3"
python -m vestigraph setup
python -m vestigraph doctor --integration
```

Restart KLayout and the MCP client. Standalone mode only needs the Vestigraph package and service restarted. Keep the virtual environment used for setup.

## Disable automatic companion startup

```console
python -m vestigraph companion status
python -m vestigraph companion unregister
```

Unregistering does not delete history and does not immediately stop an already running service. Close all KLayout windows and allow the companion service to exit, or stop a foreground service with Ctrl+C.
