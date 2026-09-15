# 文件与历史

## 保存文件版本

```console
python -m vestigraph --repo ./my-history init
python -m vestigraph --repo ./my-history checkpoint ./chip.gds --title initial
python -m vestigraph --repo ./my-history history
```

每个检查点有独立 ID。CLI 保存输入文件当时的字节；后续编辑原文件不改变已有版本。

## 网页浏览 CLI 历史

在 Git 仓库之外的本地目录执行下面的命令。先准备已有的 `./workspace` 目录作为待管理文件位置；`./history-storage` 使用空目录或新目录。工作区、历史根目录和服务状态目录应彼此分开，不能互相包含。

```console
python -m vestigraph service init --state ./service-state
python -m vestigraph service add-project --state ./service-state --name MyProject --workspace ./workspace --history-root ./history-storage
python -m vestigraph service add-history --state ./service-state --project PROJECT_ID --path ./my-history
python -m vestigraph serve --state ./service-state --open-browser
```

`PROJECT_ID` 使用 `add-project` 返回值。默认只读接入已有历史；需要补录时，接入命令加 `--writable`。历史存储应放在产品源码与 Git 仓库之外。

## 自动记录 KLayout

完成[联合安装](INSTALLATION.md)后，打开已保存文件并点击 HIST。确认面板中的当前窗口和文档正在记录，再编辑版图。可以暂停、恢复记录，或给重要状态命名。

记录保存的是 KLayout 导出的副本，可能与磁盘原文件字节不同。捕获中、排队中、失败状态不能当作已保存版本。自动记录不保证捕获每个瞬间变化。

## 补录旧文件

在文档的导入操作中选择本机旧文件，查看预览计划、顺序和目标位置，再确认执行。补录的浏览顺序不代表原始编辑操作顺序。重复文件、失败条目和冲突以面板说明为准。

## 预览与说明

选择已保存版本可查看预览并编辑名称。大版图受时间和内存预算限制；预览失败不影响原始历史文件。版本名称、说明和技能草稿不等于版图文件本身。

导出与在新标签页打开旧版本见[恢复](RECOVERY.md)。

