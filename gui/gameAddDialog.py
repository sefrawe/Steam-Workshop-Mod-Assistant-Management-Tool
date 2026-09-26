"""添加游戏档案对话框
"""
"""
一屏完成"输入 AppID → 查重 → 自动查名 → 推导下载目录预览 → 确认创建"，
替代旧版三连 QInputDialog：少几轮弹窗，且确认前就能看到推导出的目录
——先看清楚再动手，与账实核验页同一哲学。
只与 ModRepository / SteamApiClient 接口交互，GUI 层零 SQL（记事本架构约定）。

绑定说明：全项目 GUI 统一 PySide6（backupPage / backupMoveDialog 同款）。
一个进程只能加载一种 Qt 绑定，PyQt6 / PySide6 混装会在运行期崩溃——
本文件因此不留任何 PyQt6 痕迹。

查名跑后台线程（既定规矩：网络操作不许卡界面）；下载目录推导由外部
注入函数——本文件不 import steamPaths，推导逻辑留在 core，对话框只
显示结果。落账走契约原文 add_game(app_id, name, download_dir, …)，
其中 download_dir 必填：由 derive_dir 现场推导；推导不出（steamcmd
未配置）就存空串——steamcmd 配好后首次扫描会经
steamPaths.refresh_download_dir 自动回填，建档不被卡住。
"""

from collections.abc import Callable

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QVBoxLayout,
)

from core.modRepository import ModRepository
from core.steamApiClient import SteamApiClient


class _NameQueryThread(QThread):
    """一次性查名线程：后台查 Steam 商店，界面不冻结。
    结果由 done 信号带走 (name, err)，两者恰好一新一旧（一个为 None）：
    - name=str：查到了
    - name=None, err=None：Steam 没这个 AppID 的名字——正常回答，不是错误
    - err=str：网络层失败（限流重试耗尽等），要让人看见（拍板 4）
    """
    done = Signal(object, object)

    def __init__(self, api: SteamApiClient, app_id: int, parent=None) -> None:
        super().__init__(parent)
        self._api = api
        self._app_id = app_id

    def run(self) -> None:
        try:
            name = self._api.query_app_name(self._app_id)
        except Exception as exc:  # SteamApiError 及一切网络层失败
            self.done.emit(None, str(exc))
        else:
            self.done.emit(name, None)


class GameAddDialog(QDialog):
    """三个协作者各司其职：
    - repo：查重（get_game）+ 落账（add_game）——唯一的落账调用点在本类
    - api：查名（query_app_name）
    - derive_dir：appid → 下载目录字符串；推导不出返回 None（不是异常），
      预览处如实说明、落账存空串

    gameSwitcher 接线示意（替换版会照此接）：
        GameAddDialog(self._repo, self._api,
                      lambda aid: steamPaths.workshop_content_dir(
                          self._settings.get("steamcmd_path"), aid),
                      self)
    """

    def __init__(self, repo: ModRepository, api: SteamApiClient,
                 derive_dir: Callable[[int], str | None],
                 parent=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._api = api
        self._derive_dir = derive_dir
        self._thread: _NameQueryThread | None = None
        self._app_free = False  # 最近一次查重的结论：该 AppID 无档案
        self.created_app_id: int | None = None  # 落账成功时记录，供调用方选中新档案

        self.setWindowTitle("添加游戏档案")
        self.setMinimumWidth(560)

        # 按钮组先建：底部布局和"创建"键的引用都要用它
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText("创建档案")
        self._buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self._buttons.accepted.connect(self._accept_create)
        self._buttons.rejected.connect(self.reject)
        self._ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("AppID："))
        self._edit_appid = QLineEdit()
        # Steam 的 AppID 是正整数；32 位上限绰绰有余
        self._edit_appid.setValidator(QIntValidator(1, 2147483647))
        self._edit_appid.setPlaceholderText("如 1158310（商店页网址里那串数字）")
        row1.addWidget(self._edit_appid, stretch=1)
        self._btn_lookup = QPushButton("自动查名")
        self._btn_lookup.setEnabled(False)
        row1.addWidget(self._btn_lookup)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("游戏名："))
        self._edit_name = QLineEdit()
        self._edit_name.setEnabled(False)
        self._edit_name.setPlaceholderText("点「自动查名」带出，或手动输入")
        row2.addWidget(self._edit_name, stretch=1)

        self._lbl_dir = QLabel("—")
        self._lbl_dir.setWordWrap(True)
        # 目录文本可选中复制，用户能直接拿去核对
        self._lbl_dir.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)

        self._lbl_hint = QLabel("请输入 AppID（纯数字）")
        self._lbl_hint.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.addLayout(row1)
        lay.addLayout(row2)
        lay.addWidget(QLabel("下载目录（推导预览，建档后可在【编辑档案】里改）："))
        lay.addWidget(self._lbl_dir)
        lay.addWidget(self._lbl_hint)
        lay.addWidget(self._buttons)

        # 输入任意变动都重新校验；AppID 变动额外触发查重与目录推导
        self._edit_appid.textChanged.connect(self._on_appid_changed)
        self._edit_name.textChanged.connect(self._sync_ok)
        self._btn_lookup.clicked.connect(self._lookup)
        self._on_appid_changed("")  # 初始校验：按钮初态与提示语在此定型

    # ---------- 校验与查重 ----------

    def _on_appid_changed(self, text: str) -> None:
        # QIntValidator 只拦非数字；"0" 这类仍要转成数值判
        app_id = int(text) if text.isdigit() else 0
        if app_id <= 0:
            self._app_free = False
            self._lbl_hint.setText("请输入 AppID（纯数字）")
            self._edit_name.setEnabled(False)
            self._btn_lookup.setEnabled(False)
            self._lbl_dir.setText("—")
            self._sync_ok()
            return
        # 查重：已有档案就直说，不让用户走完流程才发现撞车
        exists = self._repo.get_game(app_id)
        if exists is not None:
            self._app_free = False
            self._lbl_hint.setText(f"该 AppID 已有档案：{exists.name}，无需创建")
            self._edit_name.setEnabled(False)
            self._btn_lookup.setEnabled(False)
            self._sync_ok()
            return
        self._app_free = True
        self._edit_name.setEnabled(True)
        self._btn_lookup.setEnabled(True)
        self._lbl_hint.setText("就绪。名字可自动查，也可手动输入")
        self._refresh_dir_preview(app_id)
        self._sync_ok()

    def _refresh_dir_preview(self, app_id: int) -> None:
        # 推导不出（steamcmd 未配置）≠ 出错：如实说明、不拦建档，
        # steamcmd 配好后首次扫描会自动回填（steamPaths.refresh_download_dir）
        try:
            path = self._derive_dir(app_id)
        except Exception as exc:
            self._lbl_dir.setText(f"（推导失败：{exc}）")
            self._lbl_dir.setToolTip("")
            return
        if path:
            self._lbl_dir.setText(path)
            self._lbl_dir.setToolTip(path)
        else:
            self._lbl_dir.setText("（暂无法推导：设置页还没配 steamcmd 程序）")
            self._lbl_dir.setToolTip("")

    def _sync_ok(self) -> None:
        """可建档 = AppID 有效且无档案 + 名字已备好，两者同时成立。"""
        name = self._edit_name.text().strip()
        self._ok.setEnabled(self._app_free and bool(name))

    # ---------- 查名（后台线程） ----------

    def _lookup(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return  # 上一发还在路上（按钮此时应为灰，双保险）
        text = self._edit_appid.text().strip()
        if not text.isdigit():
            return
        self._thread = _NameQueryThread(self._api, int(text), self)
        self._thread.done.connect(self._on_name_done)
        self._thread.finished.connect(self._on_lookup_finished)

        self._btn_lookup.setEnabled(False)
        self._btn_lookup.setText("查询中…")
        self._thread.start()

    def _on_name_done(self, name, err) -> None:
        self._btn_lookup.setEnabled(True)
        self._btn_lookup.setText("自动查名")
        if err:
            # 网络失败：如实显示，名字留空让用户手输——不拦建档（拍板 4）
            QMessageBox.warning(self, "查询失败",
                                f"查名失败（{err}）。\n请手动输入游戏名。")
        elif not name:
            QMessageBox.information(
                self, "查无此游戏",
                "Steam 没有报出这个名字（编号可能有误；商店接口偶尔也会"
                "抽风，可再点一次「自动查名」试试）。\n"
                "若仍查不到，请手动输入游戏名，不影响建档。")
        else:
            self._edit_name.setText(name)
            self._edit_name.selectAll()  # 全选，方便微调或整体覆盖
        self._sync_ok()

    def _on_lookup_finished(self) -> None:
        """查名线程收尾（backupPage._CallWorker 同款模式）：先摘引用再
        wait。不用 deleteLater——那会把 C++ 对象先删掉，而 Python 引用
        还在 self._thread 上，下一次点击 isRunning() 就碰尸（本次实测
        翻车）。引用置 None 后包装自然回收；C++ 侧有 parent 兜底，
        对话框关闭时统一清理，无泄漏。"""
        w = self._thread
        self._thread = None
        if w is not None:
            w.wait()

    # ---------- 落账 ----------

    def _accept_create(self) -> None:
        app_id = int(self._edit_appid.text().strip())
        name = self._edit_name.text().strip()
        # 契约：add_game(app_id, name, download_dir, …) —— download_dir 必填。
        # 推导不出就存空串：账先立起来，steamcmd 配好后首次扫描自动回填
        download_dir = self._derive_dir(app_id) or ""
        try:
            self._repo.add_game(app_id, name, download_dir)
        except Exception as exc:
            # 极小概率的竞态（查重通过后同号被建）或其他账本层拒绝：
            # 显示原因、对话框不关，用户可改完再试
            QMessageBox.critical(self, "创建失败", str(exc))
            return
        self.created_app_id = app_id
        self.accept()

    # ---------- 关闭时别把查名线程晾在半空 ----------

    def _wait_lookup(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            self._thread.wait(1500)  # 查询通常秒回，1.5 秒是宽限

    def reject(self) -> None:
        self._wait_lookup()
        super().reject()

    def closeEvent(self, event) -> None:  # 点 X 直接关
        self._wait_lookup()
        super().closeEvent(event)
