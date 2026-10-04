"""备份失联重定位引擎
"""
"""
core/backupRelocate.py · 备份记录集体失联后的"指认新位置"预演引擎，
供重定位对话框调用。纯逻辑、只读（exists / is_dir）：不写库、
不动任何文件、不弹任何框。

背景：备份记录的 backup_path 存的是相对档案备份目录的路径（R1）
——本质是个"指针"。指针失准（备份文件夹被搬动/改名、steamcmd
挪位整树跟着搬、档案编辑改错地址）时，该档案全部备份记录在备份
页"盘上"列集体失联。

R1 的红利：重定位 = 把档案的备份目录指回文件真正所在的目录，
逐条记录零改动。本引擎只负责预演"指认的位置能把多少记录对回来"
（命中率）；写库动作由重定位对话框在用户确认后做（只改档案的
备份目录一个字段）。

为什么不像下载目录那样自动重推导：下载目录恒等于 steamcmd 公式，
自动刷新永远正确；备份目录是用户资产指针——默认值虽由公式来，
但明确允许每档案改。steamcmd 挪位后若自动重推导，指针会被悄悄
改到公式算出的新空目录，而文件还在旧位置：记录从"失联但线索
还在"变成"指针换了家"，还可能踩掉用户手动改过的自定义位置。
资产指针失准必须显式指认 + 命中率验证，绝不静默自愈。

口径细节：
- "对上" = <候选根>/<相对路径> 存在且是目录（备份是 robocopy
  出来的文件夹；解析到普通文件按对不上算，不猜）。相对路径一段
  或两段都能对（备份轮起新备份存两段式 "<mod编号>/<目录名>"，
  旧备份是单段——Path 拼接对两种段数天然兼容）；
- 空候选根绝不参与比对：Path("") 等价于"."，放任不管会碰巧对上
  当前工作目录——空根一律按"根不存在"处理；
- 绝对路径行单列不参与（防御性：预期为 0）——绝对路径行与候选根
  无关，重定位既救不了也影响不了，列出来人工看；
- 候选根先去首尾空白与引号（资源管理器复制的路径常带引号）。
"""
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

# 对不上/非相对路径的样例最多列几条——预演是给用户看的，
# 几百条全列只会刷屏；数量看汇总，样例看感觉
_MAX_SAMPLES = 5


@dataclass
class RelocatePreview:
    """重定位预演结果：候选根 + 相对路径清单的比对汇总。"""

    candidate_root: str = ""     # 规整后的候选根（去空白与引号）
    total: int = 0               # 参与比对的记录条数
    hit: int = 0                 # 候选根下能对回目录的条数
    root_exists: bool = False    # 候选根本身是否存在（目录）
    non_relative_count: int = 0  # 绝对路径行条数（不参与命中分母）
    missed_samples: list[str] = field(default_factory=list)
    non_relative_samples: list[str] = field(default_factory=list)

    @property
    def relocatable(self) -> int:
        """重定位管得了的条数 = 总数 - 绝对路径行。"""
        return self.total - self.non_relative_count

    @property
    def missed(self) -> int:
        """对不上的条数（重定位能管的部分里）。"""
        return self.relocatable - self.hit

    @property
    def any_hit(self) -> bool:
        return self.hit > 0

    @property
    def all_hit(self) -> bool:
        return self.relocatable > 0 and self.hit == self.relocatable


def preview(candidate_root: str | Path,
            rel_paths: Iterable[str]) -> RelocatePreview:
    """预演：把相对路径逐条对到候选根上，数命中率。只读不写。"""
    root = str(candidate_root or "").strip().strip('"').strip()
    # 空串的 Path 是"."——绝不能让空输入碰巧对上当前工作目录
    root_exists = bool(root) and Path(root).is_dir()
    out = RelocatePreview(candidate_root=root, root_exists=root_exists)
    for rel in rel_paths:
        rel = str(rel or "").strip()
        if not rel:
            # 空路径行没有定位语义，不计数（库层 NOT NULL，预期为 0）
            continue
        out.total += 1
        p = Path(rel)
        if p.is_absolute():
            out.non_relative_count += 1
            if len(out.non_relative_samples) < _MAX_SAMPLES:
                out.non_relative_samples.append(rel)
            continue
        if root_exists and (Path(root) / p).is_dir():
            out.hit += 1
        elif len(out.missed_samples) < _MAX_SAMPLES:
            out.missed_samples.append(rel)
    return out
