# Command line

Use `python -m vestigraph`. Put global options such as `--repo` before the subcommand.

| Command | Purpose |
| --- | --- |
| `--version` | Print the installed version |
| `doctor` | Check standalone installation |
| `doctor --integration` | Check optional klink, plugin, and companion registration |
| `setup` | Install the KLayout plugin integration and register the companion service |
| `serve --open-browser` | Start the local browser service |
| `init` | Create a history repository |
| `checkpoint FILE --title NAME` | Save a file version |
| `history` / `show ID` | Inspect checkpoints |
| `changes ID` | Show recorded changes for a checkpoint |
| `export ID DEST` | Export a saved file to a new path |
| `stats` | Show storage statistics |
| `fsck` | Check history integrity |
| `rebuild-index DEST` | Rebuild an index into a new directory |
| `companion status` | Show automatic-start registration |
| `companion unregister` | Remove automatic-start registration |

Examples:

```console
python -m vestigraph --repo ./my-history history --limit 20
python -m vestigraph --repo ./my-history show CHECKPOINT_ID
python -m vestigraph --repo ./my-history changes CHECKPOINT_ID --limit 20
python -m vestigraph --repo ./my-history stats
```

Read ids, statuses, and `next_action` values from command output. Fix the specific problem and retry. Do not treat a queued job as a saved file.

`capabilities --mcp-tools` prints storage-query tool descriptions. It does not start an MCP server. The joint MCP path is described in [Local agents and skills](AGENT_LOCAL.md).
