#!/usr/bin/env python3
"""Minimal, privacy-conscious original-photo smoke exporter."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import subprocess
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable
import urllib.parse
import urllib.request


CDN_HOST = "cdn-mctchildfoliocn.childfolio.net"
ORIGINAL_URL_RE = re.compile(r"原图地址::(\S+)")
MUMUTOOL = Path("/Applications/MuMuPlayer.app/Contents/MacOS/mumutool")
ADB = Path(
    "/Applications/MuMuPlayer.app/Contents/MacOS/"
    "MuMuEmulator.app/Contents/MacOS/tools/adb"
)
PACKAGE = "com.childfolio.family"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIN_BYTES = 1024
MAX_BYTES = 50 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
SYSTEM_CA_FILE = Path("/etc/ssl/cert.pem")
SETFILE = Path("/usr/bin/SetFile")
RECORD_DATE_RE = re.compile(
    r"(?:^|/)moments/images/(\d{4}-\d{2}-\d{2})(?:/|$)"
)
BATCH_OUTPUT = REPOSITORY_ROOT / "build" / "originals"
WM_SIZE_RE = re.compile(r"(\d+)x(\d+)")
SWIPE_DURATION_MS = 300
SWIPE_START_FRACTION = 0.75
SWIPE_END_FRACTION = 0.30
DEFAULT_MAX_STABLE_SCREENS = 6
DEFAULT_MAX_SWIPES = 2000
DEFAULT_SETTLE_SECONDS = 2.5


class SmokeError(RuntimeError):
    """Expected smoke-check failure whose message contains no sensitive URL."""


@dataclass(frozen=True)
class Device:
    serial: str


@dataclass(frozen=True)
class DownloadResult:
    byte_count: int
    sha256: str


@dataclass(frozen=True)
class CandidateOutcome:
    status: str
    date_failed: bool = False


@dataclass(frozen=True)
class BatchSummary:
    total: int
    downloaded: int
    existing: int
    failed: int
    date_failed: int
    unprocessed: int


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def select_ca_file(python_ca_file: Path, system_ca_file: Path = SYSTEM_CA_FILE) -> Path:
    for candidate in (python_ca_file, system_ca_file):
        if candidate.is_file():
            return candidate
    raise SmokeError("ca-bundle-not-found")


def build_opener():
    python_ca = Path(ssl.get_default_verify_paths().openssl_cafile)
    context = ssl.create_default_context(cafile=str(select_ca_file(python_ca)))
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        RejectRedirects(),
    )


def run_command(argv: list[str | Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(value) for value in argv],
        capture_output=True,
        text=True,
        check=False,
    )


def run_binary_command(argv: list[str | Path]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(value) for value in argv],
        capture_output=True,
        check=False,
    )


def validate_original_url(raw: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if parsed.scheme != "https":
        return None
    if parsed.hostname != CDN_HOST or port not in (None, 443):
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    if not parsed.path.endswith(".jpeg"):
        return None
    return raw


def extract_urls(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in ORIGINAL_URL_RE.finditer(text):
        candidate = validate_original_url(match.group(1))
        if candidate is not None and candidate not in seen:
            found.append(candidate)
            seen.add(candidate)
    return found


def extract_record_date(url: str) -> date | None:
    try:
        path = urllib.parse.urlsplit(url).path
    except ValueError:
        return None
    match = RECORD_DATE_RE.search(path)
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def batch_destination(url: str, output_dir: Path) -> Path:
    record_date = extract_record_date(url)
    prefix = record_date.isoformat() if record_date else "unknown-date"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return output_dir / f"{prefix}_{digest}.jpeg"


def select_samples(urls: list[str], count: int = 3) -> list[str]:
    samples = list(dict.fromkeys(urls))[:count]
    if len(samples) < count:
        raise SmokeError("not-enough-candidates")
    return samples


def _info_rows(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("return", "results", "devices", "data", "infos", "items"):
            if key in payload:
                return _info_rows(payload[key])
        return [payload]
    return []


def discover_running_device(
    run_command: Callable = run_command,
) -> Device:
    result = run_command([MUMUTOOL, "info", "all"])
    if result.returncode != 0:
        raise SmokeError("mumu-not-running")
    try:
        rows = _info_rows(json.loads(result.stdout))
    except (TypeError, json.JSONDecodeError):
        raise SmokeError("mumu-not-running") from None
    running = [
        row
        for row in rows
        if row.get("is_android_started") is True
        or row.get("isAndroidStarted") is True
        or row.get("running") is True
        or row.get("status") == "running"
        or row.get("state") == "running"
    ]
    if not running:
        raise SmokeError("mumu-not-running")
    if len(running) != 1:
        raise SmokeError("ambiguous-device")
    port = running[0].get("adb_port", running[0].get("adbPort"))
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise SmokeError("mumu-not-running")
    serial = f"127.0.0.1:{port}"
    connected = run_command([ADB, "connect", serial])
    if connected.returncode != 0:
        raise SmokeError("mumu-not-running")
    return Device(serial=serial)


def discover_app_pid(device: Device, run_command: Callable = run_command) -> int:
    result = run_command([ADB, "-s", device.serial, "shell", "pidof", PACKAGE])
    values = result.stdout.split() if result.returncode == 0 else []
    if len(values) != 1 or not values[0].isdigit():
        raise SmokeError("app-not-running")
    return int(values[0])


def read_current_logcat(
    device: Device, pid: int, run_command: Callable = run_command
) -> str:
    result = run_command(
        [ADB, "-s", device.serial, "logcat", "-d", f"--pid={pid}", "-v", "brief"]
    )
    if result.returncode != 0:
        raise SmokeError("logcat-failed")
    return result.stdout


def start_logcat_stream(
    device: Device,
    _pid: int,
    popen: Callable = subprocess.Popen,
):
    try:
        return popen(
            [
                str(ADB),
                "-s",
                device.serial,
                "logcat",
                "-v",
                "brief",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            errors="replace",
            bufsize=1,
        )
    except OSError:
        raise SmokeError("logcat-stream-failed") from None


def _stop_logcat_process(process) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def collect_streaming_urls(
    device: Device,
    pid: int,
    *,
    popen: Callable = subprocess.Popen,
    progress: Callable[[str], None] = print,
) -> list[str]:
    process = start_logcat_stream(device, pid, popen)
    if process.stdout is None:
        _stop_logcat_process(process)
        raise SmokeError("logcat-stream-failed")
    ordered: list[str] = []
    seen: set[str] = set()
    user_stopped = False
    try:
        for line in process.stdout:
            for url in extract_urls(line):
                if url not in seen:
                    seen.add(url)
                    ordered.append(url)
                    progress(f"已发现唯一原图：{len(ordered)}")
    except KeyboardInterrupt:
        user_stopped = True
    except Exception:
        raise SmokeError("logcat-stream-failed") from None
    finally:
        _stop_logcat_process(process)
    if not user_stopped:
        raise SmokeError("logcat-stream-failed")
    return ordered


def get_screen_size(
    device: Device, run_command: Callable = run_command
) -> tuple[int, int]:
    result = run_command([ADB, "-s", device.serial, "shell", "wm", "size"])
    if result.returncode != 0:
        raise SmokeError("screen-size-failed")
    override: tuple[int, int] | None = None
    physical: tuple[int, int] | None = None
    for line in result.stdout.splitlines():
        match = WM_SIZE_RE.search(line)
        if match is None:
            continue
        size = (int(match.group(1)), int(match.group(2)))
        if "Override" in line:
            override = size
        elif "Physical" in line:
            physical = size
    chosen = override or physical
    if chosen is None or chosen[0] <= 0 or chosen[1] <= 0:
        raise SmokeError("screen-size-failed")
    return chosen


def swipe_scroll(
    device: Device,
    width: int,
    height: int,
    *,
    run_command: Callable = run_command,
    duration_ms: int = SWIPE_DURATION_MS,
) -> None:
    center_x = width // 2
    y_start = int(height * SWIPE_START_FRACTION)
    y_end = int(height * SWIPE_END_FRACTION)
    result = run_command(
        [
            ADB,
            "-s",
            device.serial,
            "shell",
            "input",
            "swipe",
            str(center_x),
            str(y_start),
            str(center_x),
            str(y_end),
            str(duration_ms),
        ]
    )
    if result.returncode != 0:
        raise SmokeError("swipe-failed")


def dump_logcat_urls(
    device: Device, run_binary: Callable = run_binary_command
) -> list[str]:
    result = run_binary(
        [ADB, "-s", device.serial, "logcat", "-d", "-v", "brief"]
    )
    if result.returncode != 0:
        raise SmokeError("logcat-failed")
    stdout = result.stdout
    text = (
        stdout.decode("utf-8", "replace") if isinstance(stdout, bytes) else stdout
    )
    return extract_urls(text)


def capture_screen_signature(
    device: Device, run_binary: Callable = run_binary_command
) -> bytes | None:
    """Return a hash of the current screen, or None if capture failed.

    The screenshot bytes only exist in memory long enough to hash; they are
    never written to disk, printed, or transmitted. Only the opaque digest is
    kept, purely to decide whether the list is still scrolling.
    """
    result = run_binary(
        [ADB, "-s", device.serial, "exec-out", "screencap", "-p"]
    )
    if result.returncode != 0 or not result.stdout:
        return None
    return hashlib.sha256(result.stdout).digest()


def collect_with_auto_scroll(
    device: Device,
    *,
    run_command: Callable = run_command,
    run_binary: Callable = run_binary_command,
    sleep_fn: Callable = time.sleep,
    progress: Callable[[str], None] = print,
    max_stable_screens: int = DEFAULT_MAX_STABLE_SCREENS,
    max_swipes: int = DEFAULT_MAX_SWIPES,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
) -> list[str]:
    width, height = get_screen_size(device, run_command=run_command)
    ordered: list[str] = []
    seen: set[str] = set()

    def merge() -> None:
        for url in dump_logcat_urls(device, run_binary=run_binary):
            if url not in seen:
                seen.add(url)
                ordered.append(url)

    try:
        merge()
        progress(f"已发现唯一原图：{len(ordered)}")
        last_signature = capture_screen_signature(device, run_binary=run_binary)
        stable = 0
        swipes = 0
        while stable < max_stable_screens and swipes < max_swipes:
            swipe_scroll(device, width, height, run_command=run_command)
            sleep_fn(settle_seconds)
            merge()
            signature = capture_screen_signature(device, run_binary=run_binary)
            if signature is not None and signature == last_signature:
                stable += 1
            else:
                stable = 0
            last_signature = signature
            swipes += 1
            progress(f"已发现唯一原图：{len(ordered)}")
    except KeyboardInterrupt:
        pass
    return ordered


def apply_record_date(
    destination: Path,
    record_date: date | None,
    run_command: Callable = run_command,
) -> bool:
    if record_date is None:
        return True
    local_noon = datetime(
        record_date.year,
        record_date.month,
        record_date.day,
        12,
        0,
        0,
    ).astimezone()
    timestamp = local_noon.timestamp()
    formatted = record_date.strftime("%m/%d/%Y 12:00:00")
    try:
        os.utime(destination, (timestamp, timestamp))
        result = run_command(
            [SETFILE, "-d", formatted, "-m", formatted, destination]
        )
    except OSError:
        return False
    return result.returncode == 0


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def download_sample(
    opener,
    url: str,
    destination: Path,
    timeout: int = 120,
) -> DownloadResult:
    part = destination.with_suffix(destination.suffix + ".part")
    _safe_unlink(part)
    digest = hashlib.sha256()
    byte_count = 0
    first_bytes = b""
    request = urllib.request.Request(url, headers={"User-Agent": "xin-photo-smoke/1"})
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise SmokeError("http-not-200")
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "image/jpeg":
                raise SmokeError("wrong-content-type")
            with part.open("xb") as output:
                while True:
                    chunk = response.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    byte_count += len(chunk)
                    if byte_count > MAX_BYTES:
                        raise SmokeError("too-large")
                    if len(first_bytes) < 2:
                        first_bytes = (first_bytes + chunk)[:2]
                    digest.update(chunk)
                    output.write(chunk)
        if byte_count <= MIN_BYTES:
            raise SmokeError("too-small")
        if first_bytes != b"\xff\xd8":
            raise SmokeError("invalid-jpeg")
        os.replace(part, destination)
        return DownloadResult(byte_count=byte_count, sha256=digest.hexdigest())
    except SmokeError:
        _safe_unlink(part)
        raise
    except KeyboardInterrupt:
        _safe_unlink(part)
        raise
    except Exception:
        _safe_unlink(part)
        raise SmokeError("download-failed") from None


def looks_like_existing_jpeg(path: Path) -> bool:
    try:
        if path.stat().st_size <= MIN_BYTES:
            return False
        with path.open("rb") as handle:
            return handle.read(2) == b"\xff\xd8"
    except OSError:
        return False


def download_batch_candidate(
    url: str,
    output_dir: Path,
    *,
    opener,
    date_setter: Callable = apply_record_date,
) -> CandidateOutcome:
    destination = batch_destination(url, output_dir)
    record_date = extract_record_date(url)
    if looks_like_existing_jpeg(destination):
        try:
            date_ok = date_setter(destination, record_date)
        except Exception:
            date_ok = False
        return CandidateOutcome("existing", date_failed=not date_ok)
    try:
        download_sample(opener, url, destination)
    except SmokeError:
        return CandidateOutcome("failed")
    try:
        date_ok = date_setter(destination, record_date)
    except Exception:
        date_ok = False
    return CandidateOutcome("downloaded", date_failed=not date_ok)


def download_batch(
    urls: list[str],
    output_dir: Path,
    *,
    opener=None,
    sleep_fn: Callable = time.sleep,
    date_setter: Callable = apply_record_date,
    candidate_downloader: Callable | None = None,
) -> BatchSummary:
    ensure_build_is_ignored()
    candidates = list(dict.fromkeys(urls))
    candidate_downloader = candidate_downloader or download_batch_candidate
    opener = opener or build_opener()
    previous_umask = os.umask(0o077)
    downloaded = 0
    existing = 0
    failed = 0
    date_failed = 0
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        pending = candidates
        for pass_index in range(3):
            if not pending:
                break
            if pass_index > 0:
                sleep_fn(2)
            next_pending: list[str] = []
            for index, url in enumerate(pending):
                print(f"第 {pass_index + 1} 轮：{index + 1}/{len(pending)}")
                try:
                    outcome = candidate_downloader(
                        url,
                        output_dir,
                        opener=opener,
                        date_setter=date_setter,
                    )
                except KeyboardInterrupt:
                    unprocessed = len(next_pending) + len(pending) - index
                    summary = BatchSummary(
                        len(candidates),
                        downloaded,
                        existing,
                        failed,
                        date_failed,
                        unprocessed,
                    )
                    _print_batch_summary(summary, output_dir)
                    return summary
                if outcome.status == "downloaded":
                    downloaded += 1
                    date_failed += int(outcome.date_failed)
                elif outcome.status == "existing":
                    existing += 1
                    date_failed += int(outcome.date_failed)
                elif pass_index == 2:
                    failed += 1
                else:
                    next_pending.append(url)
            pending = next_pending
        summary = BatchSummary(
            len(candidates), downloaded, existing, failed, date_failed, 0
        )
        _print_batch_summary(summary, output_dir)
        return summary
    finally:
        os.umask(previous_umask)


def _print_batch_summary(summary: BatchSummary, output_dir: Path) -> None:
    print(
        "结果："
        f"下载 {summary.downloaded}，已存在 {summary.existing}，"
        f"失败 {summary.failed}，日期设置失败 {summary.date_failed}，"
        f"未处理 {summary.unprocessed}"
    )
    print(f"输出目录：{output_dir}")


def create_run_directory(parent: Path, timestamp: str | None = None) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    base = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = 0
    while True:
        name = base if suffix == 0 else f"{base}-{suffix}"
        candidate = parent / name
        try:
            candidate.mkdir(exist_ok=False)
            return candidate
        except FileExistsError:
            suffix += 1


def ensure_build_is_ignored(repository_root: Path = REPOSITORY_ROOT) -> None:
    ignore_file = repository_root / ".gitignore"
    try:
        rules = {
            line.strip()
            for line in ignore_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
    except OSError:
        raise SmokeError("build-not-ignored") from None
    if "build/" not in rules and "/build/" not in rules:
        raise SmokeError("build-not-ignored")


def execute_samples(
    samples: list[str],
    output_parent: Path,
    *,
    opener=None,
    timestamp: str | None = None,
    require_ignore: bool = True,
) -> int:
    if len(samples) != 3:
        raise SmokeError("sample-count-not-three")
    if require_ignore:
        ensure_build_is_ignored()
    previous_umask = os.umask(0o077)
    try:
        run_directory = create_run_directory(output_parent, timestamp)
        opener = opener or build_opener()
        successes = 0
        failures = 0
        seen_hashes: set[str] = set()
        print("提示：仅处理本人账号；样本像素与 EXIF 可能包含敏感信息。")
        for ordinal, url in enumerate(samples, start=1):
            destination = run_directory / f"sample-{ordinal:02d}.jpeg"
            try:
                result = download_sample(opener, url, destination)
                duplicate = result.sha256 in seen_hashes
                seen_hashes.add(result.sha256)
                successes += 1
                label = "（内容重复）" if duplicate else ""
                print(
                    f"样本 {ordinal}：成功，{result.byte_count} 字节，"
                    f"SHA-256 {result.sha256}{label}"
                )
            except SmokeError as exc:
                failures += 1
                print(f"样本 {ordinal}：失败（{exc}）")
        print(f"完成：成功 {successes}，失败 {failures}")
        print(f"样本目录：{run_directory}")
        return 0 if failures == 0 else 1
    finally:
        os.umask(previous_umask)


def run_smoke(
    execute: bool = False,
    *,
    run_command: Callable = run_command,
    downloader: Callable | None = None,
    output_parent: Path | None = None,
) -> int:
    device = discover_running_device(run_command)
    pid = discover_app_pid(device, run_command)
    urls = extract_urls(read_current_logcat(device, pid, run_command))
    samples = select_samples(urls)
    destination = output_parent or REPOSITORY_ROOT / "build" / "direct-original-smoke"
    print("MuMu 设备：已连接")
    print("目标 App：已运行")
    print(f"候选原图：{len(urls)}")
    print(f"计划样本：{len(samples)}")
    print(f"输出父目录：{destination}")
    if not execute:
        return 0
    downloader = downloader or execute_samples
    return downloader(samples, destination)


def confirm_download(candidate_count: int, input_fn: Callable = input) -> bool:
    try:
        answer = input_fn(
            f"已发现 {candidate_count} 个唯一原图候选。输入 DOWNLOAD 开始下载："
        )
    except (EOFError, KeyboardInterrupt):
        return False
    return answer == "DOWNLOAD"


def run_batch(
    *,
    run_command: Callable = run_command,
    popen: Callable = subprocess.Popen,
    input_fn: Callable = input,
    downloader: Callable | None = None,
    output_dir: Path = BATCH_OUTPUT,
    auto_scroll: bool = False,
) -> int:
    device = discover_running_device(run_command)
    pid = discover_app_pid(device, run_command)
    if auto_scroll:
        print(
            "批量采集已启动（自动滚动）；连续无新候选会自动停止，"
            "可随时按 Ctrl-C 提前结束。"
        )
        candidates = collect_with_auto_scroll(device, run_command=run_command)
    else:
        print("批量采集已启动；请在 MuMu 中手动滚动，完成后按 Ctrl-C。")
        candidates = collect_streaming_urls(device, pid, popen=popen)
    print(f"采集结束：{len(candidates)} 个唯一原图候选。")
    if not candidates:
        print("没有候选，不创建输出目录。")
        return 0
    if not confirm_download(len(candidates), input_fn):
        print("已取消；没有下载照片。")
        return 0
    downloader = downloader or download_batch
    result = downloader(candidates, output_dir)
    if isinstance(result, int):
        return result
    if result.failed == 0 and result.unprocessed == 0:
        print("本轮候选下载完成。")
        return 0
    print("本轮候选尚未全部完成。")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="鑫时光集 Android 原图 Smoke 验证")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="明确下载且只下载三个原图样本",
    )
    subparsers = parser.add_subparsers(dest="command")
    batch_parser = subparsers.add_parser("batch", help="手动滚动并流式采集批量原图")
    batch_parser.add_argument(
        "--auto-scroll",
        action="store_true",
        help="自动滚动相册并在连续无新候选时停止",
    )
    api_parser = subparsers.add_parser(
        "api", help="直连后端 API 分页导出全部原图/视频/正文"
    )
    api_parser.add_argument(
        "--counter",
        type=int,
        default=None,
        help="起始分页游标（默认从最新开始）",
    )
    api_parser.add_argument(
        "--no-videos",
        action="store_true",
        help="只下照片，不下载视频（正文仍会保存）",
    )
    api_parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过 DOWNLOAD 交互确认，直接下载（用于非交互/自动化）",
    )
    return parser


def _run_api(counter: int | None, include_videos: bool, assume_yes: bool) -> int:
    try:
        from tools.feed_api import DEFAULT_INITIAL_COUNTER, run_api
    except ImportError:
        from feed_api import DEFAULT_INITIAL_COUNTER, run_api
    return run_api(
        initial_counter=counter or DEFAULT_INITIAL_COUNTER,
        include_videos=include_videos,
        assume_yes=assume_yes,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "batch":
        return run_batch(auto_scroll=args.auto_scroll)
    if args.command == "api":
        return _run_api(
            args.counter, include_videos=not args.no_videos, assume_yes=args.yes
        )
    return run_smoke(execute=args.execute)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeError as exc:
        print(f"失败：{exc}")
        raise SystemExit(1) from None
