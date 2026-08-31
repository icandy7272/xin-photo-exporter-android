# 鑫时光集内容导出工具

把你自己「鑫时光集家长版」账号里的**全部照片、视频、帖子文字**，一次性备份到你的 Mac 电脑。全程只读**你自己的账号**、只在本机处理，**不上传到任何地方**。适合想长期保存孩子成长记录、担心 App 哪天停用就丢了内容的家长。

> ⚠️ 仅用于导出**你自己账号**能看到的内容，用的是你在 App 里的登录状态。请勿用于他人账号。
> 老师代家长导出请先看 [七、隐私与安全](#七隐私与安全)。

**不需要会编程**，跟着下面步骤复制粘贴即可。全程约 20–40 分钟（大头是下载相册的时间）。

---

## 一、需要准备什么

| 需要 | 说明 |
|---|---|
| 一台 **Mac 电脑** | macOS 系统。**目前仅支持 Mac**（Windows 暂不支持）。 |
| **安卓模拟器** | 一条命令自动装好（免费，见步骤 3）。**不需要 MuMu**——它的 Mac 版现在要订阅。 |
| **鑫时光集家长版** 的 apk | 装进模拟器用，见步骤 4。 |
| **Python 3** | macOS 一般自带；下面教你确认。**不需要安装任何额外的库。** |
| 硬盘空间 | 模拟器约 6 GB（一次性），加上相册本身几百 MB 到几 GB。 |

> **模拟器只在最开始用一次**，就是让工具读一次 App 的登录信息。之后全程是你的 Mac 直接连服务器下载，模拟器可以关掉。

---

## 二、安装步骤

### 步骤 1：确认 Python 3

1. 打开「**终端**」（Terminal）：访达 → 应用程序 → 实用工具 → 终端。
2. 输入下面这行，回车：
   ```bash
   python3 --version
   ```
3. 看到 `Python 3.x.x`（3.9 及以上均可）就说明有了，跳到步骤 2。
4. 如果提示找不到 python3，运行下面这行安装（会弹窗，点「安装」）：
   ```bash
   xcode-select --install
   ```
   装完再执行一次 `python3 --version` 确认。

### 步骤 2：下载本工具

**方式 A（会用 git 的话，推荐）**：终端里运行
```bash
git clone https://github.com/icandy7272/xin-photo-exporter-android.git
cd xin-photo-exporter-android
```

**方式 B（不用 git）**：
1. 浏览器打开 `https://github.com/icandy7272/xin-photo-exporter-android`
2. 点绿色 **Code → Download ZIP**，下载后解压。
3. 终端里输入 `cd `（cd 后面有个空格），把解压出来的文件夹拖进终端窗口，回车进入该目录。

### 步骤 3：装模拟器（一条命令）

工具需要从 App 里读一次登录信息，这需要一个有 **Root 权限**的安卓环境。
下面这条命令会替你装好**免费**的安卓官方模拟器并启动它：

```bash
python3 tools/setup_emulator.py
```

第一次运行要下载约 4.5 GB（Java、安卓工具、系统镜像），大概十几分钟。
中途会问你几次**是否接受 Android 的许可协议**，看到 `Accept? (y/N):` 输入 `y` 回车即可。

装好后会自动弹出一个安卓手机窗口，界面是中文的。

> **不需要 Homebrew，也不需要管理员密码。** 所有东西装在 `~/.xin-exporter/` 里，
> 删掉这个文件夹就等于卸载干净（模拟器另存在 `~/.android/avd/`，见步骤 5 的清理命令）。

> **为什么不用 MuMu？** MuMu 的 Mac 版现在是订阅制。这个脚本用的是安卓官方模拟器，免费。
> 如果你**已经装了 MuMu 并且能打开**，也可以继续用它（在 MuMu 设置里打开 Root 权限），
> 工具两种都支持，会自动认出来。

<details>
<summary>已经有安卓设备？（点开看）</summary>

- **已经装了 Android Studio**：脚本会自动复用它的 SDK 和 Java，不重复下载。
- **已经 root 过的安卓手机**：手机上开启「开发者选项 → USB 调试」，数据线连上 Mac 就行，
  不用跑上面的脚本。工具会自动通过 `su` 读取。**没有 root 的手机不行**——系统不允许读 App 的私有数据。
- **同时连了好几台**：工具会报 `ambiguous-device`，用 `--serial` 指定一台，
  或者只留一台。`adb devices` 可以查看当前连着哪些。

</details>

### 步骤 4：装 App 并登录

1. 拿到**鑫时光集家长版的 apk 安装包**，把它**直接拖进模拟器窗口**即可安装。

   > apk 从哪来？最省事的办法是**找个已经有这个 apk 的人直接发给你**。
   > 如果你自己的安卓手机上装着这个 App，也可以开启「开发者选项 → USB 调试」后连上 Mac，
   > 用 `python3 tools/setup_emulator.py --apk 那个文件.apk` 一并安装。

2. 在模拟器里打开鑫时光集，用**你自己的账号登录**。
3. 进入**照片 / 成长记录**页面浏览一下，确保内容能正常加载。之后保持登录状态。

---

## 三、开始导出

先确认三件事：**模拟器开着、鑫时光集已登录、你已在工具目录里**，然后运行：

```bash
python3 tools/export_originals.py api --yes
```

会看到类似这样的输出：
```
定位最新游标中（二分探测）…
最新游标：2392xxx
直连 API 采集中（翻页拉取，不经模拟器渲染）…
已采集：120 帖 / 350 原图
...
采集结束：560 帖，1426 原图，22 视频。
帖子清单：.../build/moments.jsonl（560 条）
正文文本：.../build/captions.txt（190 条有正文）
已确认下载（--yes）：1426 原图，22 视频
结果：下载 1426，已存在 0，失败 0，...
视频结果：下载 22，已存在 0，失败 0
本轮媒体下载完成。
```

跑完就好了。**再次运行是安全的**——已下载的会自动跳过，只补新增的（比如你以后又发了新帖，重跑一次即可增量补齐）。

> **模拟器可以提前关掉。** 工具只在最开始读一次登录信息，之后全是你电脑直接连服务器下载。
> 看到 `账号下共 N 个孩子档案，一起拉取。` 这行，就可以关掉模拟器了，省下的内存反而让下载更快。
> （若中途失败要重跑，需要重新打开模拟器。）

> **换账号登录后重跑，内容会合并在同一个 `build/` 里**（文件名只有日期和哈希，不区分账号）。
> 想分开保存，用下面的 `--out` 指定不同目录。

### 一个账号下有多个孩子：分开导出

不加任何选项时，账号下**所有孩子的内容会混在一起**下载到 `build/`——文件名只有日期和哈希，事后分不出谁是谁。要分开保存，先列出档案 ID：

```bash
python3 tools/export_originals.py api --list-children
```

```
账号下共 2 个孩子档案：
  1. 11111111-2222-3333-4444-555555555555
  2. 99999999-8888-7777-6666-555555555555
用 --child-id <ID> 单独导出其中一个；档案 ID 属于个人信息，请勿外发。
```

然后一个孩子跑一次，各自指定输出目录（目录名自己起，比如孩子的名字）：

```bash
python3 tools/export_originals.py api --child-id 11111111-2222-3333-4444-555555555555 --out ~/Desktop/导出/小明 --yes
```

每个 `--out` 目录都是完整独立的一份（`originals/`、`videos/`、`captions.txt`、`moments.jsonl`），可以直接整个文件夹发给对应的家长。

> 哪个 ID 对应哪个孩子？先随便挑一个跑，跑完打开 `captions.txt` 或 `originals/` 看一眼内容就知道了，然后把目录改成对应的名字。

### 拿到 token 后可以完全不用模拟器

登录信息（token）在过期前一直有效。如果你把它存进一个文件，之后重跑就**完全不需要开模拟器**：

```bash
python3 tools/export_originals.py api --token-file ~/xin-token.txt --child-id <孩子ID> --out ~/Desktop/导出/小明 --yes
```

也可以用环境变量 `XIN_ACCESS_TOKEN` 代替 `--token-file`。

> **不要**把 token 直接打在命令行上——命令行会进终端历史记录，同一台电脑上的其他程序也看得到。用文件或环境变量。
> token 文件等同于账号密码，别外发、别提交到 git；不用了就删掉。token 过期后重新开一次模拟器即可。

### 用完之后：清除模拟器里的登录信息

导出完了就把模拟器删掉，里面的登录状态会一并清除：

```bash
python3 tools/setup_emulator.py --delete
```

已经导出到 `build/`（或 `--out` 目录）的照片不受影响。以后要再导，重跑一次
`python3 tools/setup_emulator.py` 即可——下载过的东西会跳过，几十秒就好。

想彻底卸载模拟器相关的东西：删掉 `~/.xin-exporter/` 文件夹。

---

## 四、导出的东西在哪

默认都在工具目录下的 `build/` 文件夹里（用了 `--out` 就在你指定的目录里）：

| 位置 | 内容 |
|---|---|
| `build/originals/` | 全部**原图照片**（文件名 `日期_哈希.jpeg`，按记录日期命名）|
| `build/videos/` | 全部**视频**（`.mp4`）|
| `build/captions.txt` | 帖子**正文文字**，可读，格式 `[时间] 正文` |
| `build/moments.jsonl` | 每条帖子一行（正文 + 对应的照片/视频文件名，方便按帖子对应）|

照片的「创建 / 修改日期」会被设为帖子当天中午，方便你在访达里按时间排序。

---

## 五、常用选项（一般不用改）

| 选项 | 作用 |
|---|---|
| `--yes` | 不弹确认、直接下载（**推荐加上**）。不加则会先让你输入 `DOWNLOAD` 确认。 |
| `--no-videos` | 只下照片、不下视频（正文照样保存）。网络差时可先只导照片，视频等网好再单独跑。 |
| `--out <目录>` | 输出到指定目录（默认 `build/`）。**多个孩子分开导出时必用**。 |
| `--list-children` | 只列出账号下的孩子档案 ID 后退出，不下载任何东西。 |
| `--child-id <ID>` | 只导出指定的孩子，可重复写多个。不填则导出账号下全部。 |
| `--token-file <文件>` | 从文件读登录 token，配合 `--child-id` 可完全不用模拟器。也可用环境变量 `XIN_ACCESS_TOKEN`。 |
| `--counter <数字>` | 手动指定起始位置，兜底用，**一般不需要**（默认自动定位到最新）。 |
| `--workers <数字>` | 同时下载几个文件，默认 `4`（上限 16）。网络好可调到 `8` 再快些；网络不稳或被限速就调到 `2`。 |

连接安卓设备的选项，**一般不用填**（工具会自动找）：

| 选项 | 作用 |
|---|---|
| `--adb <路径>` | 指定 adb 程序。默认按此顺序找：`ANDROID_SDK_ROOT`/`ANDROID_HOME` → Android Studio 默认 SDK 目录 → 系统 PATH → MuMu 自带的那个。 |
| `--serial <序列号>` | 同时连了多台设备时指定用哪台。`adb devices` 可以查看。 |
| `--package <包名>` | App 包名，默认 `com.childfolio.family`。 |

只想先看看有多少、暂不下载：去掉 `--yes` 运行，看到候选数量后**直接按回车**即可取消，不会下载任何东西。

---

## 六、遇到问题怎么办

工具失败时会打印一行 `失败：xxx`，对照下表处理：

| 提示 | 原因 / 解决 |
|---|---|
| `credentials-not-found` | **最常见**。App 没装、没登录，或还没进过相册页 → 在模拟器里打开鑫时光集、用自己账号登录、进一次照片页再重试。 |
| `no-device` | 模拟器没开 → 重新运行 `python3 tools/setup_emulator.py` 启动它，等它进入桌面。 |
| `prefs-read-failed` | 设备不肯交出 App 的私有数据，**是 Root 权限问题**（跟没登录不是一回事）→ 用 `setup_emulator.py` 装的模拟器不会出这个；若你自己建了模拟器，多半是选了 Google Play 镜像（必须选 **Google APIs**）；MuMu 要在设置里开 Root 权限；真机必须已 root。 |
| `ambiguous-device` | 同时连了多台设备（比如模拟器 + 手机）→ 只留一台，或用 `--serial` 指定。`adb devices` 可查。 |
| `adb-not-found` | 找不到 adb → 先跑一次 `python3 tools/setup_emulator.py`；已装 Android Studio 的可用 `--adb ~/Library/Android/sdk/platform-tools/adb` 指定。 |
| `mumu-not-running` | 用 MuMu 时它没开、或安卓没启动完 → 打开 MuMu，等进入安卓桌面再运行。 |
| `invalid-child-id` | `--child-id` 写错了 → 必须是 `--list-children` 打印出来的那一长串带横线的 ID，整串复制。 |
| `token-file-in-repository` | token 文件放在工具目录里了 → **挪到外面**（比如 `~/xin-token.txt`）。放在仓库里一旦 `git add` 就会永久泄露。 |
| `token-file-unreadable` / `token-file-empty` | `--token-file` 指的文件不存在或是空的 → 检查路径，或去掉这个选项改用模拟器。 |
| `image-not-rootable` | 系统镜像不是 Google APIs 的 → 删掉模拟器重装：`python3 tools/setup_emulator.py --delete` 后再跑一次。 |
| `download-failed:xxx` | 装模拟器时网络中断 → **直接重跑** `python3 tools/setup_emulator.py`，已下好的部分会跳过。`403` 多为网络代理干扰。 |
| `api-not-200` / `api-request-failed` | 网络问题或登录过期 → 检查网络；在 App 里**重新登录一次**换新登录态；再重试。 |
| 卡住 / 下载很慢 | 多为网络原因，耐心等或换网络；随时按 `Ctrl-C` 停止，已下的会保留，之后重跑续下。也可以试试调小 `--workers`（如 `--workers 2`），有些网络对并发敏感。 |
| 结尾出现 `失败 N` | 下面会跟一行 `失败原因：xxx × N`。`download-failed` / `http-not-200` 多是网络问题，**重跑一次通常就好**；其他原因重跑无用，请提 issue。 |
| 设备掉线 / `device offline` | 重启一次模拟器再运行。 |

---

## 七、隐私与安全

- **只导出你自己账号**的内容，用的就是你在 App 里的登录状态，不碰别人的账号。
- **老师 / 园所代为导出时**：导出范围仅限你的账号本来就能看到的内容；请按孩子分目录（`--child-id` + `--out`），**只把每个孩子的目录发给他自己的家长**，发完及时清理本机副本。孩子的照片属于个人信息，转交前请确认家长知情同意，并遵守园所的相关规定。
- 登录凭证（token）只在你电脑内存里用来调用**你自己账号**的接口，**绝不打印、不保存、不上传**。
- **token 等同于账号密码，甚至更危险**：拿到它就能以你的身份读取全部内容，**不需要密码、不需要短信验证码**。一个账号一个 token，覆盖该账号下的所有孩子。
  - **绝对不要提交到 GitHub。** Git 会永久记住——事后删文件没用，历史里还在，别人克隆走的那份也追不回来。工具会拒绝读取放在本仓库目录里的 token 文件（报 `token-file-in-repository`）。
  - 不要写在命令行参数里（会进终端历史），用 `--token-file` 或环境变量 `XIN_ACCESS_TOKEN`。
  - 万一泄露了：**立刻在 App 里退出登录再重新登录**，旧 token 随即作废。只删文件是没用的。
- 导出的照片 / 视频 / 文字**只存在你电脑本地** `build/` 目录，工具不向任何服务器上传内容。
- `build/` 已在 `.gitignore` 中，即使你用 git，也不会把私人内容传到网上。
- 不要把 `build/`、完整媒体 URL、请求日志、模拟器数据目录、截图或包含 `childId`/园所标识的配置文件上传到 GitHub、Issue、网盘或聊天工具。
- 测试代码只使用 `example.invalid` 和虚构 ID；真实 childId、provider ID、Cookie、access token 和 Authorization 头都不得写入源码、测试夹具或提交历史。
- 如果登录态曾出现在日志、截图或提交中，应立即在鑫时光集里退出并重新登录；仅仅删除本地文件不能撤回已经复制出去的凭证。
- 本工具使用的是 App 的**非官方接口**（通过技术手段分析得到）。App 若大改版本，接口可能失效。

---

## 八、给开发者（可选）

- **无第三方依赖**，纯 Python 标准库，无需 `pip install`。
- 运行测试：`python3 -m unittest discover -s tests -t .`（190 项，全部离线，不碰真实设备或网络）
- 接口若失效需重新分析，见 [`docs/mitm-capture-runbook.md`](docs/mitm-capture-runbook.md)。
- 模块划分：`device.py` 找 adb / 选设备 / 读 prefs；`credentials.py` 把设备与命令行参数汇成 token+childIds；`feed_api.py` 翻页与下载；`setup_emulator.py` 一键装模拟器。
- 设备层与模拟器品牌无关：读 prefs 时依次尝试直接 `cat`、`adb root` 后重试、`su -c`，因此 Google APIs 镜像、MuMu 和已 root 真机走同一条代码路径。
- 两类失败必须分清：**读不到** `/data/data`（Root/镜像问题）报 `prefs-read-failed`；**读到了但没有登录态**报 `credentials-not-found`。判据是 prefs XML 的根元素 `<map>`——不能用 `<string>`，App 装了没登录时 prefs 里可能一个字符串都没有。
- `setup_emulator.py` 固化了三个实测踩过的坑：必须 `google_apis` 镜像（`google_play` 禁用 root）、必须删掉 `disk.dataPartition.path=<temp>`（否则重启丢登录态）、locale 设 `zh-CN`（默认英文 + 区号 +1）。
- 工作原理：从 App 的 `shared_prefs` 读取登录态，翻页调用后端 `moment/FamilyMoment/v2/getPageMomentList` 接口（`counter` 游标 + `hasMore` 分页），抽取每条帖子的正文、照片（`pictureURLs`）、视频（`videoUrl`），交给本地下载器。只拉 JSON、不渲染图片，低内存、可导全库。
