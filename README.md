# Vestigraph

Vestigraph 在用户本地保存文件版本和版图历史，提供时间轴、预览、旧版本导入与恢复。基础功能可以独立安装；**推荐与 klink 一起安装**，获得 KLayout 自动记录、HIST 按钮和现有 klink MCP 中的历史与技能工具。

需要 Python 3.10+。基础安装包含网页服务和 KLayout Python 预览引擎，不要求 KLayout 桌面、klink、Git、Rust 或 AI 账号。桌面集成需要另行安装 KLayout 0.30.x。

## 独立安装

```console
python -m pip install "vestigraph==0.2.0"
python -m vestigraph doctor
python -m vestigraph serve --open-browser
```

独立模式可保存、导入、浏览和导出文件版本，不需要运行 `setup`。没有 klink 时，编辑器自动记录和在 KLayout 中打开版本不可用。

```console
python -m vestigraph --repo ./my-history init
python -m vestigraph --repo ./my-history checkpoint ./chip.gds --title first
python -m vestigraph --repo ./my-history history
python -m vestigraph --repo ./my-history export CHECKPOINT_ID ./restored.gds
```

将 `CHECKPOINT_ID` 替换为返回的完整 ID。导出目标不得已存在，父目录必须存在，目标须位于历史库之外。如何把 CLI 历史接入网页，见[文件与历史](docs/HISTORY.md)。

## 推荐：与 klink 联合安装

在运行 klink MCP 的同一个 Python 环境中安装：

```console
python -m pip install "klayout-klink>=0.6.0,<0.7" "vestigraph[klink]==0.2.0"
python -m vestigraph setup
python -m vestigraph doctor --integration
```

重启 KLayout，打开已保存的 GDS/OASIS，点击 **HIST**。确认面板显示正在记录，再进行编辑。关闭网页不会停止伴随服务。

若使用本地发行包，在两个 wheel 所在目录执行：

```console
python -m pip install ./klayout_klink-0.6.0-py3-none-any.whl ./vestigraph-0.2.0-py3-none-any.whl
python -m vestigraph setup
```

PyPI 命令适用于对应版本已经可下载时；本地 wheel 安装不要求这两个版本已上传，但其余依赖仍须可获取。

## 升级与同步

先退出旧的 Vestigraph 服务，再升级两个包并同步插件：

```console
python -m pip install --upgrade "klayout-klink>=0.6.0,<0.7" "vestigraph[klink]>=0.2,<0.3"
python -m vestigraph setup
python -m vestigraph doctor --integration
```

随后重启 KLayout 和 MCP 客户端。`setup` 同步插件并登记本地伴随服务，不迁移或上传历史。保留设置时使用的 Python 环境。

## 本地技能炼化与 MCP

实验功能支持选择历史区间、保存解释、冻结证据、由用户选定的 agent 提交草稿、检查文档结构、保存修订和导出本地文件。

联合安装并重启 MCP 后，已配置的 klink MCP 可发现 Vestigraph 工具。安装 Python 包不会自动配置任意聊天客户端或启动 agent。

```json
{"tool":"klink.find_tools","arguments":{"domain":"vestigraph"}}
```

然后调用 `vestigraph.guide`，按返回指引操作。启用与完整流程见[本地 Agent 与技能](docs/AGENT_LOCAL.md)。

## 数据与功能范围

历史、证据、技能及导出均保存在用户本地。软件不提供云同步或自动上传。默认使用用户应用数据目录，可用 `VESTIGRAPH_HOME` 指定位置。私人历史和技能不属于产品发行包。

预览受资源预算限制，预览失败不影响已保存文件的导出。恢复对象是实际保存的文件，不包含编辑器撤销栈、外部 PDK、完整项目环境或每一个编辑中间态。OASIS 支持字节保存与预览，尚无专用结构化历史分析。

## 文档

- [安装与联合升级](docs/INSTALLATION.md)
- [文件与历史](docs/HISTORY.md)
- [恢复与数据位置](docs/RECOVERY.md)
- [命令行](docs/CLI.md)
- [本地 Agent 与技能](docs/AGENT_LOCAL.md)
- [常见问题](docs/TROUBLESHOOTING.md)
- [功能范围](docs/PUBLIC_RELEASE.md)

许可证为 [Apache-2.0](LICENSE)。另见[本地访问边界](SECURITY.md)、[第三方声明](THIRD_PARTY_NOTICES.md)与[版本变化](CHANGELOG.md)。
