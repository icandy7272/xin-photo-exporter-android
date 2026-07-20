import io
import json
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest import mock

from tools import export_originals as eo
from tools import feed_api


CDN = "https://cdn-mctchildfoliocn.childfolio.net"


def _pic(date: str, name: str) -> str:
    return f"{CDN}/provider/1/moments/images/{date}/{name}.jpeg"


def _payload(moments, *, has_more=False, counter=0):
    return {
        "code": 0,
        "msg": "success",
        "data": {"hasMore": has_more, "counter": counter, "momentList": moments},
    }


def _completed(stdout: str, returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class ExtractPrefStringTests(unittest.TestCase):
    def test_found(self):
        xml = '<map><string name="accessToken">tok123</string></map>'
        self.assertEqual(feed_api.extract_pref_string(xml, "accessToken"), "tok123")

    def test_missing_returns_none(self):
        self.assertIsNone(feed_api.extract_pref_string("<map/>", "accessToken"))

    def test_empty_value_returns_none(self):
        xml = '<string name="album_child_id"></string>'
        self.assertIsNone(feed_api.extract_pref_string(xml, "album_child_id"))


class ExtractPictureUrlsTests(unittest.TestCase):
    def test_pulls_and_dedupes_across_moments(self):
        u1, u2 = _pic("2024-01-02", "a"), _pic("2024-01-03", "b")
        payload = _payload([{"pictureURLs": [u1, u2]}, {"pictureURLs": [u2]}])
        self.assertEqual(feed_api.extract_picture_urls(payload), [u1, u2])

    def test_ignores_avatars_logos_and_video(self):
        payload = _payload(
            [
                {
                    "pictureURLs": [_pic("2024-01-02", "a")],
                    "logo": f"{CDN}/provider/1/logo.jpeg",
                    "videoUrl": f"{CDN}/x.mp4",
                    "childList": [{"faceUrl": f"{CDN}/face.jpeg"}],
                }
            ]
        )
        self.assertEqual(feed_api.extract_picture_urls(payload), [_pic("2024-01-02", "a")])

    def test_rejects_invalid_urls(self):
        payload = _payload(
            [
                {
                    "pictureURLs": [
                        f"{CDN}/a/b.png",  # not jpeg
                        "http://evil.example/x.jpeg",  # wrong host + scheme
                        f"{CDN}/a/b.jpeg?x-oss-process=resize",  # has query
                        12345,  # not a string
                    ]
                }
            ]
        )
        self.assertEqual(feed_api.extract_picture_urls(payload), [])

    def test_handles_missing_or_malformed(self):
        self.assertEqual(feed_api.extract_picture_urls(None), [])
        self.assertEqual(feed_api.extract_picture_urls({}), [])
        self.assertEqual(feed_api.extract_picture_urls({"data": {}}), [])
        self.assertEqual(feed_api.extract_picture_urls({"data": {"momentList": "x"}}), [])


class CollectApiUrlsTests(unittest.TestCase):
    def test_paginates_until_no_more(self):
        pages = {
            100: _payload([{"pictureURLs": [_pic("2024-01-02", "a")]}], has_more=True, counter=90),
            90: _payload([{"pictureURLs": [_pic("2024-01-01", "b")]}], has_more=True, counter=80),
            80: _payload([{"pictureURLs": [_pic("2023-12-31", "c")]}], has_more=False),
        }
        calls: list[int] = []

        def post(child_id, counter):
            calls.append(counter)
            return pages[counter]

        with redirect_stdout(io.StringIO()):
            urls = feed_api.collect_api_urls(post, "child", initial_counter=100)
        self.assertEqual(calls, [100, 90, 80])
        self.assertEqual(len(urls), 3)

    def test_dedupes_across_pages(self):
        u = _pic("2024-01-02", "a")
        pages = {
            100: _payload([{"pictureURLs": [u]}], has_more=True, counter=90),
            90: _payload([{"pictureURLs": [u]}], has_more=False),
        }
        with redirect_stdout(io.StringIO()):
            urls = feed_api.collect_api_urls(lambda c, n: pages[n], "child", initial_counter=100)
        self.assertEqual(urls, [u])

    def test_safety_break_when_cursor_stalls(self):
        stuck = _payload([], has_more=True, counter=100)  # counter never moves off 100
        calls: list[int] = []

        def post(cid, counter):
            calls.append(counter)
            return stuck

        with redirect_stdout(io.StringIO()):
            feed_api.collect_api_urls(post, "child", initial_counter=100)
        self.assertEqual(calls, [100])

    def test_max_pages_cap(self):
        def post(cid, counter):
            return _payload([], has_more=True, counter=counter - 1)

        calls: list[int] = []

        def counting_post(cid, counter):
            calls.append(counter)
            return post(cid, counter)

        with redirect_stdout(io.StringIO()):
            feed_api.collect_api_urls(counting_post, "child", initial_counter=1000, max_pages=3)
        self.assertEqual(len(calls), 3)

    def test_keyboard_interrupt_returns_partial(self):
        def post(cid, counter):
            if counter == 100:
                return _payload([{"pictureURLs": [_pic("2024-01-02", "a")]}], has_more=True, counter=90)
            raise KeyboardInterrupt

        with redirect_stdout(io.StringIO()):
            urls = feed_api.collect_api_urls(post, "child", initial_counter=100)
        self.assertEqual(len(urls), 1)


class ReadCredentialsTests(unittest.TestCase):
    def test_reads_token_and_child_id(self):
        xml = (
            '<string name="accessToken">tok</string>'
            '<string name="album_child_id">cid-uuid</string>'
        )
        token, child = feed_api.read_app_credentials(
            eo.Device("127.0.0.1:1"), run_command=lambda argv: _completed(xml)
        )
        self.assertEqual((token, child), ("tok", "cid-uuid"))

    def test_missing_child_id_raises(self):
        xml = '<string name="accessToken">tok</string>'
        with self.assertRaises(eo.SmokeError):
            feed_api.read_app_credentials(eo.Device("s"), run_command=lambda argv: _completed(xml))

    def test_command_failure_raises(self):
        with self.assertRaises(eo.SmokeError):
            feed_api.read_app_credentials(
                eo.Device("s"), run_command=lambda argv: _completed("", returncode=1)
            )


class FetchMomentPageTests(unittest.TestCase):
    class _Resp:
        def __init__(self, status, body=b"{}"):
            self.status = status
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def test_posts_json_body_and_parses(self):
        captured = {}

        class Opener:
            def open(self, request, timeout=0):
                captured["url"] = request.full_url
                captured["data"] = request.data
                captured["headers"] = dict(request.headers)
                return FetchMomentPageTests._Resp(200, json.dumps({"data": {"hasMore": False}}).encode())

        payload = feed_api.fetch_moment_page(Opener(), "tok", "cid", 42)
        self.assertEqual(payload, {"data": {"hasMore": False}})
        self.assertIn("getPageMomentList", captured["url"])
        self.assertEqual(
            json.loads(captured["data"]),
            {"childIds": ["cid"], "counter": 42, "paChildIds": ["cid"]},
        )
        # urllib title-cases header names.
        self.assertEqual(captured["headers"].get("Client"), "fa_app")
        self.assertEqual(captured["headers"].get("Authorization"), "Bearer tok")

    def test_non_200_raises(self):
        class Opener:
            def open(self, request, timeout=0):
                return FetchMomentPageTests._Resp(500)

        with self.assertRaises(eo.SmokeError):
            feed_api.fetch_moment_page(Opener(), "t", "c", 1)


class RunApiTests(unittest.TestCase):
    def _creds(self):
        return _completed(
            '<string name="accessToken">tok</string>'
            '<string name="album_child_id">cid</string>'
        )

    def test_happy_path_collects_and_downloads(self):
        url = _pic("2024-01-02", "a")
        page = _payload([{"pictureURLs": [url]}], has_more=False)
        got = {}

        def fake_downloader(urls, output_dir):
            got["urls"] = urls
            return eo.BatchSummary(len(urls), len(urls), 0, 0, 0, 0)

        with mock.patch.object(eo, "discover_running_device", return_value=eo.Device("s")), \
                mock.patch.object(feed_api, "fetch_moment_page", return_value=page):
            with redirect_stdout(io.StringIO()):
                rc = feed_api.run_api(
                    run_command=lambda argv: self._creds(),
                    opener=object(),
                    input_fn=lambda prompt: "DOWNLOAD",
                    downloader=fake_downloader,
                )
        self.assertEqual(rc, 0)
        self.assertEqual(got["urls"], [url])

    def test_cancel_skips_download(self):
        page = _payload([{"pictureURLs": [_pic("2024-01-02", "a")]}], has_more=False)
        called = {"n": 0}

        def fake_downloader(urls, output_dir):
            called["n"] += 1
            return eo.BatchSummary(0, 0, 0, 0, 0, 0)

        with mock.patch.object(eo, "discover_running_device", return_value=eo.Device("s")), \
                mock.patch.object(feed_api, "fetch_moment_page", return_value=page):
            with redirect_stdout(io.StringIO()):
                rc = feed_api.run_api(
                    run_command=lambda argv: self._creds(),
                    opener=object(),
                    input_fn=lambda prompt: "",  # not DOWNLOAD
                    downloader=fake_downloader,
                )
        self.assertEqual(rc, 0)
        self.assertEqual(called["n"], 0)

    def test_no_candidates_returns_zero(self):
        empty = _payload([], has_more=False)
        with mock.patch.object(eo, "discover_running_device", return_value=eo.Device("s")), \
                mock.patch.object(feed_api, "fetch_moment_page", return_value=empty):
            with redirect_stdout(io.StringIO()):
                rc = feed_api.run_api(
                    run_command=lambda argv: self._creds(),
                    opener=object(),
                    input_fn=lambda prompt: "DOWNLOAD",
                    downloader=lambda urls, out: eo.BatchSummary(0, 0, 0, 0, 0, 0),
                )
        self.assertEqual(rc, 0)

    def test_credentials_failure_returns_one(self):
        with mock.patch.object(eo, "discover_running_device", return_value=eo.Device("s")):
            with redirect_stdout(io.StringIO()):
                rc = feed_api.run_api(
                    run_command=lambda argv: _completed("", returncode=1),
                    opener=object(),
                )
        self.assertEqual(rc, 1)


class CliDispatchTests(unittest.TestCase):
    def test_api_command_dispatches_with_counter(self):
        with mock.patch("tools.feed_api.run_api", return_value=0) as run_api:
            rc = eo.main(["api", "--counter", "12345"])
        self.assertEqual(rc, 0)
        run_api.assert_called_once_with(initial_counter=12345)

    def test_api_command_defaults_counter(self):
        with mock.patch("tools.feed_api.run_api", return_value=0) as run_api:
            eo.main(["api"])
        run_api.assert_called_once_with(initial_counter=feed_api.DEFAULT_INITIAL_COUNTER)


if __name__ == "__main__":
    unittest.main()
