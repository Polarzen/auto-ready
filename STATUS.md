# 实际交付状态

更新时间：2026-10-13（Asia/Hong_Kong），当前版本 **0.2.0**。
代码与验证准备已完成；源码及 EXE 的真实输入 A/B/C 均为 **未执行**，真实游戏未验证。
自动化和只读启动检查没有发送真实鼠标输入，不能作为“自动准备已实测”的证据。

## v0.2.0 当前验证范围

| 类别 | 当前结果与边界 |
| --- | --- |
| 自动化测试 | 196 项通过；Ruff 通过；pip check 无依赖冲突。包括 12 场景的离屏模拟窗口假输入矩阵 |
| 只读桌面探测 | 新版截图及同尺寸平移识别通过；150% / DPI 144，客户区 1080×795；未输入 |
| F11 实机注册 | 注册/释放通过；同键第二次注册因冲突被拒绝（WinError 1409）；未输入 |
| 源码启动 | 普通、`--simulator`、默认受限入口均退出 0，未启用、仅提醒、attempted=false，线程已停止 |
| 源码真实输入 A/B/C | **NOT_EXECUTED**；需用户现场逐轮手动授权 |
| 新版便携包 | 待本轮源码审查提交后重建并在新中文/空格目录验收；旧包不计入新版结果 |
| 便携包真实输入 A/B/C | **NOT_EXECUTED**；源码结果不能替代 EXE 验收 |
| 真实游戏 | **未验证**；本轮未扩展适配 |

默认停止键改为 `Ctrl+Alt+F11`。旧 F12 配置原样显示迁移提示，不静默覆盖文件；
非 F12 自定义值保留。替代热键必须实际注册成功，自动确认才可用；变更会取消本轮及排队请求。

源码与 EXE 共用 `auto_ready.verification`：只允许绑定本次专用模拟器，默认使用拒绝输入适配器。
修复了 Windows 虚拟环境启动器与实际 Python 进程路径不同而误拒绝自身窗口的问题；
仍精确核对自己的 HWND、PID、标题和可执行文件，冻结程序只接受自身 EXE。

证据分开记录单次资格、SendInput 原生返回、模拟器 Qt 按下/释放/点击、新截图成功判断。
记录写入耗时后重新检查取消、几何及帧新鲜度；必要的鼠标释放先于日志 I/O。
Qt 回执的会话关联是时间关联，终态之后到达的事件明确标为歧义，不自动判定成功。
关闭后的生命周期状态不覆盖本轮终态。启用真实输入却缺少 OS 结果时标为
`UNVERIFIED_NO_OS_RESULT`，不把日志丢失误写成“未执行”。每次运行的摘要只陈述证据。

本轮只读桌面截图耗时 25.04 / 14.80 ms，初始及平移后唯一候选得分约 1；
遮挡检查两次为 false，故不说明当时具备可点击条件。F11 注册成功，第二实例锁被拒绝。
没有保存桌面截图、抢焦点或发送输入；几何及进程等原始证据只保存在 Git 排除目录。

假输入矩阵使用实际 Qt 模拟窗口渲染和合成鼠标事件：仅提醒、未启用、停止、急停、
失焦、遮挡、移动中、过期帧、多候选、旧会话和退出均为零尝试、零回执；
重复回调只产生 1 次尝试及按下/释放/点击各 1 次，后续渲染图像由生产引擎识别成功。
所有这些记录标为 `UNIT_FAKE_QT`，不能说明 Windows SendInput 或真实桌面表现。
引擎独立单测另外覆盖平移、成功标志缺失及点击前竞态，矩阵结果保存在 JUnit 属性。

## 当前本地证据与复现

- `.venv\Scripts\python.exe -m pytest -q --junitxml=artifacts\pytest-results-0.2.0.xml`
- `.venv\Scripts\python.exe -m ruff check .`、`.venv\Scripts\python.exe -m pip check`
- `.venv\Scripts\python.exe scripts\desktop_probe.py`：`artifacts\desktop-probe-0.2.0.json`
- `.venv\Scripts\python.exe scripts\check_launch.py --mode normal|simulator|verification`（每次选择一种模式）
- `.\scripts\build_windows.ps1 -Zip`；解压 EXE 用同一 `check_launch.py` 显式路径检查三种模式
- `artifacts\startup-checks\`：每次独立目录内的配置、日志、退出及身份记录；不纳入源码仓库
- 最终 ZIP 与 SHA-256 文件在 `dist\`；构建提交及 dirty 状态在包内 `build-info.json`

原始证据留在本地，发布文档只包含脱敏摘要。真实输入的 A/B/C 最小流程见 README；
每轮由用户手动选择模式和开始，不自动续轮。剩余动作是分别用源码版及最终解压 EXE
执行该流程，再独立核对原生返回、窗口回执和生产引擎新截图判据。

源码同步已获授权，仍须完成待提交内容检查和远端重新核对后普通推送；不上传二进制或发布 Release。
全局路由配置保持原样；按本轮明确例外使用已注册角色，不覆盖模型参数。

---

以下仅保留 v0.1.0 当时的记录；“完成”等判断不表示本轮真实输入验收通过。
历史产物副本存于 `artifacts/history-0.1.0/` 和 `dist/history-0.1.0/`，新结果单独编号。

## v0.1.0 历史记录（保留原始结果）

更新时间：2026-10-13（Asia/Hong_Kong）。通用版本的代码、自动化测试与本地便携包验证已完成。
真实游戏尚未采样或适配验证；开发过程中没有发送真实鼠标输入。

已实现：中文界面、窗口身份绑定、客户区截图、相对搜索区域、静态采集/导入模板、
无点击预览、唯一候选连续确认、单轮集中点击入口、真实 Windows SendInput 适配器、
正向成功模板验证、急停/单实例、配置与轮转日志、可移动/缩放的模拟准备窗口。
默认仅提醒、未启用；必须手动授权每一轮，异常与未确认均不重试。

### 历史执行结果

| 命令/检查 | 结果 |
| --- | --- |
| `.venv\Scripts\python.exe -m pytest -q --junitxml=artifacts\pytest-results.xml` | 117 通过，6.18 秒；构建前再次全量运行 117 通过，6.12 秒 |
| `.venv\Scripts\python.exe -m ruff check .` | 通过 |
| `.venv\Scripts\python.exe -m pip check` | 无依赖冲突 |
| `.venv\Scripts\python.exe -m auto_ready --smoke-test` | 退出 0，无输入 |
| `.venv\Scripts\python.exe scripts\check_launch.py` | 真实后端正常启动、自己的窗口收到 WM_CLOSE 后退出 0；未启用、无输入 |
| `.\scripts\build_windows.ps1 -Zip` | PyInstaller onedir 构建与打包 smoke test 通过 |
| `scripts\check_launch.py dist\auto-ready\auto-ready.exe` | 从发行目录启动真实后端，窗口 DPI 144，正常关闭，退出 0 |
| ZIP 解压到 `artifacts\unpacked-verification` 后执行同一启动检查 | 从解压目录运行，正常关闭，退出 0 |

测试覆盖 49 项引擎、14 项识别、27 项原生适配、8 项配置、4 项模拟器、6 项 UI、
9 项显式模拟器验证入口检查。UI 完整流程使用假输入及可控时钟，覆盖提醒零点击与自动模式
单次尝试后成功证据；可控时钟只用于测试，生产的 0.35 秒新帧限制未放宽。

本机只读桌面探测：Windows 11 build 26200、Python 3.11.9、PySide6 6.10.3，单显示器
150% / DPI 144。Qt 客户区 720×530 对应截图 1080×795；平移前后唯一候选成立。
两次截图耗时 24.41 / 15.98 ms。未抢焦点、未输入；F12/F11 注册与释放通过，第二实例拒绝。
中文界面在 150% 缩放下完成截图复核。系统提示音 API 调用成功，实际是否听见未确认。

合成图像基准：OpenCV 单线程、1080p 图像与 160×60 模板、每种 20 次。
全客户区匹配中位 42.26 ms、最大 48.56 ms；四分之一搜索区域中位 9.56 ms。
这些数字只代表识别步骤，不代表游戏性能或整机 CPU 占用。

### 历史本地产物

- `dist\auto-ready\auto-ready.exe`：Windows x64 onedir；必须保留整个同级目录。
- `dist\auto-ready-windows-x64.zip`：约 96 MiB，完整解压后无需另装 Python。
- `dist\auto-ready-windows-x64.zip.sha256`：最终 ZIP 的 SHA-256。
- `artifacts\pytest-results.xml`、`desktop-probe.json`、`benchmark.json`、`source-launch.json`、
  `packaged-launch.json`、`unpacked-launch.json`、`package.json`：实际验证记录，均排除在 Git 外。

已核对 qwindows 插件、Qt DLL、OpenCV、Python、winsound、示例配置和第三方许可证。
EXE 内嵌 PerMonitorV2 与 asInvoker 清单；配置和日志使用用户可写的
`%LOCALAPPDATA%\auto-ready`，不会把有效窗口绑定或本轮授权保存到下一次启动。

首次打包因构建环境 PATH 中其他工具的 ICU DLL 被误收集而启动失败。构建脚本现只在
PyInstaller 子过程使用最小 PATH，随后恢复；修复后重建与两次正常启动均通过。
没有覆盖系统 DLL 或更改全局环境。

### 历史未验证范围

真实游戏/按钮、真实系统鼠标点击、混合 DPI、多显示器实体负坐标、独占全屏、
反作弊兼容性及实际声音可听性。自动化坐标与拒绝测试不能替代这些验证。
用户可按 README 的显式命令在专用模拟器上选择测试真实输入，再采集具体游戏模板。
检查与系统输入之间仍有竞态，不能承诺零误点或所有游戏兼容。

没有提交、推送、发布、提权、关闭安全软件或修改游戏文件。依赖仅安装在项目 `.venv`。
项目最初为空仓库；全部代码保留在本地供审阅。

路由记录（Root checkpoint schema 2.4）：provider 哈希存在 DRIFT，本轮按用户明确批准的
例外使用已注册角色；全局 provider/代理配置未改。4 个子代理均 depth=1、fork_turns=none，
持久化记录均有 task_complete；上游实际 provider 未知。原生、UI、配置/模拟器与安全复核
均已交接，Root 完成集成；最后只修改交付文档，没有后续应用代码变更或未完成验收项。
