"""Steam Web API 密钥（注册指引 · 保存 · 在线验证） """
"""
本页解决一件事：把依赖检测需要的 Steam Web API key 引导到位。

为什么需要 key：匿名工坊接口（GetPublishedFileDetails）不返回 mod 的
「必需物品」（依赖）清单——实测字段全集里没有 children；只有带 key 的
IPublishedFileService/GetDetails 才返回。异常处理的新桶（缺依赖 /
依赖变化）以此为主要数据源。

key 的纪律（与登录命令同待遇，R2）：
- 只存本机设置（AppSettings 键 steam_api_key），绝不写入运行日志、
  绝不进命令回显，界面只显示尾号四位；
- key 绑定 Steam 账号，泄露了随时到注册页重置（旧 key 立即失效）。

「验证此 key」把探针（tools/probe_children_key.py 的思路）内置进来：
拿一个工坊页明确写了必需物品的 mod 现查一次，回报 children 有没有、
有几项——这是依赖检测方案的实测入口，把结果贴给开发即可定稿。

本页零 SQL、零 repo（记事本架构约定）：只读写 AppSettings 的一个键。
key 未设置时依赖检测应降级为"未开启"并指向本页，属功能未配置，
不是故障，不弹错。
"""
import re

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# AppSettings 键名（将来设置页如收编同一键，两处同名即可）
_KEY = "steam_api_key"
# 注册页：免费，个人默认额度 10 万次/天，本工具每轮深检十几次调用
_REGISTER_URL = "https://steamcommunity.com/dev/apikey"
# 验证样本：Better Architect Menu（3563882422）——工坊页明确写了
# 3 个必需物品（Harmony / Architect Icons / Architect Menu Optimizer），
# 正好用来对照接口给不给 children
_DEFAULT_PROBE_ID = "3563882422"
# Steam Web API key 的标准形状：32 位十六进制
_HEX32 = re.compile(r"[0-9a-fA-F]{32}")


class _ProbeWorker(QThread):
    """后台验证线程：带 key 现查一次 GetDetails，回报 children 形状。
    联网必须走工作线程（R11），证书走 truststore（踩坑⑦同款解法，
    与主程序一致）。"""
    done = Signal(dict)  # {"ok": bool, "children": list[str], "msg": str}

    def __init__(self, key: str, mod_id: str, parent=None) -> None:
        super().__init__(parent)
        self._key = key
        self._mod_id = mod_id

    def run(self) -> None:
        try:
            import truststore
            truststore.inject_into_ssl()
        except Exception:
            pass  # 没装 truststore 就退回 certifi 默认（本机已装）
        import requests
        try:
            resp = requests.get(
                "https://api.steampowered.com/"
                "IPublishedFileService/GetDetails/v1/",
                params={
                    "key": self._key,
                    "publishedfileids[0]": self._mod_id,
                    "includemetadata": "true",
                    "includechildren": "true",
                },

                timeout=30,
            )
            if resp.status_code == 403:
                # 无效/被重置的 key，Steam 的表现就是 403 Forbidden
                self.done.emit({"ok": False, "children": [],
                                "msg": "服务器拒绝（403）：key 无效或已被重置。"
                                       "请回 steamcommunity.com/dev/apikey "
                                       "核对或重新注册一个"})
                return
            resp.raise_for_status()
            items = (resp.json().get("response", {})
                     .get("publishedfiledetails") or [])
            if not items:
                self.done.emit({"ok": False, "children": [],
                                "msg": "响应里没有条目数据（验证编号对吗？）"})
                return
            item = items[0]
            # 绝不静默口径：children 键缺席（参数未生效/接口行为变了）
            # 和空数组（真无依赖）是两种状态——键缺席时明确报错，
            # 绝不静默当 0 项
            raw = item.get("children")
            if raw is None:
                self.done.emit({
                    "ok": False, "children": [],
                    "msg": "响应条目里没有 children 键：includechildren "
                           "参数未生效或接口行为变了——把此结果告诉开发者",
                })
                return
            ids = [str(c.get("publishedfileid", "?"))
                   for c in raw if isinstance(c, dict)]

            title = item.get("title") or "?"
            banned = item.get("banned")
            msg = (f"接口通了：《{title}》 banned={banned}，"
                   f"children（必需物品）共 {len(ids)} 项"
                   + (f"，前几项：{'、'.join(ids[:5])}" if ids else "。"
                      "——这个编号工坊页写了必需物品却没有 children？"
                      "请把此结果告诉开发者"))
            self.done.emit({"ok": True, "children": ids, "msg": msg})
        except Exception as exc:  # noqa: BLE001 —— 线程边界兜底
            self.done.emit({"ok": False, "children": [],
                            "msg": f"请求失败：{exc}"})


class ApiKeyPage(QWidget):
    """注册指引 + 保存 + 在线验证，一页闭环。"""

    def __init__(self, settings, stack, log=None) -> None:
        super().__init__(stack)
        self._settings = settings
        self._log = log
        self._worker: _ProbeWorker | None = None  # 验证线程（shutdown 要等）
        self._build_ui()
        self.refresh()

    # ---------------- UI ----------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(10)

        title = QLabel("<b>Steam Web API 密钥</b>", self)
        title.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(title)

        intro = QLabel(
            "依赖检测（异常处理的「缺依赖 / 依赖变化」）需要一把免费的 "
            "Steam Web API key：匿名接口拿不到 mod 的「必需物品」清单，"
            "只有带 key 的官方接口才返回。key 只保存在本机设置里，"
            "绝不写入日志；泄露了随时可在注册页重置。", self)
        intro.setWordWrap(True)
        root.addWidget(intro)

        # 状态行 + 输入行
        self._status = QLabel(self)
        root.addWidget(self._status)

        row = QHBoxLayout()
        self._input = QLineEdit(self)
        self._input.setPlaceholderText("把 32 位 key 粘贴到这里")
        row.addWidget(self._input, 1)
        btn_save = QPushButton("保存", self)
        btn_save.clicked.connect(self._on_save)
        row.addWidget(btn_save)
        btn_clear = QPushButton("清除", self)
        btn_clear.clicked.connect(self._on_clear)
        row.addWidget(btn_clear)
        root.addLayout(row)

        btn_open = QPushButton("打开 Steam 注册页（steamcommunity.com/dev/apikey）",
                               self)
        btn_open.clicked.connect(self._on_open_register)
        root.addWidget(btn_open)

        guide = QLabel(
            "<h3>四步拿到 key（免费，约两分钟）</h3>"
            "1. 浏览器登录 steamcommunity.com（你逛创意工坊的账号）<br>"
            '2. 打开 <a href="https://steamcommunity.com/dev/apikey">'
            "steamcommunity.com/dev/apikey</a><br>"
            "3. 域名一栏随便填（例如 localhost），勾选同意条款，点【注册】"
            "<br>4. 复制页面上 32 位的 key，粘贴到上面输入框，点【保存】"
            "<br><br>忘了 key 是什么？回注册页点【重置】拿一个新的即可，"
            "旧 key 同时失效。"
            "<br><br><b>看到「拒绝访问」？</b>新注册或消费未满 $5 的受限"
            "账户暂时拿不到 key（在别的平台买的 CDKey 激活不计入消费"
            "额度）。在 Steam 商店直接消费满 $5（约 ¥36，买游戏/充钱包"
            "都算）后回来重试即可。注册不了不影响本软件其他功能：依赖"
            "检测保持未开启，其余一切照常。", self)

        guide.setTextFormat(Qt.TextFormat.RichText)
        guide.setOpenExternalLinks(True)
        guide.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction)
        guide.setWordWrap(True)
        root.addWidget(guide)

        # ---- 在线验证区 ----
        verify_title = QLabel("<b>在线验证（可选，但强烈建议）</b>", self)
        verify_title.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(verify_title)
        verify_hint = QLabel(
            "保存之后，用下面这个 mod 现查一次接口：它的工坊页明确写了 "
            "3 个必需物品，正好用来验证接口给不给依赖清单。验证结果请"
            "反馈给开发者（决定依赖检测的最终实现）。", self)
        verify_hint.setWordWrap(True)
        root.addWidget(verify_hint)

        vrow = QHBoxLayout()
        self._probe_input = QLineEdit(_DEFAULT_PROBE_ID, self)
        vrow.addWidget(QLabel("验证编号：", self))
        vrow.addWidget(self._probe_input, 1)
        self._verify_btn = QPushButton("验证此 key（联网查询一次）", self)
        self._verify_btn.clicked.connect(self._on_verify)
        vrow.addWidget(self._verify_btn)
        root.addLayout(vrow)

        self._result = QLabel(self)
        self._result.setWordWrap(True)
        self._result.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self._result)

        root.addStretch(1)

    # ---------------- 槽 ----------------
    def _on_save(self) -> None:
        key = self._input.text().strip()
        if not key:
            QMessageBox.information(
                self, "保存密钥", "输入框是空的：要清掉已保存的 key 请点【清除】。")
            return
        if not _HEX32.fullmatch(key):
            ret = QMessageBox.question(
                self, "格式不像 key",
                "Steam Web API key 通常是 32 位十六进制字符，当前输入不是"
                "这个形状——可能是没复制全。\n\n仍要保存吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                return
        # AppSettings 的写入方法按项目惯例叫 set；万一版本不同没有这个
        # 方法，明确告诉用户要补什么，而不是甩一个 traceback
        setter = getattr(self._settings, "set", None)
        if setter is None:
            QMessageBox.warning(
                self, "无法保存",
                "设置对象不支持写入（缺 set 方法）——请把 core/appSettings.py "
                "发给开发者补一行。")
            return
        setter(_KEY, key)
        self._input.clear()
        self.refresh()
        if self._log is not None:
            self._log.ok(f"Steam API 密钥已保存（尾号 {key[-4:]}）")

    def _on_clear(self) -> None:
        setter = getattr(self._settings, "set", None)
        if setter is None:
            QMessageBox.warning(
                self, "无法清除",
                "设置对象不支持写入——请把 core/appSettings.py 发给开发者。")
            return
        setter(_KEY, "")
        self.refresh()
        if self._log is not None:
            self._log.info("已清除 Steam API 密钥")

    def _on_open_register(self) -> None:
        QDesktopServices.openUrl(QUrl(_REGISTER_URL))

    def _on_verify(self) -> None:
        key = (self._settings.get(_KEY) or "").strip()
        if not key:
            QMessageBox.information(
                self, "验证密钥", "还没有保存 key：先粘贴并【保存】，再验证。")
            return
        target = self._probe_input.text().strip() or _DEFAULT_PROBE_ID
        if not target.isdigit():
            QMessageBox.information(
                self, "验证编号", "验证编号应是纯数字的工坊编号。")
            return
        self._verify_btn.setEnabled(False)
        self._result.setText("正在查询……")
        self._worker = _ProbeWorker(key, target, self)
        self._worker.done.connect(self._on_probe_done)
        self._worker.start()

    def _on_probe_done(self, payload: dict) -> None:
        self._verify_btn.setEnabled(True)
        self._result.setText(payload.get("msg") or "")
        if self._log is not None:
            if payload.get("ok"):
                n = len(payload.get("children") or [])
                self._log.ok(f"API key 验证完成：接口返回 children {n} 项"
                             "（详情见「Steam API 密钥」页）")
            else:
                self._log.warn(f"API key 验证未通过：{payload.get('msg')}")

    # ---------------- 对外 ----------------
    def refresh(self) -> None:
        """进页刷新（MainWindow _on_nav_changed 调用）：重读设置。"""
        key = ""
        if self._settings is not None:
            key = (self._settings.get(_KEY) or "").strip()
        if key:
            self._status.setText(
                f"当前状态：已设置（尾号 {key[-4:]}）——依赖检测可用")
        else:
            self._status.setText(
                "当前状态：未设置——依赖检测暂不可用，按下面四步注册即可")

    def shutdown(self) -> None:
        """主窗口关窗前的收尾（MainWindow closeEvent 统一回调）：
        等验证线程退出，防止退出时销毁活线程闪退。线程内网络超时上限
        30 秒，最多等这么久——程序正在退场，值得。"""
        if self._worker is not None:
            self._worker.wait()
