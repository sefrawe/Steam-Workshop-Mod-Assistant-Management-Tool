r""" 日志总线
"""
"""
：UI 信号 + 文件旁路（loguru 按天轮转）。

LogBus：极小的日志总线。工作线程只调它的 info/ok/warn/error 发
信号，绝不直接碰任何控件——Qt 跨线程信号自动排队到主线程执行，
线程安全白送。这与"网络在线程、写库在主线程"是同一条分工原则。

没有监听者时照样能发（信号发进空气）——所以页面可以无条件
打日志，不用判断"有没有人接"。

来历（M3）：原本定义在 consolePanel.py 里。终端面板要用它，
而控制台面板又要构造终端面板——两家互相 import 成了环，
Python 加载到一半就报 ImportError。把公共小件搬进自己的文件，
环就解开了；consolePanel 里保留一条再导出的 import，
其他文件的老写法不受影响。

文件旁路（决策 67）：每次 emit 同步写一份进
data/logs/app_YYYY-MM-DD.log（00:00 轮转、约留 14 天、UTF-8）。
三层各司其职，不互相替代：
- 控制台 UI：给人看的结论，关程序即清空；
- operations_log 表：给程序读的结构化操作史；
- app_*.log：全量旁路文件——打包后没有终端，排障全靠它。
loguru 未安装 = 静默跳过（pyqtdarktheme 同款：可选依赖缺省降级，
不算错误）；写失败也静默（catch=True）——日志是旁路，绝不反过来
影响主流程。loguru 的 sink 自带内部锁，工作线程直接调安全。
反向桥（2026-10 增补）：core 层直写 loguru 的日志（如
steamApiClient 的重试警告）经桥接 sink 同步上软件内控制台——
注册入口 set_log_bridge（主窗口调用一次）；filter 只放行本项目
模块且排除 gui.logBus 自身写入，防重复显示与递归环。

"""
from PySide6.QtCore import QObject, Signal

# 文件旁路的全局件：成功后指向 loguru 单例；失败保持 None，
# LogBus 照常工作。多实例安全——loguru 是模块级单例，页面里
# 兜底新建的 LogBus 实例写的也是同一份文件，不会丢行
_FILE_LOGGER = None

# 桥接目标（2026-10 增补）：loguru → 软件内控制台。None = 未注册
# （主窗口构造 LogBus 后调 set_log_bridge 注册一次），行为与旧版一致
_BRIDGE = None

# loguru 级别名 → UI 级别名（consolePanel 着色契约的四个值）
_UI_LEVELS = {"INFO": "info", "SUCCESS": "ok", "WARNING": "warn",
              "ERROR": "error", "CRITICAL": "error"}

def set_log_bridge(bus) -> None:
    """注册桥接目标（主窗口构造自己的 LogBus 后调用一次）。此后
    core 层直接写 loguru 的日志会同步出现在软件内控制台；不注册
    = 完全旧行为，调用方零负担。"""
    global _BRIDGE
    _BRIDGE = bus

def _bridge_sink(message) -> None:
    """loguru 的 sink：把每条记录转发到注册的 LogBus 信号。
    线程安全：loguru sink 在调用线程同步执行，Qt 信号跨线程自动
    排队（与 LogBus 自身同一套机制）——工作线程里的 logger.warning
    也能安全上屏。挂载处的 filter 已排除 gui.logBus 的写入：
    页面经 LogBus 发的消息不会经桥回来显示第二遍（防重复），
    _emit → loguru → 桥 → emit 的环也被斩断（防递归）。"""
    bus = _BRIDGE
    if bus is None:
        return
    try:
        level = _UI_LEVELS.get(message.record["level"].name, "info")
        bus.line_emitted.emit(level, str(message))
    except Exception:
        pass  # 桥是旁路：任何失败都不反伤主流程（与 catch=True 同哲学）

def _setup_file_log() -> None:
    """挂 loguru 文件 sink（模块 import 末尾调用一次，幂等）。

    依赖导入、目录创建、sink 挂载任何一环失败都整段放弃——
    文件日志缺席时 UI 日志与信号链路不受任何影响（try 全包，
    绝不往外冒）。
    """
    global _FILE_LOGGER
    if _FILE_LOGGER is not None:
        return
    try:
        from loguru import logger as _lg
        from core import appPaths
        log_dir = appPaths.data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        _lg.add(
            log_dir / "app_{time:YYYY-MM-DD}.log",  # 按天命名，跨天自动换文件
            rotation="00:00",      # 每天零点切新文件
            retention="14 days",   # 超龄自动清，防长期膨胀（约保留两周）
            encoding="utf-8",      # Windows 默认编码会写坏中文与 traceback
            level="INFO",
            catch=True,            # 写失败（磁盘满等）静默吞，不打断主流程
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {message}",
            )
        # 桥接 sink（2026-10 增补）：core 层直写 loguru 的日志同步上
        # 软件内控制台。level 与文件 sink 对齐（INFO 起）；filter 只放
        # 行本项目模块且排除 gui.logBus——record["name"] 是调用
        # logger 的模块名，防重复、防递归（见 _bridge_sink 注释）
        _lg.add(
            _bridge_sink,
            level="INFO",
            filter=lambda r: (r["name"] or "").startswith(("core.", "gui."))
                             and r["name"] != "gui.logBus",
            format="{message}",
        )

        _FILE_LOGGER = _lg
    except Exception:
        _FILE_LOGGER = None


class LogBus(QObject):
    """日志总线：line_emitted(级别, 文本)。任何线程都可安全调用。"""

    line_emitted = Signal(str, str)

    def info(self, text: str) -> None:
        self._emit("info", "INFO", text)

    def ok(self, text: str) -> None:
        self._emit("ok", "SUCCESS", text)

    def warn(self, text: str) -> None:
        self._emit("warn", "WARNING", text)

    def error(self, text: str) -> None:
        self._emit("error", "ERROR", text)

    def _emit(self, ui_level: str, file_level: str, text: str) -> None:
        """单一出口：信号照旧（级别名是既有契约，consolePanel 靠它
        着色，不许动）；文件旁路用 loguru 的级别名，未挂成功就跳过。"""
        self.line_emitted.emit(ui_level, text)
        if _FILE_LOGGER is not None:
            try:
                _FILE_LOGGER.log(file_level, text)
            except Exception:  # 双保险：sink 已 catch，这里兜 log 调用本身
                pass


_setup_file_log()
