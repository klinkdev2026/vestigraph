<p align="right">
  <a href="CLI.md">English</a> | <a href="CLI.zh-CN.md">中文</a>
</p>

# 命令行

使用 `python -m vestigraph`。`--repo` 等全局选项放在子命令之前。

| 命令 | 用途 |
| --- | --- |
| `--version` | 打印安装版本 |
| `doctor --integration` | 检查 Klink、插件和 companion 登记 |
| `serve --open-browser` | 为诊断或自定义服务状态启动本地网页服务 |
| `init` | 创建历史库 |
| `checkpoint FILE --title NAME` | 保存文件版本 |
| `history` / `show ID` | 查看检查点 |
| `changes ID` | 查看某个检查点的记录变化 |
| `export ID DEST` | 导出已保存文件到新路径 |
| `stats` | 查看存储统计 |
| `fsck` | 检查历史完整性 |
| `rebuild-index DEST` | 向新目录重建索引 |
| `companion status` | 查看自动启动登记 |
| `companion unregister` | 移除自动启动登记 |
| `setup` | 兼容旧流程的手动插件/companion 设置命令；正常安装使用 Klink 插件安装和 MCP 自动登记 |

底层存储 CLI 示例：

```console
python -m vestigraph --repo ./my-history history --limit 20
python -m vestigraph --repo ./my-history show CHECKPOINT_ID
python -m vestigraph --repo ./my-history changes CHECKPOINT_ID --limit 20
python -m vestigraph --repo ./my-history stats
```

读取输出中的 ID、状态和 `next_action`。按具体问题修正后重试。不要把排队任务当作已保存文件。

`capabilities --mcp-tools` 会打印存储查询工具描述，但不会启动 MCP server。联合 MCP 路径见[本地 agent 与技能](AGENT_LOCAL.zh-CN.md)。
