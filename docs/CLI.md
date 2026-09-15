# 命令行

使用 `python -m vestigraph`；`--help` 显示选项，`--repo` 放在子命令之前。

| 命令 | 用途 |
|---|---|
| `--version` | 查看版本 |
| `doctor` | 检查独立安装 |
| `doctor --integration` | 检查可选 klink、插件与登记 |
| `setup` | 安装 KLayout 插件并登记伴随服务 |
| `serve --open-browser` | 启动本地网页 |
| `init` | 创建历史库 |
| `checkpoint FILE --title NAME` | 保存文件 |
| `history` / `show ID` | 查询检查点 |
| `changes ID` | 查看记录变化 |
| `export ID DEST` | 导出文件到新路径 |
| `stats` | 查看存储统计 |
| `fsck` | 检查历史完整性 |
| `rebuild-index DEST` | 向新目录恢复索引 |
| `companion status` | 查看自动启动登记 |
| `companion unregister` | 取消自动启动登记 |

```console
python -m vestigraph --repo ./my-history history --limit 20
python -m vestigraph --repo ./my-history show CHECKPOINT_ID
python -m vestigraph --repo ./my-history changes CHECKPOINT_ID --limit 20
python -m vestigraph --repo ./my-history stats
```

读取输出中的 ID、状态与 `next_action`，按具体错误修正后重试，不把排队任务当作已保存文件。

`capabilities --mcp-tools` 输出存储查询工具描述，不启动 MCP 服务。联合使用入口见[本地 Agent 与技能](AGENT_LOCAL.md)。
