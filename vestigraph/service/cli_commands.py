"""CLI sub-commands: ``service init|add-project|add-history`` and ``serve``.

Registration of paths happens only here, on the local machine; the web API
never accepts paths. Each command returns a JSON-serializable dict with
``next_action``.
"""
from __future__ import annotations

import argparse

from .catalog import Catalog
from .errors import ServiceError
from .state import ServiceState


def add_parsers(commands):
    service = commands.add_parser("service", help="本地自动记录服务：初始化状态目录、注册项目/旧历史")
    sub = service.add_subparsers(dest="service_command", required=True)

    p = sub.add_parser("init", help="初始化服务状态目录（与历史库分开）")
    p.add_argument("--state", required=True, metavar="STATE_DIR")

    p = sub.add_parser("add-project", help="注册一个受管项目：工作区 + 历史保存位置")
    p.add_argument("--state", required=True, metavar="STATE_DIR")
    p.add_argument("--name", required=True)
    p.add_argument("--workspace", required=True, metavar="WORKSPACE_DIR")
    p.add_argument("--history-root", required=True, metavar="HISTORY_ROOT")
    p.add_argument("--allow-unsaved", action="store_true", help="允许记录选定 KLayout 窗口里未保存的文档")
    p.add_argument("--allow-inside-git", action="store_true", help="允许历史保存位置位于 Git 检出目录内（默认拒绝）")

    p = sub.add_parser("add-history", help="把已有 v1 历史库接入项目（默认只读，不改任何记录）")
    p.add_argument("--state", required=True, metavar="STATE_DIR")
    p.add_argument("--project", required=True, metavar="PROJECT_ID")
    p.add_argument("--path", required=True, metavar="LEGACY_REPO")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--read-only", action="store_true", default=True, help="只读接入（默认）")
    group.add_argument("--writable", action="store_true", help="显式允许继续写入该旧库（历史记录仍不可改）")

    p = commands.add_parser("serve", help="启动本机服务（仅 127.0.0.1）；不带 --state 时使用用户默认目录并自动记录所有版图")
    p.add_argument("--state", metavar="STATE_DIR", help="服务状态目录；省略则用用户默认位置并自动初始化")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--host", default="127.0.0.1", help="仅接受 loopback 地址")
    p.add_argument("--open-browser", action="store_true", help="启动后打开一次登录链接（仅在你明确要求时）")
    p.add_argument("--control-file", action="store_true", help="写入仅本用户可读的控制文件，供 KLayout 插件索取登录链接")
    p.add_argument("--control-stdio", action="store_true", help="通过标准输入/输出接受启动器的 issue-link / shutdown")
    p.add_argument("--exit-when-idle", type=float, metavar="SECONDS", help="所有 KLayout 都不在线超过该秒数后退出")
    p.add_argument("--no-zero-config", action="store_true", help="不自动初始化、不创建默认项目")

    companion = commands.add_parser("companion", help="把 Vestigraph 登记为 klink 伴随服务（KLayout 启动即拉起 + HIST 工具栏按钮）")
    companion_sub = companion.add_subparsers(dest="companion_command", required=True)
    cr = companion_sub.add_parser("register", help="写入 klink 的伴随服务描述文件（幂等）")
    cr.add_argument("--python", metavar="EXE", help="运行服务的 Python；省略则自动探测（配置 → 环境变量 → 当前解释器 → PATH）")
    cr.add_argument("--port", type=int, default=8787, help="首选端口；被占用时 klink 会顺延")
    cr.add_argument("--registry-root", metavar="DIR", help="覆盖 klink 注册表根目录（默认与 klink 相同）")
    cu = companion_sub.add_parser("unregister", help="删除描述文件")
    cu.add_argument("--registry-root", metavar="DIR")
    cs = companion_sub.add_parser("status", help="查看是否已登记及所登记的 Python 是否可用")
    cs.add_argument("--registry-root", metavar="DIR")


def run(args: argparse.Namespace, *, services=None, skill_services=None):
    if args.command == "serve":
        from ..web.app import serve
        return {"exit_code": serve(args.state, host=args.host, port=args.port, open_browser=args.open_browser,
                                   control_file=args.control_file, control_stdio=args.control_stdio,
                                   exit_when_idle=args.exit_when_idle, zero_config=not args.no_zero_config, services=services, skill_services=skill_services)}
    if args.command == "companion":
        from vestigraph_backends.vesti_backend_klayout import companion as companion
        if args.companion_command == "register":
            return companion.register(python=args.python, port=args.port, root=args.registry_root)
        if args.companion_command == "unregister":
            return companion.unregister(args.registry_root)
        if args.companion_command == "status":
            return companion.status(args.registry_root)
        raise ServiceError("BAD_REQUEST", f"Unknown companion command {args.companion_command}")
    if args.service_command == "init":
        state = ServiceState.init(args.state)
        Catalog.init(state, services=services)
        return {"state": str(state.root), "initialized": True,
                "next_action": "service add-project --state ... --name ... --workspace ... --history-root ..."}
    state = ServiceState.open(args.state)
    catalog = Catalog(state, services=services)
    if args.service_command == "add-project":
        project = catalog.add_project(args.name, args.workspace, args.history_root,
                                      allow_unsaved=args.allow_unsaved, allow_inside_git=args.allow_inside_git)
        return {"project_id": project["id"], "name": project["name"], "workspace": project["workspace"],
                "history_root": project["history_root"], "policy": project["policy"],
                "next_action": f"serve --state {state.root} --port 8787  (or add-history to attach an old repository)"}
    if args.service_command == "add-history":
        document = catalog.add_history(args.project, args.path, read_only=not args.writable)
        return {"document_id": document["id"], "project_id": document["project_id"], "name": document["name"],
                "origin": document["origin"], "read_only": document["read_only"],
                "classification": document["classification"],
                "next_action": "serve --state ... then browse this document in the web panel"}
    raise ServiceError("BAD_REQUEST", f"Unknown service command {args.service_command}")
