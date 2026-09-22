"""日志总线
"""
"""
LogBus：极小的日志总线。工作线程只调它的 info/ok/warn/error
发信号，绝不直接碰任何控件——Qt 跨线程信号自动排队到主线程执行，
线程安全白送。这与"网络在线程、写库在主线程"是同一条分工原则。

没有监听者时照样能发（信号发进空气）——所以页面可以无条件
打日志，不用判断"有没有人接"。

来历（M3）：原本定义在 consolePanel.py 里。终端面板要用它，
而控制台面板又要构造终端面板——两家互相 import 成了环，
Python 加载到一半就报 ImportError。把公共小件搬进自己的文件，
环就解开了；consolePanel 里保留一条再导出的 import，
其他文件的老写法不受影响。
"""
from PySide6.QtCore import QObject, Signal


class LogBus(QObject):
    """日志总线：line_emitted(级别, 文本)。任何线程都可安全调用。"""

    line_emitted = Signal(str, str)

    def info(self, text: str) -> None:
        self.line_emitted.emit("info", text)

    def ok(self, text: str) -> None:
        self.line_emitted.emit("ok", text)

    def warn(self, text: str) -> None:
        self.line_emitted.emit("warn", text)

    def error(self, text: str) -> None:
        self.line_emitted.emit("error", text)
