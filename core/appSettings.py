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

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "GlobalSettings.json"

# 默认值：数字也写成字符串（理由见文件头），使用处一律 get_int() 转换。
# 设置页（_FIELDS）加行/删行时，这里必须同步——两处的键集必须一致
# （决策 12），不一致的后果：_reset 直接取 DEFAULTS[key]，缺键当场崩。
DEFAULTS: dict[str, str] = {
    # steamcmd 程序路径是整个工具的定位钥匙：命令生成用它、
    # 本地扫描按它找工坊账本（acf）、新建档案按它推导下载目录、
    # 备份引擎按它推导默认备份位置
    "steamcmd_path": "",           # steamcmd.exe 完整路径
    "steamcmd_login_cmd": "",      # steamcmd 登录命令，整行原样使用（如 login 你的账号）
    "api_request_interval_ms": "200",  # 批量查 Steam 接口时，两次请求的间隔毫秒数
    "api_max_retries": "3",        # 被限流（429/503）时的自动重试上限
    "slow_update_days": "30",      # 距上次已知更新超过 N 天，检测结果里标红提醒
    "snapshot_keep": "5",          # 每个 mod 保留的历史快照条数（滚动淘汰）
    "backup_keep_per_mod": "1",    # 每个 mod 保留的备份份数（超出淘汰最旧，钉住豁免）
    "backup_total_quota_gb": "100",  # 全部备份合计的容量上限 GB（超出从最旧清腾）
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
