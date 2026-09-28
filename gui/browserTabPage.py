"""从浏览器取网址"""
"""gui/browserTabPage.py · 基础功能：读 Edge/Chrome 会话文件里的
标签页，勾选后送「加入新 mod」或复制——替代旧 1.5 脚本的采集入口。
解析全在 core/browserSessionReader（本页零解析逻辑）；工坊预勾选
判定调 urlParser 单源。Firefox 暂不支持（另一种会话格式，第二批）。
形态说明：本页是基础功能不是功能模块——按决策 22 做 tooltip +
操作反馈 + 空态引导即可，三行说明块是功能模块页的规矩。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QMessageBox,
)

from core import browserSessionReader
from core import tabCollector
from gui.consolePanel import LogBus


class BrowserTabPage(QWidget):
    # 勾选的网址清单 → 主窗口转交「加入新 mod」第②步
    handoff_to_addmod = Signal(list)

    def __init__(self, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._log = log or LogBus()
        self._stop = False

        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("从浏览器取网址", self)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        tip = QLabel(
            "实时采集 Edge 当前打开的标签页：点下方按钮后，程序自己逐个"
            "切换标签读取网址（期间占用鼠标键盘，请勿操作；要中断按 Esc）。"
            "工坊条目自动预勾选——勾好后【送到「加入新 mod」】继续入库，"
            "或【复制选中网址】走流程。",
            self)

        tip.setWordWrap(True)  # 可能变长的标签一律开换行
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)


        # 空态引导（决策 22④）：有结果就藏起来
        self._empty = QLabel(
            "还没采集。点【实时采集 Edge 标签页…】开始：程序会自动"
            "切换 Edge 的标签页逐个读取，完成后清单显示在这里"
            "（工坊条目已预勾选）。", self)

        self._empty.setWordWrap(True)
        self._empty.setStyleSheet("color: #8a8a8f;")
        root.addWidget(self._empty)

        self._tree = QTreeWidget(self)
        self._tree.setHeaderLabels(["勾选", "标题", "网址"])
        self._tree.setRootIsDecorated(False)
        self._tree.header().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)   # 网址列吃剩余宽度
        self._tree.setColumnWidth(0, 40)
        self._tree.setColumnWidth(1, 320)
        root.addWidget(self._tree, 1)

        btns = QHBoxLayout()
        for text, tip, slot in (
                ("全选", "勾选列表里全部标签页", lambda: self._set_all_checked(True)),
                ("全不选", "取消全部勾选", lambda: self._set_all_checked(False)),
                ("复制选中网址", "把勾选的网址逐行复制到剪贴板"
                                 "（兼容旧流程：可粘贴进【网址批量导入】）", self._on_copy_clicked),
                ("送到「加入新 mod」", "把勾选的网址填入「加入新 mod」第②步"
                                     "并自动解析预览；入库仍需在那边亲手点第③步按钮", self._on_handoff_clicked),
        ):
            btn = QPushButton(text, self)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            btns.addWidget(btn)
        # 第二采集路线：实时采集（键盘自动化，1.5 转正版）
        self._btn_collect = QPushButton("实时采集 Edge 标签页…", self)
        self._btn_collect.setToolTip(
            "用键盘自动化逐个读取 Edge 当前打开的标签页（程序自己切窗口，"
            "期间占用鼠标键盘：别动）。中断方式：按 Esc、Alt+Tab 回本工具"
            "点【停止采集】、或鼠标甩到屏幕左上角。首次使用会弹窗引导安装"
            "依赖，装完需重启本工具")

        self._btn_collect.clicked.connect(self._on_collect_clicked)
        btns.addWidget(self._btn_collect)
        self._btn_stop = QPushButton("停止采集", self)
        self._btn_stop.setToolTip("中断实时采集，返回已采到的部分")
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._stop_collection)
        btns.addWidget(self._btn_stop)
        btns.addStretch(1)
        root.addLayout(btns)

    # ---------- 读取 ----------

    def _fill_tree(self, tabs: list) -> None:
        self._tree.clear()
        for t in tabs:
            item = QTreeWidgetItem(["", t.title or "（无标题）", t.url])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            state = (Qt.CheckState.Checked if t.is_workshop
                     else Qt.CheckState.Unchecked)
            item.setCheckState(0, state)
            item.setToolTip(1, t.title)
            item.setToolTip(2, t.url)
            self._tree.addTopLevelItem(item)
        self._empty.setVisible(not tabs)

    # ---------- 勾选与出口 ----------

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self._tree.topLevelItemCount()):
            self._tree.topLevelItem(i).setCheckState(0, state)

    def _checked_urls(self) -> list[str]:
        return [self._tree.topLevelItem(i).text(2)
                for i in range(self._tree.topLevelItemCount())
                if self._tree.topLevelItem(i).checkState(0)
                == Qt.CheckState.Checked]

    def _on_copy_clicked(self) -> None:
        urls = self._checked_urls()
        if not urls:
            self._log.warn("没有勾选任何标签页")
            return
        QApplication.clipboard().setText("\n".join(urls))
        self._log.info(f"已复制 {len(urls)} 条网址到剪贴板")

    def _on_collect_clicked(self) -> None:
        """实时采集（pywinauto 路线）：同步执行，进度走日志，
        期间 processEvents 保界面呼吸 + 停止按钮可点。"""
        missing = tabCollector.missing_packages()
        if missing:
            QMessageBox.warning(
                self, "需要先安装采集依赖",
                "实时采集缺 " + "、".join(missing) + "。\n\n"
                                                    "安装命令（复制到 cmd 执行）：\n"
                                                    "pip install pywinauto pyautogui pyperclip psutil\n\n"
                                                    "装完后请重启本工具，再回来点本按钮。")
            return

        self._stop = False

        self._btn_collect.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._log.warn("实时采集中：会占用鼠标键盘，请勿操作；"
                       "中断就按 Esc，或 Alt+Tab 回本工具点【停止采集】")

        def progress(n: int) -> None:
            QApplication.processEvents()
            if n % 5 == 0:
                self._log.info(f"已采集 {n} 个标签页……")

        try:
            driver = tabCollector.EdgeDriver()
            tabs = tabCollector.collect_tabs(
                driver,
                should_stop=lambda: self._stop or driver.esc_pressed(),
                on_progress=progress)

        except tabCollector.CollectError as exc:
            self._log.warn(str(exc))
            return
        except Exception as exc:  # 含 pyautogui FAILSAFE（鼠标甩左上角）
            self._log.error(f"采集中断：{exc}")
            return
        finally:

            self._btn_collect.setEnabled(True)
            self._btn_stop.setEnabled(False)
        # 装进 BrowserTab 复用现有树渲染与工坊预勾选判定（urlParser 单源）
        items = [browserSessionReader.BrowserTab(t.url, t.title,
                                                 "实时采集", "-")
                 for t in tabs]
        self._fill_tree(items)
        n_ws = sum(1 for t in items if t.is_workshop)
        self._log.ok(f"实时采集完成：{len(items)} 个标签页"
                     f"（工坊条目 {n_ws} 个，已预勾选）")
        # 采集期间焦点被 Edge 拿走：把本工具拉回前台——下一步就是
        # 在这里核对勾选，回工具比回桌面少一步（故不回桌面）
        self.window().activateWindow()
        self.window().raise_()


    def _stop_collection(self) -> None:
        self._stop = True
        self._log.info("已请求停止采集……（当前标签处理完后收工）")


    def _on_handoff_clicked(self) -> None:
        urls = self._checked_urls()
        if not urls:
            self._log.warn("没有勾选任何标签页")
            return
        self.handoff_to_addmod.emit(urls)
