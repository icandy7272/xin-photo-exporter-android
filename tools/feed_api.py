#!/usr/bin/env python3
"""Direct-API exporter for the family growth feed.

Reverse-engineered on 2026-07-20 via a one-time MITM capture
(see docs/mitm-capture-runbook.md). The native family app pages its
growth feed through:

    POST https://api-gateway.childfolio.net/moment/FamilyMoment/v2/getPageMomentList

with a Bearer token, ``client: fa_app`` / ``lang`` headers and a body of
``{childIds, counter, paChildIds}``. Each moment carries
``pictureURLs`` (original .jpeg), ``videoUrl``, ``momentCaption`` (post
text) and ``publishedTime``; pagination is a ``counter`` cursor plus
``hasMore``. ``childIds`` must list *every* child of the account: the
server ends the feed at the oldest post of the ids it was given.

This module only pulls the JSON feed (no image rendering), so it stays
low-memory and reaches the whole library without the emulator crashing.
It exports photos (build/originals/), videos (build/videos/) and the
post text (build/moments.jsonl + build/captions.txt). Device discovery,
URL validation and photo downloading are reused from ``export_originals``.
The access token is used only in-memory to call the user's own account;
it is never printed, logged or persisted. Manifests reference local
filenames only, never URLs.
"""

from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

try:  # package import (tests / `python3 -m ...`)
    from tools import export_originals as eo
    from tools import credentials as creds
except ImportError:  # pragma: no cover - `python3 tools/export_originals.py`
    import export_originals as eo
    import credentials as creds


API_BASE = "https://api-gateway.childfolio.net"
FEED_PATH = "/moment/FamilyMoment/v2/getPageMomentList"
API_CLIENT = "fa_app"
API_LANG = "zh-Hans"
API_USER_AGENT = "okhttp/3.14.9"
# The feed pages by a `counter` cursor that grows over time (higher = newer).
# Empirically the API returns data only up to just above the newest post; higher
# counters return an empty page. So the start counter is found dynamically
# (find_start_counter): binary-search between COUNTER_SEED (a value known to sit
# inside the populated range, ~2026-04) and COUNTER_CEILING (known empty).
COUNTER_SEED = 2_360_360
COUNTER_CEILING = 2_000_000_000
DEFAULT_MAX_PAGES = 5000

VIDEO_EXTS = (".mp4", ".mov", ".m4v")
VIDEO_MIN_BYTES = 1024
VIDEO_MAX_BYTES = 1024 * 1024 * 1024  # 1 GiB safety cap
_VIDEO_CONTENT_TYPES = ("application/octet-stream", "application/mp4", "binary/octet-stream")

BUILD_ROOT = eo.REPOSITORY_ROOT / "build"
PHOTO_OUTPUT = eo.BATCH_OUTPUT  # build/originals
VIDEO_OUTPUT = BUILD_ROOT / "videos"
MANIFEST_PATH = BUILD_ROOT / "moments.jsonl"
CAPTIONS_PATH = BUILD_ROOT / "captions.txt"


@dataclass(frozen=True)
class MomentRecord:
    """One post: text plus its validated media URLs."""

    moment_id: str
    published_time: str
    caption: str
    picture_urls: tuple[str, ...]
    video_url: str | None


@dataclass(frozen=True)
class VideoSummary:
    total: int
    downloaded: int
    existing: int
    failed: int


@dataclass(frozen=True)
class OutputPaths:
    """Where one export writes. All four hang off a single root directory."""

    root: Path
    photos: Path
    videos: Path
    manifest: Path
    captions: Path


def output_paths(root: Path = BUILD_ROOT) -> OutputPaths:
    """Derive every output location from one root.

    A separate root per run is how one operator keeps several children's
    exports apart: filenames are date+hash only, so two children sharing a
    root would interleave into one indistinguishable pile.
    """
    return OutputPaths(
        root=root,
        photos=root / "originals",
        videos=root / "videos",
        manifest=root / "moments.jsonl",
        captions=root / "captions.txt",
    )


def _is_inside_repository(path: Path) -> bool:
    try:
        Path(path).resolve().relative_to(eo.REPOSITORY_ROOT)
    except ValueError:
        return False
    return True


def validate_video_url(raw: str) -> str | None:
    """Accept an https CDN video URL (.mp4/.mov/.m4v), else None."""
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname not in eo.ALLOWED_CDN_HOSTS:
        return None
    if parsed.username or parsed.password:
        return None
    if not parsed.path.lower().endswith(VIDEO_EXTS):
        return None
    return raw


def extract_moments(payload: object) -> list[MomentRecord]:
    """Build per-post records from one feed page.

    Only ``pictureURLs`` and ``videoUrl`` are treated as the child's
    media; avatars/logos in other fields are ignored.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    moments = data.get("momentList") if isinstance(data, dict) else None
    records: list[MomentRecord] = []
    if not isinstance(moments, list):
        return records
    for moment in moments:
        if not isinstance(moment, dict):
            continue
        pictures: list[str] = []
        seen: set[str] = set()
        raw_pictures = moment.get("pictureURLs")
        if isinstance(raw_pictures, list):
            for raw in raw_pictures:
                if not isinstance(raw, str):
                    continue
                url = eo.validate_original_url(raw)
                if url is not None and url not in seen:
                    seen.add(url)
                    pictures.append(url)
        raw_video = moment.get("videoUrl")
        video = validate_video_url(raw_video) if isinstance(raw_video, str) else None
        records.append(
            MomentRecord(
                moment_id=str(moment.get("momentId") or ""),
                published_time=str(moment.get("publishedTime") or ""),
                caption=str(moment.get("momentCaption") or ""),
                picture_urls=tuple(pictures),
                video_url=video,
            )
        )
    return records


def _page_pagination(payload: object) -> tuple[bool, int | None]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return False, None
    counter = data.get("counter")
    return bool(data.get("hasMore")), counter if isinstance(counter, int) else None


def collect_moments(
    post_page: Callable[[tuple[str, ...], int], object],
    child_ids: tuple[str, ...],
    *,
    initial_counter: int = COUNTER_SEED,
    max_pages: int = DEFAULT_MAX_PAGES,
    progress: Callable[[str], None] = print,
) -> list[MomentRecord]:
    """Page through the feed with a cursor, collecting unique posts.

    De-duplicates by ``momentId``. Stops when ``hasMore`` is false, the
    cursor stops advancing, or ``max_pages`` is reached. Ctrl-C stops
    early and returns what was collected. ``post_page`` is injected so
    the loop is pure and testable.
    """
    ordered: list[MomentRecord] = []
    seen_ids: set[str] = set()
    photo_count = 0
    counter = initial_counter
    try:
        for _ in range(max_pages):
            payload = post_page(child_ids, counter)
            for record in extract_moments(payload):
                if record.moment_id and record.moment_id in seen_ids:
                    continue
                if record.moment_id:
                    seen_ids.add(record.moment_id)
                ordered.append(record)
                photo_count += len(record.picture_urls)
            progress(f"已采集：{len(ordered)} 帖 / {photo_count} 原图")
            has_more, next_counter = _page_pagination(payload)
            if not has_more or next_counter is None or next_counter == counter:
                break
            counter = next_counter
    except KeyboardInterrupt:
        progress(f"已停止采集，已采集：{len(ordered)} 帖")
    return ordered


def unique_picture_urls(records: list[MomentRecord]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for record in records:
        for url in record.picture_urls:
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def collect_api_urls(
    post_page: Callable[[tuple[str, ...], int], object],
    child_ids: tuple[str, ...],
    *,
    initial_counter: int = COUNTER_SEED,
    max_pages: int = DEFAULT_MAX_PAGES,
    progress: Callable[[str], None] = print,
) -> list[str]:
    """Backwards-compatible helper: just the de-duplicated photo URLs."""
    return unique_picture_urls(
        collect_moments(
            post_page,
            child_ids,
            initial_counter=initial_counter,
            max_pages=max_pages,
            progress=progress,
        )
    )


def find_start_counter(
    page_has_data: Callable[[int], bool],
    *,
    seed: int = COUNTER_SEED,
    ceiling: int = COUNTER_CEILING,
    progress: Callable[[str], None] = print,
) -> int | None:
    """Binary-search the largest counter that still returns moments.

    The feed returns data only up to just above the newest post; higher
    counters return an empty page. ``page_has_data`` is monotonic (True on
    ``[oldest, newest]``, False above), so a binary search over
    ``[seed, ceiling]`` finds the newest usable counter. ``seed`` must sit
    inside the populated range. Returns None if even the seed is empty.
    ``page_has_data`` is injected so the search is pure and testable.
    """
    if not page_has_data(seed):
        return None
    low, high = seed, ceiling
    while high - low > 1:
        mid = (low + high) // 2
        if page_has_data(mid):
            low = mid
        else:
            high = mid
    progress(f"最新游标：{low}")
    return low


def fetch_moment_page(
    opener, token: str, child_ids: tuple[str, ...], counter: int, timeout: int = 30
) -> object:
    """POST one feed page for all children and return the parsed payload."""
    ids = list(child_ids)
    body = json.dumps(
        {"childIds": ids, "counter": counter, "paChildIds": ids}
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


_URL_DATE_RE = re.compile(r"/(\d{4}-\d{2}-\d{2})(?:/|$)")


def _url_date(url: str) -> date | None:
    """Extract a record date from any ``/YYYY-MM-DD/`` path segment."""
    try:
        path = urllib.parse.urlsplit(url).path
    except ValueError:
        return None
    match = _URL_DATE_RE.search(path)
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def video_destination(url: str, output_dir: Path) -> Path:
    record_date = _url_date(url)
    prefix = record_date.isoformat() if record_date else "unknown-date"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return output_dir / f"{prefix}_{digest}.mp4"


def download_video(opener, url: str, destination: Path, timeout: int = 600) -> int:
    """Stream a video to ``destination`` atomically; return byte count."""
    part = destination.with_suffix(destination.suffix + ".part")
    eo._safe_unlink(part)
    byte_count = 0
    request = urllib.request.Request(url, headers={"User-Agent": API_USER_AGENT})
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise eo.SmokeError("http-not-200")
            content_type = (
                response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            )
            if not (
                content_type.startswith("video/")
                or content_type in _VIDEO_CONTENT_TYPES
            ):
                raise eo.SmokeError("wrong-content-type")
            with part.open("xb") as output:
                while True:
                    chunk = response.read(eo.CHUNK_BYTES)
                    if not chunk:
                        break
                    byte_count += len(chunk)
                    if byte_count > VIDEO_MAX_BYTES:
                        raise eo.SmokeError("too-large")
                    output.write(chunk)
        if byte_count <= VIDEO_MIN_BYTES:
            raise eo.SmokeError("too-small")
        os.replace(part, destination)
        return byte_count
    except (eo.SmokeError, KeyboardInterrupt):
        eo._safe_unlink(part)
        raise
    except Exception:
        eo._safe_unlink(part)
        raise eo.SmokeError("download-failed") from None


def _video_outcome(
    url: str,
    output_dir: Path,
    *,
    opener,
    downloader: Callable,
    date_setter: Callable,
) -> str:
    """Fetch one video; return "existing" / "downloaded" / "failed"."""
    destination = video_destination(url, output_dir)
    if destination.exists() and destination.stat().st_size > VIDEO_MIN_BYTES:
        return "existing"
    try:
        downloader(opener, url, destination)
    except eo.SmokeError:
        return "failed"
    try:
        date_setter(destination, _url_date(url))
    except Exception:
        pass
    return "downloaded"


def download_videos(
    records: list[MomentRecord],
    output_dir: Path = VIDEO_OUTPUT,
    *,
    opener=None,
    date_setter: Callable = eo.apply_record_date,
    video_downloader: Callable | None = None,
    workers: int = eo.DEFAULT_WORKERS,
) -> VideoSummary:
    """Download every unique video from the collected posts, concurrently."""
    urls: list[str] = []
    seen: set[str] = set()
    for record in records:
        if record.video_url and record.video_url not in seen:
            seen.add(record.video_url)
            urls.append(record.video_url)
    if not urls:
        return VideoSummary(0, 0, 0, 0)
    downloader = video_downloader or download_video
    get_opener = eo._thread_opener(opener)
    previous_umask = os.umask(0o077)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)

        def task(url: str) -> str:
            return _video_outcome(
                url,
                output_dir,
                opener=get_opener(),
                downloader=downloader,
                date_setter=date_setter,
            )

        outcomes, _ = eo.run_concurrently(
            urls,
            task,
            workers=workers,
            progress=lambda done, total: print(f"视频 {done}/{total}"),
        )
    finally:
        os.umask(previous_umask)
    counted = collections.Counter(outcomes.values())
    return VideoSummary(
        len(urls),
        counted.get("downloaded", 0),
        counted.get("existing", 0),
        counted.get("failed", 0),
    )


def _photo_filenames(record: MomentRecord) -> list[str]:
    return [
        f"originals/{eo.batch_destination(url, Path('.')).name}"
        for url in record.picture_urls
    ]


def _video_filename(record: MomentRecord) -> str | None:
    if not record.video_url:
        return None
    return f"videos/{video_destination(record.video_url, Path('.')).name}"


def write_manifest(records: list[MomentRecord], path: Path = MANIFEST_PATH) -> int:
    """Write one JSON line per post: text + local filenames (no URLs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_umask = os.umask(0o077)
    try:
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(
                    json.dumps(
                        {
                            "momentId": record.moment_id,
                            "publishedTime": record.published_time,
                            "caption": record.caption,
                            "photos": _photo_filenames(record),
                            "video": _video_filename(record),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    finally:
        os.umask(previous_umask)
    return len(records)


def write_captions(records: list[MomentRecord], path: Path = CAPTIONS_PATH) -> int:
    """Write a human-readable ``[time] caption`` file (non-empty only)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    previous_umask = os.umask(0o077)
    try:
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                caption = record.caption.strip()
                if not caption:
                    continue
                stamp = record.published_time or "unknown-time"
                handle.write(f"[{stamp}] {caption}\n\n")
                written += 1
    finally:
        os.umask(previous_umask)
    return written


def run_list_children(
    *,
    adb_path: str | Path | None = None,
    serial: str | None = None,
    package: str = eo.PACKAGE,
    run_command: Callable = eo.run_command,
    printer: Callable[[str], None] = print,
) -> int:
    """Print the child ids the logged-in account can see, for --child-id."""
    try:
        child_ids = creds.extract_child_ids(
            creds.prefs_reader(
                adb_path=adb_path,
                serial=serial,
                package=package,
                run_command=run_command,
            )()
        )
    except eo.SmokeError as exc:
        printer(f"失败：{exc}")
        return 1
    if not child_ids:
        printer("失败：credentials-not-found")
        return 1
    printer(f"账号下共 {len(child_ids)} 个孩子档案：")
    for index, child_id in enumerate(child_ids, 1):
        printer(f"  {index}. {child_id}")
    printer("用 --child-id <ID> 单独导出其中一个；档案 ID 属于个人信息，请勿外发。")
    return 0


def run_api(
    *,
    run_command: Callable = eo.run_command,
    opener=None,
    input_fn: Callable = input,
    downloader: Callable | None = None,
    video_downloader: Callable | None = None,
    build_root: Path = BUILD_ROOT,
    output_dir: Path | None = None,
    video_output_dir: Path | None = None,
    include_videos: bool = True,
    assume_yes: bool = False,
    initial_counter: int | None = None,
    workers: int = eo.DEFAULT_WORKERS,
    adb_path: str | Path | None = None,
    serial: str | None = None,
    package: str = eo.PACKAGE,
    token: str | None = None,
    child_ids: tuple[str, ...] = (),
) -> int:
    """Collect the whole feed, save post text, then confirm and download."""
    paths = output_paths(build_root)
    output_dir = output_dir or paths.photos
    video_output_dir = video_output_dir or paths.videos
    try:
        requested_ids = tuple(child_ids)
        token, child_ids = creds.resolve_credentials(
            token=token,
            child_ids=requested_ids,
            read_prefs=creds.prefs_reader(
                adb_path=adb_path,
                serial=serial,
                package=package,
                run_command=run_command,
            ),
        )
        opener = opener or eo.build_opener()
        if requested_ids:
            print(f"只拉取指定的 {len(child_ids)} 个孩子档案。")
        else:
            print(f"账号下共 {len(child_ids)} 个孩子档案，一起拉取。")

        def post_page(cids: tuple[str, ...], counter: int) -> object:
            return fetch_moment_page(opener, token, cids, counter)

        if initial_counter is None:
            print("定位最新游标中（二分探测）…")

            def has_data(counter: int) -> bool:
                payload = post_page(child_ids, counter)
                data = payload.get("data") if isinstance(payload, dict) else None
                moments = data.get("momentList") if isinstance(data, dict) else None
                return bool(moments)

            initial_counter = find_start_counter(has_data)
            if initial_counter is None:
                raise eo.SmokeError("counter-seed-empty")
        print("直连 API 采集中（翻页拉取，不经模拟器渲染）…")
        records = collect_moments(post_page, child_ids, initial_counter=initial_counter)
        photo_urls = unique_picture_urls(records)
        video_total = sum(1 for record in records if record.video_url)
        print(
            f"采集结束：{len(records)} 帖，{len(photo_urls)} 原图，{video_total} 视频。"
        )
        if not records:
            print("没有帖子，不写文件。")
            return 0
        # The .gitignore rule only protects the repository's own build dir; an
        # export written elsewhere is the operator's to place safely.
        if _is_inside_repository(paths.root):
            eo.ensure_build_is_ignored()
        write_manifest(records, paths.manifest)
        captions = write_captions(records, paths.captions)
        print(f"帖子清单：{paths.manifest}（{len(records)} 条）")
        print(f"正文文本：{paths.captions}（{captions} 条有正文）")
        if not photo_urls and video_total == 0:
            print("没有可下载的媒体（正文已保存）。")
            return 0
        if assume_yes:
            print(f"已确认下载（--yes）：{len(photo_urls)} 原图，{video_total} 视频")
        elif not eo.confirm_download(len(photo_urls), input_fn):
            print("已取消媒体下载；正文已保存。")
            return 0
        photo_result = (downloader or eo.download_batch)(
            photo_urls, output_dir, workers=workers
        )
        video_summary = VideoSummary(0, 0, 0, 0)
        if include_videos and video_total:
            video_summary = download_videos(
                records,
                video_output_dir,
                opener=opener,
                video_downloader=video_downloader,
                workers=workers,
            )
            print(
                "视频结果："
                f"下载 {video_summary.downloaded}，已存在 {video_summary.existing}，"
                f"失败 {video_summary.failed}"
            )
            print(f"视频目录：{video_output_dir}")
    except eo.SmokeError as exc:
        print(f"失败：{exc}")
        return 1
    if isinstance(photo_result, int):
        photo_ok = photo_result == 0
    else:
        photo_ok = photo_result.failed == 0 and photo_result.unprocessed == 0
    if photo_ok and video_summary.failed == 0:
        print("本轮媒体下载完成。")
        return 0
    print("本轮媒体尚未全部完成。")
    return 1
