# 跨平台家长友好版设计

**状态：** 设计已获用户确认，待实施前审阅  
**目标：** 在保持全程本地处理的前提下，让同一套 Python 工具同时支持 macOS 和 Windows，并降低没有脚本经验的家长的使用和求助成本。

## 1. 背景与目标

当前项目能够通过 Android 模拟器中的鑫时光集登录态调用接口，将用户自己的照片、视频和帖子文字导出到本机。现有代码主要按 macOS 环境编写，命令行入口对有经验的用户可用，但普通家长需要理解 Python、模拟器、ADB 和终端命令。

本阶段目标是先完成跨平台脚本版，而不是立即重做成桌面 GUI：

- 正式支持 Windows 11 64 位 Home/Pro；
- 尽力支持 Windows 10 22H2 64 位 Home/Pro；
- 保持现有 macOS 能力和命令兼容；
- 保留 Python 脚本和高级命令行入口；
- 增加普通用户可双击的检查、导出和求助入口；
- 让家长可以把脱敏诊断报告交给 AI 辅助排错；
- 照片、视频、帖子文字、登录态和媒体 URL 全程只在家长自己的电脑和模拟器内处理；
- 不建设服务器、不上传数据、不自动收集诊断日志。

本阶段不包含：桌面 GUI、云端任务队列、在线账号系统、自动上传、自动安装 MuMu、视觉识别方案和 Windows ARM/旧版 Windows 的正式支持。

## 2. 平台范围

| 平台 | 支持级别 | 说明 |
|---|---|---|
| Windows 11 64 位 Home/Pro | 正式支持 | 第一优先级，必须实机验证 |
| Windows 10 22H2 64 位 Home/Pro | 尽力支持 | 尽量兼容，不承诺长期维护 |
| 当前项目支持的 macOS + MuMu 环境 | 保持支持 | 现有行为不能退化 |
| Windows 7/8.1 | 不支持 | 不纳入测试与兼容分支 |
| Windows ARM64 | 第一阶段不支持 | 等 x64 链路稳定后再评估 |
| Windows Server/S 模式 | 不支持 | 不属于目标家长设备 |

Windows 10 22H2 仍可作为尽力兼容目标，但其 Home/Pro 官方支持已于 2025-10-14 结束；文档必须把它标记为尽力支持，而不是与 Windows 11 等价的正式承诺。

Python 最低版本统一为 3.10，推荐 3.11 或 3.12。源码使用 `str | None` 等 3.10 语法，因此 README 不得继续写 Python 3.9。

## 3. 总体架构

保持一套共享业务核心，把操作系统差异限制在平台适配层：

```text
共享业务核心
├── feed_api.py
│   ├── 登录态读取
│   ├── API 请求
│   ├── 翻页采集
│   ├── 照片/视频/文字解析
│   └── 下载结果统计
├── export_originals.py
│   ├── CLI 入口
│   ├── api 命令
│   ├── check 命令
│   ├── support 命令
│   └── 参数和退出码
├── platform_support.py
│   ├── macOS/Windows 识别
│   ├── MuMu 路径发现
│   ├── ADB 路径发现
│   ├── 设备连接检查
│   ├── 子进程调用
│   └── 打开输出目录
└── diagnostics.py
    ├── 检查结果类型
    ├── 稳定错误编号
    ├── 脱敏报告
    └── 用户可读建议
```

API、分页、URL 校验、文件命名、照片/视频下载等逻辑只实现一份。业务模块不得散落操作系统判断；平台模块对外提供统一接口，例如 `find_mumu()`、`find_adb()`、`list_devices()` 和 `open_output_directory()`。

平台适配必须使用 `pathlib.Path` 和参数数组调用子进程，不能依赖 shell 字符串拼接。Windows 需要覆盖带空格路径、中文用户名、`adb.exe`、PowerShell/CMD 差异、子进程退出码和中文输出编码；macOS 保留现有 MuMu 默认路径作为兼容回退。

## 4. 命令与用户入口

现有导出命令保持兼容：

```bash
# macOS
python3 tools/export_originals.py api --yes

# Windows
python tools\\export_originals.py api --yes
```

新增命令：

```text
check    只检查环境，不读取或上传照片，不执行下载
support  生成脱敏支持报告，不上传报告
api      执行现有 API 采集与下载
```

普通用户入口：

```text
Windows: check.bat / export.bat / support.bat
macOS:   check.command / export.command / support.command
```

高级用户保留直接执行 Python 命令的能力。双击入口只负责调用同一套 Python CLI，不复制业务逻辑。

建议退出码：

```text
0  成功
1  参数错误
2  环境未准备好
3  登录态未检测到
4  API/网络失败
5  部分下载失败
6  用户主动取消
```

## 5. 检查流程

`check` 按固定顺序执行：

1. 系统和 Python 版本；
2. MuMu 是否安装；
3. ADB 是否找到；
4. MuMu 是否运行；
5. Android 设备是否连接；
6. 鑫时光集 App 是否安装；
7. 登录配置是否存在；
8. 输出目录是否可写；
9. 可用磁盘空间是否足够。

MuMu 采用“官方安装引导 + 自动检测 + 手动指定路径”，第一阶段不由工具自动下载、安装或开启 Root。路径发现顺序为：用户指定路径、常见安装路径、PATH 中的 ADB、运行时设备发现，最后提示用户手动选择或填写。

检查失败时必须 fail-closed：停止继续猜测，返回稳定错误编号、家长可读说明、下一步建议和是否可重试信息。例如 `ADB_NOT_FOUND`、`DEVICE_NOT_FOUND`、`APP_NOT_INSTALLED` 和 `CREDENTIALS_NOT_FOUND`。

## 6. 支持报告与隐私边界

`support` 生成本地文本报告，供家长复制给 AI 或人工支持。报告可以包含：

- 工具版本；
- 操作系统类型和版本；
- Python 版本；
- MuMu 是否发现/运行；
- ADB 是否发现；
- 设备是否连接（不显示 serial）；
- App 是否安装；
- 登录态是否检测到（只显示是/否）；
- 输出目录是否可写（不显示完整私人路径）；
- 磁盘空间等级；
- 错误编号。

报告禁止包含：

- access token、Cookie、Authorization 头；
- childId、provider ID、设备 serial；
- 完整媒体 URL；
- API 响应正文；
- 帖子文字；
- 模拟器私有数据目录内容；
- 照片、视频或含姓名/园所信息的截图。

README 和报告模板要明确告诉家长：不要把上述敏感内容发给 AI。工具不自动上传报告、不自动收集使用统计。

导出数据流保持本地：从本机模拟器内存读取登录态，在内存中调用接口，在本机保存照片、视频和文字。默认输出目录调整为用户目录下的专门备份目录：

```text
Windows: %USERPROFILE%\\鑫时光集备份
macOS:   ~/鑫时光集备份
```

高级用户可以通过 `--output` 指定目录。项目内的 `build/` 继续作为开发/测试输出，不作为普通家长的默认照片目录。

## 7. 错误处理

错误分为四类：

```text
环境错误
登录错误
网络/API 错误
下载结果错误
```

每个错误包含稳定编号、用户可读说明、下一步建议、可否重试和是否需要重新登录。例如：

```text
[CREDENTIALS_NOT_FOUND]
没有检测到鑫时光集登录状态。
请在模拟器中打开鑫时光集并进入一次照片页面，然后重新运行 check。
```

下载部分失败时保留当前增量行为，明确显示成功、已存在和失败数量，并允许重新运行补齐；不把失败响应正文或完整 URL 写入日志。

## 8. 测试与验收

### 纯逻辑测试

macOS 和 Windows CI 都运行：

- URL 校验；
- childId 解析；
- API JSON 解析；
- 分页和去重；
- 文件命名；
- 下载重试；
- 错误编号；
- 脱敏报告。

### 平台适配测试

通过模拟路径和模拟子进程，不依赖真实 MuMu：

- Windows 和 macOS 路径；
- 中文路径和带空格路径；
- ADB 缺失；
- 无设备；
- 多设备；
- 子进程退出码；
- Windows 输出编码；
- MuMu 手动路径覆盖。

### 手工实机测试

至少验证 macOS、Windows 11 64 位和 Windows 10 22H2 64 位：

- MuMu 安装、运行和设备连接；
- App 安装和登录；
- 照片、视频、帖子文字完整导出；
- 中文输出路径；
- 中断后重试；
- 部分下载失败后的补齐；
- 未安装 MuMu、未连接设备和未登录状态的提示。

验收标准：macOS 现有功能不退化；Windows 11 可完成完整导出；Windows 10 尽力验证；错误有明确建议；support 报告无敏感数据；全程无数据上传。

## 9. 发布与文档

第一阶段以 GitHub Release ZIP 分享，不要求家长 clone 仓库。发布包必须不包含 `build/`、照片、视频、日志、Token、Cookie、临时 worktree 和 Python 缓存。

每个 Release 包含：

```text
README.md
QUICKSTART-WINDOWS.md
QUICKSTART-MACOS.md
check.bat / export.bat / support.bat
check.command / export.command / support.command
tools/
```

README 首屏只提供最短路径，详细错误和高级 CLI 用法后置。每个版本记录版本号、支持平台、Python 版本、已知限制、更新内容和 SHA-256。早期不做自动更新、在线版本检查或使用统计。

## 10. 分阶段实施顺序

1. 抽取跨平台平台适配层，修正 Python 最低版本文档；
2. 增加 `check` 和结构化错误编号；
3. 增加 `support` 脱敏报告；
4. 增加 Windows/macOS 双击入口；
5. 增加平台模拟测试和 CI 矩阵；
6. 在 Windows 11 和 Windows 10 22H2 实机验证 MuMu 链路；
7. 更新 README 和快速开始文档；
8. 发布第一个跨平台脚本版 Release；
9. 根据真实用户反馈决定是否进入桌面 GUI 阶段。

桌面 GUI、`.exe` 打包、在线服务、视觉识别和 Windows ARM 支持全部后置，不能阻塞跨平台脚本版。
