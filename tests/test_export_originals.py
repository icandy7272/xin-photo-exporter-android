import hashlib
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime
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

    def test_selects_system_ca_when_framework_python_bundle_is_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "python-cert.pem"
            system = Path(temp) / "system-cert.pem"
            system.write_text("certificate bundle")
            self.assertEqual(
                export_originals.select_ca_file(missing, system),
                system,
            )

    def test_opener_disables_proxies_and_rejects_redirects(self):
        sentinel = object()
        with mock.patch.object(
            export_originals.urllib.request, "build_opener", return_value=sentinel
        ) as builder:
            self.assertIs(export_originals.build_opener(), sentinel)
        proxy_handler = next(
            handler
            for handler in builder.call_args.args
            if isinstance(handler, export_originals.urllib.request.ProxyHandler)
        )
        redirect_handler = next(
            handler
            for handler in builder.call_args.args
            if isinstance(handler, export_originals.RejectRedirects)
        )
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


class BatchPathTests(unittest.TestCase):
    def test_extracts_record_date_and_builds_stable_destination(self):
        url = (
            f"https://{export_originals.CDN_HOST}/provider/1/moments/images/"
            "2026-06-04/opaque.jpeg"
        )
        self.assertEqual(export_originals.extract_record_date(url), date(2026, 6, 4))
        first = export_originals.batch_destination(url, Path("/tmp/out"))
        second = export_originals.batch_destination(url, Path("/tmp/out"))
        self.assertEqual(first, second)
        self.assertRegex(first.name, r"^2026-06-04_[0-9a-f]{64}\.jpeg$")
        self.assertNotIn("opaque", first.name)

    def test_missing_or_invalid_date_uses_unknown_date(self):
        missing = f"https://{export_originals.CDN_HOST}/provider/1/no-date/a.jpeg"
        invalid = (
            f"https://{export_originals.CDN_HOST}/provider/1/moments/images/"
            "2026-02-30/b.jpeg"
        )
        self.assertIsNone(export_originals.extract_record_date(missing))
        self.assertIsNone(export_originals.extract_record_date(invalid))
        for url in (missing, invalid):
            self.assertTrue(
                export_originals.batch_destination(url, Path("/tmp")).name.startswith(
                    "unknown-date_"
                )
            )

    def test_different_urls_have_different_stable_paths(self):
        host = export_originals.CDN_HOST
        first = export_originals.batch_destination(
            f"https://{host}/moments/images/2026-06-04/a.jpeg", Path("/tmp")
        )
        second = export_originals.batch_destination(
            f"https://{host}/moments/images/2026-06-04/b.jpeg", Path("/tmp")
        )
        self.assertNotEqual(first, second)


class BatchDateTests(unittest.TestCase):
    def test_applies_local_noon_to_mtime_and_setfile(self):
        calls = []

        def runner(argv):
            calls.append([str(value) for value in argv])
            return completed(argv)

        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "local.jpeg"
            original = b"private-image"
            destination.write_bytes(original)
            with mock.patch.object(export_originals.os, "utime") as utime:
                self.assertTrue(
                    export_originals.apply_record_date(
                        destination, date(2026, 6, 4), run_command=runner
                    )
                )
            expected = datetime(2026, 6, 4, 12, 0, 0).astimezone().timestamp()
            utime.assert_called_once_with(destination, (expected, expected))
            self.assertEqual(
                calls,
                [[
                    "/usr/bin/SetFile",
                    "-d",
                    "06/04/2026 12:00:00",
                    "-m",
                    "06/04/2026 12:00:00",
                    str(destination),
                ]],
            )
            self.assertEqual(destination.read_bytes(), original)
            self.assertFalse(any("https://" in value for value in calls[0]))

    def test_missing_date_is_noop_success(self):
        with mock.patch.object(export_originals.os, "utime") as utime:
            self.assertTrue(
                export_originals.apply_record_date(Path("unused.jpeg"), None)
            )
        utime.assert_not_called()

    def test_date_failures_are_non_destructive_and_return_false(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "local.jpeg"
            original = b"private-image"
            destination.write_bytes(original)
            runner = lambda argv: completed(argv, returncode=1)
            self.assertFalse(
                export_originals.apply_record_date(
                    destination, date(2026, 6, 4), run_command=runner
                )
            )
            self.assertEqual(destination.read_bytes(), original)
            with mock.patch.object(export_originals.os, "utime", side_effect=OSError):
                self.assertFalse(
                    export_originals.apply_record_date(
                        destination, date(2026, 6, 4), run_command=runner
                    )
                )
            self.assertEqual(destination.read_bytes(), original)


class InterruptingLines:
    def __init__(self, lines):
        self.lines = iter(lines)

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self.lines)
        except StopIteration:
            raise KeyboardInterrupt


class FakeLogcatProcess:
    def __init__(self, stdout, returncode=None, timeout_once=False):
        self.stdout = stdout
        self.returncode = returncode
        self.timeout_once = timeout_once
        self.terminated = False
        self.killed = False
        self.wait_calls = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.timeout_once:
            self.timeout_once = False
            raise subprocess.TimeoutExpired("adb", timeout)
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


class StreamingCollectorTests(unittest.TestCase):
    def make_factory(self, process, calls):
        def factory(argv, **kwargs):
            calls.append(([str(value) for value in argv], kwargs))
            return process

        return factory

    def test_collects_ordered_unique_candidates_without_pid_binding(self):
        host = export_originals.CDN_HOST
        first = f"https://{host}/moments/images/2026-06-04/a.jpeg"
        second = f"https://{host}/moments/images/2026-06-05/b.jpeg"
        lines = InterruptingLines(
            [
                f"I/PictureSelector: 原图地址::{first}\n",
                f"I/PictureSelector: 原图地址::{first}\n",
                f"I/PictureSelector: 压缩地址::{second}\n",
                f"I/PictureSelector: 原图地址::{second}\n",
                "I/PictureSelector: 原图地址::https://evil.example/x.jpeg\n",
            ]
        )
        process = FakeLogcatProcess(lines)
        calls = []
        progress = []
        urls = export_originals.collect_streaming_urls(
            export_originals.Device("127.0.0.1:16384"),
            2468,
            popen=self.make_factory(process, calls),
            progress=progress.append,
        )
        self.assertEqual(urls, [first, second])
        self.assertEqual(progress, ["已发现唯一原图：1", "已发现唯一原图：2"])
        self.assertTrue(process.terminated)
        self.assertEqual(process.wait_calls, [2])
        argv, kwargs = calls[0]
        self.assertEqual(
            argv,
            [
                str(export_originals.ADB),
                "-s",
                "127.0.0.1:16384",
                "logcat",
                "-v",
                "brief",
            ],
        )
        self.assertNotIn("--pid=2468", argv)
        self.assertFalse(any("https://" in value for value in argv))
        self.assertEqual(kwargs["text"], True)
        self.assertEqual(kwargs["errors"], "replace")
        self.assertNotIn("https://", "\n".join(progress))

    def test_wait_timeout_kills_and_waits_again(self):
        process = FakeLogcatProcess(InterruptingLines([]), timeout_once=True)
        urls = export_originals.collect_streaming_urls(
            export_originals.Device("serial"),
            1,
            popen=lambda *args, **kwargs: process,
            progress=lambda value: None,
        )
        self.assertEqual(urls, [])
        self.assertTrue(process.killed)
        self.assertEqual(process.wait_calls, [2, 2])

    def test_spawn_stdout_and_unexpected_exit_fail_redacted(self):
        cases = [
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("private")),
            lambda *args, **kwargs: FakeLogcatProcess(None, returncode=1),
            lambda *args, **kwargs: FakeLogcatProcess(iter([]), returncode=1),
        ]
        for factory in cases:
            with self.subTest(factory=factory):
                with self.assertRaisesRegex(
                    export_originals.SmokeError, "logcat-stream-failed"
                ) as raised:
                    export_originals.collect_streaming_urls(
                        export_originals.Device("serial"), 1, popen=factory
                    )
                self.assertNotIn("private", str(raised.exception))


class BatchCliTests(unittest.TestCase):
    def test_main_dispatches_batch_without_touching_smoke(self):
        with mock.patch.object(export_originals, "run_batch", return_value=0) as batch:
            with mock.patch.object(export_originals, "run_smoke") as smoke:
                self.assertEqual(export_originals.main(["batch"]), 0)
        batch.assert_called_once_with(auto_scroll=False)
        smoke.assert_not_called()

    def test_exact_download_confirmation_only(self):
        self.assertTrue(export_originals.confirm_download(3, lambda prompt: "DOWNLOAD"))
        for answer in ("", "download", " DOWNLOAD", "DOWNLOAD ", "yes"):
            with self.subTest(answer=answer):
                self.assertFalse(
                    export_originals.confirm_download(3, lambda prompt, a=answer: a)
                )
        self.assertFalse(
            export_originals.confirm_download(
                3, lambda prompt: (_ for _ in ()).throw(EOFError)
            )
        )

    def test_zero_candidates_and_cancel_never_download_or_create_output(self):
        runner = FakeRunner(MuMuDiscoveryTests.RUNNING)
        for candidates, answer in (([], "DOWNLOAD"), (["private-url"], "")):
            with self.subTest(candidates=candidates), tempfile.TemporaryDirectory() as temp:
                output = Path(temp) / "must-not-exist"
                with mock.patch.object(
                    export_originals, "collect_streaming_urls", return_value=candidates
                ):
                    result = export_originals.run_batch(
                        run_command=runner,
                        input_fn=lambda prompt, a=answer: a,
                        downloader=lambda *args, **kwargs: self.fail("download called"),
                        output_dir=output,
                )
                self.assertEqual(result, 0)
                self.assertFalse(output.exists())

    def test_approved_batch_summary_controls_exit_and_completion_phrase(self):
        runner = FakeRunner(MuMuDiscoveryTests.RUNNING)
        candidate = f"https://{export_originals.CDN_HOST}/a.jpeg"
        for summary, expected, phrase in (
            (export_originals.BatchSummary(1, 1, 0, 0, 0, 0), 0, True),
            (export_originals.BatchSummary(1, 0, 0, 1, 0, 0), 1, False),
        ):
            with self.subTest(summary=summary):
                stream = io.StringIO()
                with mock.patch.object(
                    export_originals, "collect_streaming_urls", return_value=[candidate]
                ), redirect_stdout(stream):
                    result = export_originals.run_batch(
                        run_command=runner,
                        input_fn=lambda prompt: "DOWNLOAD",
                        downloader=lambda *args, value=summary, **kwargs: value,
                    )
                self.assertEqual(result, expected)
                self.assertEqual("本轮候选下载完成" in stream.getvalue(), phrase)
                self.assertNotIn("账号全量完成", stream.getvalue())


class BatchDownloadTests(unittest.TestCase):
    JPEG = b"\xff\xd8" + b"z" * 2048

    def url(self, name="a"):
        return (
            f"https://{export_originals.CDN_HOST}/moments/images/"
            f"2026-06-04/{name}.jpeg"
        )

    def test_valid_existing_skips_network_and_reapplies_date(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            destination = export_originals.batch_destination(self.url(), output)
            destination.write_bytes(self.JPEG)
            dates = []

            class ForbiddenOpener:
                def open(self, *args, **kwargs):
                    raise AssertionError("network must not be called")

            outcome = export_originals.download_batch_candidate(
                self.url(),
                output,
                opener=ForbiddenOpener(),
                date_setter=lambda path, value: dates.append((path, value)) or True,
            )
            self.assertEqual(outcome.status, "existing")
            self.assertFalse(outcome.date_failed)
            self.assertEqual(dates, [(destination, date(2026, 6, 4))])

    def test_invalid_final_is_preserved_on_failure_and_replaced_on_success(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            destination = export_originals.batch_destination(self.url(), output)
            original = b"not-a-jpeg"
            destination.write_bytes(original)
            failed = export_originals.download_batch_candidate(
                self.url(),
                output,
                opener=FakeOpener([OSError("network")]),
                date_setter=lambda *args: True,
            )
            self.assertEqual(failed.status, "failed")
            self.assertEqual(destination.read_bytes(), original)

            stale = destination.with_suffix(".jpeg.part")
            stale.write_bytes(b"stale-private-data")
            succeeded = export_originals.download_batch_candidate(
                self.url(),
                output,
                opener=FakeOpener([FakeResponse(self.JPEG)]),
                date_setter=lambda *args: True,
            )
            self.assertEqual(succeeded.status, "downloaded")
            self.assertEqual(destination.read_bytes(), self.JPEG)
            self.assertFalse(stale.exists())

    def test_too_small_or_non_soi_existing_is_not_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "candidate.jpeg"
            for body in (b"\xff\xd8short", b"NO" + b"x" * 2048):
                path.write_bytes(body)
                self.assertFalse(export_originals.looks_like_existing_jpeg(path))


class BatchRetryTests(unittest.TestCase):
    def test_first_pass_continues_and_only_failures_retry_twice(self):
        urls = ["private-one", "private-two", "private-three"]
        scripted = {
            urls[0]: ["failed", "downloaded"],
            urls[1]: ["downloaded"],
            urls[2]: ["failed", "failed", "failed"],
        }
        calls = []
        sleeps = []

        def candidate(url, output_dir, *, opener, date_setter):
            calls.append(url)
            status = scripted[url].pop(0)
            return export_originals.CandidateOutcome(
                status, date_failed=(url == urls[1])
            )

        with tempfile.TemporaryDirectory() as temp:
            stream = io.StringIO()
            with redirect_stdout(stream):
                summary = export_originals.download_batch(
                    urls,
                    Path(temp) / "originals",
                    opener=object(),
                    sleep_fn=sleeps.append,
                    candidate_downloader=candidate,
                )
        self.assertEqual(
            calls,
            [urls[0], urls[1], urls[2], urls[0], urls[2], urls[2]],
        )
        self.assertEqual(sleeps, [2, 2])
        self.assertEqual(summary.total, 3)
        self.assertEqual(summary.downloaded, 2)
        self.assertEqual(summary.existing, 0)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.date_failed, 1)
        self.assertEqual(summary.unprocessed, 0)
        self.assertEqual(
            summary.total,
            summary.downloaded + summary.existing + summary.failed + summary.unprocessed,
        )
        self.assertNotIn("private-one", stream.getvalue())
        self.assertNotIn("private-two", stream.getvalue())
        self.assertNotIn("private-three", stream.getvalue())

    def test_interrupt_marks_current_unattempted_and_retry_queue_unprocessed(self):
        urls = ["one", "two", "three"]
        calls = []

        def candidate(url, output_dir, *, opener, date_setter):
            calls.append(url)
            if url == "one":
                return export_originals.CandidateOutcome("failed")
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as temp:
            summary = export_originals.download_batch(
                urls,
                Path(temp) / "originals",
                opener=object(),
                sleep_fn=lambda value: None,
                candidate_downloader=candidate,
            )
        self.assertEqual(calls, ["one", "two"])
        self.assertEqual(summary.failed, 0)
        self.assertEqual(summary.unprocessed, 3)
        self.assertEqual(
            summary.total,
            summary.downloaded + summary.existing + summary.failed + summary.unprocessed,
        )

    def test_keyboard_interrupt_during_read_removes_part(self):
        class InterruptResponse(FakeResponse):
            def read(self, size=-1):
                raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "candidate.jpeg"
            with self.assertRaises(KeyboardInterrupt):
                export_originals.download_sample(
                    FakeOpener([InterruptResponse(b"")]),
                    "https://example.invalid/private.jpeg",
                    destination,
                )
            self.assertFalse(destination.with_suffix(".jpeg.part").exists())


class AutoUiRunner:
    """run_command fake for auto-scroll UI ops: wm size + input swipe."""

    def __init__(self, size_output="Physical size: 1080x1920"):
        self.size_output = size_output
        self.calls = []
        self.swipes = 0

    def __call__(self, argv):
        argv = [str(value) for value in argv]
        self.calls.append(argv)
        if any("https://" in value for value in argv):
            raise AssertionError("command arguments must not contain photo URLs")
        if "wm" in argv and "size" in argv:
            return completed(argv, self.size_output)
        if "swipe" in argv:
            self.swipes += 1
            return completed(argv)
        return completed(argv, returncode=1, stderr="unexpected-command")


class AutoDataRunner:
    """run_binary fake: logcat -d dumps (bytes) + screencap frames (bytes)."""

    def __init__(self, dumps, frames, fail_capture=False):
        self.dumps = list(dumps)
        self.frames = list(frames)
        self.fail_capture = fail_capture
        self.calls = []

    def __call__(self, argv):
        argv = [str(value) for value in argv]
        self.calls.append(argv)
        if "logcat" in argv:
            text = self.dumps.pop(0) if self.dumps else ""
            body = text.encode("utf-8") if isinstance(text, str) else text
            return completed(argv, body)
        if "screencap" in argv:
            if self.fail_capture:
                return completed(argv, b"", returncode=1)
            frame = self.frames.pop(0) if self.frames else b""
            return completed(argv, frame)
        return completed(argv, b"", returncode=1)


class ScreenSizeTests(unittest.TestCase):
    def _size(self, output, returncode=0):
        def runner(argv):
            self.assertIn("wm", [str(value) for value in argv])
            return completed(argv, output, returncode=returncode)

        return export_originals.get_screen_size(
            export_originals.Device("127.0.0.1:16384"), run_command=runner
        )

    def test_parses_physical_size(self):
        self.assertEqual(self._size("Physical size: 1080x1920\n"), (1080, 1920))

    def test_prefers_override_over_physical(self):
        output = "Physical size: 1080x1920\nOverride size: 720x1280\n"
        self.assertEqual(self._size(output), (720, 1280))

    def test_failure_returncode_is_redacted(self):
        with self.assertRaisesRegex(export_originals.SmokeError, "screen-size-failed"):
            self._size("", returncode=1)

    def test_missing_size_line_raises(self):
        with self.assertRaisesRegex(export_originals.SmokeError, "screen-size-failed"):
            self._size("no size here\n")


class SwipeTests(unittest.TestCase):
    def test_issues_scroll_swipe_within_bounds_without_urls(self):
        calls = []

        def runner(argv):
            calls.append([str(value) for value in argv])
            return completed(argv)

        export_originals.swipe_scroll(
            export_originals.Device("127.0.0.1:16384"), 1080, 1920, run_command=runner
        )
        self.assertEqual(
            calls,
            [[
                str(export_originals.ADB),
                "-s",
                "127.0.0.1:16384",
                "shell",
                "input",
                "swipe",
                "540",
                "1440",
                "540",
                "576",
                str(export_originals.SWIPE_DURATION_MS),
            ]],
        )
        _, _, _, _, _, _, x1, y1, x2, y2, _ = calls[0]
        self.assertEqual(x1, x2)
        self.assertGreater(int(y1), int(y2))
        self.assertLess(int(y1), 1920)
        self.assertGreater(int(y2), 0)

    def test_swipe_failure_is_redacted(self):
        runner = lambda argv: completed(argv, returncode=1)
        with self.assertRaisesRegex(export_originals.SmokeError, "swipe-failed"):
            export_originals.swipe_scroll(
                export_originals.Device("serial"), 1080, 1920, run_command=runner
            )


class DumpLogcatTests(unittest.TestCase):
    def test_extracts_unique_urls_and_tolerates_invalid_bytes(self):
        host = export_originals.CDN_HOST
        first = f"https://{host}/moments/images/2026-06-04/a.jpeg"
        second = f"https://{host}/moments/images/2026-06-05/b.jpeg"
        calls = []

        def run_binary(argv):
            calls.append([str(value) for value in argv])
            body = "\n".join(
                [f"原图地址::{first}", f"原图地址::{first}", f"原图地址::{second}"]
            ).encode("utf-8")
            # Full device logcat carries other apps' non-UTF-8 bytes.
            body += b"\n\xc0\xc1 other-app garbage \x80\xff\n"
            return completed(argv, body)

        urls = export_originals.dump_logcat_urls(
            export_originals.Device("127.0.0.1:16384"), run_binary=run_binary
        )
        self.assertEqual(urls, [first, second])
        self.assertEqual(
            calls[0],
            [
                str(export_originals.ADB),
                "-s",
                "127.0.0.1:16384",
                "logcat",
                "-d",
                "-v",
                "brief",
            ],
        )
        self.assertNotIn("--pid=2468", calls[0])

    def test_dump_failure_is_redacted(self):
        run_binary = lambda argv: completed(argv, b"", returncode=1)
        with self.assertRaisesRegex(export_originals.SmokeError, "logcat-failed"):
            export_originals.dump_logcat_urls(
                export_originals.Device("serial"), run_binary=run_binary
            )


class ScreenSignatureTests(unittest.TestCase):
    def test_hashes_bytes_and_never_returns_raw_frame(self):
        frame = b"\x89PNG-secret-photo-bytes"
        captured = []

        def run_binary(argv):
            captured.append([str(value) for value in argv])
            return completed(argv, frame)

        signature = export_originals.capture_screen_signature(
            export_originals.Device("127.0.0.1:16384"), run_binary=run_binary
        )
        self.assertEqual(signature, hashlib.sha256(frame).digest())
        self.assertNotEqual(signature, frame)
        self.assertEqual(
            captured[0],
            [
                str(export_originals.ADB),
                "-s",
                "127.0.0.1:16384",
                "exec-out",
                "screencap",
                "-p",
            ],
        )

    def test_failure_or_empty_returns_none(self):
        for result in (completed([], b"", returncode=1), completed([], b"")):
            with self.subTest(result=result):
                self.assertIsNone(
                    export_originals.capture_screen_signature(
                        export_originals.Device("serial"),
                        run_binary=lambda argv: result,
                    )
                )


class AutoScrollCollectTests(unittest.TestCase):
    def setUp(self):
        host = export_originals.CDN_HOST
        self.a = f"https://{host}/moments/images/2026-06-04/a.jpeg"
        self.b = f"https://{host}/moments/images/2026-06-05/b.jpeg"

    def _line(self, url):
        return f"I/PictureSelector: 原图地址::{url}\n"

    def _collect(
        self,
        dumps,
        frames,
        *,
        max_stable=2,
        max_swipes=2000,
        sleep_fn=None,
        progress=None,
        fail_capture=False,
    ):
        ui = AutoUiRunner()
        data = AutoDataRunner(dumps, frames, fail_capture=fail_capture)
        urls = export_originals.collect_with_auto_scroll(
            export_originals.Device("127.0.0.1:16384"),
            run_command=ui,
            run_binary=data,
            sleep_fn=sleep_fn or (lambda seconds: None),
            progress=progress if progress is not None else (lambda value: None),
            max_stable_screens=max_stable,
            max_swipes=max_swipes,
            settle_seconds=0,
        )
        return urls, ui, data

    def test_stops_when_screen_goes_stable_and_collects_throughout(self):
        text_a = self._line(self.a)
        text_ab = self._line(self.a) + self._line(self.b)
        # b only appears after an idle swipe: collection is not tied to stall.
        dumps = [text_a, text_a, text_ab, text_ab, text_ab]
        frames = [b"f1", b"f2", b"f3", b"f3", b"f3"]
        progress = []
        urls, ui, _ = self._collect(
            dumps, frames, max_stable=2, progress=progress.append
        )
        self.assertEqual(urls, [self.a, self.b])
        self.assertEqual(ui.swipes, 4)
        self.assertTrue(all("https://" not in item for item in progress))
        self.assertIn(
            [
                str(export_originals.ADB),
                "-s",
                "127.0.0.1:16384",
                "shell",
                "wm",
                "size",
            ],
            ui.calls,
        )

    def test_screen_change_resets_stable_counter(self):
        dumps = [self._line(self.a)] * 6
        frames = [b"f1", b"f1", b"f2", b"f1", b"f1", b"f1"]
        urls, ui, _ = self._collect(dumps, frames, max_stable=2)
        self.assertEqual(urls, [self.a])
        self.assertEqual(ui.swipes, 5)

    def test_keyboard_interrupt_returns_partial(self):
        def boom(seconds):
            raise KeyboardInterrupt

        urls, ui, _ = self._collect(
            [self._line(self.a)], [b"f1"], max_stable=2, sleep_fn=boom
        )
        self.assertEqual(urls, [self.a])
        self.assertEqual(ui.swipes, 1)

    def test_never_stable_stops_at_max_swipes(self):
        dumps = [self._line(self.a)] * 10
        frames = [b"f1", b"f2", b"f3", b"f4"]
        urls, ui, _ = self._collect(dumps, frames, max_stable=2, max_swipes=3)
        self.assertEqual(urls, [self.a])
        self.assertEqual(ui.swipes, 3)

    def test_capture_failure_does_not_stop_early(self):
        dumps = [self._line(self.a)] * 10
        _, ui, _ = self._collect(
            dumps, [], max_stable=2, max_swipes=3, fail_capture=True
        )
        self.assertEqual(ui.swipes, 3)

    def test_no_candidates_stops_and_returns_empty(self):
        urls, ui, _ = self._collect([], [b"f1", b"f1", b"f1"], max_stable=2)
        self.assertEqual(urls, [])
        self.assertEqual(ui.swipes, 2)


class AutoScrollCliTests(unittest.TestCase):
    def test_batch_auto_scroll_flag_dispatches(self):
        with mock.patch.object(export_originals, "run_batch", return_value=0) as batch:
            self.assertEqual(export_originals.main(["batch", "--auto-scroll"]), 0)
        batch.assert_called_once_with(auto_scroll=True)

    def test_batch_without_flag_defaults_manual(self):
        with mock.patch.object(export_originals, "run_batch", return_value=0) as batch:
            self.assertEqual(export_originals.main(["batch"]), 0)
        batch.assert_called_once_with(auto_scroll=False)

    def test_run_batch_auto_scroll_uses_auto_collector(self):
        runner = FakeRunner(MuMuDiscoveryTests.RUNNING)
        candidate = f"https://{export_originals.CDN_HOST}/a.jpeg"
        with mock.patch.object(
            export_originals, "collect_with_auto_scroll", return_value=[candidate]
        ) as auto, mock.patch.object(
            export_originals, "collect_streaming_urls"
        ) as manual, redirect_stdout(io.StringIO()):
            result = export_originals.run_batch(
                run_command=runner,
                input_fn=lambda prompt: "",
                auto_scroll=True,
            )
        auto.assert_called_once()
        manual.assert_not_called()
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
