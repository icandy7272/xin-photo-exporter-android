#!/usr/bin/env python3
"""Turn a device and the operator's flags into a token plus child ids.

Three sources feed in, in order of preference: values supplied on the
command line, a token file (or ``XIN_ACCESS_TOKEN``), and finally the app's
own ``shared_prefs`` read off an Android device. Supplying both a token and
a child id means no device is touched at all, so a run can happen long after
the emulator was shut down - until the token expires.

The token is used only in memory to call the operator's own account; it is
never printed, logged or persisted.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Mapping, Sequence

try:  # package import (tests / `python3 -m ...`)
    from tools import export_originals as eo
    from tools import device as android
except ImportError:  # pragma: no cover - `python3 tools/export_originals.py`
    import export_originals as eo
    import device as android


_PREF_STRING_RE = r'<string name="{key}">([^<]*)</string>'
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# album_child_id is only set once the album is opened, and holds just the open
# album. childIds/paChildIds are JSON arrays holding *every* child of the
# account, so all three keys are read and unioned (see extract_child_ids).
_CHILD_ID_KEYS = ("album_child_id", "childIds", "paChildIds")
# The token is read from a file or the environment, never from argv: command
# lines land in shell history and are visible to every process on the machine.
TOKEN_ENV_VAR = "XIN_ACCESS_TOKEN"


def extract_pref_string(prefs_xml: str, key: str) -> str | None:
    """Return the value of a ``<string name="key">`` entry, or None."""
    match = re.search(_PREF_STRING_RE.format(key=re.escape(key)), prefs_xml)
    if match is None:
        return None
    return match.group(1) or None


def extract_child_ids(prefs_xml: str) -> tuple[str, ...]:
    """Find every child UUID in album_child_id / childIds / paChildIds.

    An account can hold more than one child record - a sibling, or the same
    child re-enrolled under a new record. The app merges their timelines, so
    the exporter must ask for all of them: requesting only the first ends the
    feed at that record's oldest post and silently loses the earlier years.
    """
    found: list[str] = []
    for key in _CHILD_ID_KEYS:
        value = extract_pref_string(prefs_xml, key)
        if not value:
            continue
        for child_id in _UUID_RE.findall(value):
            if child_id not in found:
                found.append(child_id)
    return tuple(found)


def read_app_credentials(
    device: "eo.Device",
    run_command: Callable = eo.run_command,
    package: str = eo.PACKAGE,
) -> tuple[str, tuple[str, ...]]:
    """Read the Bearer token and every child id from the app's prefs."""
    prefs_xml = android.read_prefs_xml(device, package, run_command)
    token = extract_pref_string(prefs_xml, "accessToken")
    child_ids = extract_child_ids(prefs_xml)
    if not token or not child_ids:
        raise eo.SmokeError("credentials-not-found")
    return token, child_ids


def _refuse_token_inside_repository(token_file: Path) -> None:
    """Reject a token stored inside the checkout.

    The token is account-equivalent - it calls the API with no password and
    no SMS code - and only ``build/`` is gitignored, so a token file left
    anywhere else here is one ``git add .`` from being published. Git never
    forgets: deleting the file later does not remove it from history, and
    anyone who already cloned keeps their copy. Keep it outside the repo.
    """
    try:
        token_file.resolve().relative_to(eo.REPOSITORY_ROOT)
    except (ValueError, OSError):
        return
    raise eo.SmokeError("token-file-in-repository")


def load_token(
    token_file: Path | None = None, env: Mapping[str, str] | None = None
) -> str | None:
    """Read a Bearer token from a file, else the environment, else None.

    A token obtained once (on any machine with a rooted Android device) lets
    later runs skip the emulator entirely, until it expires.
    """
    env = os.environ if env is None else env
    if token_file is not None:
        _refuse_token_inside_repository(Path(token_file))
        try:
            raw = Path(token_file).read_text(encoding="utf-8")
        except OSError:
            raise eo.SmokeError("token-file-unreadable") from None
    else:
        raw = env.get(TOKEN_ENV_VAR, "")
    token = raw.strip()
    if token.lower().startswith("bearer "):
        token = token[len("bearer ") :].strip()
    if token:
        return token
    if token_file is not None:
        raise eo.SmokeError("token-file-empty")
    return None


def normalise_child_ids(raw: Sequence[str] | None) -> tuple[str, ...]:
    """Validate and de-duplicate child ids supplied on the command line."""
    if not raw:
        return ()
    ordered: list[str] = []
    for value in raw:
        child_id = value.strip()
        if not _UUID_RE.fullmatch(child_id):
            raise eo.SmokeError("invalid-child-id")
        if child_id not in ordered:
            ordered.append(child_id)
    return tuple(ordered)


def resolve_credentials(
    *,
    token: str | None = None,
    child_ids: tuple[str, ...] = (),
    read_prefs: Callable[[], str] | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Combine supplied credentials with whatever the device still has to give.

    Both supplied means the device is never touched. Anything missing is
    filled from the app's prefs, so a child-id filter still works against an
    emulator-read token.
    """
    if token and child_ids:
        return token, child_ids
    if read_prefs is None:
        raise eo.SmokeError("credentials-not-found")
    prefs_xml = read_prefs()
    token = token or extract_pref_string(prefs_xml, "accessToken")
    child_ids = child_ids or extract_child_ids(prefs_xml)
    if not token or not child_ids:
        raise eo.SmokeError("credentials-not-found")
    return token, child_ids


def prefs_reader(
    *,
    adb_path: str | Path | None = None,
    serial: str | None = None,
    package: str = eo.PACKAGE,
    run_command: Callable = eo.run_command,
) -> Callable[[], str]:
    """A callable that reads the app's prefs, resolving adb only when called.

    Deferring the work means a run with supplied credentials never looks for
    an emulator at all.
    """

    def read_prefs() -> str:
        adb = android.find_adb(adb_path)
        found = android.discover_device(adb=adb, serial=serial, run_command=run_command)
        return android.read_prefs_xml(found, package, run_command)

    return read_prefs
