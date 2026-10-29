# -*- mode: python ; coding: utf-8 -*-
"""Linux 打包 spec（PyInstaller onedir → AppImage / deb 的上游产物）。
Windows 打包继续用仓库根的「Steam Workshop Mod Assistant Management Tool V2.spec」，
两者互不影响。

Linux 侧说明：
- pexpect 是纯 Python 包，用 collect_all 兜住其数据/动态导入；
- pywinauto/pyautogui/pyperclip 是 Windows 专属功能（core/tabCollector 惰性导入，
  非 Windows 平台被 unsupported_reason 门挡住），Linux 构建直接排除；
- console=False：Linux GUI 应用不需要终端窗口（Windows 版 console=True 是给
  steamcmd 日志观察用的，Linux 桌面双击启动没有终端可挂）；
- 无 version.txt（那是 Windows VSVersionInfo 格式，Linux 不适用）。
"""

from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = [("icon/icon.png", "icon")]
binaries = []
hiddenimports = ["psutil"]

datas += collect_data_files("qdarktheme")
pexpect_datas, pexpect_bins, pexpect_hidden = collect_all("pexpect")
datas += pexpect_datas
binaries += pexpect_bins
hiddenimports += pexpect_hidden

a = Analysis(
    ["main.py"],
    pathex=[],
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
    icon=["icon/icon.png"],
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
