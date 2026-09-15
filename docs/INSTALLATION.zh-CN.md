<p align="right">
  <a href="INSTALLATION.md">English</a> | <a href="INSTALLATION.zh-CN.md">中文</a>
</p>

# 安装与升级

## 要求

Vestigraph 需要 Python 3.10 或更新版本、KLayout 桌面 0.30.x、`klayout-klink>=0.6.0,<0.7`，以及 `vestigraph-scan-core`。正常 `vestigraph` 包会安装兼容 Klink 和扫描器依赖。Python `klayout` 包用于离线预览；它不会安装 KLayout 桌面应用。

Klink 负责 KLayout 插件安装。首次安装和升级都使用 `klink plugin install`。

## 从 PyPI 安装

下面的命令安装已经发布到 PyPI 的版本。对于尚未发布到 PyPI 的版本，请使用下面的 [GitHub Actions release artifacts](https://github.com/klinkdev2026/vestigraph/actions/workflows/release.yml)。

```console
python -m pip install vestigraph
klink plugin install
```

重启运行 `klink-mcp` 的 MCP 客户端。Klink 扩展注册表会在该 Python 环境中发现 Vestigraph 并登记本地 companion。不需要单独的 Vestigraph MCP server。扫描器会优先使用 Rust `vestigraph-scan-core` 后端；如果本机 wheel 不可用，则带诊断原因回退到 Python。受支持 wheel 平台上的用户通常不需要本地 Rust 工具链。

重启 KLayout，打开已保存的 GDS/OASIS，点击 **HIST**。面板会打开本地历史 UI 并显示实时记录状态。需要检查安装时运行：

```console
python -m vestigraph doctor --integration
```

安装 Python 包不会自动配置任意聊天客户端。请配置或重启你实际使用的 MCP 客户端。

## PyPI 发布前的 artifacts

未发布版本请使用 [GitHub Actions release workflow](https://github.com/klinkdev2026/vestigraph/actions/workflows/release.yml) 生成的 wheel 和 sdist artifacts。不要假定未发布包已存在于 PyPI。

Vestigraph artifact 安装：

```console
python -m pip install --find-links ./wheels "vestigraph-scan-core" ./wheels/vestigraph-0.2.0-py3-none-any.whl
klink plugin install
```

在两个项目都发布到 PyPI 之前，下载由匹配的 Klink 与 Vestigraph 修订构建的 artifacts。把两个 Klink Rust wheel、`klayout_klink` core wheel、`vestigraph_scan_core` wheel 和 Vestigraph wheel 放入同一个本地目录，然后运行：

```console
python -m pip install --find-links ./wheels "klayout-klink>=0.6.0,<0.7" "vestigraph-scan-core" "vestigraph>=0.2,<0.3"
klink plugin install
```

随后重启 MCP，重启 KLayout，打开已保存版图并点击 **HIST**。

## 升级

如果旧 Vestigraph 服务正在运行，先停止它。升级包和 Klink 插件，然后重启 MCP 与 KLayout：

```console
python -m pip install --upgrade "vestigraph>=0.2,<0.3"
klink plugin install
python -m vestigraph doctor --integration
```

Klink MCP、Vestigraph 和 KLayout companion 登记使用同一个 Python 环境。`KLAYOUT_HOME` 选择 KLayout 配置目录。`KLINK_REGISTRY_ROOT` 选择本机会话注册表。数据位置见[恢复](RECOVERY.zh-CN.md)。

## 底层存储 CLI

Vestigraph 仍包含本地存储 CLI，可显式保存文件检查点并导出版本。它适合恢复任务和测试，但主要用户安装路径是完整的 Klink-backed KLayout 历史流程。

## 停用自动 companion 启动

```console
python -m vestigraph companion status
python -m vestigraph companion unregister
```

取消登记不会删除历史，也不会立即停止已经运行的服务。关闭全部 KLayout 窗口并等待 companion 退出，或用 Ctrl+C 停止前台服务。
