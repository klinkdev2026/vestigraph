# 恢复与数据位置

## 导出旧版本

在面板选择已保存版本下载，或执行：

```console
python -m vestigraph --repo ./my-history export CHECKPOINT_ID ./restored.gds
```

目标文件必须尚不存在，父目录必须存在，目标须位于历史库之外。打开导出副本检查后再决定如何使用。

安装 klink 并连接 KLayout 后，可在面板将旧版本打开到新标签页，不替换原始磁盘文件。

## 数据位置

| 平台 | 默认根目录 |
|---|---|
| Windows | `%LOCALAPPDATA%` 下的 `Vestigraph` |
| macOS | 用户 `Library/Application Support` 下的 `Vestigraph` |
| Linux | `$XDG_DATA_HOME/vestigraph`，未设置时使用用户 `.local/share/vestigraph` |

`VESTIGRAPH_HOME` 覆盖默认根目录。服务配置位于 `state`，默认历史位于 `history`，MCP 导出的技能位于服务状态目录的 `exports`。CLI 历史由 `--repo` 指定。

备份应包括历史和服务状态目录：技能修订保存在服务 catalog 中。先正常停止写入服务，再复制备份；不要只保存预览缓存。使用本地文件系统，网络盘和云同步目录不在并发一致性支持范围内。

## 完整性检查

停止有关写入服务后运行：

```console
python -m vestigraph --repo ./my-history fsck
python -m vestigraph --repo ./my-history rebuild-index ./recovered-history
```

`fsck` 检查历史；`rebuild-index` 向新目录写入索引恢复结果。先保留原始目录，不手动删除锁或修改对象存储。检查结果仅覆盖工具报告的项目。

## 恢复范围

恢复实际进入历史库的文件字节。自动记录的副本不保证等同于编辑器之外的原始文件。外部 PDK、库、仿真环境、选区、撤销栈及未记录的中间态不包含在文件恢复中。
