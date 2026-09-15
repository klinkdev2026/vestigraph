<p align="right">
  <a href="PUBLIC_RELEASE.md">English</a> | <a href="PUBLIC_RELEASE.zh-CN.md">中文</a>
</p>

# 发布范围与兼容性

## 完整产品路径

公开包安装本地文件和 KLayout 历史。与同一 Python 环境中的兼容 Klink 配合后，Vestigraph 会记录 KLayout 中已保存的 GDS/OASIS 文档，提供 HIST 入口、暂停/恢复控制、在新 KLayout 标签页打开旧版本，并通过现有 Klink MCP server 暴露本地工具。

## 底层存储功能

存储层包括文件检查点、版本查询、变化记录、字节导出、本地浏览器 UI、GDS/OASIS 预览、命名、旧文件导入、历史完整性检查，以及向新目录恢复索引。这些能力保持本地，并由完整 KLayout 流程使用。

## 实验技能功能

显式启用后，Vestigraph 支持区间请求、冻结证据、草稿提交、文档结构验证、修订、本地发布状态和文件导出。它不会自行运行脚本、安装技能、调用模型或联系托管服务。

## 兼容性

Vestigraph 需要 Python 3.10 或更新版本、KLayout 桌面 0.30.x，以及 Klink 0.6.0 或兼容的后续 0.6.x。Klink 0.5.x 不提供 0.6 companion 和工具发现约定。基础包安装 Python 预览引擎。基本历史和恢复不需要可选扫描器或 delta encoder 包。

GitHub Actions 会在配置的 Python 与操作系统矩阵上构建并测试公开包。发布使用经过审核的 tag 和 OIDC trusted publishing 的仓库 CI/CD 路径。

## 边界

服务面向可信本机用户。它不提供远程协作、云同步、自动合并或完整编辑器环境恢复。GDS 可提供结构化变化；OASIS 会保存并预览，但没有专用结构化历史分析。大文件受捕获时间、存储和预览预算限制。

操作系统和可选组件支持以实际 CI 和环境检查为准。如果缺少所需 native 或浏览器依赖，本地环境仍可能失败。
