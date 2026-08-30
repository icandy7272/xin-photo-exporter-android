import contextlib
import io
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from tools import export_originals as eo


HOST = eo.CDN_HOST


def _completed(stdout, returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, "")


class ValidateOriginalUrlTests(unittest.TestCase):
    def test_accepts_https_cdn_jpeg_jpg_png(self):
        for ext in ("jpeg", "jpg", "png"):
            url = f"https://{HOST}/provider/1/moments/images/2026-04-17/a.{ext}"
            self.assertEqual(eo.validate_original_url(url), url)

    def test_accepts_uppercase_extension(self):
        # Some older uploads kept the camera's ".JPG"; the extension check must
        # not be case-sensitive or those photos are silently skipped.
        for ext in ("JPG", "JPEG", "PNG", "Jpg"):
            url = f"https://{HOST}/provider/1/moments/images/2022-09-01/a.{ext}"
            self.assertEqual(eo.validate_original_url(url), url)

    def test_rejects_unsafe_or_wrong_format(self):
        for url in [
            f"http://{HOST}/a.jpeg",  # scheme
            "https://evil.example/a.jpeg",  # host
            f"https://{HOST}.evil.example/a.jpeg",  # suffix host
            f"https://{HOST}:444/a.jpeg",  # port
            f"https://{HOST}/a.jpeg?v=1",  # query
            f"https://{HOST}/a.jpeg#x",  # fragment
            f"https://{HOST}/a.gif",  # extension
            "https://[malformed/a.jpeg",  # malformed
        ]:
            self.assertIsNone(eo.validate_original_url(url), url)


class RecordDateTests(unittest.TestCase):
    def test_extracts_date_from_moments_images_path(self):
        url = f"https://{HOST}/provider/1/moments/images/2026-04-17/a.jpeg"
        self.assertEqual(eo.extract_record_date(url), date(2026, 4, 17))

    def test_missing_or_invalid_date_returns_none(self):
        self.assertIsNone(eo.extract_record_date(f"https://{HOST}/no-date/a.jpeg"))
        self.assertIsNone(
            eo.extract_record_date(f"https://{HOST}/moments/images/2026-02-30/a.jpeg")
        )


class BatchDestinationTests(unittest.TestCase):
    def test_stable_dated_name(self):
        url = f"https://{HOST}/provider/1/moments/images/2026-06-04/x.jpeg"
        first = eo.batch_destination(url, Path("/tmp/out"))
        second = eo.batch_destination(url, Path("/tmp/out"))
        self.assertEqual(first, second)
        self.assertTrue(first.name.startswith("2026-06-04_"))

    def test_jpg_maps_to_jpeg_png_stays_png(self):
        base = f"https://{HOST}/provider/1/moments/images/2026-06-04/x"
        self.assertTrue(eo.batch_destination(base + ".jpg", Path("/o")).name.endswith(".jpeg"))
        self.assertTrue(eo.batch_destination(base + ".png", Path("/o")).name.endswith(".png"))

    def test_missing_date_uses_unknown_prefix(self):
        url = f"https://{HOST}/provider/1/no-date/a.jpeg"
        self.assertTrue(eo.batch_destination(url, Path("/o")).name.startswith("unknown-date_"))

    def test_different_urls_differ(self):
        a = f"https://{HOST}/moments/images/2026-06-04/a.jpeg"
        b = f"https://{HOST}/moments/images/2026-06-04/b.jpeg"
        self.assertNotEqual(eo.batch_destination(a, Path("/o")), eo.batch_destination(b, Path("/o")))


class DiscoverDeviceTests(unittest.TestCase):
    def _runner(self, info_json, *, connect_ok=True):
        def run(argv):
            if str(argv[1]) == "info":
                return _completed(info_json)
            return _completed("connected") if connect_ok else _completed("", 1)

        return run

    def test_single_running_instance(self):
        info = '{"return":{"results":[{"state":"running","adb_port":16384}]}}'
        self.assertEqual(eo.discover_running_device(self._runner(info)).serial, "127.0.0.1:16384")

    def test_zero_running_raises(self):
        with self.assertRaises(eo.SmokeError):
            eo.discover_running_device(self._runner('{"return":{"results":[]}}'))

    def test_multiple_running_raises(self):
        info = '{"return":{"results":[{"state":"running","adb_port":1},{"state":"running","adb_port":2}]}}'
        with self.assertRaises(eo.SmokeError):
            eo.discover_running_device(self._runner(info))

    def test_mumu_command_failure_raises(self):
        with self.assertRaises(eo.SmokeError):
            eo.discover_running_device(lambda argv: _completed("", 1))


class CaAndOpenerTests(unittest.TestCase):
    def test_select_ca_prefers_python_then_system(self):
        with tempfile.TemporaryDirectory() as temp:
            python_ca = Path(temp) / "py.pem"
            system_ca = Path(temp) / "sys.pem"
            system_ca.write_text("x")
            self.assertEqual(eo.select_ca_file(python_ca, system_ca), system_ca)
            python_ca.write_text("y")
            self.assertEqual(eo.select_ca_file(python_ca, system_ca), python_ca)

    def test_no_ca_raises(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(eo.SmokeError):
                eo.select_ca_file(Path(temp) / "a", Path(temp) / "b")

    def test_opener_disables_proxy_and_rejects_redirects(self):
        sentinel = object()
        with mock.patch.object(
            eo.urllib.request, "build_opener", return_value=sentinel
        ) as builder:
            self.assertIs(eo.build_opener(), sentinel)
        handlers = builder.call_args.args
        proxy = next(h for h in handlers if isinstance(h, eo.urllib.request.ProxyHandler))
        self.assertEqual(proxy.proxies, {})
        self.assertTrue(any(isinstance(h, eo.RejectRedirects) for h in handlers))


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


class DownloadSampleTests(unittest.TestCase):
    JPEG = b"\xff\xd8" + b"x" * 2048
    PNG = b"\x89P" + b"x" * 2048

    def test_jpeg_streams_and_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "a.jpeg"
            result = eo.download_sample(FakeOpener([FakeResponse(self.JPEG)]), "https://x/a.jpeg", dest)
            self.assertEqual(dest.read_bytes(), self.JPEG)
            self.assertEqual(result.byte_count, len(self.JPEG))
            self.assertFalse(dest.with_suffix(".jpeg.part").exists())

    def test_png_streams(self):
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "a.png"
            opener = FakeOpener([FakeResponse(self.PNG, content_type="image/png")])
            eo.download_sample(opener, "https://x/a.png", dest)
            self.assertEqual(dest.read_bytes(), self.PNG)

    def test_failures_leave_no_file(self):
        cases = [
            FakeResponse(self.JPEG, status=500),
            FakeResponse(self.JPEG, content_type="text/html"),
            FakeResponse(b"NO" + b"x" * 2048),  # wrong magic
            FakeResponse(b"\xff\xd8"),  # too small
            RuntimeError("boom"),
        ]
        for resp in cases:
            with tempfile.TemporaryDirectory() as temp:
                dest = Path(temp) / "a.jpeg"
                with self.assertRaises(eo.SmokeError):
                    eo.download_sample(FakeOpener([resp]), "https://x/a.jpeg", dest)
                self.assertFalse(dest.exists())
                self.assertFalse(dest.with_suffix(".jpeg.part").exists())

    def test_over_limit_removed(self):
        big = b"\xff\xd8" + b"x" * (eo.MAX_BYTES + 10)
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "a.jpeg"
            with self.assertRaises(eo.SmokeError):
                eo.download_sample(FakeOpener([FakeResponse(big)]), "https://x/a.jpeg", dest)
            self.assertFalse(dest.exists())


class ExistingImageTests(unittest.TestCase):
    def test_jpeg_png_valid_others_invalid(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "f"
            path.write_bytes(b"\xff\xd8" + b"x" * 2048)
            self.assertTrue(eo.looks_like_existing_image(path))
            path.write_bytes(b"\x89P" + b"x" * 2048)
            self.assertTrue(eo.looks_like_existing_image(path))
            path.write_bytes(b"NO" + b"x" * 2048)
            self.assertFalse(eo.looks_like_existing_image(path))
            path.write_bytes(b"\xff\xd8short")  # too small
            self.assertFalse(eo.looks_like_existing_image(path))


class ApplyRecordDateTests(unittest.TestCase):
    def test_none_date_is_noop(self):
        self.assertTrue(eo.apply_record_date(Path("/nonexistent"), None))

    def test_sets_utime_and_setfile(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a.jpeg"
            path.write_bytes(b"x")
            calls = []
            ok = eo.apply_record_date(
                path, date(2026, 6, 4), run_command=lambda a: calls.append(a) or _completed("")
            )
            self.assertTrue(ok)
            self.assertTrue(any("SetFile" in str(c[0]) for c in calls))

    def test_oserror_returns_false(self):
        ok = eo.apply_record_date(
            Path("/definitely/missing/a.jpeg"), date(2026, 6, 4), run_command=lambda a: _completed("")
        )
        self.assertFalse(ok)


class DownloadBatchCandidateTests(unittest.TestCase):
    URL = f"https://{HOST}/provider/1/moments/images/2026-06-04/a.jpeg"

    def test_existing_skips_network(self):
        with tempfile.TemporaryDirectory() as temp:
            dest = eo.batch_destination(self.URL, Path(temp))
            dest.write_bytes(b"\xff\xd8" + b"x" * 2048)
            out = eo.download_batch_candidate(self.URL, Path(temp), opener=None, date_setter=lambda d, rd: True)
            self.assertEqual(out.status, "existing")

    def test_download_success(self):
        with tempfile.TemporaryDirectory() as temp:
            opener = FakeOpener([FakeResponse(b"\xff\xd8" + b"x" * 2048)])
            out = eo.download_batch_candidate(self.URL, Path(temp), opener=opener, date_setter=lambda d, rd: True)
            self.assertEqual(out.status, "downloaded")

    def test_download_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            opener = FakeOpener([FakeResponse(b"", status=500)])
            out = eo.download_batch_candidate(self.URL, Path(temp), opener=opener, date_setter=lambda d, rd: True)
            self.assertEqual(out.status, "failed")


class DownloadBatchTests(unittest.TestCase):
    def test_success_and_persistent_failure_retried_then_counted(self):
        attempts = []

        def downloader(url, output_dir, *, opener, date_setter):
            attempts.append(url)
            return eo.CandidateOutcome("downloaded" if url == "good" else "failed")

        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(eo, "ensure_build_is_ignored"):
                with contextlib.redirect_stdout(io.StringIO()):
                    summary = eo.download_batch(
                        ["good", "bad"],
                        Path(temp),
                        opener=object(),
                        sleep_fn=lambda s: None,
                        candidate_downloader=downloader,
                    )
        self.assertEqual(summary.downloaded, 1)
        self.assertEqual(summary.failed, 1)
        # "bad" is retried across all three passes.
        self.assertEqual(attempts.count("bad"), 3)


class ConfirmDownloadTests(unittest.TestCase):
    def test_only_exact_download_confirms(self):
        self.assertTrue(eo.confirm_download(3, input_fn=lambda p: "DOWNLOAD"))
        for answer in ("download", "yes", "", "DOWNLOAD "):
            self.assertFalse(eo.confirm_download(3, input_fn=lambda p, a=answer: a))

    def test_eof_cancels(self):
        def boom(prompt):
            raise EOFError

        self.assertFalse(eo.confirm_download(3, input_fn=boom))


class EnsureBuildIgnoredTests(unittest.TestCase):
    def test_passes_when_build_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / ".gitignore").write_text("build/\n")
            eo.ensure_build_is_ignored(Path(temp))  # no raise

    def test_raises_when_not_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / ".gitignore").write_text("other\n")
            with self.assertRaises(eo.SmokeError):
                eo.ensure_build_is_ignored(Path(temp))


class CliTests(unittest.TestCase):
    def test_api_dispatches(self):
        with mock.patch("tools.feed_api.run_api", return_value=0) as run_api:
            self.assertEqual(eo.main(["api", "--yes"]), 0)
        run_api.assert_called_once_with(initial_counter=None, include_videos=True, assume_yes=True)

    def test_no_command_prints_help_and_returns_one(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(eo.main([]), 1)


if __name__ == "__main__":
    unittest.main()
