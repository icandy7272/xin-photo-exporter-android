#!/usr/bin/env python3
"""一问一答的导出向导：把 7 条命令压成 1 条。

设计目标是让不懂命令行的家长/老师能独立走完，所以砍掉了三处纯粹的摩擦：

* **不再手抄档案 ID。** 原来要先跑 ``--list-children`` 看到一串 UUID，
  再整串复制到 ``--child-id`` 后面，而且根本分不出哪个 UUID 是哪个孩子。
  现在为每个档案抓一页动态，显示最近更新日期和一句正文，让人直接认出来，
  然后输 1 / 2 就行——UUID 全程不出现（它本身也是个人信息）。
* **没登录不再是终点。** 原来直接报 ``credentials-not-found`` 退出，
  用户去登录完还得从头重跑。现在原地等着，登录好按回车继续。
* **不用找文件。** 跑完直接把文件夹打开。

向导只负责问和引导，真正的采集下载仍然走 ``feed_api.run_api``，
两条路的行为完全一致。
"""

from __future__ import annotations

import hashlib
import io
import re
import subprocess
import zipfile
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

try:  # package import (tests / `python3 -m ...`)
    from tools import credentials as creds
    from tools import device as android
    from tools import export_originals as eo
    from tools import feed_api
    from tools import setup_emulator
except ImportError:  # pragma: no cover - `python3 tools/wizard.py`
    import credentials as creds
    import device as android
    import export_originals as eo
    import feed_api
    import setup_emulator


CAPTION_LIMIT = 24
DEFAULT_EXPORT_DIRNAME = "鑫时光集导出"
# Enough tries for a real login, few enough that a wedged run still ends.
MAX_LOGIN_ATTEMPTS = 10
# SHA-256 of apks we have actually handled, mapped to their version. A match
# only proves "byte-identical to the copy we pinned" - never "official" - so
# the wording downstream stays modest. New app versions will not be in here;
# that is why a mismatch warns instead of blocking.
KNOWN_APK_HASHES = {
    "9394e2d829fcd2c29f01b390883e28dab82098216af4029c14f7ad8eaf2aae85": "1.9.17",
}
_SEPARATORS = re.compile(r"[,\s、，]+")
_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_QUIT_ANSWERS = frozenset({"q", "quit", "退出"})
_ALL_ANSWERS = frozenset({"a", "all", "全部", "都要"})


@dataclass(frozen=True)
class ChildSummary:
    """What we can tell a parent about one archive without naming it.

    ``sampled_photos`` counts only the page we fetched, never the whole
    library - so it is labelled as a sample rather than a total.
    """

    child_id: str
    latest_time: str
    caption_excerpt: str
    sampled_photos: int


def excerpt(text: str, limit: int = CAPTION_LIMIT) -> str:
    """Collapse a caption onto one line, cut to ``limit`` characters."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…"


def format_date(raw: str) -> str:
    """Show `2026-04-30`, not `2026-04-30T09:00:13.491Z`.

    The feed returns full ISO timestamps; the menu only needs the day, and
    the raw form is noise to whoever is trying to recognise their child.
    """
    match = _DATE_PREFIX.match(raw)
    return match.group(1) if match else raw


def summarise_child(child_id: str, records: Sequence) -> ChildSummary:
    """Describe one archive from a sampled page of its feed."""
    latest_time = ""
    caption = ""
    photos = 0
    for record in sorted(records, key=lambda r: r.published_time, reverse=True):
        photos += len(record.picture_urls)
        if not latest_time:
            latest_time = record.published_time
        # The newest post is often a wordless photo; fall through to the
        # newest one that actually says something.
        if not caption and record.caption.strip():
            caption = record.caption
    return ChildSummary(
        child_id=child_id,
        latest_time=latest_time,
        caption_excerpt=excerpt(caption),
        sampled_photos=photos,
    )


def format_child_line(index: int, summary: ChildSummary) -> str:
    """One menu row. Deliberately never shows the child id."""
    parts = [
        f"最近更新 {format_date(summary.latest_time)}"
        if summary.latest_time
        else "暂无内容"
    ]
    if summary.sampled_photos:
        parts.append(f"近期 {summary.sampled_photos} 张照片")
    if summary.caption_excerpt:
        parts.append(f"“{summary.caption_excerpt}”")
    return f"  {index}) " + " · ".join(parts)


def parse_child_selection(answer: str, count: int) -> tuple[int, ...] | None:
    """Turn a typed menu answer into archive indexes, or None if unusable."""
    cleaned = answer.strip()
    if not cleaned:
        return None
    if cleaned.lower() in _ALL_ANSWERS:
        return tuple(range(count))
    chosen: list[int] = []
    for token in _SEPARATORS.split(cleaned):
        if not token.isdigit():
            return None
        number = int(token)
        if not 1 <= number <= count:
            return None
        if number - 1 not in chosen:
            chosen.append(number - 1)
    return tuple(chosen) if chosen else None


def sanitize_folder_name(name: str) -> str | None:
    """Make a typed name safe as a single folder, or None if unusable.

    Separators become underscores rather than nested folders: someone typing
    a nickname with a slash means one folder, not a directory tree.
    """
    stripped = name.strip()
    # Judge the typed text before substituting: an answer made only of
    # separators and dots is a non-answer, and turning it into a folder
    # called "_" would just confuse whoever goes looking for the photos.
    if not stripped.strip("./\\ "):
        return None
    return stripped.replace("/", "_").replace("\\", "_")


def default_export_root(home: Path | None = None) -> Path:
    home = Path.home() if home is None else home
    return home / "Desktop" / DEFAULT_EXPORT_DIRNAME


def display_path(path: Path, home: Path | None = None) -> str:
    """Shorten a path for display: `/Users/x/Desktop/a` -> `~/Desktop/a`."""
    home = Path.home() if home is None else home
    try:
        return f"~/{Path(path).relative_to(home)}"
    except ValueError:
        return str(path)


@dataclass(frozen=True)
class ApkReport:
    looks_like_apk: bool
    sha256: str
    problem: str = ""


@dataclass(frozen=True)
class ApkVerdict:
    trusted: bool
    message: str


def inspect_apk(path: Path) -> ApkReport:
    """Check the file really is an Android package, and hash it.

    Catching a wrong file here gives a sentence the user can act on, instead
    of adb's opaque failure a minute later.
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ApkReport(False, "", "读不到这个文件")
    digest = hashlib.sha256(data).hexdigest()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            names = bundle.namelist()
    except (zipfile.BadZipFile, OSError):
        return ApkReport(False, digest, "这个文件不是安卓安装包（apk）")
    if "AndroidManifest.xml" not in names or not any(
        n.startswith("classes") and n.endswith(".dex") for n in names
    ):
        return ApkReport(False, digest, "这个文件不是安卓安装包（apk）")
    return ApkReport(True, digest)


def judge_apk_trust(
    sha256: str, known: dict[str, str] = KNOWN_APK_HASHES
) -> ApkVerdict:
    """Say whether this apk is byte-identical to one we have pinned.

    Deliberately not a hard block: the vendor ships versions we cannot know
    about, and refusing every new one would strand users. But the warning is
    blunt, because a repackaged apk shows a normal login screen while
    handing the account to someone else.
    """
    version = known.get(sha256)
    if version:
        return ApkVerdict(True, f"校验通过：与已记录的 {version} 版完全一致。")
    return ApkVerdict(
        False,
        "⚠️ 这个 apk 和已记录的版本对不上。可能只是版本更新了，"
        "也可能被人改过——被改过的安装包会照常显示登录界面，"
        "却把你的账号发给别人。只装你信得过的人直接发给你的文件。",
    )


def clean_dropped_path(raw: str) -> Path | None:
    """Turn what Terminal produces when you drag a file in into a real path.

    Dragging quotes the path or backslash-escapes its spaces, and neither
    form opens as-is.
    """
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    text = re.sub(r"\\(.)", r"\1", text).strip()
    if not text:
        return None
    return Path(text).expanduser()


def ensure_app_installed(
    *,
    is_installed: Callable[[], bool],
    install_apk: Callable[[Path], bool],
    input_fn: Callable = input,
    printer: Callable[[str], None] = print,
    max_attempts: int = MAX_LOGIN_ATTEMPTS,
) -> bool:
    """Make sure the app exists on the device before asking anyone to log in."""
    for _ in range(max_attempts):
        if is_installed():
            return True
        printer("")
        printer("模拟器里还没有「鑫时光集家长版」。")
        printer("装它有两个办法：")
        printer("  · 把 apk 安装包直接拖进模拟器窗口，装好后回来按回车")
        printer("  · 或者把 apk 拖到这个终端窗口里，我来装")
        answer = input_fn("拖入 apk，或装好后按回车（输 q 退出）：")
        if answer.strip().lower() in _QUIT_ANSWERS:
            return False
        apk = clean_dropped_path(answer)
        if apk is None:
            continue
        if not apk.is_file():
            printer(f"找不到这个文件：{apk}")
            continue
        report = inspect_apk(apk)
        if not report.looks_like_apk:
            printer(report.problem + "。请确认拖的是 .apk 安装包。")
            continue
        verdict = judge_apk_trust(report.sha256)
        printer(verdict.message)
        if not verdict.trusted:
            printer(f"这个文件的校验码：{report.sha256}")
            if not input_fn("仍然要安装吗？（y=装 / 其他=不装）：").strip().lower().startswith("y"):
                continue
        printer(f"正在安装 {apk.name} …")
        if not install_apk(apk):
            printer("装不上。确认这是家长版的 apk，模拟器也还开着。")
    return False


def ensure_credentials(
    *,
    read_prefs: Callable[[], str],
    input_fn: Callable = input,
    printer: Callable[[str], None] = print,
    max_attempts: int = MAX_LOGIN_ATTEMPTS,
) -> tuple[str, tuple[str, ...]] | None:
    """Read the login state, waiting for the user to log in if needed.

    Returns ``(token, child_ids)``, or None if the user gave up. Not being
    logged in is the single most common stumble, and it is fixed inside the
    emulator that is already open - so the wizard waits instead of exiting
    and making them start over.
    """
    for _ in range(max_attempts):
        try:
            return creds.resolve_credentials(read_prefs=read_prefs)
        except eo.SmokeError as exc:
            reason = str(exc)
        if reason == "prefs-read-failed":
            printer("")
            printer("读不到 App 的登录信息，这通常是模拟器没开 Root 权限。")
            printer("  · 用 MuMu：设置 → 其他/系统 → 打开 Root 权限，然后重启模拟器")
            printer("  · 用免费模拟器：python3 tools/setup_emulator.py --delete 后重新装一次")
        else:
            printer("")
            printer("还没检测到登录信息。请在模拟器里：")
            printer("  1. 打开「鑫时光集家长版」")
            printer("  2. 用你自己的账号登录")
            printer("  3. 进一次照片/成长记录页面")
        if input_fn("好了之后按回车重试（输 q 退出）：").strip().lower() in _QUIT_ANSWERS:
            return None
    printer("试了多次仍然读不到登录信息，先退出。")
    return None


def ask_folder_name(
    *,
    root: Path,
    input_fn: Callable = input,
    printer: Callable[[str], None] = print,
) -> Path:
    """Ask what to call this export, and return the folder it lands in."""
    while True:
        answer = input_fn(f"给这份导出起个名字（会存到 {display_path(root)}/名字）：")
        name = sanitize_folder_name(answer)
        if name:
            return root / name
        printer("名字不能为空，请重新输入。")


def open_folder(path: Path, run_command: Callable = eo.run_command) -> None:
    """Reveal the finished export in Finder; never fatal if it fails."""
    try:
        run_command(["open", str(path)])
    except Exception:
        pass


def install_emulator(argv: list[str] | None = None) -> int:
    """Hand off to the emulator installer (kept separate so tests can stub it).

    ``--no-next-steps`` because the installer's own closing advice ("go back
    to the terminal and run the export command") is wrong here: the user is
    already inside the wizard, which gives the next step itself.
    """
    return setup_emulator.main(argv or ["--no-next-steps"])


def ensure_device(
    *,
    adb_path: str | Path | None = None,
    serial: str | None = None,
    input_fn: Callable = input,
    printer: Callable[[str], None] = print,
) -> bool:
    """Make sure some Android device is reachable, offering to install one."""
    try:
        adb = android.find_adb(adb_path)
        android.discover_device(adb=adb, serial=serial)
        return True
    except eo.SmokeError:
        pass
    printer("")
    printer("没有检测到安卓模拟器。")
    printer("如果你已经装了 MuMu，请先打开它并等进入安卓桌面，然后重新运行本向导。")
    printer("也可以让我装一个免费的（约 4.5 GB，十几分钟，中途会问你是否接受许可）。")
    if input_fn("现在就装免费模拟器吗？（回车=好 / n=我自己开 MuMu）：").strip().lower().startswith("n"):
        printer("那先去打开模拟器，好了之后重新运行本向导。")
        return False
    if install_emulator() != 0:
        printer("模拟器没装成功，上面有失败原因。")
        return False
    return True


def describe_children(
    child_ids: Sequence[str],
    *,
    fetch_page: Callable[[str], Sequence],
) -> list[ChildSummary]:
    """Sample one feed page per archive so the menu can be human-readable.

    A failed sample degrades to a blank row rather than aborting: a preview
    is a convenience, and losing it should never cost the whole export.
    """
    summaries: list[ChildSummary] = []
    for child_id in child_ids:
        try:
            records = list(fetch_page(child_id))
        except (eo.SmokeError, OSError):
            records = []
        summaries.append(summarise_child(child_id, records))
    return summaries


def choose_children(
    summaries: Sequence[ChildSummary],
    *,
    input_fn: Callable = input,
    printer: Callable[[str], None] = print,
) -> tuple[str, ...]:
    """Show the readable menu and return the chosen archives' ids."""
    if len(summaries) == 1:
        return (summaries[0].child_id,)
    printer("")
    printer(f"这个账号下有 {len(summaries)} 个孩子的档案：")
    for index, summary in enumerate(summaries, 1):
        printer(format_child_line(index, summary))
    while True:
        answer = input_fn(f"要导出哪个？（1-{len(summaries)}，a=全部）：")
        chosen = parse_child_selection(answer, len(summaries))
        if chosen is not None:
            return tuple(summaries[i].child_id for i in chosen)
        printer(f"没看懂，请输入 1 到 {len(summaries)} 之间的数字，或 a 表示全部。")


def _app_present(adb_path, serial, package: str, run_command: Callable) -> bool:
    try:
        adb = android.find_adb(adb_path)
        found = android.discover_device(adb=adb, serial=serial, run_command=run_command)
        return android.is_app_installed(found, package, run_command)
    except eo.SmokeError:
        return False


def _install_apk(adb_path, serial, apk: Path, run_command: Callable) -> bool:
    try:
        adb = android.find_adb(adb_path)
        found = android.discover_device(adb=adb, serial=serial, run_command=run_command)
    except eo.SmokeError:
        return False
    result = run_command([found.adb, "-s", found.serial, "install", "-r", str(apk)])
    return "Success" in (result.stdout or "")


def export_one(**kwargs) -> int:
    """Thin seam over the real exporter so the wizard stays testable."""
    return feed_api.run_api(**kwargs)


def _page_sampler(token: str, opener) -> Callable[[str], Sequence]:
    def fetch(child_id: str) -> Sequence:
        payload = feed_api.fetch_moment_page(
            opener, token, (child_id,), feed_api.COUNTER_SEED
        )
        return feed_api.extract_moments(payload)

    return fetch


def run_wizard(
    *,
    adb_path: str | Path | None = None,
    serial: str | None = None,
    package: str = eo.PACKAGE,
    run_command: Callable = eo.run_command,
    input_fn: Callable = input,
    printer: Callable[[str], None] = print,
    export_root: Path | None = None,
    opener=None,
) -> int:
    """Walk the whole export as a conversation. Returns a process exit code."""
    printer("鑫时光集 · 照片导出向导")
    printer("跟着提示走就行，随时可以按 Control-C 停止。")

    if not ensure_device(
        adb_path=adb_path, serial=serial, input_fn=input_fn, printer=printer
    ):
        return 1

    if not ensure_app_installed(
        is_installed=lambda: _app_present(adb_path, serial, package, run_command),
        install_apk=lambda apk: _install_apk(adb_path, serial, apk, run_command),
        input_fn=input_fn,
        printer=printer,
    ):
        return 1

    resolved = ensure_credentials(
        read_prefs=creds.prefs_reader(
            adb_path=adb_path, serial=serial, package=package, run_command=run_command
        ),
        input_fn=input_fn,
        printer=printer,
    )
    if resolved is None:
        return 1
    token, child_ids = resolved

    printer("")
    printer("正在读取档案信息…")
    opener = opener or eo.build_opener()
    summaries = describe_children(child_ids, fetch_page=_page_sampler(token, opener))
    chosen = choose_children(summaries, input_fn=input_fn, printer=printer)

    root = export_root or default_export_root()
    by_id = {summary.child_id: summary for summary in summaries}
    failures = 0
    destinations: list[Path] = []
    for position, child_id in enumerate(chosen, 1):
        printer("")
        if len(chosen) > 1:
            # Without this, two identical "name this export" prompts in a row
            # give the user no way to tell which child they are naming.
            summary = by_id.get(child_id)
            label = format_child_line(position, summary).strip() if summary else ""
            printer(f"第 {position}/{len(chosen)} 份 · {label.split(') ', 1)[-1]}")
        destination = ask_folder_name(root=root, input_fn=input_fn, printer=printer)
        destinations.append(destination)
        printer(f"开始导出到 {display_path(destination)} …")
        code = export_one(
            build_root=destination,
            token=token,
            child_ids=(child_id,),
            assume_yes=True,
            opener=opener,
            run_command=run_command,
            adb_path=adb_path,
            serial=serial,
            package=package,
        )
        if code != 0:
            failures += 1
            printer("这一份没有全部完成，稍后重跑本向导会自动补齐缺的部分。")

    printer("")
    if failures:
        printer(f"完成 {len(chosen) - failures} 份，{failures} 份未全部完成。")
    else:
        printer(f"全部完成，共 {len(chosen)} 份。")
    if destinations:
        open_folder(destinations[0], run_command=run_command)
        printer(f"已为你打开：{display_path(destinations[0])}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run_wizard()
    except KeyboardInterrupt:
        print("\n已停止。已经下载好的照片都还在，重跑本向导会接着下。")
        return 1
    except eo.SmokeError as exc:
        print(f"失败：{exc}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
