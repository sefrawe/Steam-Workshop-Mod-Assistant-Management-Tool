"""全局应用设置
"""
"""
config/GlobalSettings.json 的读写封装，程序内唯一的配置入口。

- 启动 load()：文件不存在/损坏 → 回退默认值，损坏文件备份为 .bak，绝不崩
- save()：原子写入（临时文件+替换），断电也不会留下半截配置
- 未知字段原样保留，向后兼容；缺失字段自动补默认值
  （T10 删掉的旧键在老配置文件里会留着，无害，不用手动清理）

数字配置的存取约定（重要）：
- 所有数字配置在内存里一律是"字符串"，读的时候用 get_int() 转整数，
  转不动（空值、乱写）回退默认值，绝不抛异常
- 为什么不直接存数字：load() 以字符串为口径合并配置。如果混入数字类型，
  会出现"手改 JSON 生效、从界面保存一遍后字段消失"的诡异行为
- 手改 JSON 时直接写数字也接受：load() 会自动转成字符串再入库，
  bool 单独排除（True 是 int 的子类，不排除会被转成 "1"）
"""
import json
import shutil
from pathlib import Path
from core.appPaths import app_root

PROJECT_ROOT = app_root()  # T17：数据根统一定位（源码=项目根；打包=exe 旁）

CONFIG_PATH = PROJECT_ROOT / "config" / "GlobalSettings.json"

# 默认值：数字也写成字符串（理由见文件头），使用处一律 get_int() 转换。
# 键集口径（决策 12）：设置页 _FIELDS 的键必须全部在这里有默认值
# （_reset 直接取 DEFAULTS[key]，缺键当场崩）；反过来不要求——
# 界面就地开关（console_auto_show、下面两个 auto_* 键）只住在这里，
# 不进设置页 _FIELDS。

DEFAULTS: dict[str, str] = {
    # steamcmd 程序路径是整个工具的定位钥匙：命令生成用它、
    # 本地扫描按它找工坊账本（acf）、新建档案按它推导下载目录、
    # 备份引擎按它推导默认备份位置
    "steamcmd_path": "",           # steamcmd.exe 完整路径
    "steamcmd_login_cmd": "",      # steamcmd 登录命令，整行原样使用（如 login 你的账号）
    "steam_client_library": "",  # Steam 客户端库目录（首次使用向导第④步读订阅记录用；与 steamcmd 的目录树是两回事）

    "api_request_interval_ms": "200",  # 批量查 Steam 接口时，两次请求的间隔毫秒数
    "api_max_retries": "3",        # 被限流（429/503）时的自动重试上限
    "auto_inventory_after_batch": "1",  # 批次收尾自动盘点（默认开）
    "slow_update_days": "180",      # 距上次已知更新超过 N 天，检测结果里标红提醒
    "snapshot_keep": "5",          # 每个 mod 保留的历史快照条数（滚动淘汰）
    "backup_keep_per_mod": "3",    # 每个 mod 保留的备份份数（超出淘汰最旧，钉住豁免）
    "backup_total_quota_gb": "10",  # 全部备份合计的容量上限 GB（超出从最旧清腾）
    "console_auto_show": "1",  # 控制台被关闭时来了新日志要不要自动弹出
    "local_title_warn_keywords": "abandoned,deprecated,discontinued,unmaintained,outdated",
    # 关闭高级筛选窗口时自动清空条件（决策 97）："1" = 关窗即清空
    # （默认，原行为）；"0" = 关窗保留条件，重开接着用
    "advsearch_autoclear": "1",

    "theme_mode": "auto",  # 界面主题三态：auto=跟随系统 / dark / light（决策 66）

    # ↓ 一个"日常更新一条龙"开关（决策 26）：界面就地开关，不进设置页

    "auto_download_after_check": "0",  # 检测到新版本后跳过询问直接下载（默认关=每次弹窗确认；勾选框在更新检测页）
    "mod_col_status": "1",
    "mod_col_remote_ver": "1",
    "mod_col_local_ver": "1",
    "mod_col_update": "1",
    "mod_col_size": "1",
    "mod_col_tags": "1",
    "mod_col_special": "1",
    "mod_col_note": "1",
    # 批量下载自动登录开关（决策 91）：设置页 _FIELDS 里同名键的默认值。
    # "1" = 批次开始前自动发送设置页里的登录命令（维持原行为；
    # 旧配置文件没这个键时，load() 从 DEFAULTS 起底合并，自动落到这里）；
    # "0" = 不预发登录命令——换账号场景：先在终端手动 login 另一账号，
    # 再开批次。
    # 决策 12 键集口径：进了设置页 _FIELDS 的键必须在这里有默认值，
    # 否则设置页「恢复默认」取 DEFAULTS[key] 时缺键当场崩。
    "batch_auto_login": "1",
    # 批次前清理 steamcmd 缓存开关（决策 100）：设置页 _FIELDS 同名键的
    # 默认值。"1" = 每批下载开始前清 depotcache 与 workshop/downloads
    # （防已删除 mod 复活、减少下载失败，RimSort 同款对策、同默认开）；
    # "0" = 不清（保留缓存省流量，但删过的 mod 可能被 steamcmd 复活）
    "steamcmd_clear_cache_before_batch": "1",


}



class AppSettings:
    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self._path = Path(path)
        self._data: dict[str, str] = dict(DEFAULTS)
        self.load()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
            self._backup_broken()
            self._data = dict(DEFAULTS)
            return
        merged = dict(DEFAULTS)
        for key, value in raw.items():
            if isinstance(value, bool):  # 先排除 bool（bool 是 int 子类）
                continue
            if isinstance(value, (int, float)):  # 手写 JSON 直接写数字也接受
                value = str(value)
            if isinstance(value, str):
                merged[key] = value
        # 密钥不落地（用户拍板）：旧版本可能已在文件里存过 key——
        # 内存里就地抹掉，下次 save() 时文件里那行也随之消失
        merged.pop("steam_api_key", None)

        self._data = merged

    def get(self, key: str, default: str = "") -> str:
        if key in self._data:
            return self._data[key]
        return DEFAULTS.get(key, default)

    def get_int(self, key: str, default: int) -> int:
        """读数字配置：字符串转 int，转不动（空值/乱写）回退 default。
        设置页保存前会校验正整数，这里再兜一层底——手改 JSON 打错字也不崩。"""
        try:
            return int(self.get(key))
        except (TypeError, ValueError):
            return default

    def set(self, key: str, value: str) -> None:
        self._data[key] = value

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8")
        tmp.replace(self._path)  # 同盘替换是原子的

    def _backup_broken(self) -> None:
        try:
            shutil.copy2(self._path, self._path.with_suffix(".json.bak"))
        except OSError:
            pass
