# -*- mode: python ; coding: utf-8 -*-
"""Linux 打包 spec（PyInstaller onedir → AppImage 的上游产物）。
AppImage 是唯一维护的 Linux 格式（deb 已从 build-linux.yml 移除：
U 盘 live 系统里 deb 装进系统盘、重启即失，单文件 AppImage 随 U 盘走）。
Windows 打包继续用仓库根的「Steam Workshop Mod Assistant Management Tool V2.spec」，
两者互不影响。

用法（在仓库根执行）：
    pyinstaller --noconfirm --clean packaging/linux/SteamWorkshopTool-linux.spec

Linux 侧说明：
- pexpect 是纯 Python 包，用 collect_all 兜住其数据/动态导入；
- pywinauto/pyautogui/pyperclip 是 Windows 专属功能（core/tabCollector 惰性导入，
  非 Windows 平台被 unsupported_reason 门挡住），Linux 构建直接排除；
- console=False：Linux GUI 应用不需要终端窗口（Windows 版 console=True 是给
  steamcmd 日志观察用的，Linux 桌面双击启动没有终端可挂）；
- 无 version.txt（那是 Windows VSVersionInfo 格式，Linux 不适用）。
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_data_files

# SPEC 是 PyInstaller 注入的本 spec 文件绝对路径。spec 位于
# packaging/linux/ 下，上两级就是仓库根——无论从哪个目录调用都成立
_SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))  # …/packaging/linux
PROJECT_ROOT = os.path.dirname(os.path.dirname(_SPEC_DIR))  # 仓库根

datas = [
    (os.path.join(PROJECT_ROOT, "icon", "icon.png"), "icon"),
    # 建库脚本：sqliteRepository 开空库时经 appPaths.resource_path 读取。
    # PyInstaller 只自动收 .py，数据文件必须显式声明——漏了它，打包版
    # 首次建档就是 FileNotFoundError（v2.0.8 实测翻过车）。
    (os.path.join(PROJECT_ROOT, "core", "schema.sql"), "core"),
]
binaries = []
hiddenimports = ["psutil"]

datas += collect_data_files("qdarktheme")
pexpect_datas, pexpect_bins, pexpect_hidden = collect_all("pexpect")
datas += pexpect_datas
binaries += pexpect_bins
hiddenimports += pexpect_hidden

a = Analysis(
    [os.path.join(PROJECT_ROOT, "main.py")],
    pathex=[PROJECT_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pywinauto", "pyautogui", "pyperclip"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SteamWorkshopTool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Windows 的 EXE 图标只收 .exe/.ico（png 会直接报错）；本 spec 面向
    # Linux 构建，png 交给 AppImage 的 desktop/icon 机制处理。在 Windows
    # 上做结构化构建测试时跳过 icon，走 PyInstaller 默认图标。
    icon=(
        [os.path.join(PROJECT_ROOT, "icon", "icon.png")]
        if os.name != "nt" else None),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SteamWorkshopTool",
)
