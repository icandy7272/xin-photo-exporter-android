import subprocess
import unittest
from pathlib import Path

from tools import device
from tools import export_originals as eo


UUID = "11111111-2222-3333-4444-555555555555"
# Real shared_prefs always have a <map> root; the fixtures used to omit it,
# which is exactly why the "app installed but not logged in" case was
# misdiagnosed as a root failure.
PREFS = (
    "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n<map>"
    f'<string name="accessToken">tok</string>'
    f'<string name="album_child_id">{UUID}</string>'
    "</map>"
)
# The app installed and opened, but nobody has logged in yet: a valid prefs
# file that simply holds no strings. Observed on a real emulator.
PREFS_NO_LOGIN = (
    "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n"
    '<map>\n    <boolean name="hadAccessProto" value="true" />\n</map>'
)
DENIED = "cat: /data/data/x/shared_prefs/*.xml: Permission denied"
NOT_FOUND = "cat: /data/data/x/shared_prefs/*.xml: No such file or directory"


def _completed(stdout: str, returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class AdbCandidateTests(unittest.TestCase):
    def test_explicit_path_wins(self):
        candidates = device.adb_candidates("/custom/adb", env={}, home=Path("/home/u"))
        self.assertEqual(candidates[0], Path("/custom/adb"))

    def test_sdk_env_vars_before_defaults(self):
        candidates = device.adb_candidates(
            None, env={"ANDROID_SDK_ROOT": "/sdk"}, home=Path("/home/u")
        )
        self.assertEqual(candidates[0], Path("/sdk/platform-tools/adb"))

    def test_includes_macos_default_and_mumu_fallback(self):
        candidates = device.adb_candidates(None, env={}, home=Path("/home/u"))
        self.assertIn(Path("/home/u/Library/Android/sdk/platform-tools/adb"), candidates)
        self.assertIn(eo.ADB, candidates)

    def test_no_duplicates(self):
        candidates = device.adb_candidates(
            None,
            env={"ANDROID_SDK_ROOT": "/sdk", "ANDROID_HOME": "/sdk"},
            home=Path("/home/u"),
        )
        self.assertEqual(len(candidates), len(set(candidates)))


class FindAdbTests(unittest.TestCase):
    def test_returns_first_existing_candidate(self):
        found = device.find_adb(
            None,
            env={"ANDROID_SDK_ROOT": "/sdk"},
            home=Path("/home/u"),
            is_file=lambda path: str(path) == "/sdk/platform-tools/adb",
            which=lambda name: None,
        )
        self.assertEqual(found, Path("/sdk/platform-tools/adb"))

    def test_falls_back_to_path_lookup(self):
        found = device.find_adb(
            None,
            env={},
            home=Path("/home/u"),
            is_file=lambda path: False,
            which=lambda name: "/usr/local/bin/adb",
        )
        self.assertEqual(found, Path("/usr/local/bin/adb"))

    def test_missing_everywhere_raises(self):
        with self.assertRaises(eo.SmokeError) as caught:
            device.find_adb(
                None, env={}, home=Path("/home/u"), is_file=lambda p: False, which=lambda n: None
            )
        self.assertEqual(str(caught.exception), "adb-not-found")

    def test_explicit_path_that_does_not_exist_raises(self):
        with self.assertRaises(eo.SmokeError):
            device.find_adb(
                "/nope/adb",
                env={},
                home=Path("/home/u"),
                is_file=lambda p: str(p) != "/nope/adb",
                which=lambda n: "/usr/local/bin/adb",
            )


class ListDevicesTests(unittest.TestCase):
    def test_parses_ready_devices_only(self):
        output = (
            "List of devices attached\n"
            "emulator-5554\tdevice\n"
            "127.0.0.1:16384\toffline\n"
            "ZY223\tunauthorized\n"
            "127.0.0.1:5555\tdevice\n"
        )
        serials = device.list_devices(Path("adb"), lambda argv: _completed(output))
        self.assertEqual(serials, ("emulator-5554", "127.0.0.1:5555"))

    def test_command_failure_returns_empty(self):
        self.assertEqual(device.list_devices(Path("adb"), lambda argv: _completed("", 1)), ())


class DiscoverDeviceTests(unittest.TestCase):
    def _runner(self, listing, *, calls=None):
        def run(argv):
            if calls is not None:
                calls.append([str(value) for value in argv])
            if "devices" in [str(value) for value in argv]:
                return _completed(listing)
            return _completed("connected")

        return run

    def test_explicit_serial_is_used(self):
        listing = "List of devices attached\nemulator-5554\tdevice\nZY223\tdevice\n"
        found = device.discover_device(
            adb=Path("adb"), serial="emulator-5554", run_command=self._runner(listing)
        )
        self.assertEqual(found.serial, "emulator-5554")
        self.assertEqual(found.adb, Path("adb"))

    def test_explicit_serial_that_is_not_attached_raises(self):
        listing = "List of devices attached\nemulator-5554\tdevice\n"
        with self.assertRaises(eo.SmokeError) as caught:
            device.discover_device(
                adb=Path("adb"), serial="typo-5554", run_command=self._runner(listing)
            )
        self.assertEqual(str(caught.exception), "no-device")

    def test_explicit_network_serial_connects_first(self):
        calls: list[list[str]] = []
        listing = "List of devices attached\n127.0.0.1:5555\tdevice\n"
        found = device.discover_device(
            adb=Path("adb"), serial="127.0.0.1:5555", run_command=self._runner(listing, calls=calls)
        )
        self.assertEqual(found.serial, "127.0.0.1:5555")
        self.assertIn(["adb", "connect", "127.0.0.1:5555"], calls)

    def test_single_attached_device_is_picked(self):
        listing = "List of devices attached\nemulator-5554\tdevice\n"
        found = device.discover_device(adb=Path("adb"), run_command=self._runner(listing))
        self.assertEqual(found.serial, "emulator-5554")

    def test_two_devices_raise_ambiguous(self):
        listing = "List of devices attached\na\tdevice\nb\tdevice\n"
        with self.assertRaises(eo.SmokeError) as caught:
            device.discover_device(adb=Path("adb"), run_command=self._runner(listing))
        self.assertEqual(str(caught.exception), "ambiguous-device")

    def test_no_device_falls_back_to_mumu(self):
        info = '{"return":{"results":[{"state":"running","adb_port":16384}]}}'

        def run(argv):
            argv = [str(value) for value in argv]
            if argv[1] == "info":
                return _completed(info)
            if "devices" in argv:
                return _completed("List of devices attached\n")
            return _completed("connected")

        found = device.discover_device(adb=Path("adb"), run_command=run)
        self.assertEqual(found.serial, "127.0.0.1:16384")

    def test_no_device_and_no_mumu_raises(self):
        def run(argv):
            argv = [str(value) for value in argv]
            if "devices" in argv:
                return _completed("List of devices attached\n")
            return _completed("", 1)

        with self.assertRaises(eo.SmokeError) as caught:
            device.discover_device(adb=Path("adb"), run_command=run)
        self.assertEqual(str(caught.exception), "no-device")


class ReadPrefsTests(unittest.TestCase):
    def _device(self, serial="emulator-5554"):
        return eo.Device(serial=serial, adb=Path("adb"))

    def test_plain_cat_succeeds(self):
        calls: list[list[str]] = []

        def run(argv):
            calls.append([str(value) for value in argv])
            return _completed(PREFS)

        xml = device.read_prefs_xml(self._device(), "com.example.app", run)
        self.assertIn("accessToken", xml)
        self.assertEqual(len(calls), 1)
        self.assertIn("cat /data/data/com.example.app/shared_prefs/*.xml", calls[0][-1])

    def test_permission_denied_retries_after_adb_root(self):
        calls: list[list[str]] = []
        rooted = {"value": False}

        def run(argv):
            argv = [str(value) for value in argv]
            calls.append(argv)
            if argv[-1] == "root":
                rooted["value"] = True
                return _completed("restarting adbd as root")
            if "cat" in argv[-1]:
                return _completed(PREFS if rooted["value"] else "cat: Permission denied")
            return _completed("connected")

        xml = device.read_prefs_xml(self._device(), "com.example.app", run)
        self.assertIn("accessToken", xml)
        self.assertIn(["adb", "-s", "emulator-5554", "root"], calls)

    def test_falls_back_to_su_on_a_rooted_phone(self):
        def run(argv):
            argv = [str(value) for value in argv]
            if argv[-1] == "root":
                return _completed("adbd cannot run as root in production builds", 1)
            if argv[-1].startswith("su -c"):
                return _completed(PREFS)
            if "cat" in argv[-1]:
                return _completed("cat: Permission denied")
            return _completed("connected")

        self.assertIn("accessToken", device.read_prefs_xml(self._device(), "pkg", run))

    def test_permission_denied_everywhere_is_a_root_problem(self):
        with self.assertRaises(eo.SmokeError) as caught:
            device.read_prefs_xml(
                self._device(), "pkg", lambda argv: _completed("", 1, stderr=DENIED)
            )
        self.assertEqual(str(caught.exception), "prefs-read-failed")

    def test_prefs_without_strings_is_not_a_read_failure(self):
        """App installed and opened, but not logged in yet.

        The read succeeded - there is simply no credential in it. Calling
        that a read failure sends the user off to rebuild their emulator
        instead of just logging in.
        """
        xml = device.read_prefs_xml(
            self._device(), "pkg", lambda argv: _completed(PREFS_NO_LOGIN)
        )
        self.assertIn("hadAccessProto", xml)

    def test_missing_prefs_reports_missing_credentials_not_a_root_problem(self):
        with self.assertRaises(eo.SmokeError) as caught:
            device.read_prefs_xml(
                self._device(), "pkg", lambda argv: _completed("", 1, stderr=NOT_FOUND)
            )
        self.assertEqual(str(caught.exception), "credentials-not-found")

    def test_network_serial_reconnects_after_root(self):
        calls: list[list[str]] = []
        rooted = {"value": False}

        def run(argv):
            argv = [str(value) for value in argv]
            calls.append(argv)
            if argv[-1] == "root":
                rooted["value"] = True
                return _completed("restarting adbd as root")
            if "cat" in argv[-1]:
                return _completed(PREFS if rooted["value"] else "")
            return _completed("connected")

        device.read_prefs_xml(self._device("127.0.0.1:16384"), "pkg", run)
        self.assertIn(["adb", "connect", "127.0.0.1:16384"], calls)


if __name__ == "__main__":
    unittest.main()
