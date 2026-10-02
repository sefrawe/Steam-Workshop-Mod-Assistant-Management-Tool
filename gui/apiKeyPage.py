r"""gui/apiKeyPage.py · Steam Web API key：注册指引 + 现场验证器。

本页解决一件事：把依赖检测需要的 Steam Web API key 引导到位，
并提供一次性的现场验证。

key 的纪律（用完即弃）：
- 本软件不保存 key——不写设置文件、不进日志、不进会话缓存；
  验证与【拉取依赖】都是现场粘贴，用完即弃；
- 输入框在每次验证结束后自动清空：key 不在界面上过夜
  （防截图、防旁人窥屏）；要再用，重新粘贴即可；
- key 绑定 Steam 账号，泄露了随时到注册页重置（旧 key 立即失效）。

为什么需要 key：匿名工坊接口（GetPublishedFileDetails）不返回 mod
的「必需物品」（依赖）清单——实测字段全集里没有 children；只有带
key 的 IPublishedFileService/GetDetails（includechildren=true）才返回。
异常处理的「桶B 依赖检测」（缺依赖 / 依赖变化）以此为主要数据源。

「验证此 key」= 现场探针：拿一个工坊页明确写了必需物品的 mod 现查
一次，回报 children 有没有、有几项——key 有效性与数据管道一次验清。

本页零 SQL、零 repo：不碰设置对象（不保存任何东西）、不碰数据库。
"""
import re

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

# 注册页：免费，个人默认额度 10 万次/天，本工具每轮拉取个位数请求
_REGISTER_URL = "https://steamcommunity.com/dev/apikey"

# 验证样本：Better Architect Menu（3563882422）——工坊页明确写了
# 3 个必需物品（Harmony / Architect Icons / Architect Menu Optimizer），
# 验证通过 = key 有效 + 依赖数据管道畅通（children 应报 3 项）
_DEFAULT_PROBE_ID = "3563882422"

# Steam Web API key 的标准形状：32 位十六进制
_HEX32 = re.compile(r"[0-9a-fA-F]{32}")


class _ProbeWorker(QThread):
    """后台验证线程：带 key 现查一次 GetDetails，回报 children 形状。

    联网必须走工作线程（R11），证书走 truststore（与主程序一致）。
    key 以构造参数传入本线程内部，结束后随对象销毁——不落任何缓存。
    """

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
                                "msg": "服务器拒绝（403）：key 无效或已被"
                                       "重置。请回 "
                                       "steamcommunity.com/dev/apikey "
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
                   + (f"，前几项：{'、'.join(ids[:5])}" if ids else
                      "。——这个编号工坊页写了必需物品却没有 children？"
                      "请把此结果告诉开发者"))
            self.done.emit({"ok": True, "children": ids, "msg": msg})
        except Exception as exc:  # noqa: BLE001 —— 线程边界兜底
            self.done.emit({"ok": False, "children": [],
                            "msg": f"请求失败：{exc}"})


class ApiKeyPage(QWidget):
    """注册指引 + 现场验证，一页闭环。不保存任何东西。"""

    def __init__(self, settings, stack, log=None) -> None:
        super().__init__(stack)
        # settings 参数按主窗口现有接线原样保留；本页不再读写设置
        # ——key 用完即弃，没有任何需要持久化的状态
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
            "只有带 key 的官方接口才返回。", self)
        intro.setWordWrap(True)
        root.addWidget(intro)

        # 提醒行（固定文案）：纪律一次说清
        self._status = QLabel(
            "本软件不保存 key：验证与【拉取依赖】都是现场粘贴、用完即弃；"
            "绝不写入设置文件与日志。验证结束后输入框会自动清空。"
            "泄露了随时到注册页点【重置】，旧 key 立即失效。", self)
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: gray;")
        root.addWidget(self._status)

        # key 输入行：粘完直接点验证（没有保存按钮——不保存）
        row = QHBoxLayout()
        row.addWidget(QLabel("key：", self))
        self._input = QLineEdit(self)
        self._input.setPlaceholderText(
            "粘贴 32 位 key（仅本次使用，验证后自动清空）")
        row.addWidget(self._input, 1)
        self._verify_btn = QPushButton("验证此 key（联网查询一次）", self)
        self._verify_btn.setToolTip(
            "现场探针：用下面的验证编号现查一次官方接口，回报该 mod "
            "的必需物品清单——通过 = key 有效、依赖数据管道畅通。\n"
            "验证结束后输入框自动清空（本软件不保存 key）")
        self._verify_btn.clicked.connect(self._on_verify)
        row.addWidget(self._verify_btn)
        root.addLayout(row)

        btn_open = QPushButton(
            "打开 Steam 注册页（steamcommunity.com/dev/apikey）", self)
        btn_open.clicked.connect(self._on_open_register)
        root.addWidget(btn_open)

        guide = QLabel(
            "<h3>四步拿到 key（免费，约两分钟）</h3>"
            "1. 浏览器登录 steamcommunity.com（你逛创意工坊的账号）<br>"
            '2. 打开 <a href="https://steamcommunity.com/dev/apikey">'
            "steamcommunity.com/dev/apikey</a><br>"
            "3. 域名一栏随便填（例如 localhost），勾选同意条款，点【注册】"
            "<br>4. 复制页面上 32 位的 key，粘贴到上面输入框点【验证此 "
            "key】试一把；到异常处理页【拉取依赖（联网）】时也会现场问"
            "你粘贴——本软件不保存 key，用完即弃。"
            "<br><br>忘了 key 是什么？回注册页点【重置】拿一个新的即可，"
            "旧 key 同时失效。"
            "<br><br><b>看到「拒绝访问」？</b>新注册或消费未满 $5 的受限"
            "账户暂时拿不到 key（在别的平台买的 CDKey 激活不计入消费"
            "额度）。在 Steam 商店直接消费满 $5（约 ¥36，买游戏/充钱包"
            "都算）后回来重试即可。\n除了要求消费，还要求账号在 steam "
            "app 中开启 QR 码验证身份方式防止盗号。steam app 下载地址："
            "https://store.steampowered.com/mobile?l=schinese，可以直接"
            "找到 app 的下载链接而不用通过 geogle play，手机登录和开启 "
            "QR 码相关操作不用加速，电脑端问你要域名用“localhost”就行，"
            "之后接收授权密钥的信息需要（电脑显示“正在等待来自您 steam "
            "令牌手机验证器的确认”）加速，手机推荐“biubiu加速器”，加速"
            "后点第四个铃铛图标“notifications”，刷新等显示“1 pending "
            "confirmation”就点它，点进去授权就能在电脑上看到密钥。\n"
            "注册不了不影响本软件其他功能：依赖检测保持未开启，"
            "其余一切照常。", self)
        guide.setTextFormat(Qt.TextFormat.RichText)
        guide.setOpenExternalLinks(True)
        guide.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction)
        guide.setWordWrap(True)
        root.addWidget(guide)

        # ---- 现场验证区 ----
        verify_hint = QLabel(
            "<b>现场验证（可选，但强烈建议）</b>：探针用下面这个 mod 现查"
            "一次接口——它的工坊页明确写了 3 个必需物品，children 应报 "
            "3 项。通过 = key 有效、依赖数据管道畅通，之后到异常处理页"
            "【拉取依赖（联网）】再粘一次就能用。", self)
        verify_hint.setTextFormat(Qt.TextFormat.RichText)
        verify_hint.setWordWrap(True)
        root.addWidget(verify_hint)

        vrow = QHBoxLayout()
        vrow.addWidget(QLabel("验证编号（默认即可）：", self))
        self._probe_input = QLineEdit(_DEFAULT_PROBE_ID, self)
        self._probe_input.setToolTip(
            "探针查询的工坊编号：默认这个 mod 的必需物品数已知"
            "（3 项），适合当对照样本；一般不用改")
        vrow.addWidget(self._probe_input, 1)
        root.addLayout(vrow)

        self._result = QLabel(self)
        self._result.setWordWrap(True)
        self._result.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self._result)

        root.addStretch(1)

    # ---------------- 槽 ----------------
    def _on_verify(self) -> None:
        key = self._input.text().strip()
        if not key:
            QMessageBox.information(
                self, "验证密钥",
                "先粘贴 key（本软件不保存：用完即弃，要再用重新粘贴）。")
            return
        if not _HEX32.fullmatch(key):
            ret = QMessageBox.question(
                self, "格式不像 key",
                "Steam Web API key 通常是 32 位十六进制字符，当前输入不是"
                "这个形状——可能是没复制全。\n\n仍要验证吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                return
        target = self._probe_input.text().strip() or _DEFAULT_PROBE_ID
        if not target.isdigit():
            QMessageBox.information(
                self, "验证编号", "验证编号应是纯数字的工坊编号。")
            return
        self._verify_btn.setEnabled(False)
        self._result.setText("正在查询……（验证结束后输入框会自动清空）")
        self._worker = _ProbeWorker(key, target, self)
        self._worker.done.connect(self._on_probe_done)
        self._worker.start()

    def _on_probe_done(self, payload: dict) -> None:
        self._verify_btn.setEnabled(True)
        # 用完即弃的落实点：无论成败，验证一结束就清空输入框——
        # key 不在界面上残留；失败要重试，重新粘贴即可
        self._input.clear()
        msg = payload.get("msg") or ""
        self._result.setText(msg + "\n（输入框已清空——本软件不保存 key）")
        if self._log is not None:
            if payload.get("ok"):
                n = len(payload.get("children") or [])
                self._log.ok(f"API key 验证完成：接口返回 children {n} 项"
                             "（详情见「Steam API 密钥」页）")
            else:
                self._log.warn(f"API key 验证未通过：{payload.get('msg')}")

    def _on_open_register(self) -> None:
        QDesktopServices.openUrl(QUrl(_REGISTER_URL))

    # ---------------- 对外 ----------------
    def refresh(self) -> None:
        """进页刷新（MainWindow _on_nav_changed 调用）：清空输入框与
        上次结果——key 不在界面上过夜；提醒行是固定文案。"""
        self._input.clear()
        self._result.setText("")

    def shutdown(self) -> None:
        """主窗口关窗前的收尾（MainWindow closeEvent 统一回调）：
        等验证线程退出，防止退出时销毁活线程闪退。线程内网络超时上限
        30 秒，最多等这么久——程序正在退场，值得。"""
        if self._worker is not None:
            self._worker.wait()
