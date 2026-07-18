import io
import os
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

    def test_accepts_current_mumu_return_results_shape(self):
        runner = FakeRunner(
            '{"errcode":0,"return":{"count":1,"results":['
            '{"index":0,"state":"running","adb_port":16384}]}}'
        )
        self.assertEqual(
            export_originals.discover_running_device(runner).serial,
            "127.0.0.1:16384",
        )

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


class FakeResponse:
    def __init__(self, body, status=200, content_type="image/jpeg"):
        self.body = io.BytesIO(body)
        self.status = status
        self.headers = {"Content-Type": content_type}

    def read(self, size=-1):
        return self.body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def open(self, request, timeout):
        self.urls.append(request.full_url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class DownloadTests(unittest.TestCase):
    JPEG = b"\xff\xd8" + b"x" * 2048

    def test_opener_disables_proxies_and_rejects_redirects(self):
        sentinel = object()
        with mock.patch.object(
            export_originals.urllib.request, "build_opener", return_value=sentinel
        ) as builder:
            self.assertIs(export_originals.build_opener(), sentinel)
        proxy_handler, redirect_handler = builder.call_args.args
        self.assertIsInstance(proxy_handler, export_originals.urllib.request.ProxyHandler)
        self.assertEqual(proxy_handler.proxies, {})
        self.assertIsInstance(redirect_handler, export_originals.RejectRedirects)

    def test_valid_jpeg_streams_through_part_and_returns_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "sample-01.jpeg"
            opener = FakeOpener([FakeResponse(self.JPEG)])
            result = export_originals.download_sample(
                opener, "https://example.invalid/secret.jpeg", destination
            )
            self.assertEqual(destination.read_bytes(), self.JPEG)
            self.assertFalse(destination.with_suffix(".jpeg.part").exists())
            self.assertEqual(result.byte_count, len(self.JPEG))
            self.assertEqual(len(result.sha256), 64)

    def test_failures_leave_no_complete_or_part_file(self):
        cases = [
            FakeResponse(self.JPEG, status=500),
            FakeResponse(self.JPEG, content_type="text/plain"),
            FakeResponse(b"\xff\xd8short"),
            FakeResponse(b"NO" + b"x" * 2048),
            OSError("network failed"),
        ]
        for index, response in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temp:
                destination = Path(temp) / "sample.jpeg"
                with self.assertRaises(export_originals.SmokeError):
                    export_originals.download_sample(
                        FakeOpener([response]),
                        "https://example.invalid/private.jpeg",
                        destination,
                    )
                self.assertFalse(destination.exists())
                self.assertFalse(destination.with_suffix(".jpeg.part").exists())

    def test_over_limit_is_removed(self):
        body = b"\xff\xd8" + b"x" * (export_originals.MAX_BYTES + 1)
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "sample.jpeg"
            with self.assertRaisesRegex(export_originals.SmokeError, "too-large"):
                export_originals.download_sample(
                    FakeOpener([FakeResponse(body)]),
                    "https://example.invalid/private.jpeg",
                    destination,
                )
            self.assertFalse(destination.with_suffix(".jpeg.part").exists())

    def test_run_directory_collision_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            first = export_originals.create_run_directory(parent, "20260718-120000")
            marker = first / "keep"
            marker.write_text("safe")
            second = export_originals.create_run_directory(parent, "20260718-120000")
            third = export_originals.create_run_directory(parent, "20260718-120000")
            self.assertEqual(second.name, "20260718-120000-1")
            self.assertEqual(third.name, "20260718-120000-2")
            self.assertEqual(marker.read_text(), "safe")

    def test_execute_downloads_uses_exactly_three_selected_urls(self):
        host = export_originals.CDN_HOST
        urls = [f"https://{host}/{index}.jpeg" for index in range(3)]
        opener = FakeOpener([FakeResponse(self.JPEG) for _ in range(3)])
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp) / "build" / "direct-original-smoke"
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = export_originals.execute_samples(
                    urls,
                    parent,
                    opener=opener,
                    timestamp="20260718-120000",
                    require_ignore=False,
                )
            self.assertEqual(result, 0)
            self.assertEqual(opener.urls, urls)
            self.assertEqual(len(list(parent.rglob("*.jpeg"))), 3)
            self.assertNotIn("https://", stream.getvalue())
            self.assertFalse(any(url in stream.getvalue() for url in urls))


if __name__ == "__main__":
    unittest.main()
