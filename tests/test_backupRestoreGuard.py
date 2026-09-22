"""恢复的 steamcmd 运行守卫测试
"""
"""
恢复是覆盖性操作：steamcmd 在跑 → 引擎必须在任何仓库访问、
任何文件操作之前拒绝。守卫放在 restore_backup 最顶上，
所以本测试连假仓库都不需要——BackupManager(None) 即可。
运行：python -m pytest tests/test_backupRestoreGuard.py -v
"""
from core import backupManager as bm


def test_restore_rejected_while_steamcmd_running(monkeypatch):
    monkeypatch.setattr(bm, "steamcmd_running", lambda: True)
    mgr = bm.BackupManager(None)  # 守卫先于一切仓库访问，repo 用不上
    rep = mgr.restore_backup(1)
    assert rep.ok is False
    assert "steamcmd" in (rep.error or "")
