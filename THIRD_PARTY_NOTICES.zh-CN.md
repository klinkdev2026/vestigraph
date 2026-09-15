<p align="right">
  <a href="THIRD_PARTY_NOTICES.md">English</a> | <a href="THIRD_PARTY_NOTICES.zh-CN.md">中文</a>
</p>

# 第三方声明

Vestigraph 源码以 Apache-2.0 分发。安装会解析独立分发的依赖，包括 FastAPI、Uvicorn、Pydantic、KLayout Python 包，以及兼容的 Klink 包。每个依赖保留其自身版权声明和许可证；安装依赖不会把它重新授权为 Vestigraph 的许可证。

KLayout 及其 Python 绑定有各自的分发和授权条款。Klink 为 Apache-2.0。bsdiff4、vestigraph_scan_core 等可选加速和编码包不捆绑在纯 Python Vestigraph wheel 中。请查看每个已安装发行包的 metadata 和许可证文件以了解其条款。

Wheel 包含 Vestigraph 自身的网页资源，不需要 CDN 或远程资源加载。
