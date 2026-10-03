"""一级单源常量"""
# 准入规则：≥2 处使用（跨文件 / 跨 core·gui 层）才准入本文件；单处使用的留在原地。
# 本文件是叶子模块：禁止 import 项目内任何东西（结构上杜绝循环依赖）。
"""一级单源常量（D37） """ """改一词全项目跟的常量住这里；模块私有常量（设置键 K_*、API 端点、正则）
留各模块顶部；一次性文案就写在页面文件里。三级政策见计划书 D37。 """

# ============================================================
# 状态值与显示词（D30：tracked =「待下载」；D6 状态机四态）
# ============================================================
STATUS_TRACKED = "tracked"
STATUS_DOWNLOADED = "downloaded"
STATUS_DELETED = "deleted"
STATUS_FAILED = "failed"

STATUS_VALUES = frozenset({STATUS_TRACKED, STATUS_DOWNLOADED, STATUS_DELETED, STATUS_FAILED})

# 库内状态值 → 中文（表格/下拉/详情三处共用；函数 status_zh 留 gui/formatters）
STATUS_ZH = {
    STATUS_TRACKED: "待下载",
    STATUS_DOWNLOADED: "已下载",
    STATUS_DELETED: "已删除",
    STATUS_FAILED: "已失败",
}

# ============================================================
# 本地确认来源徽章（D21：库页第九列显隐 + 详情面板 + 更新对照组头）
# ============================================================
CONFIRMED_SOURCE_ZH = {
    "verified": "✓已验证",       # 终端 SUCCESS 判决背书（D4a）
    "unverified": "？未验证",     # 批查失败照常确认（D4c）
    "inherited_acf": "⤵继承旧账", # D9 一次性迁移
    "claim": "👆认领",            # 盘面事实背书（D7）
    "manual": "✎手动",            # 右键「设定本地版本…」（D5③；C 级提案②配套）
}

# ============================================================
# 判决种类显示词（D22 详情面板判决史 / D20 入账中心两处共用）
# ============================================================
VERDICT_KIND_ZH = {
    "success": "下载成功",
    "timeout": "超时",
    "fail": "失败",
    "claim": "认领",
    "manual": "手动设定",
}

# ============================================================
# 颜色标记唯一色表（D26：取色器 / 顶栏筛选 / 渲染归一化三消费方）
# 现值自旧 gui/modListModel.py 原样收编，零改动
# ============================================================
COLOR_CHOICES = {
    "红": "#e5484d",
    "橙": "#f76b15",
    "黄": "#f5d90a",
    "绿": "#46a758",
    "蓝": "#0091ff",
    "紫": "#8e4ec6",
}

# ============================================================
# Steam API（跨层使用的部分；端点 URL 属 steamApiClient 模块私有，留原地）
# ============================================================
BATCH_SIZE = 100  # GetPublishedFileDetails 单批上限（官方；D37 实例）
WORKSHOP_URL_TEMPLATE = "https://steamcommunity.com/sharedfiles/filedetails/?id={mod_id}"

# ============================================================
# netGate 五入口（D37 实例：入口名收编，各页 acquire 只引常量不手写）
# ============================================================
NET_GATE_UPDATE_CHECK = "更新检测"
NET_GATE_DEEP_CHECK = "深度检测"
NET_GATE_COLLECTION = "展开合集"
NET_GATE_QUICK_CMD = "快速命令查询"
NET_GATE_DEPENDENCIES = "依赖拉取"

# ============================================================
# 导航页 ID（D31 导航总表 → 代码形态的一半；树结构 _NAV_SCHEMA 留 MainWindow）
# 编号 = M3 建页时 _pages.append 的落位序；M0 先定名防 M3 串号。
# 每轮加页：常量追加到对应组末尾，编号顺延，不许中间插号（决策 13 精神）。
# ============================================================
# 欢迎页（启动默认页，不入组）
PAGE_WELCOME = 0
# 主循环组
PAGE_MOD_LIST = 1
PAGE_ACCOUNT_CENTER = 2
PAGE_UPDATE_CHECK = 3
PAGE_UPDATE_COMPARE = 4
# 功能模块组
PAGE_ADD_MOD = 5
PAGE_DAILY_UPDATE = 6
PAGE_FIRST_USE = 7
PAGE_GAME_EXIT = 8
PAGE_UNINSTALL = 9
# 下载与备份组
PAGE_COMMAND_GEN = 10
PAGE_BATCH_OPEN = 11
PAGE_BACKUP = 12
PAGE_BACKUP_OVERVIEW = 13
# 检测与异常组
PAGE_TITLE_CHECK = 14
PAGE_REMOTE_HEALTH = 15
PAGE_DEP_CHECK = 16
PAGE_EXCEPTION = 17
# 清理与账务组
PAGE_VERIFY = 18
PAGE_JUNCTION_CHECK = 19
PAGE_CLEANUP = 20
PAGE_PURGED = 21
# 档案与工具组
PAGE_IMPORT = 22
PAGE_MIGRATION = 23
PAGE_RESCUE = 24
PAGE_SHARE_LIST = 25
PAGE_STATS = 26
PAGE_API_KEY = 27
PAGE_SETTINGS = 28
