import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tools import export_originals


class UrlSelectionTests(unittest.TestCase):
    def test_extracts_only_original_https_cdn_jpegs_in_first_seen_order(self):
        host = export_originals.CDN_HOST
        text = "\n".join(
            [
                f"压缩地址::https://{host}/small.jpeg",
                f"原图地址::https://{host}/a.jpeg",
                f"原图地址::https://{host}/a.jpeg",
                f"原图地址::https://{host}/folder/a%20b.jpeg",
                "原图地址::https://evil.example/a.jpeg",
                f"原图地址::http://{host}/plain.jpeg",
                f"原图地址::https://{host}/not-a-photo.png",
            ]
        )
        self.assertEqual(
            export_originals.extract_urls(text),
            [
                f"https://{host}/a.jpeg",
                f"https://{host}/folder/a%20b.jpeg",
            ],
        )

    def test_rejects_unsafe_variants_without_printing_urls(self):
        host = export_originals.CDN_HOST
        candidates = [
            f"https://{host}:444/a.jpeg",
            f"https://{host}.evil.example/a.jpeg",
            f"https://{host}/a.jpeg?variant=other",
            "https://[malformed/a.jpeg",
        ]
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(
                export_originals.extract_urls(
                    "\n".join(f"原图地址::{url}" for url in candidates)
                ),
                [],
            )
        for url in candidates:
            self.assertNotIn(url, output.getvalue())

    def test_selects_exactly_three_unique_candidates(self):
        urls = ["one", "one", "two", "three", "four"]
        self.assertEqual(export_originals.select_samples(urls), ["one", "two", "three"])

    def test_fewer_than_three_candidates_raises_redacted_error(self):
        with self.assertRaisesRegex(export_originals.SmokeError, "not-enough-candidates"):
            export_originals.select_samples(["secret-url", "second-secret"])


def completed(argv, stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class FakeRunner:
    def __init__(self, info):
        self.info = info
        self.calls = []

    def __call__(self, argv):
        argv = [str(value) for value in argv]
        self.calls.append(argv)
        self.assert_no_url(argv)
        if argv[:2] == [str(export_originals.MUMUTOOL), "info"]:
            return completed(argv, self.info)
        if argv[-2:] == ["connect", "127.0.0.1:16384"]:
            return completed(argv, "connected to 127.0.0.1:16384")
        if "pidof" in argv:
            return completed(argv, "2468\n")
        if "logcat" in argv:
            host = export_originals.CDN_HOST
            return completed(
                argv,
                "\n".join(f"原图地址::https://{host}/{n}.jpeg" for n in range(4)),
            )
        return completed(argv, returncode=1, stderr="unexpected-command")

    def assert_no_url(self, argv):
        if any("https://" in value for value in argv):
            raise AssertionError("command arguments must not contain photo URLs")


class MuMuDiscoveryTests(unittest.TestCase):
    RUNNING = '[{"index":0,"is_android_started":true,"adb_port":16384}]'

    def test_discovers_one_running_instance_and_reads_pid_logcat(self):
        runner = FakeRunner(self.RUNNING)
        device = export_originals.discover_running_device(runner)
        self.assertEqual(device.serial, "127.0.0.1:16384")
        pid = export_originals.discover_app_pid(device, runner)
        self.assertEqual(pid, 2468)
        text = export_originals.read_current_logcat(device, pid, runner)
        self.assertIn("原图地址::", text)
        self.assertIn(
            [
                str(export_originals.ADB),
                "-s",
                device.serial,
                "logcat",
                "-d",
                "--pid=2468",
                "-v",
                "brief",
            ],
            runner.calls,
        )

    def test_zero_running_instances_is_redacted(self):
        runner = FakeRunner('[{"index":0,"is_android_started":false,"adb_port":16384}]')
        with self.assertRaisesRegex(export_originals.SmokeError, "mumu-not-running"):
            export_originals.discover_running_device(runner)

    def test_multiple_running_instances_is_redacted(self):
        runner = FakeRunner(
            '[{"is_android_started":true,"adb_port":16384},'
            '{"is_android_started":true,"adb_port":16385}]'
        )
        with self.assertRaisesRegex(export_originals.SmokeError, "ambiguous-device"):
            export_originals.discover_running_device(runner)


class DryRunTests(unittest.TestCase):
    RUNNING = '[{"index":0,"is_android_started":true,"adb_port":16384}]'

    def test_dry_run_does_not_touch_downloader_or_create_output(self):
        runner = FakeRunner(self.RUNNING)
        with tempfile.TemporaryDirectory() as temp:
            output_parent = Path(temp) / "must-not-exist"

            def forbidden_downloader(*args, **kwargs):
                self.fail("downloader was called during dry-run")

            stream = io.StringIO()
            with redirect_stdout(stream):
                result = export_originals.run_smoke(
                    execute=False,
                    run_command=runner,
                    downloader=forbidden_downloader,
                    output_parent=output_parent,
                )
            self.assertEqual(result, 0)
            self.assertFalse(output_parent.exists())
            self.assertNotIn("https://", stream.getvalue())
            self.assertIn("候选原图：4", stream.getvalue())
            self.assertIn("计划样本：3", stream.getvalue())

    def test_cli_defaults_to_dry_run_and_execute_is_explicit(self):
        with mock.patch.object(export_originals, "run_smoke", return_value=0) as run:
            self.assertEqual(export_originals.main([]), 0)
            run.assert_called_once_with(execute=False)
        with mock.patch.object(export_originals, "run_smoke", return_value=0) as run:
            self.assertEqual(export_originals.main(["--execute"]), 0)
            run.assert_called_once_with(execute=True)

    def test_invalid_cli_does_not_run_smoke(self):
        with mock.patch.object(export_originals, "run_smoke") as run:
            with self.assertRaises(SystemExit):
                export_originals.main(["--unknown"])
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
