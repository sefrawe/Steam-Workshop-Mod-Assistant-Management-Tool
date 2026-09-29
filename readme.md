# Steam Workshop Mod Assistant Management Tool

<div align="center">
  <img src="https://raw.githubusercontent.com/sefrawe/Steam-Workshop-Mod-Assistant-Management-Tool/master/picture/main.png" width="100%" alt="主界面：mod 库 + 底部 steamcmd 终端">
  <p><b>不打开 Steam 客户端，完成创意工坊 mod 的日常管理。</b></p>
  <p>Windows 10/11 · 中文界面 · 绿色便携 · 免费开源</p>
  <p>
    <img src="https://img.shields.io/badge/platform-Windows%2010%2F11-blue" alt="platform">
    <img src="https://img.shields.io/badge/python-3.12%2B-informational" alt="python">
    <img src="https://img.shields.io/badge/GUI-PySide6-green" alt="PySide6">
    <img src="https://img.shields.io/github/v/release/sefrawe/Steam-Workshop-Mod-Assistant-Management-Tool?color=orange" alt="release">
    <img src="https://img.shields.io/badge/license-MIT-yellow" alt="MIT">
  </p>
</div>

---

## 这是什么

一个 Windows 桌面工具，帮大量使用 mod 的 Steam 玩家**不打开 Steam 客户端**完成 mod 日常管理：

- **信息中枢** —— 本地 SQLite 账本登记每个 mod 的编号、状态、版本、更新历史、备注与标记；
- **调度器** —— 向 Steam 官方接口查询远端版本，引导内置的 steamcmd 终端完成下载；
- **可回滚** —— 更新前自动备份旧版本，新版不满意随时恢复。

软件**绝不代替你执行下载**，也**不碰 Steam 客户端**：所有下载命令由你亲自在内置终端里执行，所有账本变化都来自 steamcmd 的真实文件。

## 工作原理（三句话）

1. **检测更新** —— 向 Steam 官方接口询问每个 mod 的最新更新时间，与硬盘上 steamcmd 记录的本地时间对比：谁的时间新，谁就是新版本；
2. **下载** —— 把要下载的 mod 整理成官方命令，你复制粘贴到底部内置终端（steamcmd）里执行；
3. **确认** —— 下载完成后扫描 steamcmd 的账实文件（acf），把「真的下载好了」写回本工具的账本。

## 功能一览

**更新管理**
- 🔍 批量更新检测 —— 远端 vs 本地逐条对比，结果分桶展示
- 🔄 日常更新一条龙 —— 检测 → 勾选确认（可选先备份）→ 批量下载 → 自动复扫入账
- 📜 更新对照 —— 每个 mod 的版本时间线与历史快照

<img src="https://raw.githubusercontent.com/sefrawe/Steam-Workshop-Mod-Assistant-Management-Tool/master/picture/daily.png" width="80%" alt="日常更新模块">

**下载执行**
- 🖥️ 内置 steamcmd 终端 —— ConPTY 实时伪终端，登录一次、批次自动排队
- ⏯️ 批量下载编排 —— 温和停止、断线不丢队列、失败清单一键重跑、未登录自动挂起

**数据安全**
- 💾 更新前备份 —— 旧版本完整复制（robocopy 多线程），按 mod 保留份数 + 全局容量上限 + 钉住豁免
- ♻️ 一键恢复 —— 恢复前强制先备份当前版本，全程可退
- 🧾 账实核验 —— 「账上有 / 盘上有 / 版本一致」三差集报告，异常条目一键生成修复命令
- 🗑️ 软删除 —— 删除可恢复，彻底清账前须先处置备份

<img src="https://raw.githubusercontent.com/sefrawe/Steam-Workshop-Mod-Assistant-Management-Tool/master/picture/backup.png" width="80%" alt="备份管理">

**多游戏与搬家**
- 🎮 多游戏档案 —— RimWorld / CK3 / 任意创意工坊游戏，独立账本一键切换
- 🔗 目录连接指引 —— junction 双向拓扑识别，下载完游戏立即可读
- 🚚 换机迁移 —— 导出账本 → 搬文件 → 重建连接，七步引导
- 📋 清单分享 —— 收录清单（含备注/标签/特别关注）导出发给别人，一键并入

**体验与维护**
- 🌗 深色主题三态（跟随系统 / 深 / 浅），即时切换
- 📊 统计图表、高级筛选（编号/大小/时间/标签/作者）、颜色标记
- 🧰 失效 mod 处置 —— 远端失效自动归档，重查 / 搜索 / 关联替换
- 🧹 清理与卸载 —— 绿色软件的「反安装」：盘点、指引、零残留
- 🌐 可选：浏览器标签页一键采集工坊网址

<img src="https://raw.githubusercontent.com/sefrawe/Steam-Workshop-Mod-Assistant-Management-Tool/master/picture/stats.png" width="80%" alt="统计页">

## 设计原则

- **单源架构** —— steamcmd 目录是唯一数据源；acf 文件只读，永不修改（账实不一致由 steamcmd 自愈，实测实证）
- **不代替用户判断** —— 下载成败只认 steamcmd 账实文件，软件不猜输出
- **绿色软件** —— 账本、设置、日志全部在软件自己的文件夹里；搬家 = 拷文件夹，卸载 = 删文件夹
- **界面自文档化** —— 每个按钮都有悬浮说明，每个步骤都写清「做什么、失败意味着什么、下一步是什么」
- **不内置 steamcmd** —— 引导从 Valve 官方下载（自更新机制 + 许可考虑 + 减少杀软误报）

## 快速开始

1. 到 [Releases](https://github.com/sefrawe/Steam-Workshop-Mod-Assistant-Management-Tool/releases) 下载最新 zip
2. 解压到任意文件夹（免安装）
3. 双击 `Steam Workshop Mod Assistant Management Tool.exe` 运行
4. 跟着「首次使用」向导四步走：定位 steamcmd → 建立游戏档案 → 接通游戏目录 → 纳入已有 mod

**使用须知**：

- steamcmd 请从 [Valve 官方](https://developer.valvesoftware.com/wiki/SteamCMD)下载，放入**纯英文路径**的空文件夹
- 用本工具管理的游戏，**不要再在 Steam 客户端里订阅同样的 mod** —— 两边各记各的账，版本会乱
- steamcmd 与 Steam 客户端单点登录：登录 steamcmd 会把客户端顶下线，属正常现象
- 终端里请使用英文命令



## 技术栈

| | |
|---|---|
| GUI | PySide6（Qt 6） |
| 终端 | pywinpty（ConPTY 伪终端） |
| 存储 | SQLite（WAL）+ JSON 配置 |
| 网络 | requests + truststore（系统证书库） |
| 备份 | robocopy（/MT 多线程，/XJ 防链接环） |
| 打包 | PyInstaller |

## 常见问题

**Q：需要 Steam 账号吗？**
下载需要登录拥有该游戏的 Steam 账号（steamcmd 登录一次后本机缓存）。

**Q：支持哪些游戏？**
任何有创意工坊的 Steam 游戏。

**Q：数据存在哪？会上传吗？**
全部在软件文件夹内（`data\` `config\`），本工具不上传任何数据；更新检测只向 Steam 官方接口**读取**公开信息。

**Q：和 Steam 客户端的订阅冲突吗？**
本工具走 steamcmd 独立下载，互不干扰——但同一个 mod 请不要两边同时管理（见使用须知）。

**Q：杀软报毒？**
PyInstaller 打包的免费软件偶被误报（已知问题）；不放心可从源码运行。

## 许可证

[MIT](LICENSE) © sefrawe

本工具为个人开源项目，与 Valve 无关。Steam © Valve Corporation。
