"""Display model for the native local panel. No Tk, no threads, no I/O of its own.

Everything here turns dicts returned by ``vestigraph.store.Repository`` into
plain-text rows and summaries, validates user input, and tracks the state of
the single observation worker. It only ever calls the published Repository
methods listed in ``docs/archive/SPEC_V0.md`` §5.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

# Segment status text is shown verbatim. "open" additionally gets a note so an
# abandoned segment is never mistaken for a finished one.
STATUS_NOTES = {
    "open": "未完成（进行中或进程异常退出）",
    "closed": "",
    "failed": "失败",
    "interrupted": "被打断，过程记录可能有缺口",
}

SOURCE_NOTES = {
    "manual": "保存动作由用户发起",
    "automation": "带 caused_by 的 RPC",
    "mixed": "手动与自动混合",
    "unknown": "可能来自 GUI 或未归因代码",
    "system": "Vestigraph 自身记录",
}

LIST_LIMIT = 10000  # Repository caps every limit at this value.

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def plain(value, default="") -> str:
    """Render any value as one line of plain text without control characters."""
    if value is None:
        return default
    text = value if isinstance(value, str) else str(value)
    text = _CONTROL.sub("", text).replace("\r", "").replace("\n", " ")
    return text if text else default


def short_id(value) -> str:
    text = plain(value)
    return text[:12] if text else ""


def status_note(status) -> str:
    text = plain(status, "unknown")
    return STATUS_NOTES.get(text, "未知状态")


def source_note(source) -> str:
    text = plain(source, "unknown")
    return SOURCE_NOTES.get(text, "未知来源")


def format_bytes(size) -> str:
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return "?"
    units = ["B", "KiB", "MiB", "GiB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{size} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def segment_row(segment: dict) -> dict:
    status = plain(segment.get("status"), "unknown")
    return {
        "id": plain(segment.get("id")),
        "short_id": short_id(segment.get("id")),
        "title": plain(segment.get("title"), "(无标题)"),
        "source": plain(segment.get("source"), "unknown"),
        "status": status,
        "status_note": status_note(status),
        "started_at": plain(segment.get("started_at")),
        "ended_at": plain(segment.get("ended_at"), "—"),
    }


def _document_name(checkpoint: dict) -> str:
    """Observed exports are named after a temp file; prefer the recorded document name."""
    metadata = checkpoint.get("metadata")
    document = metadata.get("document") if isinstance(metadata, dict) else None
    name = document.get("filename") if isinstance(document, dict) else None
    if isinstance(name, str) and name.strip():
        return plain(Path(name).name) + "（观察导出）"
    return plain(checkpoint.get("filename"))


from .vesti_formats.registry import filename_suffix


def export_name(checkpoint: dict) -> str:
    """Default export filename: document stem + suffix of the format actually stored.

    Display text (e.g. "（观察导出）") never leaks in. When the record carries
    ``metadata.format`` that decides the suffix; otherwise the stored filename's
    own suffix is kept.
    """
    stored = Path(plain(checkpoint.get("filename"), "export.gds"))
    metadata = checkpoint.get("metadata")
    document = metadata.get("document") if isinstance(metadata, dict) else None
    name = document.get("filename") if isinstance(document, dict) else None
    stem = Path(name).stem if isinstance(name, str) and name.strip() else stored.stem
    fmt = metadata.get("format") if isinstance(metadata, dict) else None
    suffix = filename_suffix(fmt, stored)
    return plain(stem or "export") + suffix


def checkpoint_row(checkpoint: dict) -> dict:
    size = checkpoint.get("size")
    return {
        "id": plain(checkpoint.get("id")),
        "short_id": short_id(checkpoint.get("id")),
        "title": plain(checkpoint.get("title"), "(无标题)"),
        "filename": _document_name(checkpoint),
        "source": plain(checkpoint.get("source"), "unknown"),
        "created_at": plain(checkpoint.get("created_at")),
        "size": format_bytes(size),
        "sha256": short_id(checkpoint.get("sha256")),
        "segment_id": plain(checkpoint.get("segment_id")),
    }


def event_row(event: dict) -> dict:
    payload = event.get("payload")
    truncated = isinstance(payload, dict) and payload.get("vestigraph_truncated") is True
    return {
        "seq": plain(event.get("seq")),
        "created_at": plain(event.get("created_at")),
        "kind": plain(event.get("kind")),
        "source": plain(event.get("source"), "unknown"),
        "source_note": source_note(event.get("source")),
        "segment_id": plain(event.get("segment_id")),
        "summary": "载荷已截断" if truncated else _payload_summary(payload),
    }


def _payload_summary(payload) -> str:
    if not isinstance(payload, dict) or not payload:
        return ""
    parts = []
    for key in sorted(payload):
        value = payload[key]
        if isinstance(value, (dict, list)):
            value = f"{type(value).__name__}[{len(value)}]"
        parts.append(f"{plain(key)}={plain(value)}")
        if len(parts) >= 4:
            break
    text = ", ".join(parts)
    return text if len(text) <= 120 else text[:117] + "..."


def summarize(segments, checkpoints, events, limit=LIST_LIMIT) -> dict:
    """Count checkpoints and events per segment within the fetched windows.

    The Repository has no per-segment count API, so the counts are only exact
    when the fetched lists are shorter than ``limit``. ``partial`` says so.
    """
    counts = {}
    for segment in segments:
        counts[plain(segment.get("id"))] = {"checkpoints": 0, "events": 0}
    unassigned = {"checkpoints": 0, "events": 0}
    for name, items in (("checkpoints", checkpoints), ("events", events)):
        for item in items:
            key = plain(item.get("segment_id"))
            (counts.get(key) or unassigned)[name] += 1
    partial = len(checkpoints) >= limit or len(events) >= limit or len(segments) >= limit
    return {"per_segment": counts, "unassigned": unassigned, "partial": partial,
            "window": limit}


def filter_by_segment(items, segment_id):
    if segment_id is None:
        return list(items)
    return [item for item in items if plain(item.get("segment_id")) == segment_id]


def open_segments(segments):
    return [segment for segment in segments if plain(segment.get("status")) == "open"]


# ---------------------------------------------------------------- errors ----

_ADVICE = (
    ("another writer holds this history", "这个历史库正被服务或另一个面板记录。优先使用网页面板；如需旧面板写入，先正常停止占用它的 Vestigraph 服务或录制进程，等待退出后重试。不要删除锁文件。"),
    ("capture queue is full", "待整理副本已达到数量或磁盘容量限制。等待后台整理、检查可用磁盘；已保留副本不要手动删除。"),
    ("captured copy is blocked", "副本已保留，但整理遇到问题。到网页查看原因，处理后重试整理；不要删除历史锁或直接覆盖副本。"),
    ("not initialized", "该目录还没有历史库。点击“初始化”创建，或选择已初始化的目录。"),
    ("unsupported repository format", "历史库格式版本不匹配。换用与数据目录匹配的 Vestigraph 版本。"),
    ("already exists", "目标文件已存在，没有覆盖任何内容。换一个新文件名再导出。"),
    ("appeared during export", "导出过程中目标文件被别的程序创建，没有覆盖。换一个新文件名。"),
    ("outside the repository", "导出位置不能在历史数据目录里。选择数据目录之外的文件夹。"),
    ("parent directory does not exist", "导出目录不存在。先创建文件夹再导出。"),
    ("hard-link", "该位置不支持原子发布。改导出到本地 NTFS/ext4/APFS 磁盘。"),
    ("integrity mismatch", "历史块损坏或缺失，导出已中止。从完整备份恢复数据目录。"),
    ("source changed while being read", "文件在读取期间被修改。先暂停编辑并保存，再重新保存检查点。"),
    ("source file does not exist", "文件不存在。先在 KLayout 保存/导出到磁盘，再选这个文件。"),
    ("segment is missing or closed", "所选过程段已结束。不选段直接保存，或先开始新段。"),
    ("install a compatible klink", "运行本面板的 Python 里没有 klink 客户端。用带 klink 的解释器启动，或先安装。"),
    ("lacks required channels", "这个 KLink 端点没有必要的事件通道。升级/启用兼容的 klink 插件。"),
    ("active document changed", "录制期间当前文档被切换，本段已标记 interrupted。回到目标文档后重新开始观察。"),
    ("events_coalesced", "部分操作明细因事件过密被合并计数，版图仍会继续捕获。可等待后台整理，事件明细不能视为完整操作回放。"),
    ("event queue overflow: capture has a gap", "旧版录制器报告了事件缺口；不要把这段操作记录视为完整。检查当次录制状态后再继续；新版事件合并不等于录制停止。"),
    ("before a baseline was saved", "还没有导出基线就停止了。确认目标版图是当前 tab 后重新开始。"),
    ("cannot identify the document", "找不到当前文档。在 KLayout 打开一个版图并使其为当前 tab。"),
    ("no active document", "没有活动文档。在 KLayout 打开一个版图并使其为当前 tab。"),
    ("connection", "连不上 KLink。确认 KLayout 已启动、klink 插件已加载、端口填写正确。"),
    ("refused", "连接被拒绝。确认 KLayout 已启动、klink 插件已加载、端口填写正确。"),
    ("timed out", "RPC 超时。KLayout 可能正忙或已挂起；等待后重试。"),
    ("permission", "没有写权限。检查目录权限或换一个位置。"),
    # Messages raised by this module's own validators (already Chinese).
    ("已存在", "换一个新文件名再导出；没有覆盖任何内容。"),
    ("不存在", "先在 KLayout 保存/导出到磁盘，再选择该文件。"),
    ("端口必须", "填 KLink 会话状态里显示的端口；默认 8765。"),
    ("先选择", "在对应输入框里选择文件后再操作。"),
    ("先在列表", "在检查点列表里点选一个版本后再导出。"),
    ("标题过长", "缩短标题后重试。"),
    ("还没有打开", "在顶部填写数据目录，点击“打开”或“初始化”。"),
    ("已有一个观察", "先点“停止”，等状态变为已停止再开始。"),
)


def error_advice(error) -> dict:
    """Return the raw message and one next-step suggestion for a failure."""
    message = plain(error if isinstance(error, str) else f"{type(error).__name__}: {error}",
                    "未知错误")
    # Exception TYPES first: the OS writes socket errors in the system language ("[WinError
    # 10061] 由于目标计算机积极拒绝..." on a Chinese Windows), so matching English words in the
    # text alone sent real connection failures to the generic "report it" advice.
    if isinstance(error, (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError)):
        return {"message": message, "advice": "连接被拒绝。确认 KLayout 已启动、klink 插件已加载、端口填写正确。"}
    if isinstance(error, TimeoutError):
        return {"message": message, "advice": "RPC 超时。KLayout 可能正忙或已挂起；等待后重试。"}
    if isinstance(error, PermissionError):
        return {"message": message, "advice": "没有写权限。检查目录权限或换一个位置。"}
    if isinstance(error, OSError) and getattr(error, "errno", None) in (111, 10061, 10060, 10054):
        return {"message": message, "advice": "连不上 KLink。确认 KLayout 已启动、klink 插件已加载、端口填写正确。"}
    lowered = message.lower()
    for needle, advice in _ADVICE:
        if needle in lowered:
            return {"message": message, "advice": advice}
    return {"message": message, "advice": "把上面的错误原文交给维护者；不要重试覆盖式操作。"}


# ----------------------------------------------------------------- input ----

def parse_port(text) -> int:
    value = plain(text).strip()
    if not value.isdigit():
        raise ValueError("端口必须是 1..65535 的整数；填 KLink 会话显示的端口。")
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError("端口必须是 1..65535 的整数；填 KLink 会话显示的端口。")
    return port


def validate_layout_path(text) -> Path:
    value = plain(text).strip()
    if not value:
        raise ValueError("先选择要保存的 GDS/OAS 文件。")
    path = Path(value).expanduser()
    if not path.is_file():
        raise ValueError("文件不存在；先在 KLayout 保存/导出到磁盘。")
    return path


def validate_export_target(text) -> Path:
    value = plain(text).strip()
    if not value:
        raise ValueError("先选择导出的新文件名。")
    path = Path(value).expanduser()
    if path.exists():
        raise ValueError("目标文件已存在，不会覆盖；换一个新文件名。")
    return path


def validate_title(text) -> str:
    value = plain(text).strip()
    if len(value) > 200:
        raise ValueError("标题过长；限制在 200 字符以内。")
    return value


# --------------------------------------------------------- observe state ----

class ObserveState:
    """State of the single observation worker.

    idle -> starting -> running -> stopping -> stopped
    starting/running/stopping -> failed
    stopped/failed -> starting (a new worker)
    """

    ORDER = ("idle", "starting", "running", "stopping", "stopped", "failed")

    def __init__(self):
        self.state = "idle"
        self.port = None
        self.count = None
        self.error = None
        self.stop_event = None

    @property
    def active(self) -> bool:
        return self.state in ("starting", "running", "stopping")

    def start(self, port: int, stop_event=None):
        if self.active:
            raise RuntimeError("已有一个观察在运行；先停止它。")
        self.state, self.port = "starting", port
        self.count = self.error = None
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        return self.stop_event

    def running(self):
        if self.state != "starting":
            raise RuntimeError(f"observe worker cannot run from state {self.state}")
        self.state = "running"

    def request_stop(self) -> bool:
        """Ask the worker to stop. Returns False when nothing is running."""
        if not self.active:
            return False
        self.stop_event.set()
        self.state = "stopping"
        return True

    def finished(self, count: int):
        if not self.active:
            raise RuntimeError(f"observe worker cannot finish from state {self.state}")
        self.state, self.count = "stopped", count

    def failed(self, error):
        if not self.active:
            raise RuntimeError(f"observe worker cannot fail from state {self.state}")
        self.state, self.error = "failed", error_advice(error)

    def label(self) -> str:
        if self.state == "idle":
            return "未开始观察"
        if self.state == "starting":
            return f"正在连接 127.0.0.1:{self.port} 并导出基线…"
        if self.state == "running":
            return f"观察中（127.0.0.1:{self.port}）"
        if self.state == "stopping":
            return "正在停止：等待当前导出/RPC 完成，尚未保存最终检查点"
        if self.state == "stopped":
            return f"已停止；本次观察共保存 {self.count} 个检查点"
        return f"观察失败：{self.error['message']}"


# ---------------------------------------------------------- panel model ----

class PanelModel:
    """Holds the open repository and the last fetched snapshot.

    All methods are synchronous and may be called from a worker thread; the
    UI copies results back to the main thread through its own queue.
    """

    def __init__(self, repository_class):
        self._repository_class = repository_class
        self.repo = None
        self.root = None
        self.segments = []
        self.checkpoints = []
        self.events = []
        self.stats = {}
        self.summary = summarize([], [], [])

    def open(self, root):
        self.repo = self._repository_class(root)
        self.root = plain(getattr(self.repo, "root", root))
        return self.refresh()

    def initialize(self, root):
        self.repo = self._repository_class.init(root)
        self.root = plain(getattr(self.repo, "root", root))
        return self.refresh()

    def _require(self):
        if self.repo is None:
            raise RuntimeError("还没有打开历史库；先选择或初始化数据目录。")
        return self.repo

    def refresh(self):
        repo = self._require()
        self.segments = list(repo.segments(limit=LIST_LIMIT))
        self.checkpoints = list(repo.history(limit=LIST_LIMIT))
        self.events = list(repo.events(limit=LIST_LIMIT))
        self.stats = dict(repo.stats())
        self.summary = summarize(self.segments, self.checkpoints, self.events)
        return self.snapshot()

    def snapshot(self):
        return {
            "root": self.root,
            "segments": [segment_row(s) for s in self.segments],
            "checkpoints": [checkpoint_row(c) for c in self.checkpoints],
            "events": [event_row(e) for e in self.events],
            "stats": dict(self.stats),
            "summary": self.summary,
        }

    def save_checkpoint(self, path, title="", segment_id=None):
        repo = self._require()
        return repo.checkpoint(validate_layout_path(path), title=validate_title(title),
                               source="manual", segment_id=segment_id or None)

    def export_checkpoint(self, checkpoint_id, destination):
        repo = self._require()
        if not plain(checkpoint_id):
            raise ValueError("先在列表里选中一个检查点。")
        return repo.export(checkpoint_id, validate_export_target(destination))

    def stats_line(self) -> str:
        stats = self.stats
        if not stats:
            return ""
        note = "（计数只覆盖最近 %d 条）" % self.summary["window"] if self.summary["partial"] else ""
        return ("检查点 {c} · 过程段 {s} · 事件 {e} · 对象 {o} · 存储 {sb} / 逻辑 {lb}{n}").format(
            c=stats.get("checkpoints", "?"), s=stats.get("segments", "?"),
            e=stats.get("events", "?"), o=stats.get("objects", "?"),
            sb=format_bytes(stats.get("stored_bytes")), lb=format_bytes(stats.get("logical_bytes")),
            n=note)
