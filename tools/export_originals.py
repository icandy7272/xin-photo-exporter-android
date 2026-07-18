#!/usr/bin/env python3
"""Minimal, privacy-conscious original-photo smoke exporter."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import urllib.parse


CDN_HOST = "cdn-mctchildfoliocn.childfolio.net"
ORIGINAL_URL_RE = re.compile(r"原图地址::(\S+)")
MUMUTOOL = Path("/Applications/MuMuPlayer.app/Contents/MacOS/mumutool")
ADB = Path(
    "/Applications/MuMuPlayer.app/Contents/MacOS/"
    "MuMuEmulator.app/Contents/MacOS/tools/adb"
)
PACKAGE = "com.childfolio.family"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class SmokeError(RuntimeError):
    """Expected smoke-check failure whose message contains no sensitive URL."""


@dataclass(frozen=True)
class Device:
    serial: str


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


def select_samples(urls: list[str], count: int = 3) -> list[str]:
    samples = list(dict.fromkeys(urls))[:count]
    if len(samples) < count:
        raise SmokeError("not-enough-candidates")
    return samples


def _info_rows(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("devices", "data", "infos", "items"):
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
    if downloader is None:
        raise SmokeError("downloader-not-ready")
    return downloader(samples, destination)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="鑫时光集 Android 原图 Smoke 验证")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="明确下载且只下载三个原图样本",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_smoke(execute=args.execute)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeError as exc:
        print(f"失败：{exc}")
        raise SystemExit(1) from None
