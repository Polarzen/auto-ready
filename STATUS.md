# 实际交付状态

更新时间：2026-10-13（Asia/Hong_Kong），当前版本 **0.3.0**。
v0.3.0 增加“选择并载入配置…”、“另存为配置…”及当前配置路径显示，支持多份 JSON 配置与配套模板；兼容原有默认配置和 v0.2.1 计时修复。
v0.2.0 专用模拟器真实输入六格历史验收通过（源码版 A/B/C、便携 EXE A/B/C）；后续 v0.2.1、v0.3.0 未重新执行真实输入验收，真实游戏未验证。
自动化和只读启动检查没有发送真实鼠标输入，不能作为“自动准备已实测”的证据。

## v0.3.0 多配置

此前“载入配置”固定读取默认 `config.json`，没有文件选择窗口。新版本允许选择 JSON 载入、另存为不同文件、保存回当前文件，并显示当前路径。每份配置的模板仍以相对路径保存在其旁的 `templates/`，沿用 schema 1；不自动迁移或覆盖默认配置。取消、读取失败或过期的异步载入结果不会切换当前配置，载入不会绑定新目标或开始本轮。

本机交付目录使用 `dist/0.3.0/`，构建脚本增加可选 `-OutputDirectory`，默认输出位置不变。旧版目录、配置及历史真实输入证据保留；本轮仅做临时目录配置回归、假输入 UI 检查和打包启动检查。

## v0.2.1 计时修复

假时钟回归在修复前复现了停止、终态和最小化导致再次启动失败后的计时继续增加；修复后 58 项引擎测试及 1 项针对异步失败启动的 UI 回归通过。后者确认旧轮次用时固定、暂停/继续/停止按钮不可用，且没有输入。初次启动失败保持 0 秒；新的成功启动重新计时；保留旧会话标识和已结束用时供诊断。

原 v0.2.0 ZIP 原样保存在本地 `artifacts/releases/0.2.0-original/`，SHA-256 仍为 `ba3dd6ff4f3d80c2265be3c8d845a066c908785bc6069d32523f7597ac126299`；此前解压验收目录和真实输入证据保留。新包身份及后续构建检查单独记录。

最终检查：207 项 pytest、Ruff、pip check 均通过；0.2.1 EXE 的离屏 `--smoke-test` 退出 0，使用假后端和假输入，不证明真实输入行为。原输出 EXE 被占用导致首次打包在 COLLECT 阶段失败，随后复用已通过的检查和构建产物，在新目录完成打包；未关闭用户实例、重跑全量检查或发送真实输入。

新程序为 `dist/0.2.1/auto-ready/auto-ready.exe`，修复包为 `dist/auto-ready-0.2.1-windows-x64.zip`：97,623,908 字节，SHA-256 `bc7d9bb1e0464eb38503cc7b87e4bd271d6df8daf74b5dddaa0ca60780ef86ce`，262 个文件均与交付目录一致。构建源码 `497e81a1ffc2013771e745df438581f84633375d`、dirty=false；EXE SHA-256 `7cd4ce58882acf003af4b1705efa702f32f8934bbe982d3bde1b6ac65d087aa5`。检查记录位于本地 `artifacts/timer-fix-0.2.1/verification.json`；新包真实输入未重验，旧版六格结论继续作为历史记录保留。

## v0.2.0 历史验证范围

| 类别 | 当前结果与边界 |
| --- | --- |
| 自动化测试（此前基准） | 197 项通过；Ruff 通过；pip check 无依赖冲突。包括 12 场景的离屏模拟窗口假输入矩阵 |
| 只读桌面探测 | 新版截图及同尺寸平移识别通过；150% / DPI 144，客户区 1080×795；未输入 |
| F11 实机注册 | 注册/释放通过；同键第二次注册因冲突被拒绝（WinError 1409）；未输入 |
| 源码启动 | 普通、`--simulator`、默认受限入口均退出 0，未启用、仅提醒、attempted=false，线程已停止 |
| 源码真实输入 A/B/C | A/B/C **通过**；A 沿用历史正式通过记录，B/C 见文末六格验收 |
| 新版便携包 | 退出竞态已修复并重新构建；新中文/空格目录三个入口均退出 0，零尝试，线程实际停止 |
| 便携包真实输入 A/B/C | A/B/C **通过**；详见文末六格验收 |
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

此前（2026-10-13）的只读桌面截图耗时 25.04 / 14.80 ms，初始及平移后唯一候选得分约 1；
遮挡检查两次为 false，故不说明当时具备可点击条件。F11 注册成功，第二实例锁被拒绝。
该次探测没有保存桌面截图、抢焦点或发送输入；几何及进程等原始证据只保存在 Git 排除目录。

假输入矩阵使用实际 Qt 模拟窗口渲染和合成鼠标事件：仅提醒、未启用、停止、急停、
失焦、遮挡、移动中、过期帧、多候选、旧会话和退出均为零尝试、零回执；
重复回调只产生 1 次尝试及按下/释放/点击各 1 次，后续渲染图像由生产引擎识别成功。
所有这些记录标为 `UNIT_FAKE_QT`，不能说明 Windows SendInput 或真实桌面表现。
引擎独立单测另外覆盖平移、成功标志缺失及点击前竞态，矩阵结果保存在 JUnit 属性。

最终复检发现并保留了一次真实退出失败：`--simulator` 在启动后立即关闭时，
工作线程枚举自身窗口标题会同步等待 GUI，而 GUI 在 closeEvent 等待该线程，形成死锁（[Microsoft 窗口标题 API 文档](https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-getwindowtextlengthw)）。
独立受控复现的线程转储定位到了 GetWindowTextLengthW / worker.wait；未发送输入。
首次候选包及失败记录保留在 `dist/history-0.2.0-pre-shutdown-fix/` 和 `artifacts/`，
不把此前偶然通过当作最终验收。已改为先撤销授权、让 GUI 继续处理后台请求，
再在工作线程实际停止后完成关闭；受限记录器也延后到此时封存。相同只读复现修复后退出 0。
新增确定性子进程回归也通过，重复关闭与记录器封存均等待真实线程结束。
修复后全量 197 项、Ruff、依赖检查及源码三个默认入口通过；重新构建后，
中文/空格目录解压 EXE 的普通、模拟器、默认受限入口均通过，未发送真实输入。

修复前候选包的干净源码提交为 `488321d3291859d218f701298c1ae72b6d569298`，
该包退出复检失败，已归档，不能作为最终交付。最终程序从干净源码提交
`1dd4248961a5cd4faa32ce7f8f08aeedd6794ba9` 重新构建，`source_dirty=false`。
随后仅更新交付文档和文档提交标识，应用及 DLL 字节保持不变。
构建环境为 Python 3.11.9 / PyInstaller 6.22.3；包内 `build-info.json` 区分
`source_commit` 与 `documentation_commit`，并记录构建时工作区状态。
最终 ZIP 精确字节数和 SHA-256 记录在本地 `artifacts/package-0.2.0.json`，
同名 `.sha256` 可直接校验；摘要没有放入 ZIP 内，避免文件自包含哈希。

解压运行时工作目录为 EXE 所在目录，清除了 PYTHONPATH、PYTHONHOME、VIRTUAL_ENV
及 Qt 路径覆盖，PATH 仅为 Windows/System32 与 Windows；不依赖源码目录或虚拟环境。
实际观察到普通/模拟器入口使用 WindowsInput 但未启用、仅提醒、attempted=false；
受限入口使用 RefusingInput 且 allow_real_input=false。三种入口关闭后 worker_running=false，
进程退出 0，无残留自身窗口。已检查 Qt qwindows/DLL、OpenCV、winsound、示例 F11、许可证，
EXE manifest 为 PerMonitorV2/asInvoker。构建 smoke test 也通过，仅使用假后端。

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

源码提交 `488321d3291859d218f701298c1ae72b6d569298` 已普通推送，随后重新读取远端 main 确认 SHA 一致。
初次检查 43 个文件，关闭回归加入后再次检查累计 44 个源码/测试/文档/依赖及许可证文件，
未发现密钥、令牌或个人路径；
没有上传二进制、日志、个人模板或截图，也没有发布 Release。退出修复与最终交付记录通过
后续普通快进提交同步；最终本地 HEAD / 远端 main 对照保存在 `artifacts/source-sync-0.2.0.json`。
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

## 2026-10-13 受限真实输入验收：工具覆盖层阻塞

本轮已获专用模拟器操作授权，普通入口和产品默认行为不变。源码为
`aff0d777ef43c2191e0f0c45183341419c6b5eba`；开始及封存前均核对远端 main 一致。
现有 ZIP 实测为 97167231 字节，SHA-256 为
`ba3dd6ff4f3d80c2265be3c8d845a066c908785bc6069d32523f7597ac126299`；
构建源码仍为 `1dd4248961a5cd4faa32ce7f8f08aeedd6794ba9`，完整解压文件与 ZIP 一致，未重打包。

以下证据路径以本地 `artifacts/real-input-20261013-batch1/source/` 为根，原始记录不上传。
`run` 指 `localappdata/auto-ready/simulator-check/runs/bd7610281f4d44b4b26c475bfc6ad8bd/`。

| 运行形态 | 场景 | 真实输入结果 | 证据与原因 |
| --- | --- | --- | --- |
| 源码 | A | 未执行 | `run/events.jsonl`：会话 `61769b969af94d448b2d3ee1c27c7891` 已启用，但可见性拒绝；发现后急停，未重试 |
| 源码 | B | 未执行 | `visibility-diagnostic.json`：已确认工具覆盖层阻塞，停止后续实机轮次 |
| 源码 | C | 未执行 | 同上；没有负向点击或成功判据证据 |
| EXE | A | 未执行 | 同上；仅核验产物身份，本轮未启动 EXE |
| EXE | B | 未执行 | 同上；没有活动会话平移证据 |
| EXE | C | 未执行 | 同上；没有负向点击或成功判据证据 |

源码实际使用 WindowsBackend / SimulatorBoundInput / WindowsInput。准备阶段通过原有界面
采集两份真实截图模板，单独进行 1 次成功画面采样点击（无活动会话），随后重置。
正式 A 的资格消耗、原生输入返回、Qt 回执、图像判据均为 0；摘要保持
`UNVERIFIED_NO_OS_RESULT`，不能用准备阶段回执替代正式证据。
只读诊断确认客户区 `(741,368) 1080×795 / DPI 144`、前台正常、非移动中、
按钮唯一匹配约 0.9999995；上方两个 `CodexComputerUseCursorOverlay` 窗口的边界
覆盖整个 2560×1600 桌面，因此 `unobscured=false`。这未证明产品缺陷，保留遮挡检查。
事件 133–136 记录急停、未启用及退出时 `worker_running=false`；窗口已关闭，
137 条原始事件无丢弃或写入错误。停止状态以这些原始事件核对，不以摘要保留的暂停状态代替。

未修改生产代码或新增测试。本轮复跑 197 项通过（9.15 秒），Ruff、pip check 通过；
结果在 `artifacts/real-input-20261013-batch1/pytest-results.xml`，不能替代实机验收。
人工接续的前置动作是结束电脑操作会话，使光标覆盖层消失；
之后再继续受限验收，新增证据仍写入相应隔离配置目录的 `runs/`。
六格验收尚未通过，真实游戏仍未验证。

## 2026-10-13 受限真实输入验收续测：源码 A 通过，其余待完成

受测源码提交为 `63c33f632bdbcec0821884eaa134874f378dc6fc`，应用代码未改，现有 ZIP 身份不变。
工具覆盖层消失后，使用只绑定本次进程身份的本地受限驱动操作原有界面；
正式点击仍由生产 Engine / WindowsInput 发出，未降低前台、遮挡、识别或新鲜度检查。
本节路径以本地 `artifacts/real-input-20261013-batch2/source/` 为根；
`run` 指 `localappdata/auto-ready/simulator-check/runs/1397bbf38a06421ba2afb16e1350f42a/`。

| 运行形态 | 场景 | 结果 | 证据与原因 |
| --- | --- | --- | --- |
| 源码 | A | 通过 | 明确获准补测的 `A-extra`；会话 `8f3ac897ed3b45738a6e95b06dd92426`，`run/events.jsonl`、`A-extra-preflight.*`、`A-extra-after.*` |
| 源码 | B | 未执行（输入前取消） | 会话 `dd5f7057b9cd446e8defd54e9f3102f1` 在活动轮次内平移，但驱动要求的精确位移与 UIA 实测不符，在生产输入前取消；`cases.jsonl`、`run/events.jsonl`。获准的 `B-extra` 尚未由驱动启用 |
| 源码 | C | 未执行 | GameViewer 全屏覆盖层仍在，按用户要求停止实机部分 |
| EXE | A | 未执行 | 现有便携包未开展本次真实输入轮次 |
| EXE | B | 未执行 | 无 EXE 活动轮次平移及真实输入证据 |
| EXE | C | 未执行 | 无 EXE 负向点击及超时判据证据 |

A-extra 的客户区为 `(741,368) 1080×795 / DPI 144`。唯一按钮连续确认后，
一次资格消耗产生一次 WindowsInput/SendInput 正常批次（3/3），点击点为 `(1278,930)`；
Qt 独立记录 spontaneous 按下、释放、点击各一次，关联方式为活动会话时间关联。
点击前成功候选为 0，之后新帧 5、6 连续匹配成功，终态成功；独立客户区截图也确认成功标志出现。
此前续测 A 因前台交接取消、零生产输入；该失败记录保留，不作为通过证据。

受限驱动已修正异步启动后的自身窗口前台交接，并将 B 自检改为记录实际平移、核对尺寸不变；
这是本地测试驱动调整，不是产品修复。两份模板的落盘哈希与 A 预检一致，沿用本次隔离配置。
另发现驱动记录之外的会话 `78fcaf7ae48948899765038555c93994`（一次生产输入），
以及先前终态之后的两次 Qt 点击回执（`late_after_terminal_ambiguous`）；用户已确认期间进行过手动操作。
这些额外记录不归入 B，不据此补足验收。停止后正常关闭本次控制窗口及模拟器，实际进程已退出；
事件 81–83 记录未启用、关闭及 `worker_running=false`，`run/summary.json` 已封存，
共 84 条事件、零丢弃、零写入错误。
累计记录 `ready` 按钮 Qt 点击回执 5 次：前批采样 1、A-extra 1、独立会话 1、终态后回执 2；
其中生产引擎输入共 2 次，正式通过仅 A-extra 一格；手动操作单独保留，不算补充 B。

后续只读预检确认目标在前台，但 `GameViewerPrivateWindowClass` 的可见窗口仍覆盖整个
2560×1600 桌面，`unobscured=false`。`visibility-112936.json`、`gameviewer-attributes.json`
保留遮挡及透明属性证据；鼠标穿透和色键配置不足以证明画面无遮挡，未修改安全门槛。
用户确认目前无法退出该覆盖层，并要求先停止实机部分；B-extra 未启用，未追加被测点击。
接续前需先移除覆盖层，再启动新的隔离验证进程并复核累计预算；不得将既有手动会话作为 B 验收。

本次只更新脱敏状态记录，原始日志、截图、模板、配置及驱动均留在 Git 排除目录；未重打包。
本次续测已全量复跑 197 项通过（9.27 秒），Ruff、pip check 通过；
JUnit 在 `artifacts/real-input-20261013-batch2/pytest-results.xml`，这些检查不替代剩余五格实机验收。
这仍不是“模拟器真实输入验收通过”，真实游戏始终未验证。

## 2026-10-13 覆盖层诊断与恢复条件：实机暂停未解除

开始与收尾前核对本地及远端 main 为 `d841062890b7a11fd0d87679c4ecdc19e624d326`。
生产代码与源码 A-extra 的受测提交相同，保留 A 的既有通过证据；原便携包未改、未重建。
本轮只读物理坐标快照仍发现可见的 `GameViewerPrivateWindowClass` 全屏窗口
（2560×1600），未发现此前的 `CodexComputerUseCursorOverlay`。
当前覆盖范围与历史客户区、按钮区域相交；这些历史矩形不作为新目标的预检结果。

所属进程的父进程关联到运行中的 GameViewer 服务；该服务登记的安装目录内，服务和
服务器文件均标识“网易UU远程服务”、版本 4.41.0.2311，且网易数字签名有效。
这支持其归属 UU 远程；运行进程的实际可执行路径查询被拒绝，未提权，不能把安装文件的
版本和签名当作运行中映像的完整验证。透明色键与 alpha=255 不证明目标区域全透明。
生产 `unobscured` 会在上方可见、未最小化、未 cloaked 窗口的矩形与保护区域相交时拒绝；
本轮未放宽此分支，也未将环境阻塞定性为产品缺陷。

[网易官方帮助](https://uuyc.163.com/help/20241203/40221_1197692.html)提供会话内入口：
远控界面右上角“控制中心 / 悬浮球 → 安全 → 被控端防窥模式 → 不启用”。
这是下一步可人工核对的具体设置；关闭防窥后，旁人可以看见被控端画面。
官方资料未将该窗口类映射到防窥功能，也未保证修改设置后当前连接不受影响，
所以尚不能确认该动作能移除本覆盖窗口，亦没有证据说明必须退出整个软件。

本轮仅尝试一次不带 `--allow-real-input` 的源码入口；因单实例锁已存在，
在输入适配器及新窗口构造之前退出。没有取得新目标身份、前台、几何/DPI、可见性和新鲜截图，
零输入预检结论为 **BLOCKED**，不能宣称已有新的 RefusingInput 运行通过。
诊断未发送鼠标或键盘输入，未操作第三方窗口或进程，也未重试启动。
原始诊断仅保存在被 Git 排除的 `artifacts/overlay-diagnosis-20261013/`。

剩余仍仅为源码 B/C、原 EXE A/B/C，本轮均不执行。
恢复后先用源码 `scripts/verify_simulator.py` 或原 EXE 的 `--verify-simulator` 默认拒绝输入入口
进行新的零输入预检；只有重新核实全部条件后，另行开始明确授权的真实轮次。
本地驱动位于 `artifacts/real-input-20261013-batch2/`；其旧进程已关闭，必须为新隔离批次
重新绑定新运行记录，不能沿用旧 HWND、截图、会话或覆盖封存证据。
源码 A 的证据仍在该目录的 `source/` 下，后续沿用原模板并将手动操作单独计数。

B 的本地驱动已补充生产客户区非零平移、尺寸/DPI 不变、同会话移动暂停及移动后
全新 1/3→2/3→3/3 候选链检查，并使用项目虚拟环境执行检查。仅运行对应 9 项纯数据用例、
导入检查和 PowerShell 语法检查，均通过；驱动实机行为未验证。未重跑历史 197 项全量测试。

## 2026-10-13 防窥关闭后的零输入复核：当次预检通过

用户关闭防窥后，新枚举未发现 GameViewer 或 Codex 覆盖层。经用户授权，正常关闭此前
最小化的正式版实例，确认进程退出、单实例锁释放；未强制结束进程。
新建隔离源码验证运行 `93081431891a4d4d97975816dde5ec15`，确认使用 RefusingInput，
目标身份、前台、稳定客户区 1080×795 / DPI 144、全客户区无遮挡及新截图均通过；
截图年龄约 31 ms，低于 350 ms 限值。5 条事件中轮次启动、输入尝试、系统输入结果均为 0。
验证窗口正常关闭，进程退出且 worker_running=false。此结果仅适用于当次观察，
不能复用其 HWND、截图或作为后续点击资格。源码 A 的既有结果保留，剩余五项真实验收未执行。
本地证据位于 `artifacts/overlay-recheck-20261013T123138Z/` 与对应 `artifacts/startup-checks/`；
未改生产代码、重跑全量测试或重建原包。

## 2026-10-13 模拟器真实输入六格验收通过

原始证据与独立复核均确认五个新增格子通过。模拟器六格全部通过；真实游戏仍未验证。源码 A 保留历史正式记录：运行 `1397bbf38a06421ba2afb16e1350f42a`、会话 `8f3ac897ed3b45738a6e95b06dd92426`。其余五格原始证据在 `artifacts/real-input-20261013-batch3/`，本地审计汇总为 `acceptance.json`：

| 运行形态 | 场景 | 结论与证据 |
| --- | --- | --- |
| 源码 | A | 通过；`artifacts/real-input-20261013-batch2/source/localappdata/auto-ready/simulator-check/runs/1397bbf38a06421ba2afb16e1350f42a/`；会话 `8f3ac897ed3b45738a6e95b06dd92426` |
| 源码 | B | 通过；`artifacts/real-input-20261013-batch3/source/localappdata/auto-ready/simulator-check/runs/4f7577a390474b03bbeb428e89f9ca8a/`；会话 `7858205de35c4f1aa0a49ac3c5db60cc` |
| 源码 | C | 通过预期负向验收；同上源码运行；会话 `4cea40160d8f46ddb8853fe25d6d2fb5` |
| EXE | A | 通过；`artifacts/real-input-20261013-batch3/exe/localappdata/auto-ready/simulator-check/runs/6d57edceb5d94a40a68dc77fa3edf90b/`；会话 `2d3f3bd05b6448e4aea7ade07718da17` |
| EXE | B | 通过；同上 EXE 运行；会话 `aa6c0c49b8a64fd79d975f0b690f92c7` |
| EXE | C | 通过预期负向验收；同上 EXE 运行；会话 `eb44c942d140450b823c0c11719ee14a` |

每格均为一次引擎尝试、一次 SendInput（3/3）及模拟器独立观察到的按下、释放、点击各一次。新增成功格的图像证据来自源码 B 新帧 10/11、EXE A 新帧 5/6、EXE B 新帧 16/17。B 在活动会话内平移客户区 `(+131,+104)`，尺寸仍为 1080×795、DPI 144；移动时没有输入，随后重新经过 1/3→2/3→3/3 新帧确认，输入点从 `(1278,930)` 随之变为 `(1409,1034)`。C 两格均只点击一次、不重试，未出现成功标志后以“结果未确认”超时：源码检查 26 个负向帧、5.110 秒；EXE 检查 25 个、5.047 秒。这是预期结果，不是验收失败。

所有格沿用源码 A 原始模板及参数；EXE A 使用该模板组，EXE B/C 沿用 EXE A 的模板组，均未新采 EXE 模板。每格预检均确认 `Ctrl+Alt+F11` 已注册。新增五格正式输入预算为 5/10，无正式额外尝试或重试；准备阶段 ready 点击为 0。此前手动与采样等记录在内的 ready 回执累计 10、引擎尝试累计 7；另有未启用状态下的键盘准备操作 54 对（108 个键盘事件），不计为 ready 点击。两次运行分别记录源码 83、EXE 105 条事件，丢弃、写入错误及未归属输入均为 0；窗口正常关闭，worker 停止，互斥锁及热键已释放。

EXE 身份未变：ZIP 97,167,231 字节，SHA-256 `ba3dd6ff4f3d80c2265be3c8d845a066c908785bc6069d32523f7597ac126299`，构建源码 `1dd4248961a5cd4faa32ce7f8f08aeedd6794ba9`，262 个解压文件均与 ZIP 匹配；EXE SHA-256 `7438e534f69acee59b34323d3f54cf13e869a3428acf90298081ef882825cf97`。源码代码与此前已测 `63c33f632bdbcec0821884eaa134874f378dc6fc` 相同；本地文档/验收基线为 `99cc9013a70f875a0fb7d6f4c260211b6f288ee1`。EXE A 准备时曾有重置/显示时序和名称检查问题，已在开始任何正式轮次或点击前纠正，不计为正式重试。未更改生产代码、未重跑全量 197 项测试或重建 EXE；受限证据与检查保存在上述 Git 排除的本地目录。

本轮私有预检补充了截图前的无遮挡检查及帧新鲜度记录；9 项几何检查用例、Python 语法和 PowerShell 解析检查通过。结果在 `focused-validation.json`，不能代替上述真实输入证据。
