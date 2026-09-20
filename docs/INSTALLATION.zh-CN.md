<p align="right">
  <a href="INSTALLATION.md">English</a> | <a href="INSTALLATION.zh-CN.md">中文</a>
</p>

# 安装与升级

## 要求

Vestigraph 需要 Python 3.10 或更新版本、KLayout 桌面 0.30.x、`klayout-klink>=0.6.0,<0.7`，以及 `vestigraph-scan-core`。正常 `vestigraph` 包会安装兼容 Klink 和扫描器依赖。Python `klayout` 包用于离线预览；它不会安装 KLayout 桌面应用。

Klink 负责 KLayout 插件安装。首次安装和升级都使用 `klink plugin install`。

## 从 PyPI 安装

Vestigraph 0.2.3 已发布到 [PyPI](https://pypi.org/project/vestigraph/0.2.3/)。

```console
python -m pip install vestigraph
klink plugin install
# 重启运行 klink-mcp 的 MCP 客户端
# 重启 KLayout，打开已保存的 GDS/OASIS 版图，然后点击 HIST
python -m vestigraph doctor --integration
```

`pip install vestigraph` 会安装兼容的 `klayout-klink` 和 `vestigraph-scan-core` 依赖。原生模块可用时自动使用 Rust 扫描；如果模块无法导入，则带诊断原因回退到 Python 扫描器。受支持的 Linux、macOS、Windows wheel 平台不需要本地 Rust 工具链。

`klink plugin install` 用于安装或升级 KLayout 插件。重启 MCP 客户端后，现有 Klink MCP server 会发现 Vestigraph，并为当前 Python 环境登记本地 companion。不需要额外 MCP server，也不需要运行 `vestigraph setup`。安装 Python 包不会自动配置聊天客户端。

重启 KLayout 后，打开已保存的 GDS/OASIS 版图并点击 **HIST**，即可打开本地历史网页。开始编辑前请确认记录已启用。

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
