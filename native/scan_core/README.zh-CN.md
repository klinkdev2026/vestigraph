<p align="right">
  <a href="README.md">English</a> | <a href="README.zh-CN.md">中文</a>
</p>

# vestigraph-scan-core

`vestigraph-scan-core` 是 Vestigraph 完整安装使用的 native 扫描器包。Vestigraph runtime 会优先尝试这个 Rust 后端；如果 native 模块无法导入，则带诊断原因回退到 Python 扫描器。

## 提供的功能

- 为本地历史捕获和预览元数据提供有边界的 GDS 记录扫描。
- 生成 Vestigraph 本地历史流程使用的哈希和记录偏移。
- 提供名为 `vestigraph_scan_core` 的 Python 扩展模块。

## 安装模型

用户通常通过 `pip install vestigraph` 间接安装本包；该命令会同时解析兼容的扫描器 wheel 和 `klayout-klink`。受支持的发布平台提供预构建 wheel，因此正常安装不需要本地 Rust 工具链。

在 PyPI 发布前从 GitHub Actions artifacts 安装时，把匹配的 `vestigraph_scan_core` wheel 与 Vestigraph wheel 放在同一个本地 wheel 目录，并使用 `--find-links` 安装。

## 构建与发布

公开 wheel 由 Vestigraph GitHub Actions release workflow 从审计过的公开 release tree 构建。维护者应在发布前核验生成的 wheel metadata 和第三方声明。最终用户通常不需要本地构建本包。

许可证和依赖条款通过包 metadata 以及本目录的公开 `THIRD_PARTY.md` 文件报告。
