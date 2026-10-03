"""切换器冒烟入口
"""
r"""tools/smoke_switcher.py —— 游戏档案切换器 · 临时冒烟入口（用完即弃）

现阶段 gui/ 只搬了 gameSwitcher + gameAddDialog 两件，MainWindow 还没
搬迁，V1 原版 main.py 一启动就 import 全家不存在的页面。本文件就是
绕开这一切的最小窗子：一个一次性临时账本 + 一个切换器 + 一行信号
回显。GUI 件没有 pytest（项目惯例），启动能跑、信号能到、对话框能
开，才算冒烟通过。

它只做三件事：
1. 在系统临时目录开一个一次性账本——绝不碰项目 data/ 下的真账；
   每次启动先删旧文件，保证从空库开始（空库态、建档、切档才验得
   干净）；
2. 把 GameSwitcher 装进普通窗口，current_game_changed 信号接到一行
   QLabel + 终端打印——信号到没到、带的对象对不对，一眼可见；
3. 其余全靠手点：【＋添加游戏档案】开对话框建档、切下拉看回显。
   冒烟七条清单见对话记录，窗内提示条只列动作。

为什么 main.py 现在不能跑：V1 的 main.py import MainWindow，
MainWindow import 二十多个还没搬迁的页面。等 MainWindow 轮落地、
main.py 接上正式链路，本文件删除（临时件不留到 M3 之后）。

运行：在项目根目录执行   python tools/smoke_switcher.py
"""
import sys
import tempfile
from pathlib import Path

# 直接以脚本方式运行时（python tools/xxx.py），Python 只把 tools/ 加进
# 导入路径，项目根不在其中——手动补上，from core... / from gui... 才
# 找得到（比"必须用 -m 方式运行"少一条规矩，冒烟件越省心越好）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from core.sqliteRepository import SQLiteRepository
from gui.gameSwitcher import GameSwitcher


def main() -> int:
    app = QApplication(sys.argv)

    # ---- 一次性冒烟账本：系统临时目录，与真账隔离 ----
    db_path = Path(tempfile.gettempdir()) / "swmamt_smoke_switcher.db"
    if db_path.exists():
        db_path.unlink()  # 每次从空库开始
    # 类名/构造参数对照 core/sqliteRepository.py 现稿：若 V2 构造器
    # 另有必填参数（如 snapshot_keep），全文件只改这一行
    repo = SQLiteRepository(str(db_path))

    # ---- 最小窗子：切换器 + 一行信号回显 ----
    win = QWidget()
    win.setWindowTitle("冒烟：游戏档案切换器（临时账本，随手删）")
    lay = QVBoxLayout(win)

    # settings / log 都不接：冒烟不依赖设置页与日志面板。建档时
    # 下载目录一栏显示"暂无法推导"是预期行为，照建不误
    switcher = GameSwitcher(repo, win)
    lay.addWidget(switcher)

    info = QLabel("（信号还没来过）")
    lay.addWidget(info)

    def on_changed(game) -> None:
        """信号回显：界面一行 + 终端一行。game=None = 空库或全删光。"""
        if game is None:
            info.setText("当前档案：（无）——空库或全删光")
        else:
            info.setText(f"当前档案：{game.name}（{game.app_id}）｜"
                         f"下载目录：{game.download_dir or '（空，未推导）'}")
        print(f"[smoke] current_game_changed -> {game!r}")

    switcher.current_game_changed.connect(on_changed)

    # 构造期 switcher 已发过一轮信号（当时无人监听）——与 MainWindow
    # 同一条规矩：接完线手动补一次初始化回显
    on_changed(switcher.current_game())

    hint = QLabel("冒烟动作：① 空库态观察 → ②【＋添加游戏档案】建一个"
                  "（可点「自动查名」走真网络）→ ③ 切下拉看两处回显 → "
                  "④ 建档中途点取消 / 点 X，不许闪退")
    hint.setWordWrap(True)
    lay.addWidget(hint)

    win.resize(680, 260)
    win.show()
    ret = app.exec()

    # 冒烟件宽容收尾：契约若变（close 不在了），打印即可——不让退出
    # 环节崩出一屏 traceback 吓人；正常运行时它安静无声
    try:
        repo.close()
    except Exception as exc:
        print(f"[smoke] repo.close() 异常（不影响冒烟结论）：{exc}")
    print(f"[smoke] 临时账本在：{db_path}（可手动删；不删也行，"
          "下次启动自动重建）")
    return ret


if __name__ == "__main__":
    sys.exit(main())
