import contextlib
import io
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path

from tools import export_originals as eo
from tools import setup_emulator as se


def _completed(stdout: str, returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class HostAbiTests(unittest.TestCase):
    def test_apple_silicon_maps_to_arm64(self):
        self.assertEqual(se.host_abi("arm64"), "arm64-v8a")

    def test_intel_maps_to_x86_64(self):
        self.assertEqual(se.host_abi("x86_64"), "x86_64")

    def test_unknown_architecture_raises(self):
        with self.assertRaises(eo.SmokeError) as caught:
            se.host_abi("ppc64")
        self.assertEqual(str(caught.exception), "unsupported-architecture")


class SystemImageTests(unittest.TestCase):
    def test_package_name_uses_google_apis_not_google_play(self):
        package = se.system_image_package(34, "arm64-v8a")
        self.assertEqual(package, "system-images;android-34;google_apis;arm64-v8a")
        # The whole point: google_play images forbid adb root.
        self.assertNotIn("google_play", package)

    def test_default_image_is_google_apis(self):
        self.assertIn("google_apis", se.system_image_package(se.API_LEVEL, "arm64-v8a"))


class JdkUrlTests(unittest.TestCase):
    def test_arm64_asks_for_the_aarch64_build(self):
        self.assertIn("/mac/aarch64/", se.jdk_url("arm64-v8a"))

    def test_intel_asks_for_the_x64_build(self):
        self.assertIn("/mac/x64/", se.jdk_url("x86_64"))


class FindSdkTests(unittest.TestCase):
    def test_prefers_an_explicit_environment_variable(self):
        found = se.find_sdk(
            env={"ANDROID_SDK_ROOT": "/sdk"},
            home=Path("/home/u"),
            is_dir=lambda p: str(p) == "/sdk",
        )
        self.assertEqual(found, Path("/sdk"))

    def test_finds_android_studio_default_location(self):
        found = se.find_sdk(
            env={},
            home=Path("/home/u"),
            is_dir=lambda p: str(p) == "/home/u/Library/Android/sdk",
        )
        self.assertEqual(found, Path("/home/u/Library/Android/sdk"))

    def test_finds_the_private_sdk_this_script_installs(self):
        private = se.TOOLS_ROOT / "android-sdk"
        found = se.find_sdk(env={}, home=Path("/home/u"), is_dir=lambda p: p == private)
        self.assertEqual(found, private)

    def test_nothing_installed_returns_none(self):
        self.assertIsNone(
            se.find_sdk(env={}, home=Path("/home/u"), is_dir=lambda p: False)
        )


class FindJavaTests(unittest.TestCase):
    def test_finds_a_jdk_bundled_with_android_studio(self):
        studio = Path("/Applications/Android Studio.app/Contents/jbr/Contents/Home/bin/java")
        found = se.find_java(env={}, home=Path("/home/u"), is_file=lambda p: p == studio)
        self.assertEqual(found, studio.parent.parent)

    def test_nothing_installed_returns_none(self):
        self.assertIsNone(
            se.find_java(env={}, home=Path("/home/u"), is_file=lambda p: False)
        )


class RewriteAvdConfigTests(unittest.TestCase):
    """The three settings a hand-made AVD gets wrong."""

    def test_throwaway_data_partition_is_removed(self):
        # avdmanager writes `<temp>`, which discards the login on reboot -
        # observed on a real AVD, not hypothetical.
        text = "abi.type=arm64-v8a\ndisk.dataPartition.path=<temp>\nhw.ramSize=2G\n"
        result = se.rewrite_avd_config(text)
        self.assertNotIn("disk.dataPartition.path", result)

    def test_ram_is_raised(self):
        result = se.rewrite_avd_config("hw.ramSize=2G\n")
        self.assertIn("hw.ramSize=4096", result)
        self.assertNotIn("hw.ramSize=2G", result)

    def test_missing_keys_are_appended(self):
        result = se.rewrite_avd_config("abi.type=arm64-v8a\n")
        self.assertIn("hw.keyboard=yes", result)
        self.assertIn("hw.gpu.enabled=yes", result)

    def test_unrelated_lines_survive_untouched(self):
        result = se.rewrite_avd_config("abi.type=arm64-v8a\nimage.sysdir.1=x/y/\n")
        self.assertIn("abi.type=arm64-v8a", result)
        self.assertIn("image.sysdir.1=x/y/", result)

    def test_is_idempotent(self):
        once = se.rewrite_avd_config("hw.ramSize=2G\n")
        self.assertEqual(se.rewrite_avd_config(once), once)


class BootCompletedTests(unittest.TestCase):
    def test_one_means_booted(self):
        self.assertTrue(se.boot_completed(_completed("1\n")))

    def test_anything_else_is_not_booted(self):
        self.assertFalse(se.boot_completed(_completed("")))
        self.assertFalse(se.boot_completed(_completed("0\n")))
        self.assertFalse(se.boot_completed(_completed("1", returncode=1)))


class VerifyImageTagTests(unittest.TestCase):
    """A safety net: never let a google_play image through."""

    def test_google_play_image_is_refused(self):
        with self.assertRaises(eo.SmokeError) as caught:
            se.verify_rootable("system-images;android-34;google_play;arm64-v8a")
        self.assertEqual(str(caught.exception), "image-not-rootable")

    def test_google_apis_image_passes(self):
        se.verify_rootable("system-images;android-34;google_apis;arm64-v8a")


class HttpsOnlyRedirectTests(unittest.TestCase):
    """Tool downloads redirect (Adoptium hands off to a mirror), but only
    ever to https - a plain-http hop would hand an attacker the JDK we are
    about to run."""

    def _handler(self):
        return se.HttpsOnlyRedirects()

    def test_https_redirect_is_followed(self):
        request = urllib.request.Request("https://a.example/x")
        redirected = self._handler().redirect_request(
            request, None, 302, "Found", {}, "https://b.example/y"
        )
        self.assertIsNotNone(redirected)
        self.assertEqual(redirected.full_url, "https://b.example/y")

    def test_plain_http_redirect_is_refused(self):
        request = urllib.request.Request("https://a.example/x")
        self.assertIsNone(
            self._handler().redirect_request(
                request, None, 302, "Found", {}, "http://b.example/y"
            )
        )

    def test_non_http_scheme_is_refused(self):
        request = urllib.request.Request("https://a.example/x")
        self.assertIsNone(
            self._handler().redirect_request(
                request, None, 302, "Found", {}, "file:///etc/passwd"
            )
        )


class DownloadTests(unittest.TestCase):
    class _Response:
        def __init__(self, payload: bytes):
            self._payload = payload
            self.headers = {"Content-Length": str(len(payload))}
            self._sent = False

        def read(self, size):
            if self._sent:
                return b""
            self._sent = True
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Opener:
        def __init__(self, response):
            self._response = response
            self.seen_user_agent = None

        def open(self, request, timeout=None):
            self.seen_user_agent = request.get_header("User-agent")
            return self._response

    def test_writes_atomically_and_leaves_no_part_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "thing.zip"
            opener = self._Opener(self._Response(b"payload"))
            with contextlib.redirect_stdout(io.StringIO()):
                se.download("https://x.example/thing.zip", target, opener=opener)
            self.assertEqual(target.read_bytes(), b"payload")
            self.assertEqual(list(Path(tmp).iterdir()), [target])

    def test_failure_leaves_no_partial_file(self):
        class Boom:
            def open(self, request, timeout=None):
                raise OSError("network went away")

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "thing.zip"
            with self.assertRaises(eo.SmokeError):
                with contextlib.redirect_stdout(io.StringIO()):
                    se.download("https://x.example/thing.zip", target, opener=Boom())
            self.assertEqual(list(Path(tmp).iterdir()), [])


    def test_sends_a_real_user_agent(self):
        # Adoptium's CDN 403s urllib's default agent, so this is load-bearing.
        with tempfile.TemporaryDirectory() as tmp:
            opener = self._Opener(self._Response(b"payload"))
            with contextlib.redirect_stdout(io.StringIO()):
                se.download("https://x.example/t.zip", Path(tmp) / "t.zip", opener=opener)
        self.assertEqual(opener.seen_user_agent, se.DOWNLOAD_USER_AGENT)
        self.assertNotIn("urllib", (opener.seen_user_agent or "").lower())

    def test_failure_reason_is_kept_in_the_message(self):
        class Boom:
            def open(self, request, timeout=None):
                raise OSError("network went away")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(eo.SmokeError) as caught:
                with contextlib.redirect_stdout(io.StringIO()):
                    se.download("https://x.example/t.zip", Path(tmp) / "t.zip", opener=Boom())
        self.assertIn("OSError", str(caught.exception))


class WaitUntilGoneTests(unittest.TestCase):
    """Deleting the AVD is how a user revokes their login: it must not race
    a still-running emulator that could rewrite the files behind us."""

    def test_returns_once_the_device_disappears(self):
        remaining = [2, 1, 0]

        def still_listed():
            return remaining.pop(0) > 0

        self.assertTrue(se.wait_until_gone(still_listed, timeout=5, interval=0))
        self.assertEqual(remaining, [])

    def test_gives_up_after_the_timeout(self):
        self.assertFalse(se.wait_until_gone(lambda: True, timeout=0, interval=0))


if __name__ == "__main__":
    unittest.main()
