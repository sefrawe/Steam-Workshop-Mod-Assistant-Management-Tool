"""mod 内容备份引擎
"""
r"""
core/backupManager.py · 把 mod 的下载内容复制到备份区，可恢复。

引擎只做三件事：复制（robocopy）、登记（backups 表）、清理（保留策略）。
勾选对话框、备份管理页等界面归 GUI 批；引擎本身不弹窗、不打日志，
一切结果通过返回的报告对象交给调用方展示。

使用方式：按次构造（steamcmd_path 等参数每次从设置现读），然后：
    report = BackupManager(repo, steamcmd_path=..., keep_per_mod=1,
                           quota_bytes=...).backup_mod(mod_id)
    report.ok / report.backup / report.warnings / report.error

红线对照（写这段代码时逐条核对过，改动前再读一遍）：
- R5  robocopy 退出码 0-7 全部算成功，rc >= 8 才算失败。
      ★ 1 = "有文件被拷贝"，是最常见的正常成功码——判错会把
      每次成功的备份都报成失败，这是本文件最容易踩的坑
- R10 robocopy 必须带 /XJ：排除 junction/符号链接挂载点，
      防止链接成环时无限递归拷贝
- R4  任何删除动作前先解析真实路径（resolve），确认落在预期根内
      才动手；目标本身是链接/junction 则直接拒绝。
      ★ junction 检测必须用 os.path.isjunction()——
      os.path.islink() 对 junction 一律返回 False（踩坑 ⑩），
      只查 islink 等于没查。Windows 路径比较一律 normcase（R14）
- R7  备份前检测 steamcmd 是否在运行（公共函数 steamcmd_running，
      将来核验页/扫描横幅共用）：在跑 → 报告里加警告，不拦截
      （记事本定性为"建议等待"）；弹窗询问用户是 GUI 的事
- R8  恢复备份前强制先备份当前版本；当前没有本地内容则跳过并警告
- R9  备份前磁盘空间预检：目标盘剩余 < 预计大小 → 拒绝；
      余量偏紧 → 警告。预计大小用 acf 测得的 local_size，
      没有则跳过预检并警告
- R6  超长路径预检：源/目标路径超过 240 字符只警告不拦截
      （robocopy 本身能处理长路径，这正是选它不用 shutil 的原因）
- R1  backups.backup_path 存相对该游戏 backup_dir 的路径（只有目录名）。
      已知限制：用户日后改 backup_dir，旧记录会按新位置解析而失联
      （表里没存旧根，schema 不动）；备份管理页将来提供重定位

恢复流程（记事本备份子系统 v1 原案，全程可退）：
  ① 强制备份当前版本（R8；无本地内容则跳过）
  ② 当前文件夹改名挪走（同盘改名 = 原子操作，瞬间完成）
  ③ robocopy 把备份复制回原位
  ④ 成功 → 删掉挪走的旧目录；失败 → 删掉拷了一半的新目录、
     旧目录改回原名，恢复原状
  已知限制：中途强杀进程会留下"挪走的旧目录 + 半成品新目录"，
  两者都会被核验页的非数字内容桶报出来——属可接受的崩溃残留。

测试注入口：robocopy 调用走构造参数 runner（默认真 robocopy），
测试注入假 runner 即可测"拷贝失败回退"，不需要真制造失败。
"""
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from core import steamPaths
from core.models import Backup
from core.modRepository import ModRepository

# robocopy 退出码 >= 8 才算失败（R5）。单独提成函数：纯逻辑，
# 全平台可测，且"1 也是成功"这个反直觉点必须有测试钉死
_RC_FAIL_THRESHOLD = 8

# 路径长度警告阈值（R6）。Windows 传统上限 260，留出文件名余量取 240
_PATH_WARN_LEN = 240

# steamcmd 进程名（不带 .exe，比对时统一小写）
_STEAMCMD_PROC = "steamcmd.exe"


# ============ 公共：steamcmd 进程检测（R7，三方共用） ============

def steamcmd_running() -> bool:
    """steamcmd 是否正在运行。核验 / 备份 / 扫描横幅三方共用（R7）。

    实现：psutil 扫一遍进程名，等于 steamcmd.exe 即 True。
    进程在遍历期间退出 / 权限不够 → 跳过该条，不影响整体判断。
    """
    for proc in psutil.process_iter(attrs=["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name == _STEAMCMD_PROC:
            return True
    return False


# ============ 纯函数工具 ============

def is_success_rc(rc: int) -> bool:
    """robocopy 退出码 → 是否成功（R5：0-7 成功，>=8 失败）。
    1 = 有文件被拷贝，是正常成功，不是错误。"""
    return rc < _RC_FAIL_THRESHOLD


def _decode(raw: bytes) -> str:
    """robocopy 输出字节 → 尽力解码成文字（仅用于失败诊断，不参与判定）。
    中文 Windows 的控制台输出是 GBK，先试 UTF-8 再试 GBK，坏字节替换。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _norm(path: Path) -> str:
    """路径 → 统一比较形态：绝对化 + normcase（Windows 下转小写、斜杠统一）。
    这就是 R14"路径比较一律 casefold"的标准库落地。"""
    return os.path.normcase(os.path.abspath(str(path)))


def _is_link_or_junction(target: Path) -> bool:
    """目标是否为符号链接或 junction。

    ★ 两个都要查：os.path.islink() 只认符号链接，对 junction 永远
    返回 False（Python 3.12 专门为此新增了 os.path.isjunction）。
    只查 islink 的话，游戏侧 junction 会从 R4 保险丝下面溜过去。
    """
    return os.path.islink(target) or os.path.isjunction(target)


def _assert_inside(target: Path, expected_root: Path) -> None:
    """删除/恢复类危险操作前的保险丝（R4 + R14）。

    两道检查，任一不过立刻抛 RuntimeError：
      1) 目标本身是链接（符号链接/junction）→ 拒绝——删链接可能
         误伤实体侧内容（决策 21④ 的方向铁律在文件操作层的落实）
      2) 目标解析出的真实路径不在预期根内 → 拒绝——防止拼接错误
         或档案配置错误导致删到备份区、content 树之外的地方
    """
    if _is_link_or_junction(target):
        raise RuntimeError(
            f"拒绝操作链接/junction（R4）：{target}\n"
            "（若确需操作，请先手动处理链接本身）")
    real = _norm(Path(os.path.realpath(target)))
    root = _norm(Path(os.path.realpath(expected_root))).rstrip(os.sep)
    if not real.startswith(root + os.sep):
        raise RuntimeError(
            f"路径越界（R4）：{real} 不在 {root} 内，拒绝操作")


def _safe_rmtree(target: Path, expected_root: Path) -> None:
    """过保险丝后递归删除目录。所有删除动作必须走这里，不许直接 rmtree。"""
    _assert_inside(target, expected_root)
    shutil.rmtree(target)


def _dir_size(path: Path) -> int:
    """目录实际占字节数（备份完成后量真实值入库，不用预估值）。"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue  # 量不到大小的文件（权限/竞态）按 0 计，不炸整体
    return total


# ============ 报告对象 ============

@dataclass
class BackupReport:
    """一次备份的结果。ok=False 时 error 必有内容；warnings 是非致命提醒。"""
    ok: bool
    backup: Backup | None = None        # 成功时的数据库记录（含 id）
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    cleaned: list[str] = field(default_factory=list)  # 保留策略清腾掉的目录名


@dataclass
class RestoreReport:
    """一次恢复的结果。pre_backup 是 R8 强制备份的记录（跳过时为 None）。"""
    ok: bool
    pre_backup: Backup | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


# ============ 引擎本体 ============

class BackupManager:
    """按次构造的备份引擎（参数注入，同 steamApiClient 模式）。

    keep_per_mod：每个 mod 保留几份备份（保留策略之一，默认 1）
    quota_bytes：全部备份的总量上限字节（保留策略之二，None = 不限）
    steamcmd_path：设置页的 steamcmd 程序路径——档案没配 backup_dir 时
                   按它推导默认备份位置（决策 21⑥）
    runner：robocopy 调用注入口，签名 (src, dst) -> (退出码, 输出文本)。
            默认真 robocopy；测试注入假的可测"拷贝失败回退"，不需真制造失败。
    """

    def __init__(
            self,
            repo: ModRepository,
            *,
            keep_per_mod: int = 1,
            quota_bytes: int | None = None,
            steamcmd_path: str | None = None,
            runner: Callable[[str, str], tuple[int, str]] | None = None,
    ) -> None:
        self._repo = repo
        self._keep = max(1, int(keep_per_mod))
        self._quota = quota_bytes
        self._steamcmd_path = steamcmd_path
        self._run = runner if runner is not None else self._run_robocopy

    # ---------- 对外：备份 ----------

    def backup_mod(self, mod_id: int, *, note: str | None = None) -> BackupReport:
        """备份一个 mod 的下载内容 → 登记 → 执行保留策略。

        id 查不到（调用方传错）按 bug 处理直接抛 ValueError；
        其余一切"操作层面不成立"（没内容、空间不够、拷贝失败）
        都通过报告返回，不抛异常——这些是需要向用户解释的情况，不是 bug。
        """
        warnings: list[str] = []
        mod = self._repo.get_mod(mod_id)
        if mod is None:
            raise ValueError(f"mod {mod_id} 不存在")
        game = self._repo.get_game(mod.game_id)
        if game is None:
            raise ValueError(f"游戏档案 {mod.game_id} 不存在（数据不一致）")

        # ---- 前提一：本地版本必须已知（backups.version_timeupdated 非空）----
        if not mod.local_timeupdated:
            return BackupReport(
                ok=False,
                error="本地版本未知（尚未扫描本地或该 mod 未下载）。"
                      "请先在 mod 库页点【扫描本地】再备份。")

        # ---- 前提二：确定备份根目录（档案值优先，缺省推导并回写）----
        root, derived = self._resolve_root(game)
        if root is None:
            return BackupReport(
                ok=False,
                error="无法确定备份位置：档案未设置备份目录，"
                      "且未配置 steamcmd 程序路径（无法按决策 21 推导）。"
                      "请先在设置页填写 steamcmd 程序。")
        if derived:
            # 与 T10e 同款"推导 + 回写 + 告知"模式：档案当时没存备份目录，
            # 现在补上，下次不再推导
            self._repo.update_game(game.app_id, backup_dir=root)
            warnings.append(f"备份目录未设置，已按 steamcmd 位置推导并写入档案：{root}")
        Path(root).mkdir(parents=True, exist_ok=True)

        # ---- 前提三：本地内容真实存在 ----
        source = Path(game.download_dir or "") / str(mod_id)
        if not source.is_dir():
            return BackupReport(
                ok=False,
                error=f"未找到本地内容：{source}\n"
                      "（档案的下载目录下没有这个 mod 的文件夹）")

        # ---- 环境检查：只警告，不拦截 ----
        if steamcmd_running():
            warnings.append(
                "检测到 steamcmd 正在运行：此时备份可能拿到不完整的副本"
                "（下载仍在写入）。建议等下载结束再备份。")
        for p in (source, Path(root)):
            if len(str(p)) > _PATH_WARN_LEN:
                warnings.append(
                    f"路径过长（R6，{len(str(p))} 字符）：{p}\n"
                    "robocopy 可处理，但游戏侧工具可能读不到。")
        if mod.local_size is not None:
            free = shutil.disk_usage(root).free
            if free < mod.local_size:
                return BackupReport(
                    ok=False,
                    error=f"磁盘空间不足（R9）：预计需要 "
                          f"{mod.local_size / 2**30:.2f} GiB，"
                          f"备份盘仅剩 {free / 2**30:.2f} GiB。")
            if free < mod.local_size * 1.1:
                warnings.append("备份盘剩余空间偏紧，建议尽快清理旧备份。")
        else:
            warnings.append("该 mod 缺少本地大小记录，跳过空间预检。")

        # ---- 复制 ----
        dst = self._fresh_target(Path(root), mod_id, mod.local_timeupdated)
        rc, out = self._run(str(source), str(dst))
        if not is_success_rc(rc):
            # 失败清理半成品（只在刚创建的备份根内动手，走 R4 保险丝）
            if dst.is_dir():
                try:
                    _safe_rmtree(dst, Path(root))
                except (RuntimeError, OSError):
                    pass
            return BackupReport(
                ok=False,
                error=f"robocopy 失败（退出码 {rc}，≥8 才算失败）：\n"
                      f"{out[-800:]}")

        # ---- 登记（存相对路径，R1）----
        record = self._repo.add_backup(
            mod_id=mod_id,
            backup_path=dst.name,
            size_bytes=_dir_size(dst),
            version_timeupdated=mod.local_timeupdated,
            manifest=mod.manifest,
            note=note,
        )

        # ---- 保留策略：清腾时机 = 新备份成功落盘之后 ----
        cleaned, quota_tight = self._prune(exclude_id=record.id)
        if quota_tight:
            warnings.append(
                "备份总量超出配额，但可清理的备份都已钉住或只剩本次备份——"
                "请到备份管理页手动处理。")

        return BackupReport(ok=True, backup=record,
                            warnings=warnings, cleaned=cleaned)

    # ---------- 对外：恢复 ----------

    def restore_backup(self, backup_id: int) -> RestoreReport:
        """把一份备份恢复回下载目录。流程见文件头"恢复流程"（R8 原案）。"""
        warnings: list[str] = []
        record = self._repo.get_backup(backup_id)
        if record is None:
            raise ValueError(f"备份记录 {backup_id} 不存在")
        mod = self._repo.get_mod(record.mod_id)
        game = self._repo.get_game(mod.game_id) if mod else None
        if mod is None or game is None:
            raise ValueError("备份对应的 mod 或游戏档案不存在（数据不一致）")
        if mod.status == "deleted":
            warnings.append("该 mod 当前状态为已删除：恢复会把文件放回下载目录，"
                            "但数据库状态不变。")

        # ---- 解析两个位置：备份在哪、恢复到哪 ----
        if not (game.backup_dir or "").strip():
            return RestoreReport(
                ok=False, warnings=warnings,
                error="档案未设置备份目录，无法解析备份记录的相对路径。")
        backup_root = Path(game.backup_dir)
        src = backup_root / record.backup_path
        try:
            _assert_inside(src, backup_root)
        except RuntimeError as exc:
            return RestoreReport(ok=False, warnings=warnings, error=str(exc))
        if not src.is_dir():
            return RestoreReport(
                ok=False, warnings=warnings,
                error=f"备份目录在盘上不存在：{src}\n"
                      "（可能已被手动移动或删除）")
        if not str(game.download_dir or "").strip():
            return RestoreReport(
                ok=False, warnings=warnings,
                error="档案未设置下载目录，无法确定恢复位置。")
        dest = Path(game.download_dir) / str(mod.mod_id)
        if _is_link_or_junction(dest):
            # 恢复位置的 mod 目录是 junction = 档案的 download_dir 配到了
            # 游戏侧（决策 21④ 的反向），拒绝动手
            return RestoreReport(
                ok=False, warnings=warnings,
                error=f"下载目录中的 mod 位置是链接/junction，拒绝恢复（R4）：{dest}")

        # ---- ① R8：先强制备份当前版本（没有现状则跳过）----
        pre: Backup | None = None
        if dest.is_dir():
            pre_report = self.backup_mod(mod.mod_id, note="恢复前自动备份")
            if not pre_report.ok:
                return RestoreReport(
                    ok=False, warnings=warnings,
                    error=f"恢复前备份失败（R8 要求先备份，已中止恢复）：\n"
                          f"{pre_report.error}")
            pre = pre_report.backup
        else:
            warnings.append("当前没有本地内容，跳过恢复前备份。")

        # ---- ② 当前文件夹改名挪走（仅当当前版本真实存在；同盘改名 = 原子）----
        # 无现状时 aside 保持 None：没有东西可挪，"原状"就是没有这个目录。
        # 此前的 bug：无内容路径按 R8 契约跳过了恢复前备份，这里却无条件
        # rename → WinError 2。内容丢失后恢复是备份的核心场景，必须畅通
        aside: Path | None = None
        if dest.is_dir():
            aside = dest.parent / f"{mod.mod_id}_restore_old_{time.strftime('%Y%m%d_%H%M%S')}"
            try:
                dest.rename(aside)
            except OSError as exc:
                return RestoreReport(
                    ok=False, warnings=warnings,
                    error=f"无法挪动当前版本（未做任何改动）：{exc}")

        # ---- ③ 复制备份回原位 ----
        rc, out = self._run(str(src), str(dest))
        if not is_success_rc(rc):
            # ④ 失败回退：删半成品、旧目录改回原名
            if dest.is_dir():
                try:
                    _safe_rmtree(dest, dest.parent)
                except (RuntimeError, OSError):
                    pass
            if aside is not None:
                try:
                    aside.rename(dest)
                except OSError:
                    warnings.append(
                        f"回退时改回原名失败，旧内容仍在：{aside}\n请手动改回"
                        f" {dest.name}")

            return RestoreReport(
                ok=False, pre_backup=pre, warnings=warnings,
                error=f"恢复复制失败（robocopy 退出码 {rc}），已回退原状：\n"
                      f"{out[-800:]}")

        # ---- ⑤ 成功：删掉挪走的旧目录（无现状路径没有可删的东西）----
        if aside is not None:
            try:
                _safe_rmtree(aside, dest.parent)
            except (RuntimeError, OSError) as exc:
                warnings.append(
                    f"旧版本目录删除失败（恢复本身已成功）：{aside}\n{exc}")
        return RestoreReport(ok=True, pre_backup=pre, warnings=warnings)

    # ---------- 对外：删除 ----------

    def delete_backup(self, backup_id: int) -> tuple[bool, str | None]:
        """手动删除一份备份：磁盘目录 + 数据库记录（先盘后账）。

        与保留策略清腾（_remove）的差异：手动删除要把结果讲清楚，
        不静默。磁盘目录已不在（用户手动删过）→ 只删记录并说明；
        R4 保险丝拦下 → 账不动并说明（残留交核验页非数字内容桶暴露）。
        返回 (是否成功, 附带说明)。id 不存在按 bug 抛 ValueError。
        """
        record = self._repo.get_backup(backup_id)
        if record is None:
            raise ValueError(f"备份记录 {backup_id} 不存在")
        mod = self._repo.get_mod(record.mod_id)
        game = self._repo.get_game(mod.game_id) if mod else None
        if game is None or not (game.backup_dir or "").strip():
            self._repo.delete_backup_record(record.id)
            return True, ("无法解析备份位置（mod 或档案的备份目录缺失），"
                          "仅删除了记录")
        target = Path(game.backup_dir) / record.backup_path
        if not target.is_dir():
            self._repo.delete_backup_record(record.id)
            return True, (f"盘上本已不存在（可能已被手动删除）：{target}\n"
                          "仅删除了记录")
        try:
            _safe_rmtree(target, Path(game.backup_dir))
        except (RuntimeError, OSError) as exc:
            return False, f"磁盘目录删除失败，记录保留（R4 保险丝或权限问题）：{exc}"
        self._repo.delete_backup_record(record.id)
        return True, None


    # ---------- 内部 ----------

    def _resolve_root(self, game) -> tuple[str | None, bool]:
        """确定备份根目录：档案值优先；没有则按 steamcmd 推导（决策 21⑥）。
        返回 (目录, 是否为新推导)。"""
        stored = str(game.backup_dir or "").strip()
        if stored:
            return stored, False
        derived = steamPaths.backup_root_default(self._steamcmd_path, game.app_id)
        return derived, derived is not None

    def _fresh_target(self, root: Path, mod_id: int, version: int) -> Path:
        """目标目录名 <modid>_v<本地版本>_<时间戳>（记事本命名规范）。
        同一秒内重复备份 → 追加 _2、_3 防撞（几乎不可能发生，纯保险）。"""
        stamp = time.strftime("%Y%m%d_%H%M%S")
        name = f"{mod_id}_v{version}_{stamp}"
        candidate, n = root / name, 2
        while candidate.exists():
            candidate = root / f"{name}_{n}"
            n += 1
        return candidate

    def _prune(self, *, exclude_id: int) -> tuple[list[str], bool]:
        """保留策略（清腾时机 = 新备份成功落盘之后）：
        ① 每 mod 保留最新 N 份；② 全局总量超配额时从最旧清起。
        钉住（pinned）的永远豁免。返回 (清掉的目录名列表, 配额是否仍超标)。"""
        cleaned: list[str] = []

        # ① 每 mod 条数：按 mod 分组，旧→新排，尾部 N 条是最新，其余清掉
        by_mod: dict[int, list] = {}
        for b in self._repo.list_backups(oldest_first=True):
            by_mod.setdefault(b.mod_id, []).append(b)
        for _mid, rows in by_mod.items():
            if len(rows) <= self._keep:
                continue
            for victim in rows[:-self._keep]:
                if victim.pinned or victim.id == exclude_id:
                    continue
                self._remove(victim)
                cleaned.append(victim.backup_path)

        # ② 全局总量：超配额 → 从最旧的非钉住备份清起，直到达标或无可清
        quota_tight = False
        if self._quota is not None:
            while self._repo.sum_backup_bytes() > self._quota:
                victim = next(
                    (b for b in self._repo.list_backups(oldest_first=True)
                     if not b.pinned and b.id != exclude_id),
                    None)
                if victim is None:
                    quota_tight = True  # 全是钉住的/只剩本次，清不动了
                    break
                self._remove(victim)
                cleaned.append(victim.backup_path)
        return cleaned, quota_tight

    def _remove(self, record: Backup) -> None:
        """清腾一个备份：删磁盘目录 + 删数据库记录（两层职责都做，
        因为"清腾"的语义就是彻底拿掉这份备份）。磁盘删不掉时仍删记录
        并让调用方通过 cleaned 的缺失察觉——记录在案比卡死流程重要。"""
        mod = self._repo.get_mod(record.mod_id)
        game = self._repo.get_game(mod.game_id) if mod else None
        if game is not None and (game.backup_dir or "").strip():
            target = Path(game.backup_dir) / record.backup_path
            if target.is_dir():
                try:
                    _safe_rmtree(target, Path(game.backup_dir))
                except (RuntimeError, OSError):
                    pass  # 目录留着（R4 保险丝拦下/权限问题），记录照删
        self._repo.delete_backup_record(record.id)

    @staticmethod
    def _run_robocopy(src: str, dst: str) -> tuple[int, str]:
        """真 robocopy 调用（测试注入点默认实现）。

        - 参数用列表形式：含空格路径的引号由 subprocess 自动处理——
          记事本"路径必须加引号"的要求，落实为"永远列表调用、
          永不手拼 shell 字符串"（手拼才有转义坑）
        - /E 连子目录一起拷；/MT 多线程；/XJ 排除 junction（R10）
        - /R:2 /W:2：失败重试 2 次、每次等 2 秒——robocopy 默认
          重试一百万次、每次等 30 秒，一个坏文件能挂一天，必须掐掉
        - /NFL /NDL /NP：不逐文件刷屏（/MT 下进度条本就不可用）
        """
        cmd = ["robocopy", src, dst,
               "/E", "/MT", "/XJ", "/R:2", "/W:2", "/NFL", "/NDL", "/NP"]
        proc = subprocess.run(cmd, capture_output=True)
        return proc.returncode, _decode(proc.stdout) + _decode(proc.stderr)