<p align="right">
  <a href="RECOVERY.md">English</a> | <a href="RECOVERY.zh-CN.md">中文</a>
</p>

# Recovery and data locations

## Export an old version

Use the panel download action, or run:

```console
python -m vestigraph --repo ./my-history export CHECKPOINT_ID ./restored.gds
```

The destination file must not already exist, its parent directory must exist, and it must be outside the history repository. Open the exported copy and inspect it before deciding how to use it.

When klink is installed and connected to KLayout, the panel can open an old version in a new tab. That does not replace the original file on disk.

## Restore the active KLayout document

After the user explicitly selects a checkpoint, an agent can call `vestigraph.restore`. Vestigraph first saves pending editor changes as their own checkpoint, atomically replaces the saved working file with the selected content, reloads that original path in KLayout, and appends a new checkpoint containing `restore_of` and the user's reason.

Restore is additive: no earlier or later checkpoint is deleted. Repeated restores can therefore target any checkpoint that remains in history. If the new restore checkpoint cannot be committed, Vestigraph restores the working file bytes that were present before the operation.

## Data locations

| Platform | Default root |
| --- | --- |
| Windows | `Vestigraph` under `%LOCALAPPDATA%` |
| macOS | `Vestigraph` under the user's `Library/Application Support` |
| Linux | `$XDG_DATA_HOME/vestigraph`, or `.local/share/vestigraph` under the user home |

`VESTIGRAPH_HOME` overrides the default root. Service configuration is under `state`, default history is under `history`, and MCP skill exports are under the service state's `exports` directory. CLI repositories are selected with `--repo`.

Backups should include both history and service state directories because skill revisions are stored in the service catalog. Stop writers cleanly before copying backups. Do not rely on preview caches. Use a local filesystem; network drives and cloud-sync folders are outside the supported concurrency model.

## Integrity checks

Stop services that may write to the repository, then run:

```console
python -m vestigraph --repo ./my-history fsck
python -m vestigraph --repo ./my-history rebuild-index ./recovered-history
```

`fsck` checks the history repository. `rebuild-index` writes recovered index data to a new directory. Keep the original directory, do not delete lock files by hand, and follow the reported next action.

## Recovery scope

Vestigraph recovers file bytes that were actually saved into history. Automatic-recording copies are not guaranteed to match external files byte-for-byte. External PDKs, libraries, simulation environments, selections, editor undo stacks, and unsaved intermediate states are not part of file recovery.
