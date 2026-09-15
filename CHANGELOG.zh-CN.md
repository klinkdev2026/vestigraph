<p align="right">
  <a href="CHANGELOG.md">English</a> | <a href="CHANGELOG.zh-CN.md">中文</a>
</p>

# 变更记录

## 0.2.0

- 完整 KLayout 历史路径使用兼容 Klink 作为必需依赖，用于 KLayout 插件、HIST 入口、companion 服务和 MCP 工具发现。
- 完整安装包含 `vestigraph-scan-core`；扫描会优先使用 Rust 后端，并在 native wheel 不可用时带诊断回退到 Python。受支持 wheel 平台不需要本地 Rust 工具链。
- 本地文件历史、GDS/OASIS 预览、导入、恢复和字节导出均保存在用户本地存储中。
- 重启现有 Klink MCP 进程即可发现 Vestigraph 工具；不需要单独的 Vestigraph MCP server。
- KLayout 插件首次安装和升级仍使用 Klink 命令路径（`klink plugin install`）。
- 本地技能请求、冻结证据、带修订的草稿和导出功能位于显式实验开关之后。
- 经认证的 loopback 调用使用结构化参数、修订检查和限定范围的验证报告。
