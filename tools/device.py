#!/usr/bin/env python3
"""Find adb, pick the Android device, and read the app's login state.

The exporter needs an Android device for exactly one thing: reading the
app's ``shared_prefs`` once. Everything after that is the Mac talking to
the API directly. So discovery is deliberately generic - any adb-reachable
device that can read ``/data/data`` works:

* the free Android Studio emulator (a **Google APIs** system image, not a
  Google Play one - only the former allows ``adb root``);
* a rooted phone or tablet (reached through ``su``);
* MuMu, which stays supported through the legacy fallback below.

``read_prefs_xml`` therefore tries the plain read first, then ``adb root``,
then ``su -c`` - covering all three without the caller knowing which one it
is talking to.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable, Iterable, Mapping

try:  # package import (tests / `python3 -m ...`)
    from tools import export_originals as eo
except ImportError:  # pragma: no cover - `python3 tools/export_originals.py`
    import export_originals as eo


SDK_SUBPATH = Path("platform-tools") / "adb"
SDK_ENV_VARS = ("ANDROID_SDK_ROOT", "ANDROID_HOME")
# Where Android Studio installs its SDK by default, per platform.
SDK_DEFAULT_DIRS = (
    Path("Library") / "Android" / "sdk",  # macOS
    Path("Android") / "Sdk",  # Linux / Windows
    Path("AppData") / "Local" / "Android" / "Sdk",  # Windows
)
# `adb devices` marks a usable device "device"; "offline" / "unauthorized"
# devices answer nothing useful, so they are filtered out rather than picked.
READY_STATE = "device"
PREFS_GLOB = "shared_prefs/*.xml"
# A successful read always contains at least one <string> pref. `adb shell`
# does not reliably forward the remote exit code, so the payload - not the
# return code - is what proves the read worked.
PREFS_MARKER = "<string"


def _dedupe(paths: Iterable[Path]) -> tuple[Path, ...]:
    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return tuple(ordered)


def adb_candidates(
    explicit: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[Path, ...]:
    """Ordered adb locations to try, most specific first."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    for variable in SDK_ENV_VARS:
        root = env.get(variable)
        if root:
            candidates.append(Path(root) / SDK_SUBPATH)
    candidates.extend(home / default / SDK_SUBPATH for default in SDK_DEFAULT_DIRS)
    candidates.append(eo.ADB)  # MuMu's bundled adb, for existing users
    return _dedupe(candidates)


def find_adb(
    explicit: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    is_file: Callable[[Path], bool] = Path.is_file,
    which: Callable[[str], str | None] = shutil.which,
) -> Path:
    """Locate an adb binary, or raise ``adb-not-found``.

    An explicit path that does not exist is an error rather than a hint:
    silently using a different adb than the one asked for hides typos.
    """
    if explicit:
        path = Path(explicit)
        if not is_file(path):
            raise eo.SmokeError("adb-not-found")
        return path
    for candidate in adb_candidates(None, env, home):
        if is_file(candidate):
            return candidate
    on_path = which("adb")
    if on_path:
        return Path(on_path)
    raise eo.SmokeError("adb-not-found")


def list_devices(adb: Path, run_command: Callable = eo.run_command) -> tuple[str, ...]:
    """Serials of every device adb reports as ready."""
    result = run_command([adb, "devices"])
    if result.returncode != 0:
        return ()
    serials: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == READY_STATE:
            serials.append(parts[0])
    return tuple(serials)


def _connect_mumu(adb: Path, run_command: Callable) -> str | None:
    """Legacy path: MuMu's instances are not attached until connected."""
    try:
        return eo.discover_running_device(run_command, adb=adb).serial
    except eo.SmokeError:
        return None


def discover_device(
    *,
    adb: Path,
    serial: str | None = None,
    run_command: Callable = eo.run_command,
) -> eo.Device:
    """Pick the device holding the login state.

    An explicit ``serial`` is dialled first when it is a network serial,
    then checked against the attached list. Otherwise the single attached
    device is used; MuMu is tried only when nothing is attached, since its
    instances need connecting before they show up.
    """
    if serial and ":" in serial:
        run_command([adb, "connect", serial])
    serials = list_devices(adb, run_command)
    if serial:
        # Verified rather than trusted: an unattached serial would otherwise
        # surface much later as an unexplained "cannot read the prefs".
        if serial not in serials:
            raise eo.SmokeError("no-device")
        return eo.Device(serial=serial, adb=adb)
    if not serials:
        mumu_serial = _connect_mumu(adb, run_command)
        if mumu_serial:
            return eo.Device(serial=mumu_serial, adb=adb)
        raise eo.SmokeError("no-device")
    if len(serials) > 1:
        raise eo.SmokeError("ambiguous-device")
    return eo.Device(serial=serials[0], adb=adb)


def _shell(device: eo.Device, command: str, run_command: Callable) -> str:
    result = run_command([device.adb, "-s", device.serial, "shell", command])
    return result.stdout or ""


def _restart_adbd_as_root(device: eo.Device, run_command: Callable) -> None:
    """Ask adbd to restart as root and wait for the device to come back.

    Works on Google APIs emulator images and userdebug builds; production
    builds refuse, which is fine - the ``su`` attempt covers those.
    """
    run_command([device.adb, "-s", device.serial, "root"])
    if ":" in device.serial:
        # Restarting adbd drops a network connection; redial it.
        run_command([device.adb, "connect", device.serial])
    run_command([device.adb, "-s", device.serial, "wait-for-device"])


def read_prefs_xml(
    device: eo.Device,
    package: str = eo.PACKAGE,
    run_command: Callable = eo.run_command,
) -> str:
    """Return the app's shared_prefs XML, escalating privileges as needed."""
    read = f"cat /data/data/{package}/{PREFS_GLOB}"
    output = _shell(device, read, run_command)
    if PREFS_MARKER in output:
        return output
    _restart_adbd_as_root(device, run_command)
    output = _shell(device, read, run_command)
    if PREFS_MARKER in output:
        return output
    output = _shell(device, f"su -c '{read}'", run_command)
    if PREFS_MARKER in output:
        return output
    raise eo.SmokeError("prefs-read-failed")
