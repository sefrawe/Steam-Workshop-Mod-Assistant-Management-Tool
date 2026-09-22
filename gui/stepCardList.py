"""批量下载步骤卡片
"""
"""
"下载批次"标签页的主体：把 core/batchDownloadFlow 发出的事件字典
画成步骤卡片（登录 / 逐条下载 / 汇总）+ 逐条结果列表，并提供
【停止批次】【继续批次】两个按钮。

解耦原则（本类最重要的一条）：只认识"事件字典"这一种输入
（handle_event），不 import batchDownloadFlow、不认识 TerminalDock。
卡片和状态机的唯一耦合面就是流程 _emit 出来的那些 type 字符串。
好处双向成立：卡片随便改版，状态机一行不动；状态机怎么改，
只要事件字段不变，卡片照常工作。

事件字典 → 界面（字段定义见 batchDownloadFlow 各 _emit 处）：
- item_started  → 下载卡进度 + 结果列表追加一行"下载中"
- item_done     → 那一行落定颜色和结论
- login_started / login_ok / need_login → 登录卡状态
- disconnected / warn / send_failed → 追加黄色/红色提示行
- stop_requested → 顶栏提示"停止中"
- batch_done    → 汇总卡显示成败统计，按钮复位

安全细节：登录卡只显示"登录命令已发送"，不回显命令原文——
原样命令里可能带密码（决策 19：不解析登录命令），终端里已经
能看到原文，卡片没必要再抄一份。

按钮不发命令只发信号（stop_requested / resume_requested）：
真正调 flow.stop()/flow.resume() 的是控制器——谁建的 flow 谁
接线，本类保持"只画界面"的本分。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget,
)

# 与 consolePanel 一致的配色，按用途命名
_C_OK = "#46a758"     # 成功
_C_FAIL = "#e5484d"   # 失败/出错
_C_WARN = "#f5a623"   # 需要人管（断线/未登录/超时）
_C_INFO = "#d4d4d4"   # 进行中
_C_MUTED = "#8a8a8f"  # 占位/未定


class StepCardList(QWidget):
    """批次步骤卡片；按钮信号交给控制器接 flow。"""

    stop_requested = Signal()
    resume_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._total = 0
        self._done = 0
        self._running = False
        self._cards: dict[str, tuple[QFrame, QLabel]] = {}  # name -> (框, 详情行)
        self._current_row: QListWidgetItem | None = None    # "下载中"那一行

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)

        # 顶栏：状态 + 停止/继续
        top = QHBoxLayout()
        self._status = QLabel(
            "未开始：在 mod 库勾选要下载的 mod，然后点【下载选中项】", self)
        self._btn_stop = QPushButton("停止批次", self)
        self._btn_stop.setToolTip(
            "温和停止：不再发新命令，正在下载的那条让它跑完再收尾")
        self._btn_resume = QPushButton("继续批次", self)
        self._btn_resume.setToolTip(
            "在终端里完成手动登录后点这里，继续发剩下的下载命令")
        self._btn_stop.setEnabled(False)
        self._btn_resume.setEnabled(False)
        self._btn_stop.clicked.connect(self.stop_requested.emit)
        self._btn_resume.clicked.connect(self._on_resume_clicked)
        top.addWidget(self._status, 1)
        top.addWidget(self._btn_stop)
        top.addWidget(self._btn_resume)
        root.addLayout(top)

        # 步骤卡片容器（登录/下载/汇总，按批次动态重建）
        self._cards_host = QWidget(self)
        self._cards_box = QVBoxLayout(self._cards_host)
        self._cards_box.setContentsMargins(0, 4, 0, 4)
        self._cards_box.setSpacing(6)
        root.addWidget(self._cards_host)

        # 逐条结果列表（QListWidget 自带滚动，几百条也轻快）
        self._items = QListWidget(self)
        self._items.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self._items.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        root.addWidget(self._items, 1)

    # ---------------- 对外 ----------------

    def is_running(self) -> bool:
        return self._running

    def reset_for_batch(self, total: int, has_login: bool) -> None:
        """清场并按本批次的形状重建卡片（控制器在 flow.start 之前调）。

        必须先于 start：start 内部可能同步发出事件（第一条命令
        就发不出去时连 batch_done 都会当场发出来），不能落在
        没有卡片的界面上。
        """
        self._total = total
        self._done = 0
        self._running = True
        self._current_row = None
        for frame, _ in self._cards.values():
            self._cards_box.removeWidget(frame)
            frame.deleteLater()
        self._cards.clear()
        self._items.clear()
        if has_login:
            self._make_card("login", "① 登录")
            download_title = "② 逐条下载"
        else:
            download_title = "① 逐条下载"
        self._make_card("download", download_title)
        self._set_card("download", f"0/{total} 完成", _C_MUTED)
        self._status.setText(f"批次进行中：共 {total} 条")
        self._btn_stop.setEnabled(True)
        self._btn_resume.setEnabled(False)

    def handle_event(self, ev: dict) -> None:
        """流程事件的唯一入口：一个事件字典，一次界面更新。"""
        t = ev.get("type")
        if t == "login_started":
            # 不回显命令原文（可能带密码，见文件头安全细节）
            self._set_card("login", "登录命令已发送，等待 steamcmd 应答…", _C_INFO)
        elif t == "login_ok":
            self._set_card("login", "登录成功", _C_OK)
        elif t == "need_login":
            self._set_card("login", "需要手动登录：去终端完成登录后点【继续批次】",
                           _C_WARN)
            self._btn_resume.setEnabled(True)
        elif t == "item_started":
            self._done = ev.get("done", self._done)
            mod_id = ev.get("mod_id")
            self._set_card("download",
                           f"{self._done}/{self._total} 完成 · 当前 mod {mod_id}",
                           _C_INFO)
            self._status.setText(f"进行中 {self._done + 1}/{self._total}")
            self._current_row = self._append_row(f"mod {mod_id} · 下载中…",
                                                 _C_MUTED)
        elif t == "item_done":
            self._done = ev.get("done", self._done + 1)
            mod_id = ev.get("mod_id")
            ok = ev.get("ok")
            reason = ev.get("reason") or ""
            if ok:
                self._finish_current_row(f"mod {mod_id} · 成功", _C_OK)
            elif reason == "Timeout":
                # 超时行提醒"未必真失败"：大 mod 可能还在跑，
                # 真相在终端原文里
                self._finish_current_row(
                    f"mod {mod_id} · 超时（大 mod 可能只是慢，看终端确认）",
                    _C_WARN)
            else:
                self._finish_current_row(f"mod {mod_id} · 失败：{reason}",
                                         _C_FAIL)
            self._set_card("download", f"{self._done}/{self._total} 完成",
                           _C_INFO)
            self._status.setText(f"进行中 {self._done}/{self._total}")
        elif t == "disconnected":
            code = ev.get("code")
            note = ev.get("note") or ""
            self._append_row(f"⚠ 断线（code {code}）{note}：steamcmd 会自动重试",
                             _C_WARN)
        elif t == "warn":
            self._append_row(f"⚠ {ev.get('note') or ''}", _C_WARN)
        elif t == "send_failed":
            self._append_row(f"✗ 命令发送失败：{ev.get('note') or ''}", _C_FAIL)
        elif t == "stop_requested":
            self._status.setText("停止中：不再发新命令，等当前条跑完…")
            self._btn_stop.setEnabled(False)
        elif t == "batch_done":
            self._on_batch_done(ev.get("summary") or {})

    # ---------------- 内部：卡片与行 ----------------

    def _make_card(self, name: str, title: str) -> None:
        """造一张步骤卡片（带圆角边框），登记进 self._cards。"""
        frame = QFrame(self._cards_host)
        frame.setObjectName(f"card_{name}")
        # 样式选择器限定到本框：QLabel 也是 QFrame 子类，
        # 不限定选择器会把边框画到卡片里每行字上
        frame.setStyleSheet(
            f"QFrame#card_{name} {{ border: 1px solid #3a3a3a;"
            f" border-radius: 6px; }}")
        box = QVBoxLayout(frame)
        box.setContentsMargins(8, 6, 8, 6)
        title_lbl = QLabel(title, frame)
        title_lbl.setStyleSheet("border:none; font-weight:600;")
        detail = QLabel("", frame)
        detail.setWordWrap(True)
        detail.setStyleSheet(f"border:none; color:{_C_MUTED};")
        box.addWidget(title_lbl)
        box.addWidget(detail)
        self._cards_box.addWidget(frame)
        self._cards[name] = (frame, detail)

    def _set_card(self, name: str, text: str, color: str) -> None:
        """更新某张卡的详情行；卡不存在就安静跳过（防御式）。"""
        card = self._cards.get(name)
        if card is None:
            return
        _, detail = card
        detail.setText(text)
        detail.setStyleSheet(f"border:none; color:{color};")

    def _append_row(self, text: str, color: str) -> QListWidgetItem:
        item = QListWidgetItem(text)
        item.setForeground(QColor(color))
        self._items.addItem(item)
        self._items.scrollToBottom()
        return item

    def _finish_current_row(self, text: str, color: str) -> None:
        """把当前"下载中"那一行原地改写成最终结论（不新增行）。"""
        if self._current_row is not None:
            self._current_row.setText(text)
            self._current_row.setForeground(QColor(color))
            self._current_row = None

    def _on_batch_done(self, s: dict) -> None:
        self._running = False
        self._btn_stop.setEnabled(False)
        self._btn_resume.setEnabled(False)
        # 批次结束时还挂着"下载中"（如发送失败中途收尾）→ 落定为无结论
        if self._current_row is not None:
            self._finish_current_row("（未收到结论，批次已结束）", _C_MUTED)
        ok_n = len(s.get("ok") or [])
        fail_n = len(s.get("failed") or [])
        to_n = len(s.get("timeout") or [])
        self._make_card("summary", "汇总")
        error = s.get("error") or ""
        if error:
            self._set_card("summary", f"批次出错：{error}", _C_FAIL)
            self._status.setText("批次出错")
            return
        total = s.get("total", self._total)
        text = f"成功 {ok_n} · 失败 {fail_n} · 超时 {to_n}（共 {total}）"
        if s.get("stopped"):
            self._set_card("summary", "已停止：" + text, _C_WARN)
            self._status.setText("批次已停止")
        elif fail_n == 0 and to_n == 0:
            self._set_card("summary", text, _C_OK)
            self._status.setText("批次完成")
        else:
            self._set_card("summary", text, _C_WARN)
            self._status.setText("批次完成（有未成功的条目，可重跑）")

    # ---------------- 按钮 ----------------

    def _on_resume_clicked(self) -> None:
        # 点一次即灭：等下一次 need_login 事件再亮，
        # 防止连点重复触发 resume
        self._btn_resume.setEnabled(False)
        self.resume_requested.emit()
