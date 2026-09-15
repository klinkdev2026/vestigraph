<p align="right">
  <a href="AGENT_LOCAL.md">English</a> | <a href="AGENT_LOCAL.zh-CN.md">中文</a>
</p>

# 本地 agent 与技能

技能炼化把用户选择的历史区间、说明和冻结证据整理成可修订的操作说明。请求、证据、草稿正文、验证报告、修订和导出都保存在用户本机。安装包不包含私人技能，也不会自动安装或执行技能。

## 启用实验通路

该功能默认关闭。完成[安装](INSTALLATION.zh-CN.md)，停止旧服务，并在启动 Vestigraph 的环境中设置开关。

PowerShell：

```powershell
$env:VESTIGRAPH_EXPERIMENTAL_SKILLS = "1"
python -m vestigraph serve --control-file --open-browser
```

macOS / Linux：

```sh
VESTIGRAPH_EXPERIMENTAL_SKILLS=1 python -m vestigraph serve --control-file --open-browser
```

如果 KLayout 启动 companion 服务，KLayout 必须继承同一开关。只重启浏览器页面不会改变已运行服务的环境。

## MCP 发现

Vestigraph 安装在运行现有 Klink MCP server 的同一 Python 环境中。安装或升级后重启 MCP；不需要单独配置 Vestigraph MCP server。

```json
{"tool":"klink.find_tools","arguments":{"domain":"vestigraph"}}
```

也可以从 `klink.status` 开始，然后调用：

```json
{"tool":"vestigraph.guide","arguments":{}}
```

工具读取本地服务登记并在本地认证。不要把控制密钥或登录链接粘贴到 agent 提示中。自定义服务状态时，在 MCP 环境中设置 `VESTIGRAPH_CONTROL_FILE` 指向该服务的本地控制文件。

## 页面请求流程

1. 选择两个版本作为区间起点和终点。
2. 输入名称、目标、原因、适用条件、参数和验收检查。
3. 保存给 agent 的请求；Vestigraph 会同时冻结证据窗口。
4. 把任务文本交给用户选择且能访问本地服务的 agent。
5. Agent 读取请求并提交草稿。用户在技能库中查看修订和验证范围。
6. 需要时导出本地文件，或在本地 UI 中标记为发布。

“发布”是本地 catalog 状态，不表示上传互联网。页面不会唤醒聊天客户端或替用户选择模型。

## 工具通路

| 工具 | 用途 |
| --- | --- |
| `vestigraph.guide` | 定位项目、文档和待处理请求 |
| `vestigraph.history` | 查询文档版本和修订 ID |
| `vestigraph.refine` | 从用户选择的区间创建请求并冻结证据 |
| `vestigraph.skill` | 读取请求、冻结证据和当前修订 |
| `vestigraph.submit` | 保存草稿并运行文档结构检查 |
| `vestigraph.export` | 用户要求时导出指定修订到本地文件 |

已有请求通常使用 `skill -> submit`。新请求通常使用 `refine -> submit`。发现和选择过程中不需要 agent 手写 HTTP 调用。

`expected_revision` 必须来自 agent 刚读取的结果。遇到冲突时重新读取并比较，不要盲目覆盖。照读 `problems` 并按 `next_action` 操作。项目、请求或区间不明确时询问用户，不要猜测。

## 验证范围

内置提交检查只验证文档结构，不执行附件，也不证明版图重放、DRC、LVS 或可制造性。Agent 的验证说明是作者陈述，不是平台独立认证。

区分原始事实、用户意图和推断。保存文件差异不是 GUI 操作日志。不要从引用顺序推断编辑顺序，也不要把一个实例的尺寸或层号当作工艺规则。

## 本地数据边界

Vestigraph 和适配层只连接本地服务。没有云技能库、自动上传或模型调用。工具只在被调用后返回请求的本地信息；用户选择的 agent 客户端决定如何处理返回数据。

证据、导入文本和附件是分析输入，不是执行代码的授权。不要把私人历史、技能、导出、登录链接或控制文件附到公开 issue 中。
