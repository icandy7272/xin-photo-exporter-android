#!/usr/bin/env python3
"""Direct-API exporter for the family growth feed.

Reverse-engineered on 2026-07-20 via a one-time MITM capture
(see docs/mitm-capture-runbook.md). The native family app pages its
growth feed through:

    POST https://api-gateway.childfolio.net/moment/FamilyMoment/v2/getPageMomentList

with a Bearer token, ``client: fa_app`` / ``lang`` headers and a body of
``{childIds, counter, paChildIds}``. The response carries
``data.momentList[].pictureURLs`` (original .jpeg URLs) plus cursor
pagination via ``data.counter`` / ``data.hasMore``.

This module only pulls the JSON feed (no image rendering), so it stays
low-memory and reaches the whole library without the emulator crashing.
Device discovery, URL validation and downloading are reused from
``export_originals``. The access token is used only in-memory to call the
user's own account; it is never printed, logged or persisted.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Callable

try:  # package import (tests / `python3 -m ...`)
    from tools import export_originals as eo
except ImportError:  # pragma: no cover - `python3 tools/export_originals.py`
    import export_originals as eo


API_BASE = "https://api-gateway.childfolio.net"
FEED_PATH = "/moment/FamilyMoment/v2/getPageMomentList"
API_CLIENT = "fa_app"
API_LANG = "zh-Hans"
API_USER_AGENT = "okhttp/3.14.9"
# Cursor is an internal sequence (~2.36M when observed); a large start returns
# newest. Kept within int32 in case the backend stores it as a Java int.
DEFAULT_INITIAL_COUNTER = 2_000_000_000
DEFAULT_MAX_PAGES = 5000
_PREF_STRING_RE = r'<string name="{key}">([^<]*)</string>'


def extract_pref_string(prefs_xml: str, key: str) -> str | None:
    """Return the value of a ``<string name="key">`` entry, or None."""
    match = re.search(_PREF_STRING_RE.format(key=re.escape(key)), prefs_xml)
    if match is None:
        return None
    return match.group(1) or None


def read_app_credentials(
    device: "eo.Device", run_command: Callable = eo.run_command
) -> tuple[str, str]:
    """Read the Bearer token and album child id from the app's prefs."""
    result = run_command(
        [
            eo.ADB,
            "-s",
            device.serial,
            "shell",
            f"cat /data/data/{eo.PACKAGE}/shared_prefs/*.xml",
        ]
    )
    if result.returncode != 0:
        raise eo.SmokeError("prefs-read-failed")
    token = extract_pref_string(result.stdout, "accessToken")
    child_id = extract_pref_string(result.stdout, "album_child_id")
    if not token or not child_id:
        raise eo.SmokeError("credentials-not-found")
    return token, child_id


def extract_picture_urls(payload: object) -> list[str]:
    """Return ordered, de-duplicated original photo URLs from one page.

    Only ``momentList[].pictureURLs`` is read; avatars/logos in other
    fields are ignored. Each URL is validated as a CDN original .jpeg.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    moments = data.get("momentList") if isinstance(data, dict) else None
    found: list[str] = []
    seen: set[str] = set()
    if not isinstance(moments, list):
        return found
    for moment in moments:
        if not isinstance(moment, dict):
            continue
        pictures = moment.get("pictureURLs")
        if not isinstance(pictures, list):
            continue
        for raw in pictures:
            if not isinstance(raw, str):
                continue
            url = eo.validate_original_url(raw)
            if url is not None and url not in seen:
                seen.add(url)
                found.append(url)
    return found


def _page_pagination(payload: object) -> tuple[bool, int | None]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return False, None
    counter = data.get("counter")
    return bool(data.get("hasMore")), counter if isinstance(counter, int) else None


def collect_api_urls(
    post_page: Callable[[str, int], object],
    child_id: str,
    *,
    initial_counter: int = DEFAULT_INITIAL_COUNTER,
    max_pages: int = DEFAULT_MAX_PAGES,
    progress: Callable[[str], None] = print,
) -> list[str]:
    """Page through the feed with a cursor, collecting unique photo URLs.

    Stops when ``hasMore`` is false, the cursor stops advancing, or
    ``max_pages`` is reached. Ctrl-C stops early and returns what was
    collected so far. ``post_page(child_id, counter) -> payload`` is
    injected so the loop is pure and testable.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    counter = initial_counter
    try:
        for _ in range(max_pages):
            payload = post_page(child_id, counter)
            for url in extract_picture_urls(payload):
                if url not in seen:
                    seen.add(url)
                    ordered.append(url)
            progress(f"已采集原图：{len(ordered)}")
            has_more, next_counter = _page_pagination(payload)
            if not has_more or next_counter is None or next_counter == counter:
                break
            counter = next_counter
    except KeyboardInterrupt:
        progress(f"已停止采集，已采集原图：{len(ordered)}")
    return ordered


def fetch_moment_page(
    opener, token: str, child_id: str, counter: int, timeout: int = 30
) -> object:
    """POST one feed page and return the parsed JSON payload."""
    body = json.dumps(
        {"childIds": [child_id], "counter": counter, "paChildIds": [child_id]}
    ).encode("utf-8")
    request = urllib.request.Request(
        API_BASE + FEED_PATH,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "client": API_CLIENT,
            "lang": API_LANG,
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": API_USER_AGENT,
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise eo.SmokeError("api-not-200")
            return json.loads(response.read().decode("utf-8"))
    except eo.SmokeError:
        raise
    except KeyboardInterrupt:
        raise
    except Exception:
        raise eo.SmokeError("api-request-failed") from None


def run_api(
    *,
    run_command: Callable = eo.run_command,
    opener=None,
    input_fn: Callable = input,
    downloader: Callable | None = None,
    output_dir=None,
    initial_counter: int = DEFAULT_INITIAL_COUNTER,
) -> int:
    """Collect every original via the feed API, then confirm and download."""
    output_dir = output_dir or eo.BATCH_OUTPUT
    try:
        device = eo.discover_running_device(run_command)
        token, child_id = read_app_credentials(device, run_command)
        opener = opener or eo.build_opener()

        def post_page(cid: str, counter: int) -> object:
            return fetch_moment_page(opener, token, cid, counter)

        print("直连 API 采集中（翻页拉取，不经模拟器渲染）…")
        urls = collect_api_urls(post_page, child_id, initial_counter=initial_counter)
        print(f"采集结束：{len(urls)} 个唯一原图候选。")
        if not urls:
            print("没有候选，不创建输出目录。")
            return 0
        if not eo.confirm_download(len(urls), input_fn):
            print("已取消；没有下载照片。")
            return 0
        result = (downloader or eo.download_batch)(urls, output_dir)
    except eo.SmokeError as exc:
        print(f"失败：{exc}")
        return 1
    if isinstance(result, int):
        return result
    if result.failed == 0 and result.unprocessed == 0:
        print("本轮候选下载完成。")
        return 0
    print("本轮候选尚未全部完成。")
    return 1
