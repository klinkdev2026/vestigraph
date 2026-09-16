<p align="right">
  <a href="./README.md">English</a> | <a href="./README.zh-CN.md">中文</a>
</p>

# Vestigraph

Vestigraph 是面向 KLayout 用户的本地文件与版图历史。它把版本保存在用户本机，提供浏览器时间线、GDS/OASIS 预览、旧文件导入，以及已保存版本导出恢复。

Vestigraph 需要 Python 3.10 或更新版本、KLayout 桌面 0.30.x、`klayout-klink>=0.6.0,<0.7`，以及 `vestigraph-scan-core` 扫描器包。正常安装会解析 Klink 和扫描器依赖。使用 Klink 命令安装 KLayout 插件，重启 MCP 使 Vestigraph 为当前 Python 环境自动登记本地 companion，然后重启 KLayout 并打开 **HIST**。

## 功能

- 在本地用户存储中保存文件检查点和历史数据。
- 运行仅绑定 loopback 的浏览器服务，用于浏览、命名、导入和导出历史。
- 使用 Python `klayout` 包预览 GDS/OASIS 版本。
- 使用 Klink 的 KLayout 插件和 companion 服务通路，自动记录已保存的 GDS/OASIS 文档。
- 通过现有 Klink MCP 扩展注册表暴露本地历史和技能炼化工具。
- 历史、证据、草稿、修订、导出、登录链接和控制文件均保存在用户本机。

Vestigraph 不提供云同步、远程协作、托管存储、模型服务，也不会自动配置聊天客户端。

## 安装

Vestigraph 0.2.0 已发布到 [PyPI](https://pypi.org/project/vestigraph/0.2.0/)。

```console
python -m pip install vestigraph
klink plugin install
# 重启运行 klink-mcp 的 MCP 客户端
# 重启 KLayout，打开已保存的 GDS/OASIS 版图，然后点击 HIST
python -m vestigraph doctor --integration
```

`pip install vestigraph` 会安装兼容的 `klayout-klink` 和 `vestigraph-scan-core` 依赖。原生模块可用时自动使用 Rust 扫描；如果模块无法导入，则带诊断原因回退到 Python 扫描器。受支持的 Linux、macOS、Windows wheel 平台不需要本地 Rust 工具链。

`klink plugin install` 用于安装或升级 KLayout 插件。重启 MCP 客户端后，现有 Klink MCP server 会发现 Vestigraph，并为当前 Python 环境登记本地 companion。不需要额外 MCP server，也不需要运行 `vestigraph setup`。安装 Python 包不会自动配置聊天客户端。

重启 KLayout 后，打开已保存的 GDS/OASIS 版图并点击 **HIST**，即可打开本地历史网页。开始编辑前请确认记录已启用。

## 升级

如果旧 Vestigraph 服务正在运行，先停止它。升级包和 Klink 插件，然后重启 MCP 与 KLayout：

```console
python -m pip install --upgrade "vestigraph>=0.2,<0.3"
klink plugin install
python -m vestigraph doctor --integration
```

底层存储 CLI 仍可保存和导出本地文件版本，但它不是主要产品安装路径。完整 KLayout 历史、HIST 和本地 agent 工具都需要 Klink。

## 本地技能与 agent

技能炼化是实验功能，默认关闭。启用后，用户可以选择历史区间、保存请求、冻结证据、让选定的本地 agent 提交草稿、查看验证反馈、保存修订并导出文件。安装包不包含私人技能，也不会自行调用模型。

安装并重启 MCP 后，使用现有 Klink MCP server：

```json
{"tool":"klink.find_tools","arguments":{"domain":"vestigraph"}}
```

然后调用 `vestigraph.guide` 并按 `next_action` 操作。参见[本地 agent 与技能](docs/AGENT_LOCAL.zh-CN.md)。

## 文档

- [安装与升级](docs/INSTALLATION.zh-CN.md)
- [文件与历史](docs/HISTORY.zh-CN.md)
- [恢复与数据位置](docs/RECOVERY.zh-CN.md)
- [命令行](docs/CLI.zh-CN.md)
- [本地 agent 与技能](docs/AGENT_LOCAL.zh-CN.md)
- [故障排查](docs/TROUBLESHOOTING.zh-CN.md)
- [发布范围](docs/PUBLIC_RELEASE.zh-CN.md)

Vestigraph 使用 Apache-2.0 许可证。另见[安全与本地访问](SECURITY.md)、[第三方声明](THIRD_PARTY_NOTICES.md)和[变更记录](CHANGELOG.md)。
