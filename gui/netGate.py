"""跨页面联网查询互斥闸 + 退场看护（gui/netGate.py）。

为什么有这个文件（v2.45 实测两起事故的收口）：
1. 更新检测与联网深度检测是两条互不知情的联网入口，可以同时跑——
   网络不畅时双双卡进超时重试。互斥闸让同一时刻只有一个入口在查，
   后到的当场被拒并告知谁在占（try_acquire/release）。
2. 退出冻结：两个页面的 shutdown 原来无限期 wait() 网络线程，而
   线程可能卡在一次 HTTP 超时/重试里数分钟——主线程同步等它 =
   Windows 判"未响应"。改为 shutdown 等 1.5 秒封顶，等不到就
   park()：线程引用挪进模块级列表保到进程死亡（绝不被销毁，防
   "销毁运行中线程"闪退 0xC0000409），并确保退场守卫已装——
   程序真正退场（aboutToQuit，此时 closeEvent 已全部收尾：账本、
   设置、steamcmd 都关了）时若还有存活线程，os._exit(0) 随进程
   一起带走。线程只发网络请求不写库（账本全是即时提交），
   强杀零数据损失。
"""
import os
import threading

_lock = threading.Lock()
_owner: str | None = None   # 当前占用者（"更新检测"/"联网深度检测"/…）
_parked: list = []          # shutdown 等不到的网络线程（引用保活）
_armed = False              # aboutToQuit 守卫只装一次


def try_acquire(owner: str) -> str | None:
    """申请联网查询名额：拿到返回 None；被占返回占用方名字。"""
    global _owner
    with _lock:
        if _owner is None:
            _owner = owner
            return None
        return _owner


def release(owner: str) -> None:
    """归还名额（在后台线程的 finished 收尾里调——无论成功/失败/
    停止 finished 都必发，放这里一次就够，漏不掉）。"""
    global _owner
    with _lock:
        if _owner == owner:
            _owner = None


def park(worker) -> None:
    """shutdown 等不到的线程挪进来保活，并确保退场守卫已装。
    模块级列表 = 进程死亡前绝无销毁，"销毁仍在运行的线程"从根上
    不可能发生。"""
    global _armed
    if worker is not None and worker.isRunning():
        _parked.append(worker)
        if not _armed:
            from PySide6.QtWidgets import QApplication

            def _on_quit() -> None:
                if any(w.isRunning() for w in _parked):
                    # 走到这里 = closeEvent 已全部收尾，只剩卡在
                    # 网络里的查询线程——随进程退场最干净
                    os._exit(0)

            QApplication.instance().aboutToQuit.connect(_on_quit)
            _armed = True
