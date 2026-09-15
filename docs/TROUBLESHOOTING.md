# 常见问题

## 独立安装没有 klink

基础文件历史不要求 klink。用 `doctor` 检查基础安装；需要自动记录、HIST 或联合 MCP 时才安装 `vestigraph[klink]` 并运行 `setup`。

## HIST 不出现

确认 KLayout 桌面已安装，运行 `python -m vestigraph setup`，随后重启 KLayout。用 `doctor --integration` 检查插件与伴随服务登记。设置和启动时使用一致的配置目录及注册表环境。

## MCP 没有 Vestigraph 工具

确认 Vestigraph 安装在运行 klink MCP 的 Python 中，安装后重启 MCP。查看 `klink.status` 的扩展加载失败，再用 `klink.find_tools` 查询 `vestigraph`。安装 Python 包不会自动配置任意聊天客户端。

## 工具可发现，但服务不可用

检查旧服务是否仍在运行。启动 `python -m vestigraph serve --control-file`，再调用 `vestigraph.guide`。自定义状态目录通过 MCP 环境中的 `VESTIGRAPH_CONTROL_FILE` 指定。不要复制控制密钥到聊天。

## 技能炼化未启用

设置 `VESTIGRAPH_EXPERIMENTAL_SKILLS=1` 后重启 Vestigraph 服务，仅重启网页或 MCP 不够。

## 修订冲突

另一个窗口或 agent 已保存内容。按返回的指引重新读取，比较最新修订再提交，不直接重放过期正文。

## 面板打开但没有记录

打开已保存的 GDS/OASIS，检查会话、文档、暂停状态和捕获错误。`doctor` 成功不代表正在记录。独立模式没有编辑器自动记录。

## 预览失败或大文件缓慢

预览受资源预算限制，可导出已保存版本或缩小范围。整文件捕获可能暂时占用编辑器，不保证记录所有中间态。

## 历史库被占用

正常停止占用它的服务或录制进程后重试，不手动删除锁。检查方法见[恢复](RECOVERY.md)。
