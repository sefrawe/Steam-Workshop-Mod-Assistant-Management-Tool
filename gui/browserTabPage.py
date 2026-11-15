"""从浏览器取网址（独立成页）。
"""
"""
实时采集路线（V1 tabCollector 原样复用）：点按钮后程序自己逐个切换
Edge 的标签页、读地址栏与标题——期间占用鼠标键盘，请勿操作；中断
方式 = Esc（轻点即可，粘住位不必按住）/ 随便动一下鼠标 /
【停止采集】/ 鼠标甩到屏幕左上角（pyautogui FAILSAFE）。
首次使用缺依赖时弹窗给 pip 安装指引，装完无需重启，再点一次即可。

（路线拍板记录：会话文件直读方案经 V1 多轮真机实测不可靠——Edge
SNSS v3 三轮 0 结果，用户明确否决；本页不做任何 SNSS/缓存解析，
tabCollector 是唯一采集路线。Linux（2.1.0 轮）例外：Firefox 的
recovery.jsonlz4 会话直读（FirefoxSessionDriver）与 Edge SNSS 不可
同日而语——官方格式、运行中约 15 秒自动保存，实测可靠；Windows
仍走键盘走查原路线。）

出口两条：工坊条目自动预勾选，【送到「加入新 mod」】转交第②步
（那边自动填入并解析预览，入库仍需亲手点）；或【复制选中网址】
粘贴到【入账中心 · 登记】走登记流程。工坊判定调 urlParser 单源
（extract_mod_id），与其他页面对"什么算工坊网址"的理解永远一致。
本类被两处复用：本独立页 + 模块②的 BrowserPickDialog（页内选择
器）——同一份代码零复制。

形态说明：本页是基础功能不是功能模块——tooltip + 操作反馈 +
空态引导即可（决策 22），不配三段式说明块。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import sys

from core import tabCollector
from core.i18n import t
from core.urlParser import extract_mod_id
from gui.logBus import LogBus
from gui.theme import font_px  # 字号单源（D25）
from gui.collapsibleSection import make_guide  # 先读我单源（共享件内）

_IS_WIN = sys.platform == "win32"  # Win=Edge 键盘走查；其余=Firefox 会话直读

class BrowserTabPage(QWidget):
    """从浏览器拿工坊网址。档案无关页（网址不挑游戏，
    归属在「加入新 mod」解析入库时锁定），无 set_game。
    Windows=Edge 键盘走查（V1 原路线）；其他平台=Firefox 会话直读。"""

    # 勾选的网址清单 → 主窗口转交「加入新 mod」第②步
    # （receive_external_lines 自动填入并解析预览；入库仍在那边
    # 亲手点——防呆不省，与解析→入库两段式一致）
    handoff_to_addmod = Signal(list)

    def __init__(self, parent: QWidget | None = None, *,
                 log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._log = log or LogBus()
        self._stop = False
        self._stop_reason: str | None = None  # 中断原因（Esc/鼠标/停止按钮），None=跑完
        self._build_ui()

    # ---------- UI ----------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        # 整页可滚：标签一多树吃高度，窗口矮时其余块不被裁
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(scroll)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        root = QVBoxLayout(body)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(6)

        title = QLabel(t("从浏览器取网址"), self)
        title.setStyleSheet(f"font-size: {font_px(18)}px; font-weight: 600;")
        root.addWidget(title)

        if _IS_WIN:
            guide_title = t("先读我：怎么采 · 怎么中断 · 出口两条")
            guide_text = t("实时采集 Edge 当前打开的标签页：点下方按钮后，程序自己"
                          "逐个切换标签读取网址（期间占用鼠标键盘，请勿操作；要"
                          "中断按 Esc 轻点即可，或随便动一下鼠标）。尽量只保留"
                          "不重复、要下载的标签页（空白页会自动跳过）。工坊条目"
                          "自动预勾选——勾好后【送到「加入新 mod」】继续入库，"
                          "或【复制选中网址】粘贴到【入账中心 · 登记】。")
        else:
            guide_title = t("先读我：怎么取 · 出口两条")
            guide_text = t("读取 Firefox 当前打开的标签页：点下方按钮后，程序"
                          "直读 Firefox 的会话存档——不占鼠标键盘、不用动 "
                          "Firefox。工坊条目自动预勾选——勾好后【送到「加入新 "
                          "mod」】继续入库，或【复制选中网址】粘贴到【入账中心 · "
                          "登记】。")
        # make_guide 第二参是「段落元组」，单段也要套元组——直接传
        # 字符串会被逐字拆成一个字一行（2026-10-31 踩坑：for text
        # in paras 把字符串当字符序列迭代了）
        root.addWidget(make_guide(guide_title, (guide_text,), self, 110))

        # 空态引导（决策 22④）：有结果就藏起来
        if _IS_WIN:
            empty_text = t("还没采集。点【实时采集 Edge 标签页…】开始：程序会自动"
                          "切换 Edge 的标签页逐个读取，完成后清单显示在这里"
                          "（工坊条目已预勾选）。")
        else:
            empty_text = t("还没读取。点【读取 Firefox 标签页…】开始：程序直读 "
                          "Firefox 的会话存档，完成后清单显示在这里"
                          "（工坊条目已预勾选）。")
        self._empty = QLabel(empty_text, self)
        self._empty.setWordWrap(True)
        self._empty.setStyleSheet("color: #8a8a8f;")
        root.addWidget(self._empty)

        self._tree = QTreeWidget(self)
        self._tree.setHeaderLabels([t("勾选"), t("标题"), t("网址")])
        self._tree.setRootIsDecorated(False)
        self._tree.header().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)  # 网址列吃剩余宽度
        self._tree.setColumnWidth(0, 40)
        self._tree.setColumnWidth(1, 320)
        root.addWidget(self._tree, 1)

        btns = QHBoxLayout()
        for text, tip_, slot in (
                (t("全选"), t("勾选清单里全部标签页"),
                 lambda: self._set_all_checked(True)),
                (t("全不选"), t("取消全部勾选"),
                 lambda: self._set_all_checked(False)),
                (t("复制选中网址"), t("把勾选的网址逐行复制到剪贴板"
                                 "（可粘贴进【入账中心 · 登记】）"), self._on_copy_clicked),
                (t("送到「加入新 mod」"), t("把勾选的网址填入「加入新 mod」"
                                     "第②步并自动解析预览；入库仍需在那边亲手点第③步按钮"),
                 self._on_handoff_clicked),
        ):
            btn = QPushButton(text, self)
            btn.setToolTip(tip_)
            btn.clicked.connect(slot)
            btns.addWidget(btn)
        # 采集主按钮与停止键：V1 同款位置与语义
        if _IS_WIN:
            btn_text = t("实时采集 Edge 标签页…")
            btn_tip = t("用键盘自动化逐个读取 Edge 当前打开的标签页（程序自己切"
                       "窗口，期间占用鼠标键盘：别动）。中断方式：按 Esc"
                       "（轻点即可）、随便动一下鼠标、Alt+Tab 回本工具点"
                       "【停止采集】、或鼠标甩到屏幕左上角。首次使用会弹窗引导"
                       "安装依赖，装完无需重启，再点一次即可")
        else:
            btn_text = t("读取 Firefox 标签页…")
            btn_tip = t("直读 Firefox 的会话存档拿当前打开的标签页：不占鼠标"
                       "键盘、不动 Firefox 窗口，以 Firefox 最近一次自动保存"
                       "的会话为准（正在运行约 15 秒内；已关闭则是最后退出"
                       "时的状态）")
        self._btn_collect = QPushButton(btn_text, self)
        self._btn_collect.setToolTip(btn_tip)
        self._btn_collect.clicked.connect(self._on_collect_clicked)
        btns.addWidget(self._btn_collect)
        self._btn_stop = QPushButton(t("停止采集"), self)
        self._btn_stop.setToolTip(t("中断实时采集，返回已采到的部分"))
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._stop_collection)
        btns.addWidget(self._btn_stop)
        btns.addStretch(1)
        root.addLayout(btns)

    # ---------- 采集（Win=V1 走查原路线；其余=Firefox 会话直读） ----------
    def _on_collect_clicked(self) -> None:
        """实时采集：同步执行，进度走日志。留在主线程是刻意的——
        pywinauto/pyautogui 直接操纵系统级窗口键盘，与工作线程混用
        会互相打架；四路中断（Esc 轻点 / 动鼠标 / 停止按钮 /
        FAILSAFE）全靠 probe() 里高频询问 + processEvents 泵事件。
        其他平台走 FirefoxSessionDriver（只读会话文件，瞬时完成）。"""
        msg = tabCollector.missing_packages_message()
        if msg:
            QMessageBox.warning(self, t("需要先安装采集依赖"), msg)
            return
        self._stop = False
        self._stop_reason = None  # 每轮清零：中断原因随本轮记录
        self._btn_collect.setEnabled(False)
        try:
            if _IS_WIN:
                self._btn_stop.setEnabled(True)
                self._log.warn(t("实时采集中：会占用鼠标键盘，请勿操作；"
                               "中断 = Esc 轻点一下 / 随便动一下鼠标 / "
                               "Alt+Tab 回本工具点【停止采集】"))

                def progress(n: int) -> None:
                    if n % 5 == 0:
                        self._log.info((t('已采集 ') + f"{n}" + t(' 个标签页……')))

                driver = tabCollector.EdgeDriver()

                def probe() -> bool:
                    # 每步、连同偷网址的 0.05 秒轮询都会进来。这里是
                    # 采集中唯一稳定的事件泵：processEvents 挪进来后
                    # 空转绕圈时 UI 照样呼吸、【停止采集】照点得动
                    # （原先只在新标签采到时泵一次，卡住就假死）。
                    # 同时记下是哪一路刹车触发的，收工后好打日志。
                    QApplication.processEvents()
                    if self._stop:
                        self._stop_reason = self._stop_reason or t('停止按钮')
                        return True
                    if driver.esc_pressed():
                        self._stop_reason = self._stop_reason or "Esc"
                        return True
                    if driver.mouse_moved():
                        self._stop_reason = self._stop_reason or t('鼠标移动')
                        return True
                    return False

                tabs = tabCollector.collect_tabs(
                    driver,
                    should_stop=probe,
                    on_progress=progress,
                    on_note=lambda note: self._log.warn(note))
            else:
                QApplication.processEvents()
                self._log.info(t("正在读取 Firefox 会话存档……"))
                tabs = tabCollector.FirefoxSessionDriver().collect()
        except tabCollector.CollectError as exc:
            self._log.warn(str(exc))
            return
        except Exception as exc:  # 含 pyautogui FAILSAFE（鼠标甩左上角）
            self._log.error((t('采集中断：') + f"{exc}"))
            return
        finally:
            self._btn_collect.setEnabled(True)
            self._btn_stop.setEnabled(False)

        self._fill_tree([(tab.url, tab.title) for tab in tabs])

        n_ws = sum(1 for tab in tabs if extract_mod_id(tab.url) is not None)

        if self._stop_reason:
            # 被刹车中断：单独一行点名是哪一路，方便用户判定到底
            # 停没停、靠什么停的（用户点名要的判定日志）
            self._log.warn(t("已中断采集") + f"（{self._stop_reason}）——"
                           + t("已采集") + f" {len(tabs)} " + t("个标签页")
                           + f"（{t('工坊条目')} {n_ws} " + t("个，已预勾选") + "）")
        else:
            done = t("实时采集完成") if _IS_WIN else t("Firefox 标签读取完成")
            self._log.ok(f"{done}：{len(tabs)} " + t("个标签页")
                         + f"（{t('工坊条目')} {n_ws} " + t("个，已预勾选") + "）")
        # 采集期间焦点被浏览器拿走：把本工具拉回前台——下一步就是
        # 在这里核对勾选，回工具比回桌面少一步（故不回桌面）
        self.window().activateWindow()
        self.window().raise_()

    def _stop_collection(self) -> None:
        self._stop = True
        self._log.info(t("已请求停止采集……马上收工"))

    # ---------- 勾选与出口 ----------
    def _fill_tree(self, items: list[tuple[str, str]]) -> None:
        """(网址, 标题) 清单 → 勾选树；工坊条目预勾选（urlParser 单源）。"""
        self._tree.clear()
        for url, t_title in items:
            item = QTreeWidgetItem(["", t_title or t("（无标题）"), url])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                0, Qt.CheckState.Checked
                if extract_mod_id(url) is not None
                else Qt.CheckState.Unchecked)
            item.setToolTip(1, t_title)
            item.setToolTip(2, url)
            self._tree.addTopLevelItem(item)
        self._empty.setVisible(not items)

    def _set_all_checked(self, checked: bool) -> None:
        state = (Qt.CheckState.Checked if checked
                 else Qt.CheckState.Unchecked)
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
            self._log.warn(t("没有勾选任何标签页"))
            return
        QApplication.clipboard().setText("\n".join(urls))
        self._log.info(t("已复制") + f" {len(urls)} " + t("条网址到剪贴板——可粘贴到"
                       "【入账中心 · 登记】"))

    def _on_handoff_clicked(self) -> None:
        urls = self._checked_urls()
        if not urls:
            self._log.warn(t("没有勾选任何标签页"))
            return
        self.handoff_to_addmod.emit(urls)
