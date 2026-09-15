<p align="right">
  <a href="TROUBLESHOOTING.md">English</a> | <a href="TROUBLESHOOTING.zh-CN.md">中文</a>
</p>

# 故障排查

## HIST 不出现

确认已安装 KLayout 桌面，并安装了 Klink 插件：

```console
klink plugin install
```

重启 MCP 客户端，然后重启 KLayout。打开已保存的 GDS/OASIS 文件并点击 **HIST**。使用 `python -m vestigraph doctor --integration` 检查包、插件安装和 companion 登记。Vestigraph、Klink MCP 和 companion 登记应使用同一个 Python 环境。

## MCP 没有 Vestigraph 工具

确认 Vestigraph 安装在运行 Klink MCP 的 Python 环境中。安装或升级后重启 MCP。查看 `klink.status` 中的扩展加载错误，然后用 `domain="vestigraph"` 查询 `klink.find_tools`。不需要单独的 Vestigraph MCP server。安装 Python 包不会自动配置任意聊天客户端。

## 工具可发现但服务不可用

检查是否还有旧服务在运行。仅在诊断或自定义本地服务状态时启动 `python -m vestigraph serve --control-file`，然后调用 `vestigraph.guide`。自定义服务状态时，在 MCP 环境中设置 `VESTIGRAPH_CONTROL_FILE`。不要把控制密钥复制到聊天中。

## 技能炼化未启用

设置 `VESTIGRAPH_EXPERIMENTAL_SKILLS=1`，并重启 KLayout 或 MCP 使用的 Vestigraph 服务通路。只重启浏览器页面或 MCP 客户端不足以改变已运行服务的环境。

## 修订冲突

另一个窗口或 agent 已保存修订。读取最新状态、比较后用新的 `expected_revision` 再提交。不要直接重放过期正文。

## 面板打开但没有记录

打开已保存的 GDS/OASIS 文件，然后检查面板中的当前会话、文档、暂停状态和捕获错误。doctor 成功说明安装有效，但不代表当前编辑窗口正在记录。

## 预览失败或大文件很慢

预览受时间和内存预算限制。可以导出已保存版本或缩小目标。整文件捕获可能暂时使用编辑器资源，也不承诺记录每个中间状态。

## 历史库被占用

停止会写入它的服务或记录进程后重试。不要手动删除锁。完整性检查见[恢复](RECOVERY.zh-CN.md)。
