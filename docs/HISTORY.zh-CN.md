<p align="right">
  <a href="HISTORY.md">English</a> | <a href="HISTORY.zh-CN.md">中文</a>
</p>

# 文件与历史

## 保存文件版本

```console
python -m vestigraph --repo ./my-history init
python -m vestigraph --repo ./my-history checkpoint ./chip.gds --title initial
python -m vestigraph --repo ./my-history history
```

每个检查点都有独立 ID。检查点保存输入文件当时的字节；后续编辑源文件不会改变已保存版本。

## 在网页服务中浏览本地历史

这些命令应在产品源码树和准备发布的 Git 仓库之外的本地目录运行。工作区、历史根目录和服务状态目录应彼此分开，不能互相包含。

```console
python -m vestigraph service init --state ./service-state
python -m vestigraph service add-project --state ./service-state --name MyProject --workspace ./workspace --history-root ./history-storage
python -m vestigraph service add-history --state ./service-state --project PROJECT_ID --path ./my-history
python -m vestigraph serve --state ./service-state --open-browser
```

`PROJECT_ID` 使用 `add-project` 返回值。已有历史默认只读接入；只有希望浏览器服务继续写入该历史时才加 `--writable`。历史应保存在产品源码和 Git 仓库之外。

## 自动记录 KLayout

完成[安装](INSTALLATION.zh-CN.md)后，重启 MCP，重启 KLayout，打开已保存文件并点击 **HIST**。确认面板显示当前窗口和文档正在记录后再编辑。可以暂停、恢复记录，也可以给重要状态命名。

记录版本是从 KLayout 导出的副本，可能与磁盘原文件不同。排队中、捕获中或失败的条目不是已保存版本。自动记录不承诺捕获每一个中间编辑状态。

## 导入旧文件

在文档导入操作中选择本机旧文件，检查预览计划、顺序和目标文档后再确认。导入顺序不是原始编辑顺序的证明。重复、失败和冲突以面板说明为准。

## 预览与说明

选择已保存版本可以预览并编辑名称或说明。大版图受时间和内存预算限制。预览失败不改变已存历史文件。名称、说明和技能草稿都是元数据，不是版图文件本身。

导出和在新 KLayout 标签页打开旧版本见[恢复](RECOVERY.zh-CN.md)。
