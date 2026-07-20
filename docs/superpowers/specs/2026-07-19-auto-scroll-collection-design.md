# 设计：自动滚动采集（auto-scroll）

**目标**：在批量采集阶段，用 `adb shell input swipe` 自动滚动相册列表，替代人工滚动，并在「连续若干次滑动都没有新候选」时自动停止。让用户不必手动滚一遍大相册。

**约束**：最小开发成本、第一版可用即可、不与 codex 的 `prepare` 重叠、严守既有隐私红线。

**分支归属**：`claude/new-branch-other-features-d55914`，独立于 codex 的 `agent/batch-prepare-design`。

---

## 1. 与 codex 的边界

- codex 负责 **prepare**：force-stop → 删缓存 → 清 logcat → 重启，让全库照片在重新滚动时能再次吐 URL。
- 本功能负责 **自动滚动**：驱动列表滚动 + 采集 + 自动停止。
- 两者天然配套：`prepare`（清缓存）→ 自动滚动（驱动全量重新加载）→ 采集 → 确认下载 = 全自动全库导出。但本版本**不依赖也不调用** prepare，二者各自是独立的 CLI 能力，合并时只在 `run_batch` 的采集分支处交汇，冲突面小。
- codex 设计文档 §12 已明确 prepare 不做自动滚动/覆盖判定/批次上限——本功能正是被划出的「其他功能」之一。

## 2. 采集机制：轮询 dump，而非常驻线程

现有手动采集 `collect_streaming_urls` 用常驻 `adb logcat` 流 + Ctrl-C 停止。自动滚动需要「滑动 → 等待加载 → 采集 → 决定是否继续」的节奏，与滑动强耦合。

采用**轮询式**：每次滑动后 `adb logcat -d`（dump 当前环形缓冲）→ `extract_urls` → 并入本地去重集合。相比常驻线程：

- 无多线程、无并发竞态，**更简单、更好测**（沿用既有 `run_command` 注入 mock）。
- dump 紧跟在滑动 + settle 之后进行，单次间隔内新增日志量小，环形缓冲被挤掉的风险可忽略。

代价：实时性略弱于流式；极端高频日志下理论上可能丢行。第一版可接受，作为已知限制登记。

## 2.1 停止判据：滚到底（截屏哈希），而非「无新候选」

**真机验证（2026-07-19）发现的关键问题**：最初用「连续 N 次滑动无新候选就停」做停止判据是错的——第 1 页已缓存的照片在启动时就吐过 URL，滑过它们不产生新候选，于是自动滚动会在**还没滑到第 1 页底部、还没触发下一页加载**时就误判「到头了」而停下。

正确的「是否结束」信号是**列表能否继续滚动**，而非「有没有新 URL」。故改为：每次滑动 + settle 后对屏幕 `adb exec-out screencap -p` 截图、在内存里算 sha256；当**连续 `max_stable_screens` 次画面不再变化**时判定到底并停止。

- 与「缓存挡 URL」解耦：无论照片缓存与否，都能滚到真正的底部；沿途触发后续未缓存页加载并采集，因此**即使不 prepare 也能采到第 2 页及以后**（第 1 页已缓存的那批仍需 prepare 补齐）。
- 隐私：截图字节只在内存里算一次哈希即弃，**绝不写盘/打印/外传**，只留 32 字节不可逆摘要，符合「照片只在本机处理」。
- 兜底：`max_swipes` 安全上限防止（截图持续失败或画面持续有动效时）无限滑动；截图失败返回 `None`，不会被误判成「稳定/到底」。

## 3. 新增函数（纯逻辑优先，便于测试）

- `get_screen_size(device)`：解析 `adb shell wm size`，优先取 `Override size`，否则 `Physical size`；解析失败或非正数 → `SmokeError("screen-size-failed")`。
- `swipe_scroll(device, width, height)`：`adb shell input swipe cx y_start cx y_end duration`。`cx=width//2`，`y_start=0.75H`，`y_end=0.30H`（每次约 45% 屏幕位移，屏幕间保持 >50% 重叠，尽量不跳过整行导致漏图）；非零返回码 → `SmokeError("swipe-failed")`。
- `dump_logcat_urls(device)`：`adb logcat -d -v brief`（**不绑定 pid**，与既有流式采集一致，以兼容重启/全量），返回 `extract_urls` 结果；非零返回码 → `SmokeError("logcat-failed")`。
- `capture_screen_signature(device)`：`adb exec-out screencap -p`（二进制），返回截图字节的 sha256 摘要；失败或空 → `None`。字节只在内存里算哈希即弃。
- `collect_with_auto_scroll(device, ..., max_stable_screens=6, max_swipes=2000, settle_seconds=2.5)`：先 dump 一次抓取启动缓冲并截一次基线图；随后循环「滑动 → sleep(settle) → dump 合并 → 截图算哈希」；画面与上次相同则累加稳定计数，否则清零；连续 `max_stable_screens` 次画面不变才判定到底停止；`max_swipes` 为安全上限。全程可 Ctrl-C 提前结束并返回已采集部分。返回**有序去重**列表。
  - **阈值取值（真机调优）**：初版 `max_stable_screens=2` 太松——列表中途一次偶发「滑动没动/加载中静止帧」就凑成 2 帧相同、误判到底（真机上分别停在 534、664，均非真底）。提高到 6（配合 2.5s settle）后，只有画面持续约十几秒不变才停，滤掉中途偶发相同。

## 4. CLI 集成

- `batch` 子命令新增 `--auto-scroll` 开关（默认关闭，手动路径保持不变，向后兼容）。
- `run_batch(auto_scroll=False)`：`auto_scroll=True` 时走 `collect_with_auto_scroll`，否则维持 `collect_streaming_urls`。
- 采集结束后仍复用既有的 `DOWNLOAD` 确认门 + `download_batch`，**不改动下载与确认逻辑**。

## 5. 隐私

- 所有新命令行参数只含坐标/尺寸/常量，**不含 URL**；进度只打印计数；dump 结果只在内存中 `extract_urls` 后即抛弃原文。测试对每条 argv 断言无 `https://`。

## 5.1 真机重大发现（2026-07-19，大相册）

手动 + 自动滚动一个跨 2023–2024+ 的大相册时观察到：

1. **越滑越卡、加载越来越慢，最后 App 崩溃（OOM）**——手动滚同样崩，说明是 **App/模拟器侧内存上限**，非自动滚动之过。
2. **滚动位置回弹 bug**：加载完新一页后视图跳回更靠前的位置（如 2024-01 加载后跳回 2024-04），需再滑回触发点才会加载下一页，加载后又跳回。URL 由 logcat 采集、与画面位置无关，故回弹不致命；**致命的是崩溃**。

**结论**：对大相册，任何「滚到底」策略（截屏哈希 / 结尾标记）都无效——**App 在到底前就崩**，最旧的照片藏在崩溃墙后，UI 滚动可能根本够不到。瓶颈是 App 内存，不是停止判据。

**候选缓解方向**（待与用户确认）：加大 MuMu 分配内存（用户侧设置，可能把崩溃点往后推甚至到底）；接受「能采多少采多少」的分块现实；或研究非 UI 的 API 分页路径（大改，另议）。自动滚动对**中小相册**仍有效（真机曾采到 664+）。

## 5.2 非-UI API 路径调研（2026-07-20，为绕开崩溃墙）

结论供后续「全库导出」参考：

- 照片 feed 是 App **原生页面**（APK 无 RN/Flutter bundle），数据来自 App 调 `api-gateway.childfolio.net`；`web.childfolio.net` 是**老师版 + 共享聊天 SPA**，家长照片**无网页版可绕**。
- API **鉴权为 Bearer token**（`Authorization: Bearer <accessToken>`），**无逐请求签名**——直连可行且轻量。`accessToken`、`album_child_id` 存于 App `shared_prefs`。
- 已跑通**照片冲印**接口：`GET /frame/phone/getlistterm?childId=<uuid>` 返回学期列表（字段 id/name/startDate/endDate/url），但这是冲印功能、**非成长 feed**（本账号基本为空）。
- **成长 feed 的确切接口未定位**：`family/*`、`getpagelistalbum`、album 域名等前缀均 404；App 不把 API 调用打进 logcat。可靠定位需 **MITM 抓一次真实请求**。
- **边界**：本环境安全层拦截「启动拦截代理 / 读 token / MITM 分析」，故 MITM **须由用户在本机自行完成**；拿到接口规格（endpoint+参数+头）后再据此写直连导出器（纯编码、不涉及拦截）。

**结论**：auto-scroll（UI）作为中小相册的 v1；**全库导出留作独立后续**，依赖用户侧 MITM 得到 feed 接口规格。

## 6. 已知限制（第一版有意取舍）

- 到底判据依赖「画面不再变化」；持续动画/加载动效理论上可能让它多滑几次（`max_swipes` 兜底），截图连续失败时不会误判到底、而是滑到上限。
- 轮询 dump 理论上存在环形缓冲挤出风险（见 §2）。
- 采集仍不可跨运行续采（与既有隐私约束一致，沿用现状）。
- 已缓存照片滑过不吐 URL，需先 prepare（清缓存）再自动滚动才能补齐——这半属 codex 的 prepare。

## 7. 测试计划（TDD）

`get_screen_size`（解析/优先级/失败）、`swipe_scroll`（argv/坐标/失败/无 URL）、`dump_logcat_urls`（提取/argv 不绑定 pid/失败）、`capture_screen_signature`（哈希/绝不返回原始字节/失败返回 None）、`collect_with_auto_scroll`（画面稳定即停+沿途采集/画面变化重置/初始缓冲/Ctrl-C 返回部分/max_swipes 兜底/截图失败不早停/无 URL）、CLI（`--auto-scroll` 分发）。目标覆盖沿用仓库现有粒度。
