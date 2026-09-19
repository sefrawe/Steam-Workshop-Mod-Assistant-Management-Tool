"""全局应用设置"""
"""
config/GlobalSettings.json 的读写封装，程序内唯一的配置入口。
- 启动 load()：文件不存在/损坏 → 回退默认值，损坏文件备份为 .bak，绝不崩
- save()：原子写入（临时文件+替换），断电也不会留下半截配置
- 未知字段原样保留，向后兼容；缺失字段自动补默认值
"""
import json
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "GlobalSettings.json"

# 默认值：新增配置项在这里登记，设置页（_FIELDS）同步加一行
DEFAULTS: dict[str, str] = {
    "steamcmd_path": "",         # steamcmd.exe 完整路径
    "steam_library_path": "",    # Steam 库 steamapps 目录（扫描 acf 用）
    "default_download_dir": "",  # 新建游戏档案时预填的下载目录
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
        merged.update({k: v for k, v in raw.items() if isinstance(v, str)})
        self._data = merged

    def get(self, key: str, default: str = "") -> str:
        if key in self._data:
            return self._data[key]
        return DEFAULTS.get(key, default)

    def set(self, key: str, value: str) -> None:
        self._data[key] = value

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8")
        tmp.replace(self._path)   # 同盘替换是原子的

    def _backup_broken(self) -> None:
        try:
            shutil.copy2(self._path, self._path.with_suffix(".json.bak"))
        except OSError:
            pass
