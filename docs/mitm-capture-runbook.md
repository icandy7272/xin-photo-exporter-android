# MITM 抓包操作单：拿到「成长 feed」接口规格

目的：抓一次家长 App 向 `api-gateway.childfolio.net` 发的**成长 feed 请求**，看清确切
**接口路径 + 参数 + 头 + 返回里原图 URL 的字段名**。拿到后交给 Claude，即可写「直连
API 导出器」（低内存拿全库、绕开 UI 崩溃）。

> 全程在你本机、你自己的模拟器与账号上进行。**装 CA 证书是安全设置改动，由你亲手执行**。
> mitmproxy 已 `brew install`。完事务必按 §5 还原（撤代理 + 删 CA），别把 MITM 证书留在系统里。

前置变量（每个终端先跑一遍）：

```bash
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
ADB="/Applications/MuMuPlayer.app/Contents/MacOS/MuMuEmulator.app/Contents/MacOS/tools/adb"
SER=127.0.0.1:16384
"$ADB" connect "$SER"
```

## 1. 启动 mitmproxy（生成 CA + 开抓包）

新开一个终端窗口，前台跑（保持开着）：

```bash
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
mitmweb --listen-host 0.0.0.0 -p 8080
```

- 第一次启动会生成 CA 到 `~/.mitmproxy/`，并自动打开浏览器的 mitmweb 界面（`http://127.0.0.1:8081`）。
- 让它一直开着。

## 2. 装 CA 到模拟器系统证书库（安全改动，你亲手跑）

MuMu 的 `adb shell` 本身就是 root。Android 12 的系统证书按「哈希名」放在
`/system/etc/security/cacerts/`。

```bash
# 算 Android 需要的哈希文件名，并复制成 <hash>.0
HASH=$(openssl x509 -inform PEM -subject_hash_old -noout -in ~/.mitmproxy/mitmproxy-ca-cert.pem)
cp ~/.mitmproxy/mitmproxy-ca-cert.pem "/tmp/$HASH.0"
echo "证书文件名: $HASH.0"

# 推到模拟器
"$ADB" -s "$SER" push "/tmp/$HASH.0" /sdcard/

# 重挂系统分区为可写，装入系统证书库
"$ADB" -s "$SER" shell "mount -o rw,remount /" 2>/dev/null || "$ADB" -s "$SER" shell "mount -o rw,remount /system"
"$ADB" -s "$SER" shell "cp /sdcard/$HASH.0 /system/etc/security/cacerts/$HASH.0"
"$ADB" -s "$SER" shell "chmod 644 /system/etc/security/cacerts/$HASH.0"
"$ADB" -s "$SER" shell "ls -l /system/etc/security/cacerts/$HASH.0"
```

- 若 `cp` 报只读/权限失败：多半是系统分区没重挂成功。可在 MuMu 设置里确认已开 root，重启模拟器后重试；或改挂载点（`/` 与 `/system` 都试）。
- 装完**重启一次鑫时光集**（或重启模拟器）让证书生效。

## 3. 让模拟器走 mitmproxy 代理

宿主机在 MuMu 里通常是 `10.0.2.2`；若不通改用 Mac 局域网 IP `172.20.10.2`。

```bash
"$ADB" -s "$SER" shell "settings put global http_proxy 10.0.2.2:8080"
# 验证
"$ADB" -s "$SER" shell "settings get global http_proxy"
```

## 4. 触发并找到 feed 请求

1. 打开鑫时光集 → 进入你平时滑的**照片/成长列表**页，**向上滑一两屏**触发翻页加载。
2. 回到 mitmweb 界面（`http://127.0.0.1:8081`），在过滤框输入：`~u api-gateway`
3. 找那条**返回 JSON、且响应体里含 `cdn-mctchildfoliocn` 照片链**的请求（通常是列表/分页接口，不是图片本身）。点开看。

**把这些事实告诉 Claude（不要贴 token 和真实照片 URL）：**

- 请求方法 + 路径：如 `GET /xxx/yyy/zzz`
- query 参数**名**（不要值）：如 `?childId=&pageIndex=&pageSize=`
- 若是 POST：请求体的**字段名**（不要值）
- 请求头里除 `Authorization` 外的**自定义头名**：如 `client`、`lang` 等
- 响应体结构：**原图 URL 在哪个字段**（如 `data.list[].originUrl` / `photoUrl` / `moments[].url`），以及有没有分页字段（`total` / `hasMore` / `pageIndex`）

> 判定证书是否生效：mitmweb 里能看到 `api-gateway` 的**明文 HTTPS** 请求就说明成功。
> 若 `api-gateway` 请求全部报 TLS/连接错误 → App 有**证书固定(pinning)**，MITM 被挡，
> 这条路要改用 Frida（更重），先把这个现象告诉 Claude。

## 5. 用完还原（重要，别留后门）

```bash
# 撤掉代理
"$ADB" -s "$SER" shell "settings put global http_proxy :0"
"$ADB" -s "$SER" shell "settings delete global http_proxy" 2>/dev/null

# 删掉刚装的 MITM CA（恢复系统信任库）
"$ADB" -s "$SER" shell "mount -o rw,remount /" 2>/dev/null || "$ADB" -s "$SER" shell "mount -o rw,remount /system"
"$ADB" -s "$SER" shell "rm -f /system/etc/security/cacerts/$HASH.0"

# 关掉 mitmweb（回到那个终端按 Ctrl-C），删本地临时证书
rm -f "/tmp/$HASH.0"
```

完成后重启一次模拟器，系统信任库即恢复原状。
