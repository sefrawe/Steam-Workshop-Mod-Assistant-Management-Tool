"""Steam API 密钥
"""
r"""gui/apiKeyPage.py · Steam Web API key：注册指引 + 现场验证器
（V2 消费版）。

本页解决一件事：把依赖检测需要的 Steam Web API key 引导到位，
并提供一次性的现场验证。

key 的纪律（用完即弃）：
- 本软件不保存 key——不写设置文件、不进日志、不进会话缓存；
  验证与【拉取依赖】都是现场粘贴，用完即弃；
- 输入框在每次验证结束后自动清空：key 不在界面上过夜（防截图、
  防旁人窥屏）；要再用，重新粘贴即可；
- key 绑定 Steam 账号，泄露了随时到注册页重置（旧 key 立即失效）。

为什么需要 key：匿名工坊接口（GetPublishedFileDetails）不返回 mod
的「必需物品」（依赖）清单——实测字段全集里没有 children；只有带
key 的 IPublishedFileService/GetDetails（includechildren=true）才
返回。异常处理的「桶B 依赖检测」（缺依赖 / 依赖变化）以此为主要
数据源。

「验证此 key」= 现场探针：拿一个工坊页明确写了必需物品的 mod 现查
一次，回报 children 有没有、有几项——key 有效性与数据管道一次验清。

【V2 相对 V1 的改动】
- 探针不再自抄 HTTP 调用：改走 core/steamApiClient.query_details_
  keyed（重试退避、truststore 证书注入、403 不重试的既定行为全在
  引擎单源里——V1 在线程里手写 requests.get 的那一段整体退役）；
  403 的人话转译留在本页（引擎只报错码，说人话是界面的事）；
- 备案：本页探针不进 netGate 互斥闸——手动触发、单请求、30 秒量级，
  进闸反而被五闸排队拖累；与五闸的关系仅限"同为联网动作，各自独立"；
- V1 回报里的 banned 字段随引擎 KeyedItem 退役（KeyedItem 不含它），
  验证结论不受影响——children 有没有、有几项才是管道通畅的判据。

本页零 SQL、零 repo：不碰设置对象（不保存任何东西）、不碰数据库。
"""
import re

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

from gui.logBus import LogBus   # V2：LogBus 独立成文件

# 注册页：免费，个人默认额度 10 万次/天，本工具每轮拉取个位数请求
_REGISTER_URL = "https://steamcommunity.com/dev/apikey"

# 验证样本：Better Architect Menu（3563882422）——工坊页明确写了
# 3 个必需物品（Harmony / Architect Icons / Architect Menu Optimizer），
# 验证通过 = key 有效 + 依赖数据管道畅通（children 应报 3 项）
_DEFAULT_PROBE_ID = "3563882422"

# Steam Web API key 的标准形状：32 位十六进制
_HEX32 = re.compile(r"[0-9a-fA-F]{32}")


class _ProbeWorker(QThread):
    """后台验证线程：带 key 现查一次 keyed GetDetails，回报 children
    形状。联网必须走工作线程（R11）。key 以构造参数传入线程内部，
    结束后随对象销毁——不落任何缓存。
    证书注入（truststore）由 steamApiClient 模块 import 时统一做过，
    线程里不用再注入一遍。"""
    done = Signal(object)  # {"ok": bool, "children": list[str], "msg": str}

    # 键恰好都是字符串所以此前侥幸能用；object 写法拔掉这颗
    # "将来谁加个 int 键就炸"的隐形雷

    def __init__(self, key: str, mod_id: str, parent=None) -> None:
        super().__init__(parent)
        self._key = key
        self._mod_id = mod_id

    def run(self) -> None:
        # 网络栈的装载留到真正点验证那一刻（局部 import），页面构造
        # 保持轻量——与 V1 在 run() 里 import requests 同一思路
        from core.steamApiClient import SteamApiClient, SteamApiError
        try:
            client = SteamApiClient()   # 默认参数：200ms 间隔、3 次重试
            items = client.query_details_keyed([int(self._mod_id)], self._key)
        except SteamApiError as exc:
            text = str(exc)
            if "403" in text:
                # 无效/被重置的 key，Steam 的表现就是 403 Forbidden——
                # 引擎对 403 不重试（非 429/503 的既定行为），
                # 转译成人话在这里
                self.done.emit({"ok": False, "children": [],
                                "msg": "服务器拒绝（403）：key 无效或已被"
                                       "重置。请回 steamcommunity.com/dev"
                                       "/apikey 核对或重新注册一个"})
                return
            self.done.emit({"ok": False, "children": [],
                            "msg": f"请求失败：{text}"})
            return
        except Exception as exc:  # noqa: BLE001 —— 线程边界兜底
            self.done.emit({"ok": False, "children": [],
                            "msg": f"请求失败：{exc}"})
            return
        if not items:
            self.done.emit({"ok": False, "children": [],
                            "msg": "响应里没有条目数据（验证编号对吗？）"})
            return
        item = items[0]
        if item.result is not None and item.result != 1:
            # 引擎逐条判定口径：result 非 1 = 该条目有问题（被删/私有）
            self.done.emit({"ok": False, "children": [],
                            "msg": f"Steam 回报该条目查询失败"
                                   f"（result={item.result}）——"
                                   "验证编号对吗？"})
            return
        # children 三态是引擎契约（KeyedItem 文档）：None = 响应条目里
        # 没有 children 键（includechildren 未生效/接口行为变了），
        # list = 清单（可为空 = 真无依赖）——两种状态绝不混同，
        # 绝不静默当 0 项
        if item.children is None:
            self.done.emit({"ok": False, "children": [],
                            "msg": "响应条目里没有 children 键："
                                   "includechildren 参数未生效或接口行为"
                                   "变了——把此结果告诉开发者"})
            return
        ids = [str(i) for i in item.children]
        title = item.title or "?"
        msg = (f"接口通了：《{title}》，children（必需物品）共 "
               f"{len(ids)} 项"
               + (f"，前几项：{'、'.join(ids[:5])}" if ids
                  else "。——这个编号工坊页写了必需物品却没有 children？"
                       "请把此结果告诉开发者"))
        self.done.emit({"ok": True, "children": ids, "msg": msg})


class ApiKeyPage(QWidget):
    """注册指引 + 现场验证，一页闭环。不保存任何东西。"""

    def __init__(self, settings, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        # settings 参数按主窗口现有接线原样保留；本页不读写设置
        # ——key 用完即弃，没有任何需要持久化的状态
        self._settings = settings
        self._log = log
        self._worker: _ProbeWorker | None = None   # 验证线程（shutdown 要等）
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
            "找到 app 的下载链接而不用通过 Google Play，手机登录和开启 "
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
        """进页刷新（MainWindow 的进页钩子 hasattr 自动发现并调用）：
        清空输入框与上次结果——key 不在界面上过夜；提醒行是固定文案。"""
        self._input.clear()
        self._result.setText("")

    def shutdown(self) -> None:
        """主窗口关窗前的收尾（MainWindow closeEvent 循环自动发现）：
        等验证线程退出，防止退出时销毁活线程闪退。线程内网络超时上限
        30 秒（连接 10 + 读取 30），最多等这么久——程序正在退场，值得。"""
        if self._worker is not None:
            self._worker.wait()
