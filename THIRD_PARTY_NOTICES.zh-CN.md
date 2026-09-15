<p align="right">
  <a href="THIRD_PARTY_NOTICES.md">English</a> | <a href="THIRD_PARTY_NOTICES.zh-CN.md">中文</a>
</p>

# 第三方声明

Vestigraph 源码以 Apache-2.0 分发。安装会解析独立分发的依赖，包括 FastAPI、Uvicorn、Pydantic、KLayout Python 包、兼容的 Klink 包，以及 `vestigraph-scan-core` 扫描器包。每个依赖保留其自身版权声明和许可证；安装依赖不会把它重新授权为 Vestigraph 的许可证。

KLayout 及其 Python 绑定有各自的分发和授权条款。Klink 单独分发。扫描器包在受支持平台上以独立 native wheel 分发；用户通常不需要本地 Rust 工具链。如果 native 扫描器不可用，Vestigraph 会报告原因并回退到 Python 扫描器。

`bsdiff4` 等可选 delta 编码包只在请求相关功能时解析。请查看每个已安装发行包的 metadata 和许可证文件以了解其条款。native 扫描器依赖清单记录在公开 release tree 的 `native/scan_core/THIRD_PARTY.md` 中。

Wheel 包含 Vestigraph 自身网页资源，不需要 CDN 或远程资源加载。
