#!/usr/bin/env python3
"""一条命令装好免费安卓模拟器，用来读一次 App 的登录信息。

MuMu 的 Mac 版是订阅制，这个脚本改用**免费**的 Android 官方模拟器，
只装导出器真正需要的那几样，并且替你避开手动建模拟器最容易踩的三个坑
（都是实测踩到的，不是假想）：

* 系统镜像必须是 **Google APIs**，不能是 Google Play —— 只有前者允许
  adb 读 ``/data/data``，而那正是这个模拟器存在的唯一理由；
* 数据分区必须是持久的 —— ``avdmanager`` 默认写 ``<temp>``，你刚输完的
  登录信息下次开机就没了；
* 语言设成中文 —— 新建的模拟器默认英文界面、区号 +1，登录都费劲。

不需要 Homebrew，也不需要管理员密码：工具都装在
``~/.xin-exporter/`` 下面，删掉这个目录就等于卸载干净。
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import ssl
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Mapping

try:  # package import (tests / `python3 -m ...`)
    from tools import export_originals as eo
except ImportError:  # pragma: no cover - `python3 tools/setup_emulator.py`
    import export_originals as eo


API_LEVEL = 34  # Android 14: new enough for the app, old enough to be boring
# `google_apis` (not `google_play`) is load-bearing - see the module docstring.
IMAGE_TAG = "google_apis"
AVD_NAME = "xin-exporter"
TOOLS_ROOT = Path.home() / ".xin-exporter"
SDK_DIR = TOOLS_ROOT / "android-sdk"
JDK_DIR = TOOLS_ROOT / "jdk"
CMDLINE_TOOLS_URL = (
    "https://dl.google.com/android/repository/commandlinetools-mac-11076708_latest.zip"
)
JDK_URL_TEMPLATE = (
    "https://api.adoptium.net/v3/binary/latest/21/ga/mac/{arch}/jdk/hotspot/normal/eclipse"
)
_JDK_ARCH = {"arm64-v8a": "aarch64", "x86_64": "x64"}
_ABI_BY_MACHINE = {
    "arm64": "arm64-v8a",
    "aarch64": "arm64-v8a",
    "x86_64": "x86_64",
    "amd64": "x86_64",
}
# Settings a freshly generated AVD gets wrong for this use case.
AVD_OVERRIDES = {
    "hw.ramSize": "4096",
    "vm.heapSize": "512",
    "disk.dataPartition.size": "8G",
    "hw.gpu.enabled": "yes",
    "hw.gpu.mode": "auto",
    "hw.keyboard": "yes",
}
# `<temp>` here means a throwaway data partition: the login would not survive
# a reboot. Dropping the key restores the persistent default.
AVD_DROP_KEYS = ("disk.dataPartition.path",)
BOOT_TIMEOUT_SECONDS = 600
# Adoptium's CDN returns 403 to urllib's default User-Agent.
DOWNLOAD_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
# The emulator defaults to English and a +1 dial code; both make login harder.
TARGET_LOCALE = "zh-CN"


# --- pure helpers (unit-tested) ---------------------------------------------


def host_abi(machine: str | None = None) -> str:
    """Map the Mac's CPU to the Android ABI its emulator must run."""
    machine = (machine or platform.machine()).lower()
    abi = _ABI_BY_MACHINE.get(machine)
    if abi is None:
        raise eo.SmokeError("unsupported-architecture")
    return abi


def system_image_package(api_level: int = API_LEVEL, abi: str = "arm64-v8a") -> str:
    return f"system-images;android-{api_level};{IMAGE_TAG};{abi}"


def verify_rootable(package: str) -> None:
    """Refuse any image that cannot be rooted, whatever produced it."""
    if f";{IMAGE_TAG};" not in package:
        raise eo.SmokeError("image-not-rootable")


def jdk_url(abi: str) -> str:
    return JDK_URL_TEMPLATE.format(arch=_JDK_ARCH[abi])


def find_sdk(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    is_dir: Callable[[Path], bool] = Path.is_dir,
) -> Path | None:
    """An Android SDK already on this Mac, if there is one worth reusing."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    candidates = [SDK_DIR]
    for variable in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        root = env.get(variable)
        if root:
            candidates.append(Path(root))
    candidates.append(home / "Library" / "Android" / "sdk")
    candidates.append(Path("/opt/homebrew/share/android-commandlinetools"))
    for candidate in candidates:
        if is_dir(candidate):
            return candidate
    return None


def find_java(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    is_file: Callable[[Path], bool] = Path.is_file,
) -> Path | None:
    """A JDK already on this Mac (Android Studio bundles one)."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    candidates = [
        JDK_DIR,
        Path("/Applications/Android Studio.app/Contents/jbr/Contents/Home"),
        Path("/opt/homebrew/opt/openjdk"),
    ]
    java_home = env.get("JAVA_HOME")
    if java_home:
        candidates.insert(0, Path(java_home))
    for candidate in candidates:
        if is_file(candidate / "bin" / "java"):
            return candidate
    return None


def rewrite_avd_config(
    text: str,
    overrides: Mapping[str, str] = AVD_OVERRIDES,
    drop: tuple[str, ...] = AVD_DROP_KEYS,
) -> str:
    """Return the config with our settings applied; never mutates the input."""
    kept: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line else None
        if key in drop:
            continue
        if key in overrides:
            kept.append(f"{key}={overrides[key]}")
            seen.add(key)
        else:
            kept.append(line)
    kept.extend(f"{k}={v}" for k, v in overrides.items() if k not in seen)
    return "\n".join(kept) + "\n"


def boot_completed(result: subprocess.CompletedProcess) -> bool:
    return result.returncode == 0 and result.stdout.strip() == "1"


# --- installation ------------------------------------------------------------


def _say(message: str) -> None:
    print(message, flush=True)


class HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never off https.

    Adoptium hands the JDK download off to a mirror, so redirects must be
    allowed here (the exporter's own opener refuses them outright). Letting
    one land on plain http would let anyone on the network swap the JDK we
    are about to execute.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_download_opener():
    """Trust the system CAs and allow https-to-https redirects.

    macOS' python.org builds ship without a usable CA bundle, so a bare
    urlopen fails certificate validation; `eo.select_ca_file` already solves
    that for the exporter and is reused here.
    """
    python_ca = Path(ssl.get_default_verify_paths().openssl_cafile)
    context = ssl.create_default_context(cafile=str(eo.select_ca_file(python_ca)))
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        HttpsOnlyRedirects(),
    )


def download(url: str, destination: Path, opener=None) -> Path:
    """Stream a download to disk, showing progress on one line.

    A real User-Agent is required, not cosmetic: Adoptium's CDN answers 403
    to urllib's default one.
    """
    opener = opener or build_download_opener()
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": DOWNLOAD_USER_AGENT})
    try:
        with opener.open(request, timeout=120) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            shown = -1
            with part.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    # Redraw only when the percentage actually moves: one
                    # line per chunk turns a captured log into megabytes.
                    percent = done * 100 // total if total else 0
                    if total and percent != shown:
                        shown = percent
                        print(
                            f"\r  下载中 {percent}%"
                            f"（{done // 1048576}/{total // 1048576} MB）",
                            end="",
                            flush=True,
                        )
        print()
    except Exception as exc:
        part.unlink(missing_ok=True)
        # Keep the cause: "download-failed" alone leaves the user with no
        # idea whether it was their wifi, a 403 or a full disk.
        reason = getattr(exc, "code", None) or type(exc).__name__
        raise eo.SmokeError(f"download-failed:{reason}") from None
    os.replace(part, destination)
    return destination


def _extract(archive: Path, target: Path, run_command: Callable) -> None:
    target.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(target)
        return
    result = run_command(["tar", "-xzf", str(archive), "-C", str(target)])
    if result.returncode != 0:
        raise eo.SmokeError("extract-failed")


def install_jdk(abi: str, run_command: Callable = eo.run_command) -> Path:
    existing = find_java()
    if existing:
        _say(f"已有 Java：{existing}")
        return existing
    _say("正在下载 Java（约 200 MB，一次性）…")
    archive = TOOLS_ROOT / "jdk.tar.gz"
    download(jdk_url(abi), archive)
    staging = TOOLS_ROOT / "jdk-staging"
    shutil.rmtree(staging, ignore_errors=True)
    _extract(archive, staging, run_command)
    homes = list(staging.glob("*/Contents/Home"))
    if not homes:
        raise eo.SmokeError("jdk-layout-unexpected")
    shutil.rmtree(JDK_DIR, ignore_errors=True)
    shutil.move(str(homes[0]), str(JDK_DIR))
    shutil.rmtree(staging, ignore_errors=True)
    archive.unlink(missing_ok=True)
    _say(f"Java 装好：{JDK_DIR}")
    return JDK_DIR


def install_cmdline_tools() -> Path:
    """Put Google's command-line tools where sdkmanager expects to live."""
    target = SDK_DIR / "cmdline-tools" / "latest"
    if (target / "bin" / "sdkmanager").is_file():
        _say(f"已有 Android 命令行工具：{target}")
        return SDK_DIR
    _say("正在下载 Android 命令行工具（约 150 MB，一次性）…")
    archive = TOOLS_ROOT / "cmdline-tools.zip"
    download(CMDLINE_TOOLS_URL, archive)
    staging = TOOLS_ROOT / "cmdline-staging"
    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(staging)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(target, ignore_errors=True)
    shutil.move(str(staging / "cmdline-tools"), str(target))
    shutil.rmtree(staging, ignore_errors=True)
    archive.unlink(missing_ok=True)
    for tool in (target / "bin").glob("*"):
        tool.chmod(0o755)
    _say(f"命令行工具装好：{SDK_DIR}")
    return SDK_DIR


# --- emulator orchestration ---------------------------------------------------


class Sdk:
    """Paths and environment for one SDK + JDK pair."""

    def __init__(self, sdk_root: Path, java_home: Path):
        self.root = sdk_root
        self.java_home = java_home

    @property
    def env(self) -> dict[str, str]:
        merged = dict(os.environ)
        merged["JAVA_HOME"] = str(self.java_home)
        merged["ANDROID_SDK_ROOT"] = str(self.root)
        merged["ANDROID_HOME"] = str(self.root)
        merged["PATH"] = f"{self.java_home / 'bin'}:{merged.get('PATH', '')}"
        return merged

    def tool(self, name: str) -> Path:
        for relative in (
            Path("cmdline-tools/latest/bin") / name,
            Path("cmdline-tools/bin") / name,
            Path("platform-tools") / name,
            Path("emulator") / name,
        ):
            candidate = self.root / relative
            if candidate.is_file():
                return candidate
        raise eo.SmokeError(f"tool-not-found:{name}")

    def run(self, argv: list, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(value) for value in argv],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            **kwargs,
        )


def install_packages(sdk: Sdk, image: str) -> None:
    """Install platform-tools, the emulator and the system image.

    Licences are NOT auto-accepted: sdkmanager prompts on this terminal so
    the person running the script agrees to them themselves.
    """
    verify_rootable(image)
    _say("正在下载模拟器和系统镜像（约 4 GB，一次性；会问你几次是否接受许可，输 y 回车）…")
    argv = [str(sdk.tool("sdkmanager")), "platform-tools", "emulator", image]
    result = subprocess.run(argv, env=sdk.env, check=False)
    if result.returncode != 0:
        raise eo.SmokeError("sdk-install-failed")
    if not (sdk.root / "platform-tools" / "adb").is_file():
        raise eo.SmokeError("sdk-install-failed")


def create_avd(sdk: Sdk, image: str, name: str = AVD_NAME) -> Path:
    verify_rootable(image)
    _say(f"正在创建模拟器 {name} …")
    argv = [str(sdk.tool("avdmanager")), "create", "avd", "-n", name, "-k", image, "--force"]
    result = subprocess.run(
        argv, env=sdk.env, input="no\n", text=True, capture_output=True, check=False
    )
    config = Path.home() / ".android" / "avd" / f"{name}.avd" / "config.ini"
    if not config.is_file():
        sys.stderr.write(result.stderr)
        raise eo.SmokeError("avd-create-failed")
    config.write_text(rewrite_avd_config(config.read_text(encoding="utf-8")), encoding="utf-8")
    _say("已修正模拟器配置（持久化数据分区、内存、键盘）")
    return config


def start_emulator(sdk: Sdk, name: str = AVD_NAME) -> subprocess.Popen:
    _say("正在启动模拟器（第一次开机较慢，请耐心等）…")
    log = TOOLS_ROOT / "emulator.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = log.open("wb")
    return subprocess.Popen(
        [str(sdk.tool("emulator")), "-avd", name, "-no-snapshot-load"],
        env=sdk.env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )


def wait_for_boot(sdk: Sdk, timeout: int = BOOT_TIMEOUT_SECONDS) -> str:
    adb = sdk.tool("adb")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = sdk.run([adb, "-e", "shell", "getprop", "sys.boot_completed"])
        if boot_completed(result):
            serial = sdk.run([adb, "-e", "get-serialno"]).stdout.strip()
            _say(f"模拟器已就绪：{serial}")
            return serial
        time.sleep(5)
    raise eo.SmokeError("emulator-boot-timeout")


def apply_locale(
    sdk: Sdk,
    serial: str,
    *,
    locale: str = TARGET_LOCALE,
    attempts: int = 5,
    pause: float = 2.0,
) -> bool:
    """Set the system locale and read it back; True once it actually stuck.

    `adb root` restarts adbd, and the first setprop after that can land on a
    connection that is going away — `adb shell` reports success either way,
    so the write vanishes silently. Observed on a real AVD: the emulator
    stayed in English while setup announced Chinese. Hence read-back and
    retry rather than a fixed sleep.
    """
    adb = sdk.tool("adb")
    command = (
        f"setprop persist.sys.locale {locale}; "
        f"setprop persist.sys.language {locale.split('-')[0]}; "
        f"setprop persist.sys.country {locale.split('-')[1]}"
    )
    for attempt in range(attempts):
        sdk.run([adb, "-s", serial, "shell", command])
        readback = sdk.run(
            [adb, "-s", serial, "shell", "getprop", "persist.sys.locale"]
        ).stdout.strip()
        if readback == locale:
            return True
        if attempt < attempts - 1:
            time.sleep(pause)
    return False


def set_chinese_locale(sdk: Sdk, serial: str) -> None:
    """Chinese UI and a +86 dial code; the default is English and +1."""
    adb = sdk.tool("adb")
    sdk.run([adb, "-s", serial, "root"])
    time.sleep(2)
    sdk.run([adb, "-s", serial, "wait-for-device"])
    if apply_locale(sdk, serial):
        _say("已设为中文，正在重启模拟器…")
    else:
        # Never claim a success we could not verify: the user is about to
        # look at the window and needs to know why it is not Chinese.
        _say("提示：没能把系统语言设成中文，模拟器界面会是英文的（不影响导出）。")
        _say("      想手动改：Settings → System → Languages → 添加「简体中文」并拖到最上面。")
    sdk.run([adb, "-s", serial, "reboot"])
    time.sleep(8)
    wait_for_boot(sdk)


def install_apk(sdk: Sdk, serial: str, apk: Path) -> None:
    if not apk.is_file():
        raise eo.SmokeError("apk-not-found")
    _say(f"正在安装 {apk.name} …")
    result = sdk.run([sdk.tool("adb"), "-s", serial, "install", "-r", str(apk)])
    if "Success" not in result.stdout:
        sys.stderr.write(result.stdout + result.stderr)
        raise eo.SmokeError("apk-install-failed")
    _say("App 已安装")


def wait_until_gone(
    still_running: Callable[[], bool], timeout: int = 30, interval: float = 1.0
) -> bool:
    """Poll until ``still_running`` goes false; True if it did in time."""
    deadline = time.monotonic() + timeout
    while True:
        if not still_running():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def delete_avd(sdk: Sdk, name: str = AVD_NAME) -> int:
    """Remove the emulator, and with it the login state stored inside."""
    adb = sdk.tool("adb")
    sdk.run([adb, "-e", "emu", "kill"])
    # Wait for it to actually exit: a live emulator holds the disk images
    # open and would write them back out over the deletion.
    wait_until_gone(lambda: "emulator-" in sdk.run([adb, "devices"]).stdout)
    subprocess.run(
        [str(sdk.tool("avdmanager")), "delete", "avd", "-n", name],
        env=sdk.env,
        check=False,
    )
    remaining = Path.home() / ".android" / "avd" / f"{name}.avd"
    shutil.rmtree(remaining, ignore_errors=True)
    _say(f"模拟器 {name} 已删除，里面的登录信息随之清除。")
    return 0


# --- CLI ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="一条命令装好免费安卓模拟器（用于读取鑫时光集的登录信息）"
    )
    parser.add_argument("--apk", type=Path, default=None, help="顺便安装这个 apk 安装包")
    parser.add_argument(
        "--delete", action="store_true", help="删除模拟器及其中的登录信息后退出"
    )
    parser.add_argument(
        "--no-start", action="store_true", help="只准备好，不启动模拟器"
    )
    parser.add_argument(
        "--no-next-steps",
        action="store_true",
        help=argparse.SUPPRESS,  # 向导内部用：后续步骤由向导自己说，避免两套矛盾指引
    )
    return parser


def _prepare_sdk() -> Sdk:
    TOOLS_ROOT.mkdir(parents=True, exist_ok=True)
    abi = host_abi()
    java_home = install_jdk(abi)
    existing = find_sdk()
    sdk_root = existing if existing and existing != SDK_DIR else install_cmdline_tools()
    if not (sdk_root / "cmdline-tools").is_dir():
        sdk_root = install_cmdline_tools()
    return Sdk(sdk_root, java_home)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if sys.platform != "darwin":
        print("失败：目前只支持 macOS")
        return 1
    try:
        sdk = _prepare_sdk()
        if args.delete:
            return delete_avd(sdk)
        image = system_image_package(API_LEVEL, host_abi())
        install_packages(sdk, image)
        create_avd(sdk, image)
        if args.no_start:
            _say("已准备好。稍后运行本脚本（不加 --no-start）即可启动模拟器。")
            return 0
        start_emulator(sdk)
        serial = wait_for_boot(sdk)
        set_chinese_locale(sdk, serial)
        if args.apk:
            install_apk(sdk, serial, args.apk)
        if not args.no_next_steps:
            _say("")
            _say("模拟器已就绪。请在模拟器窗口里打开鑫时光集、用你自己的账号登录，")
            _say("并进一次照片页面，然后回到终端运行导出命令。")
            _say("用完想清除登录信息：python3 tools/setup_emulator.py --delete")
    except eo.SmokeError as exc:
        print(f"失败：{exc}")
        return 1
    except KeyboardInterrupt:
        print("\n已取消。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
