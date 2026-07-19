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

现有手动采集 `collect_streaming_urls` 用常驻 `adb logcat` 流 + Ctrl-C 停止。自动滚动需要「滑动 → 等待加载 → 判断是否有新候选 → 决定是否继续」的节奏，与滑动强耦合。

采用**轮询式**：每次滑动后 `adb logcat -d`（dump 当前环形缓冲）→ `extract_urls` → 并入本地去重集合。相比常驻线程：

- 无多线程、无并发竞态，**更简单、更好测**（沿用既有 `run_command` 注入 mock）。
- dump 紧跟在滑动 + settle 之后进行，单次间隔内新增日志量小，环形缓冲被挤掉的风险可忽略。

代价：实时性略弱于流式；极端高频日志下理论上可能丢行。第一版可接受，作为已知限制登记。

## 3. 新增函数（纯逻辑优先，便于测试）

- `get_screen_size(device)`：解析 `adb shell wm size`，优先取 `Override size`，否则 `Physical size`；解析失败或非正数 → `SmokeError("screen-size-failed")`。
- `swipe_scroll(device, width, height)`：`adb shell input swipe cx y_start cx y_end duration`。`cx=width//2`，`y_start=0.75H`，`y_end=0.30H`（每次约 45% 屏幕位移，屏幕间保持 >50% 重叠，尽量不跳过整行导致漏图）；非零返回码 → `SmokeError("swipe-failed")`。
- `dump_logcat_urls(device)`：`adb logcat -d -v brief`（**不绑定 pid**，与既有流式采集一致，以兼容重启/全量），返回 `extract_urls` 结果；非零返回码 → `SmokeError("logcat-failed")`。
- `collect_with_auto_scroll(device, ..., max_idle_swipes=4, settle_seconds=1.5)`：先 dump 一次抓取启动缓冲；随后循环「滑动 → sleep(settle) → dump 合并」；有新候选则重置空转计数，否则累加；连续 `max_idle_swipes` 次无新候选即停止。全程可 Ctrl-C 提前结束并返回已采集部分。返回**有序去重**列表。

## 4. CLI 集成

- `batch` 子命令新增 `--auto-scroll` 开关（默认关闭，手动路径保持不变，向后兼容）。
- `run_batch(auto_scroll=False)`：`auto_scroll=True` 时走 `collect_with_auto_scroll`，否则维持 `collect_streaming_urls`。
- 采集结束后仍复用既有的 `DOWNLOAD` 确认门 + `download_batch`，**不改动下载与确认逻辑**。

## 5. 隐私

- 所有新命令行参数只含坐标/尺寸/常量，**不含 URL**；进度只打印计数；dump 结果只在内存中 `extract_urls` 后即抛弃原文。测试对每条 argv 断言无 `https://`。

## 6. 已知限制（第一版有意取舍）

- 停止判据是「连续 N 次滑动无新候选」的启发式，**不是**严格的「已到列表底部」证明；快速滑动仍可能漏个别图（可调大 settle 或减小滑动距离作后续迭代）。
- 轮询 dump 理论上存在环形缓冲挤出风险（见 §2）。
- 采集仍不可跨运行续采（与既有隐私约束一致，沿用现状）。

## 7. 测试计划（TDD）

`get_screen_size`（解析/优先级/失败）、`swipe_scroll`（argv/坐标/失败/无 URL）、`dump_logcat_urls`（提取/argv 不绑定 pid/失败）、`collect_with_auto_scroll`（空转停止/重置/初始缓冲/Ctrl-C 返回部分/无 URL）、CLI（`--auto-scroll` 分发）。目标覆盖沿用仓库现有粒度。
