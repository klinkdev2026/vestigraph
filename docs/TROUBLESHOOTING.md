<p align="right">
  <a href="TROUBLESHOOTING.md">English</a> | <a href="TROUBLESHOOTING.zh-CN.md">中文</a>
</p>

# Troubleshooting

## HIST does not appear

Confirm KLayout desktop is installed and Klink's plugin is installed:

```console
klink plugin install
```

Restart the MCP client, then restart KLayout. Open a saved GDS/OASIS file and click **HIST**. Use `python -m vestigraph doctor --integration` to check packages, plugin installation, and companion registration. Use the same Python environment for Vestigraph, Klink MCP, and companion registration.

## MCP does not show Vestigraph tools

Confirm Vestigraph is installed in the Python environment that runs Klink MCP. Restart MCP after installation or upgrade. Check extension load errors in `klink.status`, then query `klink.find_tools` with `domain="vestigraph"`. No separate Vestigraph MCP server is needed. Installing Python packages does not configure arbitrary chat clients.

## Tools are discoverable but the service is unavailable

Check whether an old service is still running. Start `python -m vestigraph serve --control-file` only for diagnosis or a custom local service state, then call `vestigraph.guide`. For custom service state, set `VESTIGRAPH_CONTROL_FILE` in the MCP environment. Do not copy control secrets into chat.

## Skill refinement is not enabled

Set `VESTIGRAPH_EXPERIMENTAL_SKILLS=1` and restart the Vestigraph service path that KLayout or MCP uses. Restarting only the browser page or MCP client is not enough if the running service did not inherit the switch.

## Revision conflict

Another window or agent saved a revision. Read the latest state, compare it, and submit again with the new `expected_revision`. Do not replay stale content directly.

## Panel opens but nothing records

Open a saved GDS/OASIS file, then check the current session, document, pause state, and capture errors in the panel. A successful doctor check means installation is valid; it does not mean the current editor window is recording.

## Preview fails or large files are slow

Preview is bounded by time and memory budgets. Export the saved version or narrow the target. Whole-file capture can temporarily use editor resources and does not promise every intermediate state.

## History repository is busy

Stop services or recording processes that write to it, then retry. Do not delete locks by hand. See [Recovery](RECOVERY.md) for integrity checks.
