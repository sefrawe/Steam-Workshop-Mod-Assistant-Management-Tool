"""下载命令生成器：把 mod 清单拼成 steamcmd 能用的命令文本。"""
"""
这里只做拼字符串。不碰数据库、不弹窗、不联网、不写文件。

使用方式（手动流，最简单可靠的一条路）：
  ① 用户自己打开 steamcmd 并登录（账号必须拥有该游戏，否则下载报错）；
  ② 在命令生成页勾选 mod，点"复制命令"；
  ③ 粘贴到 steamcmd 窗口回车。多行文本会被终端一行一行依次执行，
     前一条下载完才开始下一条，所以数量多少都不受限制
     （Windows"单条命令 8191 字符"的限制只针对一行，
      这里一行永远只有一条命令，碰不到上限）。

产出两种文本：
  - build_plain_commands：一行一条 workshop_download_item。
    与旧脚本 1.7 的输出、对比报告"第 3 部分"逐字一致（M1 验收线）。
  - build_copy_text：把上面的行拼成一段可复制文本（复制按钮直接用）。

有意不做的事（写下来防止以后被"好心"加回来）：
  - 不生成登录命令：登录由用户在 steamcmd 里自己完成，
    软件只提供"复制登录命令"的便捷按钮（内容来自设置页，原样传递不解析）；
  - 不生成 force_install_dir：下载位置由 steamcmd 自己的默认规则决定；
  - 不生成 .bat / +runscript / +quit：手动粘贴已覆盖全部场景。

给界面层的约定：
  - 全部是纯函数：同样输入必得同样输出，可以直接单测；
  - group_mods 需要每个 mod 有 5 个属性：
      mod_id、title、status、time_updated、local_timeupdated
    传数据库里的 Mod 对象或测试替身都行。
"""


def group_mods(mods):
    """把 mod 列表分成三组，返回字典，每组内部按 mod_id 从小到大排。

    分组尺子（和更新检测页保持同一把）：
       - 本地没有下载记录（local_timeupdated <= 0） → "not_downloaded"（键保持英文，
   命令生成页组标题显示"已收录"——T19⑦ 与 mod 库页状态列同词；
   手动确认入账但未用 steamcmd 下载的 mod 也落在本组）
      - 远端比本地新（time_updated > local_timeupdated）   → "needs_update"   需要更新
      - 本地有文件但从没查过远端（time_updated <= 0）      → 也归"需要更新"
        （宁可让用户多下载一遍，也不能漏掉可能的新版本）
      - 其余（查过且远端不比本地新）                       → "up_to_date"     已最新
      - status 是 deleted / failed 的 mod 一律不出现
    """
    groups = {
        "needs_update": [],
        "not_downloaded": [],
        "up_to_date": [],
    }
    for m in mods:
        if m.status in ("deleted", "failed"):
            continue
        local = int(m.local_timeupdated or 0)
        remote = int(m.time_updated or 0)
        if local <= 0:
            groups["not_downloaded"].append(m)
        elif remote <= 0:
            # 本地有文件、远端还没查过：保守处理，当成需要更新
            groups["needs_update"].append(m)
        elif remote > local:
            groups["needs_update"].append(m)
        else:
            groups["up_to_date"].append(m)
    for key in groups:
        groups[key].sort(key=lambda m: int(m.mod_id))
    return groups


def build_plain_commands(app_id, mod_ids):
    """裸命令列表：每行一条 workshop_download_item，格式与旧脚本逐字一致。"""
    return [f"workshop_download_item {app_id} {mod_id}" for mod_id in mod_ids]


def build_copy_text(app_id, mod_ids):
    """把命令行拼成一段文本，直接进剪贴板。

    结尾留一个换行：粘贴进 steamcmd 后直接回车就行，不用再补。
    """
    return "\n".join(build_plain_commands(app_id, mod_ids)) + "\n"
