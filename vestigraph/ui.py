"""Native local history panel (Tkinter). Run with ``python -m vestigraph.ui``.

No browser, HTTP server, account, or network. Storage/export/observation run
on worker threads; Tk widgets are touched only from the main thread, fed by a
queue polled with ``after``. Only the published ``Repository`` methods and
``capture.observe`` are used.
"""
from __future__ import annotations
import logging

import argparse
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from . import ui_model as m

POLL_MS = 100
AUTO_REFRESH_MS = 5000
LAYOUT_TYPES = [("GDS/OASIS 版图", "*.gds *.gds2 *.oas *.oasis"), ("所有文件", "*.*")]
NO_SEGMENT = "（不关联过程段）"


class Panel:
    def __init__(self, root: tk.Tk, repository_class=None, observe=None, initial_repo=None):
        if repository_class is None:
            from .store import Repository as repository_class
        self._observe_fn = observe
        self.tk = root
        self.model = m.PanelModel(repository_class)
        self.observe_state = m.ObserveState()
        self.results = queue.Queue()
        self.busy = False          # one storage/export worker at a time
        self.closing = False
        self.observe_thread = None
        self.selected_segment = None
        self.snapshot = None
        self._open_ids = {}
        self._build()
        self.tk.protocol("WM_DELETE_WINDOW", self.on_close)
        self.tk.after(POLL_MS, self._poll)
        self.tk.after(AUTO_REFRESH_MS, self._auto_refresh)
        if initial_repo:
            self.repo_var.set(m.plain(initial_repo))
            self.open_repo()

    # ------------------------------------------------------------ layout --
    def _build(self):
        self.tk.title("Vestigraph 本地历史面板")
        self.tk.minsize(960, 640)
        top = ttk.Frame(self.tk, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="数据目录").pack(side="left")
        self.repo_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.repo_var, width=60).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(top, text="浏览…", command=self.browse_repo).pack(side="left")
        self.open_btn = ttk.Button(top, text="打开", command=self.open_repo)
        self.open_btn.pack(side="left", padx=2)
        self.init_btn = ttk.Button(top, text="初始化", command=self.init_repo)
        self.init_btn.pack(side="left", padx=2)
        self.refresh_btn = ttk.Button(top, text="刷新", command=self.refresh)
        self.refresh_btn.pack(side="left", padx=2)

        self.stats_var = tk.StringVar(value="未打开历史库")
        ttk.Label(self.tk, textvariable=self.stats_var, padding=(6, 0)).pack(fill="x")

        panes = ttk.PanedWindow(self.tk, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=6, pady=4)

        left = ttk.Frame(panes)
        panes.add(left, weight=1)
        ttk.Label(left, text="过程段（点击筛选；再次点击取消）").pack(anchor="w")
        self.seg_tree = self._tree(left, (
            ("short_id", "id", 90), ("title", "标题", 180), ("status", "状态", 80),
            ("status_note", "说明", 160), ("source", "来源", 70),
            ("counts", "检查点/事件", 90), ("started_at", "开始 (UTC)", 150), ("ended_at", "结束 (UTC)", 150),
        ))
        self.seg_tree.bind("<<TreeviewSelect>>", self.on_segment_select)
        self.seg_tree.bind("<Button-1>", self.on_segment_click)

        right = ttk.Frame(panes)
        panes.add(right, weight=2)
        ttk.Label(right, text="检查点（版本历史，最新在上）").pack(anchor="w")
        self.cp_tree = self._tree(right, (
            ("short_id", "id", 90), ("title", "标题", 180), ("filename", "文件名", 140),
            ("size", "大小", 80), ("source", "来源", 70), ("created_at", "时间 (UTC)", 150),
            ("sha256", "sha256", 90), ("segment", "过程段", 90),
        ))
        ttk.Label(right, text="事件（最新在上）").pack(anchor="w")
        self.ev_tree = self._tree(right, (
            ("seq", "seq", 50), ("kind", "类型", 130), ("source", "来源", 70),
            ("source_note", "来源说明", 170), ("created_at", "时间 (UTC)", 150),
            ("summary", "摘要", 260),
        ))

        actions = ttk.Frame(self.tk, padding=6)
        actions.pack(fill="x")

        save = ttk.LabelFrame(actions, text="保存命名检查点", padding=4)
        save.pack(side="left", fill="x", expand=True, padx=2)
        self.file_var, self.title_var, self.segment_var = tk.StringVar(), tk.StringVar(), tk.StringVar(value=NO_SEGMENT)
        row = ttk.Frame(save); row.pack(fill="x")
        ttk.Label(row, text="文件").pack(side="left")
        ttk.Entry(row, textvariable=self.file_var, width=36).pack(side="left", fill="x", expand=True, padx=2)
        ttk.Button(row, text="选择…", command=self.browse_layout).pack(side="left")
        row = ttk.Frame(save); row.pack(fill="x", pady=2)
        ttk.Label(row, text="标题").pack(side="left")
        ttk.Entry(row, textvariable=self.title_var, width=24).pack(side="left", fill="x", expand=True, padx=2)
        ttk.Label(row, text="过程段").pack(side="left")
        self.segment_box = ttk.Combobox(row, textvariable=self.segment_var, state="readonly", width=28)
        self.segment_box.pack(side="left", padx=2)
        self.save_btn = ttk.Button(row, text="保存检查点", command=self.save_checkpoint)
        self.save_btn.pack(side="left", padx=2)

        export = ttk.LabelFrame(actions, text="导出", padding=4)
        export.pack(side="left", fill="y", padx=2)
        self.export_btn = ttk.Button(export, text="导出选中检查点到新文件…", command=self.export_checkpoint)
        self.export_btn.pack(fill="x")
        ttk.Label(export, text="不覆盖已有文件", foreground="#666").pack()

        obs = ttk.LabelFrame(actions, text="观察 KLink（仅 127.0.0.1）", padding=4)
        obs.pack(side="left", fill="y", padx=2)
        row = ttk.Frame(obs); row.pack(fill="x")
        ttk.Label(row, text="端口").pack(side="left")
        self.port_var = tk.StringVar(value="8765")
        ttk.Entry(row, textvariable=self.port_var, width=7).pack(side="left", padx=2)
        self.start_btn = ttk.Button(row, text="开始", command=self.start_observe)
        self.start_btn.pack(side="left", padx=2)
        self.stop_btn = ttk.Button(row, text="停止", command=self.stop_observe, state="disabled")
        self.stop_btn.pack(side="left", padx=2)
        self.observe_var = tk.StringVar(value=self.observe_state.label())
        ttk.Label(obs, textvariable=self.observe_var, wraplength=300).pack(anchor="w")

        bottom = ttk.Frame(self.tk, padding=(6, 0, 6, 6))
        bottom.pack(fill="x")
        head = ttk.Frame(bottom); head.pack(fill="x")
        ttk.Label(head, text="消息（可选中复制）").pack(side="left")
        ttk.Button(head, text="复制全部", command=self.copy_messages).pack(side="right")
        ttk.Button(head, text="清空", command=self.clear_messages).pack(side="right", padx=2)
        self.log = tk.Text(bottom, height=7, wrap="word")
        self.log.pack(fill="x")
        self.log.configure(state="disabled")

    @staticmethod
    def _tree(parent, columns):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        ids = [c[0] for c in columns]
        tree = ttk.Treeview(frame, columns=ids, show="headings", selectmode="browse", height=8)
        for cid, heading, width in columns:
            tree.heading(cid, text=heading)
            tree.column(cid, width=width, stretch=cid in ("title", "summary", "status_note"))
        bar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        return tree

    # ---------------------------------------------------------- messages --
    def say(self, text, level="info"):
        prefix = {"info": "", "ok": "✔ ", "error": "✖ "}.get(level, "")
        self.log.configure(state="normal")
        self.log.insert("end", prefix + m.plain(text) + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def fail(self, error):
        info = m.error_advice(error)
        self.say(info["message"], "error")
        self.say("下一步：" + info["advice"])

    def copy_messages(self):
        self.tk.clipboard_clear()
        self.tk.clipboard_append(self.log.get("1.0", "end").strip())

    def clear_messages(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------ workers --
    def _submit(self, name, fn, on_done):
        """Run fn on a worker; deliver (name, ok, value) back on the main thread."""
        if self.busy:
            self.say("上一个操作还在进行；请等待它完成。", "error")
            return False
        self.busy = True
        self._set_actions()

        def run():
            try:
                value, ok = fn(), True
            except BaseException as exc:  # never let a worker die silently
                value, ok = exc, False
            self.results.put((name, ok, value, on_done))

        threading.Thread(target=run, name=f"vestigraph-{name}", daemon=True).start()
        return True

    def _poll(self):
        try:
            while True:
                try:
                    name, ok, value, on_done = self.results.get_nowait()
                except queue.Empty:
                    break
                try:
                    if name == "observe":
                        self._observe_done(ok, value)
                    else:
                        self.busy = False
                        self._set_actions()
                        on_done(ok, value)
                except Exception as exc:  # noqa: BLE001 - one bad callback must not stop the poll chain
                    self.busy = False
                    try:
                        self._set_actions()
                        self.say(f"{type(exc).__name__}: {exc}", "error")
                    except Exception:  # noqa: BLE001
                        logging.getLogger(__name__).warning("Nonfatal callback or cleanup failure", exc_info=True)
            if self.closing and not self.busy and not self.observe_state.active:
                self.tk.destroy()
                return
        finally:
            if not self.closing or self.busy or self.observe_state.active:
                self.tk.after(POLL_MS, self._poll)

    def _set_actions(self):
        """Derive every action button from (busy, observe state)."""
        busy, observing = self.busy, self.observe_state.active
        store = "disabled" if busy else "normal"
        for button in (self.refresh_btn, self.save_btn, self.export_btn):
            button.configure(state=store)
        switch = "disabled" if (busy or observing) else "normal"
        for button in (self.open_btn, self.init_btn):
            button.configure(state=switch)
        # A repository open/init still in flight must finish before observing binds a repo.
        self.start_btn.configure(state="disabled" if (busy or observing) else "normal")
        self.stop_btn.configure(state="normal" if self.observe_state.state == "running" else "disabled")

    def _switch_blocked(self):
        """The observe worker holds the repository it started with; never show another."""
        if self.observe_state.active:
            self.say("观察线程仍在使用当前历史库；先停止观察并等它结束，再切换数据目录。", "error")
            return True
        return False

    def _auto_refresh(self):
        if self.observe_state.active and not self.busy and self.model.repo is not None:
            self._submit("refresh", self.model.refresh, self._show_snapshot)
        self.tk.after(AUTO_REFRESH_MS, self._auto_refresh)

    # --------------------------------------------------------- repository --
    def browse_repo(self):
        chosen = filedialog.askdirectory(title="选择或新建历史数据目录", mustexist=False)
        if chosen:
            self.repo_var.set(chosen)

    def open_repo(self):
        if self._switch_blocked():
            return
        root = self.repo_var.get().strip()
        if not root:
            return self.say("先填写数据目录。", "error")
        self._submit("open", lambda: self.model.open(root), self._opened)

    def init_repo(self):
        if self._switch_blocked():
            return
        root = self.repo_var.get().strip()
        if not root:
            return self.say("先填写数据目录。", "error")
        self._submit("init", lambda: self.model.initialize(root), self._opened)

    def _opened(self, ok, value):
        if not ok:
            return self.fail(value)
        self.say(f"已打开历史库：{self.model.root}", "ok")
        self._show_snapshot(True, value)

    def refresh(self):
        if self.model.repo is None:
            return self.say("还没有打开历史库。", "error")
        self._submit("refresh", self.model.refresh, self._show_snapshot)

    def _show_snapshot(self, ok, value):
        if not ok:
            return self.fail(value)
        self.snapshot = value
        self.stats_var.set(f"{value['root']}    {self.model.stats_line()}")
        counts = value["summary"]["per_segment"]
        keep = self.selected_segment
        self.seg_tree.delete(*self.seg_tree.get_children())
        for row in value["segments"]:
            c = counts.get(row["id"], {"checkpoints": 0, "events": 0})
            self.seg_tree.insert("", "end", iid=row["id"], values=(
                row["short_id"], row["title"], row["status"], row["status_note"], row["source"],
                f"{c['checkpoints']} / {c['events']}", row["started_at"], row["ended_at"]))
        if keep and self.seg_tree.exists(keep):
            self.seg_tree.selection_set(keep)
        else:
            self.selected_segment = None
        self._fill_detail()
        opens = [f"{s['short_id']}  {s['title']}" for s in value["segments"] if s["status"] == "open"]
        self._open_ids = {f"{s['short_id']}  {s['title']}": s["id"] for s in value["segments"] if s["status"] == "open"}
        self.segment_box.configure(values=[NO_SEGMENT] + opens)
        if self.segment_var.get() not in self._open_ids:
            self.segment_var.set(NO_SEGMENT)

    def _fill_detail(self):
        snap = self.snapshot or {"checkpoints": [], "events": []}
        sid = self.selected_segment
        self.cp_tree.delete(*self.cp_tree.get_children())
        for row in snap["checkpoints"]:
            if sid and row["segment_id"] != sid:
                continue
            self.cp_tree.insert("", "end", iid=row["id"], values=(
                row["short_id"], row["title"], row["filename"], row["size"], row["source"],
                row["created_at"], row["sha256"], m.short_id(row["segment_id"]) or "—"))
        self.ev_tree.delete(*self.ev_tree.get_children())
        for row in snap["events"]:
            if sid and row["segment_id"] != sid:
                continue
            self.ev_tree.insert("", "end", values=(
                row["seq"], row["kind"], row["source"], row["source_note"], row["created_at"], row["summary"]))

    def on_segment_click(self, event):
        """A user click on the already-selected row clears the filter."""
        row = self.seg_tree.identify_row(event.y)
        if row and row in self.seg_tree.selection():
            self.seg_tree.selection_remove(row)  # fires <<TreeviewSelect>> with no selection
            return "break"
        return None

    def on_segment_select(self, _event=None):
        """Mirror the tree selection; fired by user clicks and by refresh restoring it."""
        chosen = self.seg_tree.selection()
        self.selected_segment = chosen[0] if chosen else None
        self._fill_detail()

    # --------------------------------------------------------- checkpoint --
    def browse_layout(self):
        chosen = filedialog.askopenfilename(title="选择要保存的版图文件", filetypes=LAYOUT_TYPES)
        if chosen:
            self.file_var.set(chosen)

    def save_checkpoint(self):
        path, title = self.file_var.get(), self.title_var.get()
        segment_id = self._open_ids.get(self.segment_var.get())
        try:
            m.validate_layout_path(path)
        except ValueError as exc:
            return self.fail(exc)
        self.say(f"正在读取并入库：{m.plain(path)} …")
        self._submit("checkpoint",
                     lambda: self.model.save_checkpoint(path, title, segment_id),
                     self._saved)

    def _saved(self, ok, value):
        if not ok:
            return self.fail(value)
        row = m.checkpoint_row(value)
        self.say(f"已保存检查点 {row['short_id']}  “{row['title']}”  {row['size']}  sha256 {row['sha256']}…", "ok")
        self.title_var.set("")
        self.refresh()

    # ------------------------------------------------------------- export --
    def export_checkpoint(self):
        chosen = self.cp_tree.selection()
        if not chosen:
            return self.say("先在检查点列表里选中一个版本。", "error")
        checkpoint_id = chosen[0]
        record = next((c for c in self.model.checkpoints if m.plain(c.get("id")) == checkpoint_id), None)
        suggested = m.export_name(record) if record else "export.gds"
        target = filedialog.asksaveasfilename(
            title="导出到新文件（不会覆盖已有文件）", initialfile=suggested,
            filetypes=LAYOUT_TYPES, confirmoverwrite=False)
        if not target:
            return
        try:
            m.validate_export_target(target)
        except ValueError as exc:
            return self.fail(exc)
        self.say(f"正在校验并导出到：{m.plain(target)} …")
        self._submit("export",
                     lambda: self.model.export_checkpoint(checkpoint_id, target),
                     self._exported)

    def _exported(self, ok, value):
        if not ok:
            return self.fail(value)
        self.say(f"已导出文件字节到：{m.plain(value)}（仅恢复文件内容，不含注释/选择/undo 状态）", "ok")

    # ------------------------------------------------------------ observe --
    def start_observe(self):
        if self.model.repo is None:
            return self.say("先打开历史库，再开始观察。", "error")
        try:
            port = m.parse_port(self.port_var.get())
        except ValueError as exc:
            return self.fail(exc)
        if self.observe_state.active:
            return self.say("已有一个观察在运行；先停止它。", "error")
        if self.busy:
            return self.say("历史库正在打开/初始化或有存储操作进行中；等它完成后再开始观察，以免录制绑定到旧库。", "error")
        observe = self._observe_fn
        if observe is None:
            from vestigraph_backends.vesti_backend_klayout.capture import observe
        stop_event = self.observe_state.start(port)
        repo = self.model.repo

        def run():
            try:
                value, ok = observe(repo, host="127.0.0.1", port=port, stop_event=stop_event), True
            except BaseException as exc:
                value, ok = exc, False
            self.results.put(("observe", ok, value, None))

        self.observe_thread = threading.Thread(target=run, name="vestigraph-observe", daemon=True)
        self.observe_thread.start()
        self.observe_state.running()
        self._observe_ui()
        self.say(f"开始观察 127.0.0.1:{port}；基线导出完成后会出现在检查点列表（每 5 秒自动刷新）。")

    def stop_observe(self):
        if self.observe_state.request_stop():
            self._observe_ui()
            self.say("已请求停止；最终导出完成前不算已保存。")

    def _observe_done(self, ok, value):
        if ok:
            self.observe_state.finished(value)
            self.say(self.observe_state.label(), "ok")
        else:
            self.observe_state.failed(value)
            self.say("观察已停止（失败或被打断）。", "error")
            self.fail(value)
            self.say("对应过程段应显示为 interrupted；版图本身仍在 KLayout 中，未受影响。")
        self.observe_thread = None
        self._observe_ui()
        if self.model.repo is not None and not self.busy:
            self.refresh()

    def _observe_ui(self):
        self.observe_var.set(self.observe_state.label())
        self._set_actions()

    # -------------------------------------------------------------- close --
    def on_close(self):
        if self.closing:
            return
        if self.observe_state.active or self.busy:
            self.closing = True
            self.observe_state.request_stop()
            self._observe_ui()
            self.tk.title("Vestigraph 本地历史面板（正在停止，等待当前导出完成…）")
            self.say("窗口将在观察/存储线程结束后关闭；正在运行的导出不能被抢占。")
            return
        self.tk.destroy()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vestigraph.ui", description="Vestigraph 本地历史面板")
    parser.add_argument("--repo", metavar="DIR", help="启动时打开的数据目录")
    args = parser.parse_args(argv)
    root = tk.Tk()
    Panel(root, initial_repo=args.repo)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
