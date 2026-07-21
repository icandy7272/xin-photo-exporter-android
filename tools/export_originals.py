#!/usr/bin/env python3
"""鑫时光集内容导出器：设备发现、URL 校验、媒体下载核心 + `api` CLI。

直连后端 feed 接口的分页/正文/视频逻辑在 ``feed_api`` 中；本模块提供它
复用的下载与校验基础，以及命令行入口。仅处理用户本人账号内容，所有
数据只在本机处理。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import subprocess
import time
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable
import urllib.parse
import urllib.request


CDN_HOST = "cdn-mctchildfoliocn.childfolio.net"
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
# Content photos are jpeg/jpg or png (all seen on the CDN, no query).
IMAGE_EXTS = (".jpeg", ".jpg", ".png")
ALLOWED_IMAGE_TYPES = ("image/jpeg", "image/png")
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
    if not parsed.path.endswith(IMAGE_EXTS):
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


def discover_running_device(run_command: Callable = run_command) -> Device:
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
            if content_type not in ALLOWED_IMAGE_TYPES:
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
        "--yes",
        action="store_true",
        help="跳过 DOWNLOAD 交互确认，直接下载（用于非交互 / 自动化）",
    )
    return parser


def _run_api(counter: int | None, include_videos: bool, assume_yes: bool) -> int:
    try:
        from tools.feed_api import run_api
    except ImportError:
        from feed_api import run_api
    return run_api(
        initial_counter=counter,
        include_videos=include_videos,
        assume_yes=assume_yes,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "api":
        return _run_api(
            args.counter, include_videos=not args.no_videos, assume_yes=args.yes
        )
    parser.print_help()
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeError as exc:
        print(f"失败：{exc}")
        raise SystemExit(1) from None
