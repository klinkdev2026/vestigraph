<p align="right">
  <a href="CLI.md">English</a> | <a href="CLI.zh-CN.md">中文</a>
</p>

# Command line

Use `python -m vestigraph`. Put global options such as `--repo` before the subcommand.

| Command | Purpose |
| --- | --- |
| `--version` | Print the installed version |
| `doctor --integration` | Check Klink, plugin, and companion registration |
| `serve --open-browser` | Start the local browser service for diagnosis or custom service state |
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
| `setup` | Legacy compatibility command for manual plugin/companion setup; normal installs use Klink plugin install plus MCP auto-registration |

Examples for the underlying storage CLI:

```console
python -m vestigraph --repo ./my-history history --limit 20
python -m vestigraph --repo ./my-history show CHECKPOINT_ID
python -m vestigraph --repo ./my-history changes CHECKPOINT_ID --limit 20
python -m vestigraph --repo ./my-history stats
```

Read ids, statuses, and `next_action` values from command output. Fix the specific problem and retry. Do not treat a queued job as a saved file.

`capabilities --mcp-tools` prints storage-query tool descriptions. It does not start an MCP server. The joint MCP path is described in [Local agents and skills](AGENT_LOCAL.md).
