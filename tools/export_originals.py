#!/usr/bin/env python3
"""鑫时光集内容导出器：设备发现、URL 校验、媒体下载核心 + `api` CLI。

直连后端 feed 接口的分页/正文/视频逻辑在 ``feed_api`` 中；本模块提供它
复用的下载与校验基础，以及命令行入口。仅处理用户本人账号内容，所有
数据只在本机处理。
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import hashlib
import json
import os
import ssl
import subprocess
import threading
import time
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable
import urllib.parse
import urllib.request


CDN_HOST = "cdn-mctchildfoliocn.childfolio.net"
# `example.invalid` is reserved for offline tests and cannot resolve on the
# public DNS. Keep it separate from the production host so tests never need a
# real CDN hostname while URL validation remains strict.
ALLOWED_CDN_HOSTS = frozenset((CDN_HOST, "example.invalid"))
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
# A single serial stream never fills the link; a few parallel ones do. Kept
# modest on purpose - the CDN throttles aggressive clients.
DEFAULT_WORKERS = 4
MAX_WORKERS = 16
# Content photos are jpeg/jpg or png (all seen on the CDN, no query);
# the extension may be upper- or lower-case, so compare lower-cased.
IMAGE_EXTS = (".jpeg", ".jpg", ".png")
ALLOWED_IMAGE_TYPES = ("image/jpeg", "image/png")
# The oldest uploads are served with a generic binary type instead of an image
# one. IMAGE_MAGIC below is what actually proves the body is an image, so these
# are accepted and left for that check rather than rejected on the header.
GENERIC_BINARY_TYPES = ("application/octet-stream", "binary/octet-stream")
IMAGE_MAGIC = (b"\xff\xd8", b"\x89P")  # JPEG, PNG
SYSTEM_CA_FILE = Path("/etc/ssl/cert.pem")
SETFILE = Path("/usr/bin/SetFile")
RECORD_DATE_RE = re.compile(r"(?:^|/)moments/images/(\d{4}-\d{2}-\d{2})(?:/|$)")
BATCH_OUTPUT = REPOSITORY_ROOT / "build" / "originals"


class SmokeError(RuntimeError):
    """Expected failure whose message contains no sensitive URL."""


@dataclass(frozen=True)
class Device:
    serial: str
    # Which adb speaks to this device. Defaults to MuMu's bundled binary so
    # existing callers keep working; `device.find_adb` supplies the SDK one.
    adb: Path = ADB


@dataclass(frozen=True)
class DownloadResult:
    byte_count: int
    sha256: str


@dataclass(frozen=True)
class CandidateOutcome:
    status: str
    date_failed: bool = False
    reason: str = ""


@dataclass(frozen=True)
class BatchSummary:
    total: int
    downloaded: int
    existing: int
    failed: int
    date_failed: int
    unprocessed: int
    # (reason, count) for the files that failed every pass, most common first.
    failure_reasons: tuple[tuple[str, int], ...] = ()


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


def clamp_workers(workers: int) -> int:
    """Keep concurrency inside a range the CDN tolerates."""
    return max(1, min(int(workers), MAX_WORKERS))


def _thread_opener(opener) -> Callable:
    """Return a callable handing each worker thread its own opener.

    urllib openers carry per-request state, so threads must not share one. An
    explicitly injected opener is passed through unchanged for tests.
    """
    if opener is not None:
        return lambda: opener
    local = threading.local()

    def get_opener():
        if getattr(local, "opener", None) is None:
            local.opener = build_opener()
        return local.opener

    return get_opener


def run_concurrently(
    items: list,
    task: Callable,
    *,
    workers: int,
    progress: Callable[[int, int], None],
) -> tuple[dict, bool]:
    """Run ``task`` over ``items`` in a thread pool, keyed by item.

    Returns ``(results_by_item, interrupted)``. On Ctrl-C the queued work is
    cancelled and whatever already finished is returned, so a long run can be
    stopped without throwing away its progress.
    """
    results: dict = {}
    if not items:
        return results, False
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=clamp_workers(workers))
    futures = {pool.submit(task, item): item for item in items}
    interrupted = False
    try:
        for future in concurrent.futures.as_completed(futures):
            results[futures[future]] = future.result()
            progress(len(results), len(items))
    except KeyboardInterrupt:
        interrupted = True
    # Cancel what has not started, but wait for what is mid-flight: the pool's
    # threads never see the interrupt, and abandoning them races with whatever
    # they are still writing. The interpreter joins them at exit regardless, so
    # waiting costs no extra time - it only makes the outcome predictable.
    pool.shutdown(wait=True, cancel_futures=interrupted)
    if interrupted:
        for future, item in futures.items():
            if item in results or future.cancelled() or not future.done():
                continue
            try:  # salvage work that finished while we were unwinding
                results[item] = future.result()
            except BaseException:
                pass
    return results, interrupted


def run_command(argv: list[str | Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(value) for value in argv],
        capture_output=True,
        text=True,
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
    if parsed.hostname not in ALLOWED_CDN_HOSTS or port not in (None, 443):
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    # Case-insensitive: older uploads kept the camera's ".JPG".
    if not parsed.path.lower().endswith(IMAGE_EXTS):
        return None
    return raw


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
    ext = ".png" if url.split("?", 1)[0].lower().endswith(".png") else ".jpeg"
    return output_dir / f"{prefix}_{digest}{ext}"


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
    run_command: Callable = run_command, adb: Path = ADB
) -> Device:
    """Find the single running MuMu instance and connect adb to it.

    MuMu-specific; `tools.device.discover_device` is the generic entry point
    and falls back to this when no device is attached.
    """
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
    connected = run_command([adb, "connect", serial])
    if connected.returncode != 0:
        raise SmokeError("mumu-not-running")
    return Device(serial=serial, adb=adb)


def apply_record_date(
    destination: Path,
    record_date: date | None,
    run_command: Callable = run_command,
) -> bool:
    if record_date is None:
        return True
    local_noon = datetime(
        record_date.year, record_date.month, record_date.day, 12, 0, 0
    ).astimezone()
    timestamp = local_noon.timestamp()
    formatted = record_date.strftime("%m/%d/%Y 12:00:00")
    try:
        os.utime(destination, (timestamp, timestamp))
        result = run_command([SETFILE, "-d", formatted, "-m", formatted, destination])
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
    request = urllib.request.Request(url, headers={"User-Agent": "xin-photo-export/1"})
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise SmokeError("http-not-200")
            content_type = (
                response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            )
            if content_type not in ALLOWED_IMAGE_TYPES + GENERIC_BINARY_TYPES:
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
        if first_bytes not in IMAGE_MAGIC:
            raise SmokeError("invalid-image")
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


def looks_like_existing_image(path: Path) -> bool:
    try:
        if path.stat().st_size <= MIN_BYTES:
            return False
        with path.open("rb") as handle:
            return handle.read(2) in IMAGE_MAGIC
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
    if looks_like_existing_image(destination):
        try:
            date_ok = date_setter(destination, record_date)
        except Exception:
            date_ok = False
        return CandidateOutcome("existing", date_failed=not date_ok)
    try:
        download_sample(opener, url, destination)
    except SmokeError as exc:
        return CandidateOutcome("failed", reason=str(exc))
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
    workers: int = DEFAULT_WORKERS,
) -> BatchSummary:
    """Download every unique URL, retrying failures over three passes.

    Each pass runs concurrently; the pass structure is kept so a transient
    failure still gets two more chances before it is counted as failed.
    """
    ensure_build_is_ignored()
    candidates = list(dict.fromkeys(urls))
    candidate_downloader = candidate_downloader or download_batch_candidate
    get_opener = _thread_opener(opener)
    previous_umask = os.umask(0o077)
    downloaded = 0
    existing = 0
    failed = 0
    date_failed = 0
    reasons: collections.Counter[str] = collections.Counter()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        pending = candidates
        for pass_index in range(3):
            if not pending:
                break
            if pass_index > 0:
                sleep_fn(2)

            def task(url: str) -> CandidateOutcome:
                return candidate_downloader(
                    url, output_dir, opener=get_opener(), date_setter=date_setter
                )

            label = f"第 {pass_index + 1} 轮"
            outcomes, interrupted = run_concurrently(
                pending,
                task,
                workers=workers,
                progress=lambda done, total: print(f"{label}：{done}/{total}"),
            )
            next_pending: list[str] = []
            for url in pending:
                outcome = outcomes.get(url)
                if outcome is None:  # cancelled before it ran
                    continue
                if outcome.status == "downloaded":
                    downloaded += 1
                    date_failed += int(outcome.date_failed)
                elif outcome.status == "existing":
                    existing += 1
                    date_failed += int(outcome.date_failed)
                elif pass_index == 2:
                    failed += 1
                    reasons[outcome.reason or "unknown"] += 1
                else:
                    next_pending.append(url)
            if interrupted:
                summary = BatchSummary(
                    len(candidates),
                    downloaded,
                    existing,
                    failed,
                    date_failed,
                    len(pending) - len(outcomes) + len(next_pending),
                    tuple(reasons.most_common()),
                )
                _print_batch_summary(summary, output_dir)
                return summary
            pending = next_pending
        summary = BatchSummary(
            len(candidates),
            downloaded,
            existing,
            failed,
            date_failed,
            0,
            tuple(reasons.most_common()),
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
    for reason, count in summary.failure_reasons:
        print(f"  失败原因：{reason} × {count}")
    print(f"输出目录：{output_dir}")


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


def confirm_download(candidate_count: int, input_fn: Callable = input) -> bool:
    try:
        answer = input_fn(
            f"已发现 {candidate_count} 个唯一原图候选。输入 DOWNLOAD 开始下载："
        )
    except (EOFError, KeyboardInterrupt):
        return False
    return answer == "DOWNLOAD"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="鑫时光集内容导出器（直连 API：照片 / 视频 / 帖子正文）"
    )
    subparsers = parser.add_subparsers(dest="command")
    api_parser = subparsers.add_parser(
        "api", help="直连后端 API 分页导出全部原图 / 视频 / 正文"
    )
    api_parser.add_argument(
        "--counter",
        type=int,
        default=None,
        help="起始分页游标（不填则二分自动定位当前最新）",
    )
    api_parser.add_argument(
        "--no-videos",
        action="store_true",
        help="只下照片，不下载视频（正文仍会保存）",
    )
    api_parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"并发下载数（默认 {DEFAULT_WORKERS}，上限 {MAX_WORKERS}；网络差可调小）",
    )
    api_parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过 DOWNLOAD 交互确认，直接下载（用于非交互 / 自动化）",
    )
    api_parser.add_argument(
        "--out",
        type=Path,
        default=None,
        metavar="目录",
        help="输出目录（默认 build/）。给不同孩子分别指定，导出的内容才不会混在一起",
    )
    device_group = api_parser.add_argument_group(
        "安卓设备",
        "只用于读一次登录态。MuMu、Android Studio 模拟器（Google APIs 镜像）"
        "和已 root 的真机都可以；给了 token 就完全用不到设备。",
    )
    device_group.add_argument(
        "--adb",
        default=None,
        metavar="路径",
        help="adb 可执行文件路径（默认自动查找 Android SDK、PATH，最后回退 MuMu 自带的）",
    )
    device_group.add_argument(
        "--serial",
        default=None,
        help="指定 adb 设备序列号（同时连了多台时用；`adb devices` 可查）",
    )
    device_group.add_argument(
        "--package",
        default=PACKAGE,
        help=f"App 包名（默认 {PACKAGE}）",
    )
    device_group.add_argument(
        "--list-children",
        action="store_true",
        help="只列出账号下的孩子档案 ID 后退出，供 --child-id 使用",
    )
    api_parser.add_argument(
        "--child-id",
        action="append",
        default=None,
        metavar="ID",
        help="只导出指定孩子（可重复；不填则导出账号下全部）",
    )
    device_group.add_argument(
        "--save-token",
        type=Path,
        default=None,
        metavar="文件",
        help="把当前登录信息存到文件后退出，供以后 --token-file 使用（不要放在本工具目录里）",
    )
    api_parser.add_argument(
        "--token-file",
        type=Path,
        default=None,
        metavar="文件",
        help=(
            "存有登录 token 的文件；也可用环境变量 XIN_ACCESS_TOKEN。"
            "配合 --child-id 使用可完全不需要模拟器（token 过期前有效）"
        ),
    )
    return parser


def _modules():
    """Import the sibling modules late, so the CLI works either way it is run."""
    try:
        from tools import credentials, feed_api
    except ImportError:  # pragma: no cover - `python3 tools/export_originals.py`
        import credentials
        import feed_api
    return credentials, feed_api


def _run_api(args: argparse.Namespace) -> int:
    credentials, feed_api = _modules()
    try:
        if args.save_token:
            return feed_api.run_save_token(
                token_file=args.save_token,
                adb_path=args.adb,
                serial=args.serial,
                package=args.package,
            )
        if args.list_children:
            return feed_api.run_list_children(
                adb_path=args.adb, serial=args.serial, package=args.package
            )
        # Resolved before any network or device work so a typo fails at once.
        token = credentials.load_token(args.token_file)
        child_ids = credentials.normalise_child_ids(args.child_id)
    except credentials.eo.SmokeError as exc:
        # Run as a script this file is imported twice (as ``__main__`` and as
        # ``export_originals``), so catch the class the modules actually raise.
        print(f"失败：{exc}")
        return 1
    return feed_api.run_api(
        initial_counter=args.counter,
        include_videos=not args.no_videos,
        assume_yes=args.yes,
        workers=args.workers,
        build_root=args.out or feed_api.BUILD_ROOT,
        adb_path=args.adb,
        serial=args.serial,
        package=args.package,
        token=token,
        child_ids=child_ids,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "api":
        try:
            return _run_api(args)
        except SmokeError as exc:
            print(f"失败：{exc}")
            return 1
    parser.print_help()
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeError as exc:
        print(f"失败：{exc}")
        raise SystemExit(1) from None
