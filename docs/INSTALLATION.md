# 安装与联合升级

## 选择安装方式

| 安装方式 | 功能 |
|---|---|
| `vestigraph` | 本地文件历史、网页、GDS/OASIS 预览、导入与导出 |
| `vestigraph[klink]`，推荐 | 增加兼容的 klink；设置后自动记录 KLayout，并通过已有 klink MCP 使用本地工具 |

需要 Python 3.10+。Python 的 `klayout` 包用于离线预览，不会安装 KLayout 桌面。桌面集成使用 KLayout 0.30.x 和 klink 0.6.0 或兼容的后续 0.6.x。

## 独立模式

```console
python -m pip install "vestigraph==0.2.0"
python -m vestigraph doctor
python -m vestigraph serve --open-browser
```

不需要 `setup`，前台服务用 Ctrl+C 退出。已有 CLI 历史可按[文件与历史](HISTORY.md)接入网页。

## KLayout 集成

在运行 klink MCP 的同一个 Python 环境安装：

```console
python -m pip install "klayout-klink>=0.6.0,<0.7" "vestigraph[klink]==0.2.0"
python -m vestigraph setup
python -m vestigraph doctor --integration
```

重启 KLayout，打开已保存的版图，点击 HIST。`doctor --integration` 只检查安装、插件和登记，不连接编辑器；实际录制状态以面板为准。

默认网页端口是 8787，与编辑器 RPC 端口不同。`setup --port 8788` 修改首选网页端口；伴随服务启动时可选择后续可用端口。

`KLAYOUT_HOME` 指定 KLayout 配置目录，`KLINK_REGISTRY_ROOT` 指定会话注册表。设置与启动 KLayout 时使用相同环境。数据位置见[恢复](RECOVERY.md)。

## 本地发行包

在两个 wheel 所在目录执行：

```console
python -m pip install ./klayout_klink-0.6.0-py3-none-any.whl ./vestigraph-0.2.0-py3-none-any.whl
python -m vestigraph setup
```

基础模式只需 Vestigraph wheel。完整离线安装还需准备所有依赖 wheel；两个产品 wheel 本身不包含全部第三方依赖。

## 同步升级

先退出旧 Vestigraph 服务，再执行：

```console
python -m pip install --upgrade "klayout-klink>=0.6.0,<0.7" "vestigraph[klink]>=0.2,<0.3"
python -m vestigraph setup
python -m vestigraph doctor --integration
```

重启 KLayout 和 MCP 客户端。独立模式只升级 Vestigraph 并重启其服务。保留设置时使用的虚拟环境。

## 停用自动启动

```console
python -m vestigraph companion status
python -m vestigraph companion unregister
```

取消登记不删除历史，也不立即结束已经运行的服务。关闭全部 KLayout 窗口后，伴随服务在空闲超时后退出；手动前台服务用 Ctrl+C 退出。
