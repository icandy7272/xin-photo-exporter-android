import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tools import export_originals as eo
from tools import feed_api


# Reserved documentation-only host: tests must never contain a real service
# host or a real child/provider identifier.
CDN = "https://example.invalid"


def _pic(date: str, name: str) -> str:
    return f"{CDN}/provider/1/moments/images/{date}/{name}.jpeg"


def _vid(date: str, name: str) -> str:
    return f"{CDN}/provider/1/moments/videos/{date}/{name}.mp4"


def _moment(**kwargs) -> dict:
    return kwargs


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


class ValidateVideoUrlTests(unittest.TestCase):
    def test_accepts_cdn_mp4(self):
        u = _vid("2024-01-02", "v")
        self.assertEqual(feed_api.validate_video_url(u), u)

    def test_rejects_non_video_ext(self):
        self.assertIsNone(feed_api.validate_video_url(_pic("2024-01-02", "a")))

    def test_rejects_wrong_host_or_scheme(self):
        self.assertIsNone(feed_api.validate_video_url("https://evil.example/x.mp4"))
        self.assertIsNone(feed_api.validate_video_url(f"http://example.invalid/x.mp4"))


class ExtractMomentsTests(unittest.TestCase):
    def test_extracts_text_photos_and_video(self):
        payload = _payload(
            [
                _moment(
                    momentId="m1",
                    publishedTime="2024-01-02 10:00:00",
                    momentCaption="第一天上学",
                    pictureURLs=[_pic("2024-01-02", "a"), _pic("2024-01-02", "b")],
                    videoUrl=_vid("2024-01-02", "v"),
                )
            ]
        )
        records = feed_api.extract_moments(payload)
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec.moment_id, "m1")
        self.assertEqual(rec.published_time, "2024-01-02 10:00:00")
        self.assertEqual(rec.caption, "第一天上学")
        self.assertEqual(rec.picture_urls, (_pic("2024-01-02", "a"), _pic("2024-01-02", "b")))
        self.assertEqual(rec.video_url, _vid("2024-01-02", "v"))

    def test_accepts_jpeg_jpg_and_png(self):
        jpg = f"{CDN}/provider/1/moments/images/2024-01-02/abc.jpg"
        png = f"{CDN}/provider/1/moments/images/2024-01-02/abc.png"
        payload = _payload(
            [_moment(momentId="m1", pictureURLs=[_pic("2024-01-02", "a"), jpg, png])]
        )
        records = feed_api.extract_moments(payload)
        self.assertEqual(records[0].picture_urls, (_pic("2024-01-02", "a"), jpg, png))

    def test_ignores_avatars_and_invalid_media(self):
        payload = _payload(
            [
                _moment(
                    momentId="m2",
                    # second URL has a query string -> rejected as a resized variant
                    pictureURLs=[_pic("2024-01-02", "a"), f"{CDN}/a/b.jpeg?x-oss-process=resize"],
                    videoUrl=_pic("2024-01-02", "a"),  # jpeg, not a video
                    logo=f"{CDN}/logo.jpeg",
                    childList=[{"faceUrl": f"{CDN}/face.jpeg"}],
                )
            ]
        )
        rec = feed_api.extract_moments(payload)[0]
        self.assertEqual(rec.picture_urls, (_pic("2024-01-02", "a"),))
        self.assertIsNone(rec.video_url)

    def test_missing_fields_default_empty(self):
        rec = feed_api.extract_moments(_payload([{}]))[0]
        self.assertEqual(rec.moment_id, "")
        self.assertEqual(rec.caption, "")
        self.assertEqual(rec.picture_urls, ())
        self.assertIsNone(rec.video_url)

    def test_handles_malformed(self):
        self.assertEqual(feed_api.extract_moments(None), [])
        self.assertEqual(feed_api.extract_moments({}), [])
        self.assertEqual(feed_api.extract_moments({"data": {"momentList": "x"}}), [])


class UniquePictureUrlsTests(unittest.TestCase):
    def test_dedupes_preserving_order(self):
        u1, u2 = _pic("2024-01-02", "a"), _pic("2024-01-03", "b")
        records = [
            feed_api.MomentRecord("m1", "", "", (u1, u2), None),
            feed_api.MomentRecord("m2", "", "", (u2,), None),
        ]
        self.assertEqual(feed_api.unique_picture_urls(records), [u1, u2])


class CollectMomentsTests(unittest.TestCase):
    def test_paginates_and_dedupes_by_moment_id(self):
        pages = {
            100: _payload(
                [_moment(momentId="m1", pictureURLs=[_pic("2024-01-02", "a")])],
                has_more=True,
                counter=90,
            ),
            90: _payload(
                [_moment(momentId="m1", pictureURLs=[_pic("2024-01-02", "a")]),  # dup id
                 _moment(momentId="m2", pictureURLs=[_pic("2024-01-01", "b")])],
                has_more=False,
            ),
        }
        calls = []

        def post(child_id, counter):
            calls.append(counter)
            return pages[counter]

        with redirect_stdout(io.StringIO()):
            records = feed_api.collect_moments(post, "child", initial_counter=100)
        self.assertEqual(calls, [100, 90])
        self.assertEqual([r.moment_id for r in records], ["m1", "m2"])

    def test_safety_break_when_cursor_stalls(self):
        stuck = _payload([], has_more=True, counter=100)
        calls = []

        def post(cid, counter):
            calls.append(counter)
            return stuck

        with redirect_stdout(io.StringIO()):
            feed_api.collect_moments(post, "child", initial_counter=100)
        self.assertEqual(calls, [100])

    def test_keyboard_interrupt_returns_partial(self):
        def post(cid, counter):
            if counter == 100:
                return _payload(
                    [_moment(momentId="m1", pictureURLs=[_pic("2024-01-02", "a")])],
                    has_more=True,
                    counter=90,
                )
            raise KeyboardInterrupt

        with redirect_stdout(io.StringIO()):
            records = feed_api.collect_moments(post, "child", initial_counter=100)
        self.assertEqual(len(records), 1)


class CollectApiUrlsTests(unittest.TestCase):
    def test_returns_deduped_photo_urls(self):
        u = _pic("2024-01-02", "a")
        pages = {
            100: _payload([_moment(momentId="m1", pictureURLs=[u])], has_more=True, counter=90),
            90: _payload([_moment(momentId="m2", pictureURLs=[u])], has_more=False),
        }
        with redirect_stdout(io.StringIO()):
            urls = feed_api.collect_api_urls(lambda c, n: pages[n], "child", initial_counter=100)
        self.assertEqual(urls, [u])


_UUID = "00000000-0000-4000-8000-000000000000"
_UUID2 = "00000000-0000-4000-8000-000000000001"


class ExtractChildIdsTests(unittest.TestCase):
    def test_returns_every_child_in_the_account(self):
        # A parent account can hold more than one child record (a sibling, or
        # the same child re-enrolled). Missing one truncates the feed.
        xml = f'<string name="childIds">["{_UUID}","{_UUID2}"]</string>'
        self.assertEqual(feed_api.extract_child_ids(xml), (_UUID, _UUID2))

    def test_unions_keys_without_dropping_ids(self):
        # album_child_id holds only the open album; childIds holds them all.
        xml = (
            f'<string name="album_child_id">{_UUID2}</string>'
            f'<string name="childIds">["{_UUID}","{_UUID2}"]</string>'
            f'<string name="paChildIds">["{_UUID}","{_UUID2}"]</string>'
        )
        self.assertEqual(set(feed_api.extract_child_ids(xml)), {_UUID, _UUID2})
        self.assertEqual(len(feed_api.extract_child_ids(xml)), 2)

    def test_no_children_returns_empty(self):
        self.assertEqual(feed_api.extract_child_ids("<map/>"), ())


class ReadCredentialsTests(unittest.TestCase):
    def test_reads_token_and_album_child_id(self):
        xml = (
            f'<string name="accessToken">tok</string>'
            f'<string name="album_child_id">{_UUID}</string>'
        )
        token, children = feed_api.read_app_credentials(
            eo.Device("127.0.0.1:1"), run_command=lambda argv: _completed(xml)
        )
        self.assertEqual((token, children), ("tok", (_UUID,)))

    def test_falls_back_to_child_ids_after_login(self):
        # album_child_id empty right after login; childIds holds the id.
        xml = (
            f'<string name="accessToken">tok</string>'
            f'<string name="album_child_id"></string>'
            f'<string name="childIds">["{_UUID}"]</string>'
        )
        token, children = feed_api.read_app_credentials(
            eo.Device("s"), run_command=lambda argv: _completed(xml)
        )
        self.assertEqual((token, children), ("tok", (_UUID,)))

    def test_reads_every_child_not_just_the_first(self):
        xml = (
            f'<string name="accessToken">tok</string>'
            f'<string name="childIds">["{_UUID}","{_UUID2}"]</string>'
        )
        _, children = feed_api.read_app_credentials(
            eo.Device("s"), run_command=lambda argv: _completed(xml)
        )
        self.assertEqual(children, (_UUID, _UUID2))

    def test_missing_child_id_raises(self):
        xml = '<string name="accessToken">tok</string>'
        with self.assertRaises(eo.SmokeError):
            feed_api.read_app_credentials(eo.Device("s"), run_command=lambda argv: _completed(xml))

    def test_command_failure_raises(self):
        with self.assertRaises(eo.SmokeError):
            feed_api.read_app_credentials(
                eo.Device("s"), run_command=lambda argv: _completed("", returncode=1)
            )


class _Resp:
    def __init__(self, status, body=b"{}", content_type="application/json"):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self, *args):
        body, self._body = self._body, b""
        return body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FetchMomentPageTests(unittest.TestCase):
    def test_posts_json_body_and_parses(self):
        captured = {}

        class Opener:
            def open(self, request, timeout=0):
                captured["url"] = request.full_url
                captured["data"] = request.data
                captured["headers"] = dict(request.headers)
                return _Resp(200, json.dumps({"data": {"hasMore": False}}).encode())

        payload = feed_api.fetch_moment_page(Opener(), "tok", ("cid1", "cid2"), 42)
        self.assertEqual(payload, {"data": {"hasMore": False}})
        self.assertIn("getPageMomentList", captured["url"])
        # Every child of the account goes in one request, so the server merges
        # their timelines instead of ending at the first child's oldest post.
        self.assertEqual(
            json.loads(captured["data"]),
            {
                "childIds": ["cid1", "cid2"],
                "counter": 42,
                "paChildIds": ["cid1", "cid2"],
            },
        )
        self.assertEqual(captured["headers"].get("Client"), "fa_app")
        self.assertEqual(captured["headers"].get("Authorization"), "Bearer tok")

    def test_non_200_raises(self):
        class Opener:
            def open(self, request, timeout=0):
                return _Resp(500)

        with self.assertRaises(eo.SmokeError):
            feed_api.fetch_moment_page(Opener(), "t", ("c",), 1)


class VideoDestinationTests(unittest.TestCase):
    def test_dated_filename(self):
        dest = feed_api.video_destination(_vid("2024-01-02", "v"), Path("/out"))
        self.assertTrue(dest.name.startswith("2024-01-02_"))
        self.assertTrue(dest.name.endswith(".mp4"))

    def test_unknown_date_prefix(self):
        dest = feed_api.video_destination(f"{CDN}/x/y.mp4", Path("/out"))
        self.assertTrue(dest.name.startswith("unknown-date_"))


class DownloadVideoTests(unittest.TestCase):
    def _opener(self, resp):
        class Opener:
            def open(self, request, timeout=0):
                return resp

        return Opener()

    def test_accepts_video_content_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "v.mp4"
            opener = self._opener(_Resp(200, b"x" * 4096, content_type="video/mp4"))
            size = feed_api.download_video(opener, _vid("2024-01-02", "v"), dest)
            self.assertEqual(size, 4096)
            self.assertTrue(dest.exists())

    def test_rejects_wrong_content_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "v.mp4"
            opener = self._opener(_Resp(200, b"<html>", content_type="text/html"))
            with self.assertRaises(eo.SmokeError):
                feed_api.download_video(opener, _vid("2024-01-02", "v"), dest)
            self.assertFalse(dest.exists())

    def test_non_200_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(eo.SmokeError):
                feed_api.download_video(
                    self._opener(_Resp(404, content_type="video/mp4")),
                    _vid("2024-01-02", "v"),
                    Path(tmp) / "v.mp4",
                )


class DownloadVideosTests(unittest.TestCase):
    def _records(self, *videos):
        return [feed_api.MomentRecord(f"m{i}", "", "", (), v) for i, v in enumerate(videos)]

    def test_downloads_unique_and_skips_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            v1, v2 = _vid("2024-01-02", "a"), _vid("2024-01-03", "b")
            # Pre-create v1 so it is skipped as existing.
            existing = feed_api.video_destination(v1, out)
            existing.write_bytes(b"x" * 4096)
            calls = []

            def fake_downloader(opener, url, dest):
                calls.append(url)
                dest.write_bytes(b"y" * 4096)

            with redirect_stdout(io.StringIO()):
                summary = feed_api.download_videos(
                    self._records(v1, v2, v2),  # v2 duplicated
                    out,
                    opener=object(),
                    video_downloader=fake_downloader,
                )
            self.assertEqual(summary.total, 2)  # v1, v2 (deduped)
            self.assertEqual(summary.existing, 1)
            self.assertEqual(summary.downloaded, 1)
            self.assertEqual(calls, [v2])

    def test_videos_download_in_parallel(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = threading.Lock()
            active = peak = 0

            def fake_downloader(opener, url, dest):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.05)
                with lock:
                    active -= 1
                dest.write_bytes(b"y" * 4096)

            urls = [_vid("2024-01-%02d" % (i + 1), f"v{i}") for i in range(8)]
            with redirect_stdout(io.StringIO()):
                summary = feed_api.download_videos(
                    self._records(*urls),
                    Path(tmp),
                    opener=object(),
                    video_downloader=fake_downloader,
                    workers=4,
                )
            self.assertEqual(summary.downloaded, 8)
            self.assertGreater(peak, 1)

    def test_interrupt_keeps_finished_videos(self):
        with tempfile.TemporaryDirectory() as tmp:
            def fake_downloader(opener, url, dest):
                if url.endswith("v5.mp4"):
                    raise KeyboardInterrupt
                dest.write_bytes(b"y" * 4096)

            urls = [_vid("2024-01-%02d" % (i + 1), f"v{i}") for i in range(6)]
            with redirect_stdout(io.StringIO()):
                summary = feed_api.download_videos(
                    self._records(*urls),
                    Path(tmp),
                    opener=object(),
                    video_downloader=fake_downloader,
                    workers=1,
                )
            self.assertEqual(summary.downloaded, 5)  # v0..v4 kept
            self.assertEqual(summary.total, 6)

    def test_no_videos_returns_empty(self):
        summary = feed_api.download_videos(self._records(None, None), Path("/x"))
        self.assertEqual(summary, feed_api.VideoSummary(0, 0, 0, 0))


class WriteManifestTests(unittest.TestCase):
    def test_manifest_uses_filenames_not_urls(self):
        records = [
            feed_api.MomentRecord(
                "m1", "2024-01-02 10:00", "去公园", (_pic("2024-01-02", "a"),), _vid("2024-01-02", "v")
            ),
            feed_api.MomentRecord("m2", "2024-01-03", "", (), None),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "moments.jsonl"
            n = feed_api.write_manifest(records, path)
            self.assertEqual(n, 2)
            lines = path.read_text(encoding="utf-8").splitlines()
        self.assertNotIn("http", "\n".join(lines))  # no URLs leaked
        first = json.loads(lines[0])
        self.assertEqual(first["caption"], "去公园")
        self.assertTrue(first["photos"][0].startswith("originals/2024-01-02_"))
        self.assertTrue(first["video"].startswith("videos/2024-01-02_"))
        self.assertIsNone(json.loads(lines[1])["video"])


class WriteCaptionsTests(unittest.TestCase):
    def test_writes_only_non_empty_with_time(self):
        records = [
            feed_api.MomentRecord("m1", "2024-01-02", "开心的一天", (), None),
            feed_api.MomentRecord("m2", "2024-01-03", "   ", (), None),  # blank
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "captions.txt"
            n = feed_api.write_captions(records, path)
            text = path.read_text(encoding="utf-8")
        self.assertEqual(n, 1)
        self.assertIn("[2024-01-02] 开心的一天", text)
        self.assertNotIn("m2", text)


class RunApiTests(unittest.TestCase):
    def _creds(self):
        return _completed(
            f'<string name="accessToken">tok</string>'
            f'<string name="album_child_id">{_UUID}</string>'
        )

    def _run(self, page, *, input_answer="DOWNLOAD", include_videos=True, assume_yes=False, downloader=None, video_patch=None):
        downloader = downloader or (lambda urls, out, **kw: eo.BatchSummary(len(urls), len(urls), 0, 0, 0, 0))
        with mock.patch.object(eo, "discover_running_device", return_value=eo.Device("s")), \
                mock.patch.object(feed_api, "fetch_moment_page", return_value=page), \
                mock.patch.object(eo, "ensure_build_is_ignored"), \
                mock.patch.object(feed_api, "write_manifest", return_value=1) as wm, \
                mock.patch.object(feed_api, "write_captions", return_value=1) as wc, \
                mock.patch.object(feed_api, "download_videos", side_effect=video_patch or (lambda *a, **k: feed_api.VideoSummary(0, 0, 0, 0))) as dv:
            with redirect_stdout(io.StringIO()):
                rc = feed_api.run_api(
                    run_command=lambda argv: self._creds(),
                    opener=object(),
                    input_fn=lambda prompt: input_answer,
                    downloader=downloader,
                    include_videos=include_videos,
                    assume_yes=assume_yes,
                    initial_counter=feed_api.COUNTER_SEED,
                )
        return rc, wm, wc, dv

    def test_happy_path_saves_text_and_downloads_photos(self):
        page = _payload([_moment(momentId="m1", momentCaption="hi", pictureURLs=[_pic("2024-01-02", "a")])])
        got = {}

        def downloader(urls, out, **kw):
            got["urls"] = urls
            return eo.BatchSummary(len(urls), len(urls), 0, 0, 0, 0)

        rc, wm, wc, dv = self._run(page, downloader=downloader)
        self.assertEqual(rc, 0)
        self.assertEqual(got["urls"], [_pic("2024-01-02", "a")])
        wm.assert_called_once()
        wc.assert_called_once()
        dv.assert_not_called()  # no videos in page

    def test_downloads_videos_when_present(self):
        page = _payload(
            [_moment(momentId="m1", pictureURLs=[_pic("2024-01-02", "a")], videoUrl=_vid("2024-01-02", "v"))]
        )
        rc, wm, wc, dv = self._run(page, video_patch=lambda *a, **k: feed_api.VideoSummary(1, 1, 0, 0))
        self.assertEqual(rc, 0)
        dv.assert_called_once()

    def test_no_videos_flag_skips_video_download(self):
        page = _payload(
            [_moment(momentId="m1", pictureURLs=[_pic("2024-01-02", "a")], videoUrl=_vid("2024-01-02", "v"))]
        )
        rc, wm, wc, dv = self._run(page, include_videos=False)
        self.assertEqual(rc, 0)
        dv.assert_not_called()

    def test_cancel_saves_text_but_skips_download(self):
        page = _payload([_moment(momentId="m1", momentCaption="hi", pictureURLs=[_pic("2024-01-02", "a")])])
        called = {"n": 0}

        def downloader(urls, out, **kw):
            called["n"] += 1
            return eo.BatchSummary(0, 0, 0, 0, 0, 0)

        rc, wm, wc, dv = self._run(page, input_answer="", downloader=downloader)
        self.assertEqual(rc, 0)
        self.assertEqual(called["n"], 0)
        wm.assert_called_once()  # text still saved
        wc.assert_called_once()

    def test_yes_flag_downloads_without_confirmation(self):
        page = _payload([_moment(momentId="m1", pictureURLs=[_pic("2024-01-02", "a")])])
        called = {"n": 0}

        def downloader(urls, out, **kw):
            called["n"] += 1
            return eo.BatchSummary(len(urls), len(urls), 0, 0, 0, 0)

        # input_answer would cancel, but --yes skips the prompt entirely.
        rc, wm, wc, dv = self._run(page, input_answer="", assume_yes=True, downloader=downloader)
        self.assertEqual(rc, 0)
        self.assertEqual(called["n"], 1)

    def test_requests_every_child_of_the_account(self):
        page = _payload([_moment(momentId="m1", pictureURLs=[_pic("2024-01-02", "a")])])
        with mock.patch.object(eo, "discover_running_device", return_value=eo.Device("s")), \
                mock.patch.object(feed_api, "fetch_moment_page", return_value=page) as fetch, \
                mock.patch.object(eo, "ensure_build_is_ignored"), \
                mock.patch.object(feed_api, "write_manifest", return_value=1), \
                mock.patch.object(feed_api, "write_captions", return_value=1), \
                mock.patch.object(feed_api, "download_videos",
                                  return_value=feed_api.VideoSummary(0, 0, 0, 0)):
            xml = (
                f'<string name="accessToken">tok</string>'
                f'<string name="childIds">["{_UUID}","{_UUID2}"]</string>'
            )
            with redirect_stdout(io.StringIO()):
                feed_api.run_api(
                    run_command=lambda argv: _completed(xml),
                    opener=object(),
                    assume_yes=True,
                    downloader=lambda urls, out, **kw: eo.BatchSummary(1, 1, 0, 0, 0, 0),
                    initial_counter=feed_api.COUNTER_SEED,
                )
        self.assertEqual(fetch.call_args.args[2], (_UUID, _UUID2))

    def test_no_records_returns_zero(self):
        rc, wm, wc, dv = self._run(_payload([]))
        self.assertEqual(rc, 0)
        wm.assert_not_called()

    def test_credentials_failure_returns_one(self):
        with mock.patch.object(eo, "discover_running_device", return_value=eo.Device("s")):
            with redirect_stdout(io.StringIO()):
                rc = feed_api.run_api(
                    run_command=lambda argv: _completed("", returncode=1),
                    opener=object(),
                )
        self.assertEqual(rc, 1)


class FindStartCounterTests(unittest.TestCase):
    def test_finds_largest_counter_with_data(self):
        boundary = 2_392_800
        with redirect_stdout(io.StringIO()):
            got = feed_api.find_start_counter(
                lambda c: c <= boundary, seed=2_360_360, ceiling=2_000_000_000
            )
        self.assertEqual(got, boundary)

    def test_returns_none_when_seed_empty(self):
        with redirect_stdout(io.StringIO()):
            got = feed_api.find_start_counter(lambda c: False, seed=100, ceiling=200)
        self.assertIsNone(got)

    def test_seed_itself_is_the_boundary(self):
        with redirect_stdout(io.StringIO()):
            got = feed_api.find_start_counter(
                lambda c: c <= 2_360_360, seed=2_360_360, ceiling=9_000_000
            )
        self.assertEqual(got, 2_360_360)

    def test_run_api_auto_discovers_counter(self):
        # No initial_counter: run_api should binary-search then paginate.
        boundary = 2_400_000
        page = _payload([_moment(momentId="m1", pictureURLs=[_pic("2024-01-02", "a")])])
        empty = _payload([])

        def fake_fetch(opener, token, child_id, counter, timeout=30):
            return page if counter <= boundary else empty

        got = {}

        def fake_downloader(urls, out, **kw):
            got["urls"] = urls
            return eo.BatchSummary(len(urls), len(urls), 0, 0, 0, 0)

        with mock.patch.object(eo, "discover_running_device", return_value=eo.Device("s")), \
                mock.patch.object(feed_api, "fetch_moment_page", side_effect=fake_fetch), \
                mock.patch.object(eo, "ensure_build_is_ignored"), \
                mock.patch.object(feed_api, "write_manifest", return_value=1), \
                mock.patch.object(feed_api, "write_captions", return_value=1), \
                mock.patch.object(feed_api, "download_videos", return_value=feed_api.VideoSummary(0, 0, 0, 0)):
            with redirect_stdout(io.StringIO()):
                rc = feed_api.run_api(
                    run_command=lambda argv: subprocess.CompletedProcess(
                        [], 0,
                        f'<string name="accessToken">t</string>'
                        f'<string name="album_child_id">{_UUID}</string>', "",
                    ),
                    opener=object(),
                    assume_yes=True,
                    downloader=fake_downloader,
                )
        self.assertEqual(rc, 0)
        self.assertEqual(got["urls"], [_pic("2024-01-02", "a")])


class CliDispatchTests(unittest.TestCase):
    def test_api_dispatches_with_counter_and_videos(self):
        with mock.patch("tools.feed_api.run_api", return_value=0) as run_api:
            rc = eo.main(["api", "--counter", "12345"])
        self.assertEqual(rc, 0)
        run_api.assert_called_once_with(
            initial_counter=12345,
            include_videos=True,
            assume_yes=False,
            workers=eo.DEFAULT_WORKERS,
        )

    def test_api_no_videos_flag(self):
        with mock.patch("tools.feed_api.run_api", return_value=0) as run_api:
            eo.main(["api", "--no-videos"])
        run_api.assert_called_once_with(
            initial_counter=None,
            include_videos=False,
            assume_yes=False,
            workers=eo.DEFAULT_WORKERS,
        )

    def test_api_yes_flag(self):
        with mock.patch("tools.feed_api.run_api", return_value=0) as run_api:
            eo.main(["api", "--yes"])
        run_api.assert_called_once_with(
            initial_counter=None,
            include_videos=True,
            assume_yes=True,
            workers=eo.DEFAULT_WORKERS,
        )

    def test_api_workers_flag(self):
        with mock.patch("tools.feed_api.run_api", return_value=0) as run_api:
            eo.main(["api", "--workers", "8"])
        self.assertEqual(run_api.call_args.kwargs["workers"], 8)


if __name__ == "__main__":
    unittest.main()
