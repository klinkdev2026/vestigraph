<p align="right">
  <a href="RECOVERY.md">English</a> | <a href="RECOVERY.zh-CN.md">中文</a>
</p>

# 恢复与数据位置

## 导出旧版本

使用面板下载操作，或运行：

```console
python -m vestigraph --repo ./my-history export CHECKPOINT_ID ./restored.gds
```

目标文件不得已存在，父目录必须存在，并且目标必须位于历史库之外。使用前先打开导出副本检查。

安装并连接 Klink/KLayout 后，面板可以把旧版本打开到新的 KLayout 标签页。这不会替换磁盘上的原始文件。

## 恢复当前 KLayout 工作版图

用户明确选择检查点后，Agent 可以调用 `vestigraph.restore`。Vestigraph 会先把编辑器中待处理的修改保存为独立检查点，再以原子方式用所选内容替换已保存的工作文件，从原路径重新载入 KLayout，并追加一个包含 `restore_of` 和用户原因的新检查点。

恢复采用追加方式：不会删除更早或更晚的任何检查点。因此，多次恢复仍可选择历史中保留的任一检查点。如果新的恢复检查点无法提交，Vestigraph 会把工作文件回滚到操作前的字节内容。

## 数据位置

| 平台 | 默认根目录 |
| --- | --- |
| Windows | `%LOCALAPPDATA%` 下的 `Vestigraph` |
| macOS | 用户 `Library/Application Support` 下的 `Vestigraph` |
| Linux | `$XDG_DATA_HOME/vestigraph`，未设置时为用户目录下的 `.local/share/vestigraph` |

`VESTIGRAPH_HOME` 覆盖默认根目录。服务配置位于 `state`，默认历史位于 `history`，MCP 技能导出位于服务状态目录的 `exports`。CLI 历史由 `--repo` 选择。

备份应包括历史目录和服务状态目录，因为技能修订保存在服务 catalog 中。复制备份前请正常停止写入服务。不要只备份预览缓存。请使用本地文件系统；网络盘和云同步目录不在并发一致性支持范围内。

## 完整性检查

停止可能写入历史库的服务后运行：

```console
python -m vestigraph --repo ./my-history fsck
python -m vestigraph --repo ./my-history rebuild-index ./recovered-history
```

`fsck` 检查历史库。`rebuild-index` 向新目录写入恢复的索引数据。保留原目录，不要手动删除锁文件，并按报告的 `next_action` 处理。

## 恢复范围

Vestigraph 恢复真正进入历史库的文件字节。自动记录副本不保证与外部原始文件逐字节相同。外部 PDK、库、仿真环境、选择集、编辑器撤销栈和未保存中间态不属于文件恢复范围。
