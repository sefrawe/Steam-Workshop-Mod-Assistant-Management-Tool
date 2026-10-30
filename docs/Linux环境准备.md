# Linux 环境准备与安装说明（Linux Mint 为主，其他发行版类推）

本文面向 Linux 用户与移植验收测试（M4/M6 里程碑）。三种使用方式任选。

## 0. 前置：steamcmd（要真实下载 Mod 才需要）

steamcmd 是 32 位程序，64 位 Linux 上先补架构再装依赖：

```bash
sudo dpkg --add-architecture i386
sudo apt update
sudo apt install steamcmd   # 缺依赖时按提示补装，常见：lib32gcc-s1
```

仅浏览/管理已有账本不需要 steamcmd。

## 0.5 前置：中文显示（重要）

界面是中文，Mint 默认不装中文字体时会满屏方块（豆腐块）：

```bash
sudo apt install fonts-noto-cjk
```

装完重启应用即生效。

## 1. 源码运行（开发者 / M4 测试）

```bash
# 在线：git clone https://github.com/sefrawe/Steam-Workshop-Mod-Assistant-Management-Tool.git
# 离线：git clone /media/你/优盘/linux-port-20261030.bundle SteamWorkshopTool
cd SteamWorkshopTool
git checkout linux-port
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

**完全离线变体（U 盘套件）**：把 `linux-port-20261030.bundle` 与
`wheels/`（全部依赖的 Linux 安装包，约 260 MB，支持 Python 3.10/3.12）
拷到 U 盘，Mint 上：

```bash
git clone /media/你/优盘/linux-port-20261030.bundle SteamWorkshopTool
cd SteamWorkshopTool && git checkout linux-port
python3 -m venv .venv && source .venv/bin/activate
pip install --no-index --find-links=/media/你/优盘/wheels wheel setuptools
pip install --no-index --find-links=/media/你/优盘/wheels --no-build-isolation \
    PySide6-Essentials PySide6-Addons shiboken6 requests loguru \
    pyqtdarktheme-fork vdf truststore psutil pyautogui pyperclip pexpect pytest
python main.py
```

（套件里个别纯 Python 包是源码包，`--no-build-isolation` + 先装
setuptools/wheel 即可离线构建；打包工具 pyinstaller 不在套件内——
运行/测试用不到。）

源码模式数据就放在项目目录里（data/、config/），与 Windows 便携模式一致。

## 2. AppImage（下载即用，推荐）

```bash
chmod +x SteamWorkshopTool-x86_64.AppImage
./SteamWorkshopTool-x86_64.AppImage
```

AppImage 本体是只读挂载，**数据在 ~/.local/share/SteamWorkshopTool/**（设置
`XDG_DATA_HOME` 环境变量可改位置）。

> AppImage 是唯一维护的 Linux 打包格式（deb 已于 v2.0.9 从构建流水线移除）：
> U 盘 live 系统里 deb 装进系统盘、重启即失；单文件 AppImage 随 U 盘走，双击即用。

## 数据位置一览

| 运行方式 | 数据根 |
|---|---|
| 源码（python main.py） | 项目目录（data/、config/） |
| Windows 打包（便携版） | exe 所在文件夹（原设计不变） |
| Linux 打包（AppImage） | `$XDG_DATA_HOME/SteamWorkshopTool/` 或 `~/.local/share/SteamWorkshopTool/` |

账本 mods.db、全局设置 GlobalSettings.json、备份都在数据根下，卸载/升级不丢数据。

## 与 Windows 版的差异（功能限制说明）

| 功能 | Windows | Linux |
|---|---|---|
| steamcmd 终端（下载页） | ConPTY（winpty） | pexpect（M2 移植） |
| 实时采集（浏览器页签） | pywinauto UIA | **不支持**——入口弹「本平台不支持实时采集」，需手动录入 |
| 备份/恢复引擎 | robocopy | shutil.copytree（M3.5 移植，语义等价） |
| Mod 目录联接 | junction（mklink /J） | 符号链接（ln -s，M3.6 移植） |
| 深浅色跟随 | Windows 注册表 | Qt 主题探测（M3 移植） |
| Steam 路径探测 | 注册表 | 手填 steamcmd 路径 |

## 疑难排查

- **AppImage 双击没反应 / 报 FUSE 错误**：缺 libfuse2。`sudo apt install libfuse2`；或临时 `./SteamWorkshopTool-x86_64.AppImage --appimage-extract-and-run` 运行。
- **界面字体方块**：见 §0.5，装 fonts-noto-cjk。
- **steamcmd 起不来（登录页点启动没反应）**：32 位架构没配好，回 §0 跑 `sudo dpkg --add-architecture i386 && sudo apt update`；先在终端里确认 `steamcmd` 能进到 `Steam>` 提示符。
- **终端面板乱码/对不齐**：面板用等宽字体（monospace 别名），确认系统装了任一等宽字体（fonts-dejavu-core 或 fonts-noto-cjk-mono）。
- **窗口渲染异常**：`QT_QPA_PLATFORM=xcb ./SteamWorkshopTool-x86_64.AppImage` 强制 X11（Mint Cinnamon 默认即 X11，一般用不到）。
- **找不到 Steam 路径**：Linux 版不读注册表，路径一律手填（设置页指定 steamcmd 与游戏库位置）。

## 卸载

- AppImage：删掉 .AppImage 文件 + 数据目录 `~/.local/share/SteamWorkshopTool/`。
