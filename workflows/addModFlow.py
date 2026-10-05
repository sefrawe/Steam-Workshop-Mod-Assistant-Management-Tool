"""加入新 mod 流程（功能模块引擎）
"""
"""
workflows/addModFlow.py —— 功能模块「加入新 mod」的逻辑半件。

界面半件是 gui/addModPage.py（下一轮交付）。本文件零界面、零 Qt，
pytest 直接覆盖（tests/test_addModFlow.py）。

模块四步与引擎函数的对应关系：
  第①步 确认目标档案 —— 纯界面判断（没有档案就引导去建档），
        不需要引擎函数；
  第②步 粘贴网址 / 读文本文件 → preview_input() 解析出编号，并与
        账本对表，分成"新的 / 已在库 / 认不出"三堆；文本里若带下载
        命令，还会核对命令所属游戏与指定档案是否一致（防错档）；
  第③步 一键入库 + 生成命令 → register_mods() + build_commands_text()
        整批登记为「待下载」（内部状态码沿用 tracked），并把同一批
        编号拼成 steamcmd 下载命令文本；
  第④步 盘点本批状态 → bucket_statuses()：命令跑完、批次收尾的
        确认清单勾选入账之后，模块页点【盘点本批状态】，本函数只读
        盘出这批编号现在是"已下载"还是"待下载"。
        （V2 判决制没有"复扫"这一步：盘面事实的确认由入账中心的
        盘点认领承担，"扫描确认"的旧概念已退役。）

单源纪律（本文件刻意不重写的东西，防止两处实现将来漂移）：
- 网址解析只调 core.urlParser.parse_lines——什么算网址、什么算
  纯编号、命令行怎么认，全项目只有这一处定义；
- 下载命令只调 core.commandBuilder.build_copy_text——与命令生成页
  的输出逐字同源；
- 入库只走 ModRepository.add_mod，整批包在一个事务里，要么全成
  要么全败，不会出现"导了一半"；
- 本地盘点链（定位 acf → 解析 → 做差 → 落候选）唯一实现住在
  core/inventoryFlow（入账中心消费它）。本引擎不做扫描，模块页
  也不做——盘面事实一律转入账中心看。

与旧脚本的关系（用户的实际工作流 → 本模块）：
  1.5（浏览器抓网址）→ 保持外部工具，产出的 txt 由第②步读入；
  1.7（网址→命令） → 第③步，同一份 commandBuilder；
  1.8 + 1.9（手工维护编号清单）→ 整个消失，账本 + 盘点接管；
  1.11（对比更新） → 更新检测页，归「日常更新」模块。
"""
import time
from dataclasses import dataclass, field
from typing import Iterable

from core.commandBuilder import build_copy_text
from core.models import Mod
from core.urlParser import WORKSHOP_URL_TEMPLATE, parse_lines


@dataclass
class ParsePreview:
    """第②步的产出：一次"解析 + 对表"的完整结果。

    mod_ids   全部识别到的编号（去重、按出现顺序）
    new_ids   账本里还没有的——真正会被入库的那部分
    dup_ids   账本里已有的——入库时会自动跳过，界面上报个数即可
    invalid   认不出的原始片段——原样带回给界面展示，让用户自己
              看是哪里粘错了。不猜、不纠正、不静默丢弃
    command_app_ids  文本里下载命令携带的游戏 AppID（没有命令则空）。
              工坊编号全工坊唯一、只属于一个游戏，命令自带的 AppID
              就是这批编号的"户籍"——拿它和当前档案比对，能发现
              "粘错了游戏的命令文件"
    app_id_mismatch  上面比对的结论：True = 命令所属游戏与调用方
              指定的档案对不上。界面见到它必须拦下来向用户说明，
              绝不能默默入库（错档数据很难事后清理）
    """
    mod_ids: list[int] = field(default_factory=list)
    new_ids: list[int] = field(default_factory=list)
    dup_ids: list[int] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    command_app_ids: list[int] = field(default_factory=list)
    app_id_mismatch: bool = False

    @property
    def has_new(self) -> bool:
        """有没有可入库的新编号——界面据此点亮"入库"按钮。"""
        return bool(self.new_ids)


def preview_input(lines: Iterable[str], repo,
                  app_id: int | None = None) -> ParsePreview:
    """第②步：把粘贴/读入的文本解析成编号，并与账本对表。

    repo 只需要一个本领：filter_existing_ids(编号列表) 返回其中
    已存在的编号（真实仓库与测试替身都满足）。

    app_id = 打算把这批编号记到哪个档案名下（界面传当前档案的
    AppID）。文本里若带下载命令（workshop_download_item 行），命令
    自带游戏 AppID——两相对比就能发现"命令是别的游戏的"这种错档
    险情：app_id_mismatch 置 True，由界面拦下说明。不传 app_id 就
    不做这项比对（旧调用方式不受影响）。

    注意：结果属于"调用那一刻"的账本内容。之后如果又导入过、或换了
    游戏档案，这份预览就算过期了——界面必须在入库前重新预览一次
    （与网址批量导入页同一条防呆规矩）。
    """
    report = parse_lines(lines)
    existing = (set(repo.filter_existing_ids(report.mod_ids))
                if report.mod_ids else set())
    return ParsePreview(
        mod_ids=report.mod_ids,
        new_ids=[i for i in report.mod_ids if i not in existing],
        dup_ids=[i for i in report.mod_ids if i in existing],
        invalid=list(report.invalid),
        command_app_ids=list(report.command_app_ids),
        app_id_mismatch=(bool(report.command_app_ids)
                         and app_id is not None
                         and app_id not in report.command_app_ids),
    )


def register_mods(repo, app_id: int, mod_ids: Iterable[int]) -> int:
    """第③步前半：把编号整批登记为「待下载」（tracked）。

    - 归属档案直接用 app_id（games 表以 app_id 为主键，与入账中心
      登记区同一口径）；
    - 链接按模板现拼；只写编号与链接，标题等远端信息留给更新检测
      补全（全项目统一做法）；
    - 整批一个事务：任何一条失败全部回滚，绝不会"导了一半"。

    返回入库条数。账本里已有的编号不该传进来（第②步已分堆），万一
    撞上（预览之后、入库之前又导过一次），数据库会抛
    sqlite3.IntegrityError 并整批回滚——调用方（界面）接住它弹窗
    提示"请重新预览"，与 preview_input 的过期注释对上。

    传空列表直接返回 0，不碰数据库。
    """
    ids = list(dict.fromkeys(int(i) for i in mod_ids))  # 再去重一次，保序
    if not ids:
        return 0
    now = int(time.time())
    with repo.transaction():
        for mid in ids:
            repo.add_mod(Mod(
                mod_id=mid,
                game_id=app_id,
                url=WORKSHOP_URL_TEMPLATE.format(mid),
                status="tracked",
                first_tracked_at=now,
            ))
    return len(ids)


def build_commands_text(app_id: int, mod_ids: Iterable[int]) -> str:
    """第③步后半：把编号拼成可粘贴进 steamcmd 的命令文本。

    只调 core.commandBuilder.build_copy_text（单源）。空清单返回
    空串——不返回一个光秃秃的换行符，界面据此把"复制"按钮置灰。
    """
    ids = list(mod_ids)
    if not ids:
        return ""
    return build_copy_text(app_id, ids)


@dataclass
class StatusBuckets:
    """第④步的产出：一批编号在账本里的现状分堆。

    downloaded  已是"已下载"——收尾确认清单里背书过的，闭环；
    waiting     还是"待下载"——命令可能还没跑、或跑了还没确认；
    failed      "已失败"——正常流程到不了这个状态，列出来防呆
                （V2 的远端失效走归档，多半也落不进这堆）；
    unexpected  账本里没有（或已删除等其他状态）——更不该出现。
                多半是换了档案再查、或有人动过库，界面看到它应该
                提示"状态异常"，不能默默吞掉
    """
    downloaded: list[int] = field(default_factory=list)
    waiting: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    unexpected: list[int] = field(default_factory=list)


def bucket_statuses(repo, app_id: int, mod_ids: Iterable[int]) -> StatusBuckets:
    """第④步：把本批编号在账本里的现状只读盘出来。

    V2 判决制下本函数的用法：命令跑完、批次收尾的确认清单勾选入账
    之后，模块页点【盘点本批状态】——这里分堆报告"哪些已下载、
    哪些还是待下载"。它只读账本、不碰磁盘；盘面事实（文件在不在、
    版本对不对）由入账中心的盘点认领链负责，本函数不越界。

    一次 list_mods 全量取回后按编号对表——数百条级毫秒完成，
    不值得开线程。
    """
    buckets = StatusBuckets()
    ids = list(mod_ids)
    if not ids:
        return buckets
    status_map = {m.mod_id: m.status for m in repo.list_mods(app_id)}
    for mid in ids:
        status = status_map.get(mid)
        if status == "downloaded":
            buckets.downloaded.append(mid)
        elif status == "tracked":
            buckets.waiting.append(mid)
        elif status == "failed":
            buckets.failed.append(mid)
        else:
            # 账本里没有，或已删除等本模块到不了的状态
            buckets.unexpected.append(mid)
    return buckets
