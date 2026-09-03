import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tools import export_originals as eo
from tools import feed_api
from tools import wizard


_UUID = "00000000-0000-4000-8000-000000000000"
_UUID2 = "00000000-0000-4000-8000-000000000001"
PREFS = (
    "<map>"
    '<string name="accessToken">tok</string>'
    f'<string name="childIds">["{_UUID}","{_UUID2}"]</string>'
    "</map>"
)


def _completed(stdout: str, returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _record(published_time="2026-04-30", caption="", photos=0):
    return feed_api.MomentRecord(
        moment_id="m",
        published_time=published_time,
        caption=caption,
        picture_urls=tuple(f"u{i}" for i in range(photos)),
        video_url=None,
    )


def _answers(*values):
    """An input_fn that replays canned answers, then refuses to block."""
    queue = list(values)

    def ask(prompt=""):
        if not queue:
            raise AssertionError(f"wizard asked more than expected: {prompt!r}")
        return queue.pop(0)

    return ask


class ExcerptTests(unittest.TestCase):
    def test_collapses_whitespace_and_newlines(self):
        self.assertEqual(wizard.excerpt("今天  做\n手工", 20), "今天 做 手工")

    def test_truncates_long_text_with_ellipsis(self):
        self.assertEqual(wizard.excerpt("一二三四五六", 4), "一二三四…")

    def test_short_text_is_untouched(self):
        self.assertEqual(wizard.excerpt("短", 10), "短")

    def test_empty_stays_empty(self):
        self.assertEqual(wizard.excerpt("", 10), "")
        self.assertEqual(wizard.excerpt("   \n ", 10), "")


class SummariseChildTests(unittest.TestCase):
    def test_latest_time_is_the_newest_post(self):
        records = [_record("2026-01-02"), _record("2026-06-12"), _record("2026-03-01")]
        self.assertEqual(wizard.summarise_child(_UUID, records).latest_time, "2026-06-12")

    def test_caption_comes_from_the_newest_post_that_has_one(self):
        # The newest post is often a photo with no text; an empty excerpt
        # would defeat the whole point of showing it.
        records = [
            _record("2026-06-12", caption=""),
            _record("2026-06-01", caption="毕业典礼彩排"),
        ]
        self.assertEqual(
            wizard.summarise_child(_UUID, records).caption_excerpt, "毕业典礼彩排"
        )

    def test_counts_photos_on_the_sampled_page(self):
        records = [_record(photos=3), _record(photos=2)]
        self.assertEqual(wizard.summarise_child(_UUID, records).sampled_photos, 5)

    def test_no_records_yields_empty_summary(self):
        summary = wizard.summarise_child(_UUID, [])
        self.assertEqual(summary.latest_time, "")
        self.assertEqual(summary.caption_excerpt, "")
        self.assertEqual(summary.sampled_photos, 0)

    def test_child_id_is_carried_through(self):
        self.assertEqual(wizard.summarise_child(_UUID, []).child_id, _UUID)


class FormatChildLineTests(unittest.TestCase):
    def test_shows_index_date_and_excerpt(self):
        summary = wizard.ChildSummary(_UUID, "2026-06-12", "毕业典礼彩排", 12)
        line = wizard.format_child_line(1, summary)
        self.assertIn("1)", line)
        self.assertIn("2026-06-12", line)
        self.assertIn("毕业典礼彩排", line)

    def test_never_prints_the_raw_child_id(self):
        """The id is personal data, and the point is that nobody has to read it."""
        summary = wizard.ChildSummary(_UUID, "2026-06-12", "毕业典礼彩排", 12)
        self.assertNotIn(_UUID, wizard.format_child_line(1, summary))

    def test_index_is_not_glued_to_the_first_field(self):
        """`1) · 最近更新` reads like a missing value; it should be `1) 最近更新`."""
        summary = wizard.ChildSummary(_UUID, "2026-06-12", "毕业彩排", 12)
        self.assertNotIn(") ·", wizard.format_child_line(1, summary))
        self.assertTrue(wizard.format_child_line(1, summary).lstrip().startswith("1) 最近更新"))

    def test_empty_summary_still_renders_something_useful(self):
        line = wizard.format_child_line(2, wizard.ChildSummary(_UUID, "", "", 0))
        self.assertIn("2)", line)
        self.assertTrue(line.strip())


class FormatDateTests(unittest.TestCase):
    """The API returns full ISO timestamps; parents want a date."""

    def test_iso_timestamp_is_cut_to_the_date(self):
        self.assertEqual(wizard.format_date("2026-04-30T09:00:13.491Z"), "2026-04-30")

    def test_a_plain_date_is_unchanged(self):
        self.assertEqual(wizard.format_date("2026-04-30"), "2026-04-30")

    def test_unexpected_shapes_pass_through(self):
        self.assertEqual(wizard.format_date("稍后"), "稍后")
        self.assertEqual(wizard.format_date(""), "")


class MenuShowsAFriendlyDateTests(unittest.TestCase):
    def test_menu_line_has_no_raw_timestamp(self):
        summary = wizard.ChildSummary(_UUID, "2026-04-30T09:00:13.491Z", "做手工", 3)
        line = wizard.format_child_line(1, summary)
        self.assertIn("2026-04-30", line)
        self.assertNotIn("T09:00", line)
        self.assertNotIn("491Z", line)


class ParseChildSelectionTests(unittest.TestCase):
    def test_single_number_is_zero_indexed(self):
        self.assertEqual(wizard.parse_child_selection("1", 2), (0,))
        self.assertEqual(wizard.parse_child_selection("2", 2), (1,))

    def test_surrounding_space_is_ignored(self):
        self.assertEqual(wizard.parse_child_selection("  2  ", 2), (1,))

    def test_a_means_everyone(self):
        for answer in ("a", "A", "全部"):
            self.assertEqual(wizard.parse_child_selection(answer, 3), (0, 1, 2))

    def test_several_numbers_in_any_common_separator(self):
        for answer in ("1,2", "1 2", "1、2", "1，2"):
            self.assertEqual(wizard.parse_child_selection(answer, 2), (0, 1))

    def test_duplicates_collapse_and_order_is_kept(self):
        self.assertEqual(wizard.parse_child_selection("2,1,2", 2), (1, 0))

    def test_out_of_range_is_rejected(self):
        self.assertIsNone(wizard.parse_child_selection("3", 2))
        self.assertIsNone(wizard.parse_child_selection("0", 2))
        self.assertIsNone(wizard.parse_child_selection("-1", 2))

    def test_nonsense_is_rejected(self):
        for answer in ("", "  ", "abc", "1.5", "1,9"):
            self.assertIsNone(wizard.parse_child_selection(answer, 2))


class SanitizeFolderNameTests(unittest.TestCase):
    def test_keeps_a_normal_name(self):
        self.assertEqual(wizard.sanitize_folder_name("小明"), "小明")

    def test_trims_surrounding_space(self):
        self.assertEqual(wizard.sanitize_folder_name("  小明  "), "小明")

    def test_path_separators_cannot_escape_the_chosen_folder(self):
        self.assertEqual(wizard.sanitize_folder_name("a/b"), "a_b")
        self.assertEqual(wizard.sanitize_folder_name("a\\b"), "a_b")

    def test_empty_and_dot_names_are_rejected(self):
        for name in ("", "   ", ".", "..", "/", "  ..  "):
            self.assertIsNone(wizard.sanitize_folder_name(name))


class DisplayPathTests(unittest.TestCase):
    """Long absolute paths are noise for a parent reading a prompt."""

    def test_home_becomes_tilde(self):
        home = Path("/Users/someone")
        self.assertEqual(
            wizard.display_path(home / "Desktop" / "导出", home=home), "~/Desktop/导出"
        )

    def test_paths_outside_home_are_untouched(self):
        self.assertEqual(
            wizard.display_path(Path("/tmp/x"), home=Path("/Users/someone")), "/tmp/x"
        )


class EnsureCredentialsTests(unittest.TestCase):
    """Not being logged in must not end the run - it must wait."""

    def test_succeeds_without_asking_anything(self):
        asked = _answers()  # blows up if the wizard prompts
        token, children = wizard.ensure_credentials(
            read_prefs=lambda: PREFS, input_fn=asked, printer=lambda m: None
        )
        self.assertEqual(token, "tok")
        self.assertEqual(children, (_UUID, _UUID2))

    def test_waits_for_the_user_to_log_in_then_retries(self):
        attempts = {"n": 0}

        def read_prefs():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise eo.SmokeError("credentials-not-found")
            return PREFS

        lines: list[str] = []
        token, _ = wizard.ensure_credentials(
            read_prefs=read_prefs, input_fn=_answers(""), printer=lines.append
        )
        self.assertEqual(token, "tok")
        self.assertEqual(attempts["n"], 2)
        self.assertIn("登录", "\n".join(lines))

    def test_quitting_returns_nothing(self):
        def read_prefs():
            raise eo.SmokeError("credentials-not-found")

        result = wizard.ensure_credentials(
            read_prefs=read_prefs, input_fn=_answers("q"), printer=lambda m: None
        )
        self.assertIsNone(result)

    def test_root_problems_are_explained_differently_from_not_logged_in(self):
        """prefs-read-failed is an emulator problem; retrying alone won't fix it."""

        def read_prefs():
            raise eo.SmokeError("prefs-read-failed")

        lines: list[str] = []
        wizard.ensure_credentials(
            read_prefs=read_prefs, input_fn=_answers("q"), printer=lines.append
        )
        text = "\n".join(lines)
        self.assertIn("Root", text)

    def test_gives_up_after_a_bounded_number_of_retries(self):
        def read_prefs():
            raise eo.SmokeError("credentials-not-found")

        result = wizard.ensure_credentials(
            read_prefs=read_prefs,
            input_fn=_answers(*[""] * 50),
            printer=lambda m: None,
            max_attempts=3,
        )
        self.assertIsNone(result)


class AskFolderNameTests(unittest.TestCase):
    def test_returns_the_full_destination_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = wizard.ask_folder_name(
                root=root, input_fn=_answers("小明"), printer=lambda m: None
            )
            self.assertEqual(path, root / "小明")

    def test_reasks_on_an_invalid_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = wizard.ask_folder_name(
                root=root, input_fn=_answers("", "小明"), printer=lambda m: None
            )
            self.assertEqual(path, root / "小明")


class CleanDroppedPathTests(unittest.TestCase):
    """Dragging a file into Terminal produces escaped or quoted paths."""

    def test_plain_path(self):
        self.assertEqual(wizard.clean_dropped_path("/tmp/a.apk"), Path("/tmp/a.apk"))

    def test_surrounding_whitespace_and_quotes(self):
        for raw in ("  /tmp/a.apk  ", "'/tmp/a.apk'", '"/tmp/a.apk"'):
            self.assertEqual(wizard.clean_dropped_path(raw), Path("/tmp/a.apk"))

    def test_backslash_escaped_spaces(self):
        self.assertEqual(
            wizard.clean_dropped_path("/tmp/my\\ app.apk"), Path("/tmp/my app.apk")
        )

    def test_tilde_is_expanded(self):
        self.assertEqual(
            wizard.clean_dropped_path("~/a.apk"), Path.home() / "a.apk"
        )

    def test_empty_is_none(self):
        self.assertIsNone(wizard.clean_dropped_path("   "))


class EnsureAppInstalledTests(unittest.TestCase):
    def test_installed_app_asks_nothing(self):
        ok = wizard.ensure_app_installed(
            is_installed=lambda: True, install_apk=lambda p: True,
            input_fn=_answers(), printer=lambda m: None,
        )
        self.assertTrue(ok)

    def test_missing_app_tells_the_user_to_install_the_apk_not_to_log_in(self):
        """The old flow said "open the app" when no app existed."""
        lines: list[str] = []
        wizard.ensure_app_installed(
            is_installed=lambda: False, install_apk=lambda p: True,
            input_fn=_answers("q"), printer=lines.append,
        )
        text = "\n".join(lines)
        self.assertIn("apk", text.lower())
        self.assertNotIn("用你自己的账号登录", text)

    def test_a_dropped_apk_path_gets_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            apk = Path(tmp) / "x.apk"
            apk.write_bytes(b"PK")
            installed: list[Path] = []
            states = iter([False, True])
            ok = wizard.ensure_app_installed(
                is_installed=lambda: next(states),
                install_apk=lambda p: installed.append(p) or True,
                input_fn=_answers(str(apk)), printer=lambda m: None,
            )
        self.assertTrue(ok)
        self.assertEqual(installed, [apk])

    def test_a_path_that_does_not_exist_is_called_out(self):
        lines: list[str] = []
        wizard.ensure_app_installed(
            is_installed=lambda: False, install_apk=lambda p: True,
            input_fn=_answers("/tmp/definitely-not-here.apk", "q"), printer=lines.append,
        )
        self.assertIn("找不到这个文件", "\n".join(lines))

    def test_manual_install_then_enter_rechecks(self):
        states = iter([False, True])
        ok = wizard.ensure_app_installed(
            is_installed=lambda: next(states), install_apk=lambda p: True,
            input_fn=_answers(""), printer=lambda m: None,
        )
        self.assertTrue(ok)

    def test_quitting_returns_false(self):
        ok = wizard.ensure_app_installed(
            is_installed=lambda: False, install_apk=lambda p: True,
            input_fn=_answers("q"), printer=lambda m: None,
        )
        self.assertFalse(ok)

    def test_a_failed_install_is_reported_and_retried(self):
        lines: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            apk = Path(tmp) / "bad.apk"
            apk.write_bytes(b"not really an apk")
            ok = wizard.ensure_app_installed(
                is_installed=lambda: False, install_apk=lambda p: False,
                input_fn=_answers(str(apk), "q"), printer=lines.append,
            )
        self.assertFalse(ok)
        self.assertIn("装不上", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()


class DescribeChildrenTests(unittest.TestCase):
    def test_samples_one_page_per_child(self):
        seen = []

        def fetch(child_id):
            seen.append(child_id)
            return [_record("2026-06-12", "毕业彩排", photos=4)]

        summaries = wizard.describe_children((_UUID, _UUID2), fetch_page=fetch)
        self.assertEqual(seen, [_UUID, _UUID2])
        self.assertEqual(len(summaries), 2)
        self.assertEqual(summaries[0].latest_time, "2026-06-12")

    def test_a_failing_sample_still_yields_a_row(self):
        """A network hiccup on the preview must not sink the whole run."""

        def fetch(child_id):
            raise eo.SmokeError("api-request-failed")

        summaries = wizard.describe_children((_UUID,), fetch_page=fetch)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].child_id, _UUID)
        self.assertEqual(summaries[0].latest_time, "")


class ChooseChildrenTests(unittest.TestCase):
    def _summaries(self):
        return [
            wizard.ChildSummary(_UUID, "2026-04-30", "做手工", 3),
            wizard.ChildSummary(_UUID2, "2026-06-12", "毕业彩排", 5),
        ]

    def test_single_archive_skips_the_menu(self):
        asked = _answers()  # must not prompt when there is nothing to choose
        chosen = wizard.choose_children(
            [self._summaries()[0]], input_fn=asked, printer=lambda m: None
        )
        self.assertEqual(chosen, (_UUID,))

    def test_picks_by_number(self):
        chosen = wizard.choose_children(
            self._summaries(), input_fn=_answers("2"), printer=lambda m: None
        )
        self.assertEqual(chosen, (_UUID2,))

    def test_reasks_until_the_answer_makes_sense(self):
        lines: list[str] = []
        chosen = wizard.choose_children(
            self._summaries(), input_fn=_answers("九", "5", "1"), printer=lines.append
        )
        self.assertEqual(chosen, (_UUID,))

    def test_all_selects_every_archive(self):
        chosen = wizard.choose_children(
            self._summaries(), input_fn=_answers("a"), printer=lambda m: None
        )
        self.assertEqual(chosen, (_UUID, _UUID2))

    def test_menu_never_shows_raw_ids(self):
        lines: list[str] = []
        wizard.choose_children(
            self._summaries(), input_fn=_answers("1"), printer=lines.append
        )
        text = "\n".join(lines)
        self.assertNotIn(_UUID, text)
        self.assertNotIn(_UUID2, text)


class EnsureDeviceTests(unittest.TestCase):
    def test_an_attached_device_needs_no_setup(self):
        with mock.patch.object(wizard.android, "find_adb", return_value=Path("adb")), \
                mock.patch.object(wizard.android, "discover_device", return_value=eo.Device("s")), \
                mock.patch.object(wizard, "install_emulator") as installer:
            ok = wizard.ensure_device(input_fn=_answers(), printer=lambda m: None)
        self.assertTrue(ok)
        installer.assert_not_called()

    def test_offers_to_install_when_nothing_is_attached(self):
        with mock.patch.object(wizard.android, "find_adb", side_effect=eo.SmokeError("no-device")), \
                mock.patch.object(wizard, "install_emulator", return_value=0) as installer:
            ok = wizard.ensure_device(input_fn=_answers(""), printer=lambda m: None)
        self.assertTrue(ok)
        installer.assert_called_once()

    def test_declining_the_install_stops_the_wizard(self):
        with mock.patch.object(wizard.android, "find_adb", side_effect=eo.SmokeError("no-device")), \
                mock.patch.object(wizard, "install_emulator") as installer:
            ok = wizard.ensure_device(input_fn=_answers("n"), printer=lambda m: None)
        self.assertFalse(ok)
        installer.assert_not_called()

    def test_a_failed_install_is_reported_not_swallowed(self):
        lines: list[str] = []
        with mock.patch.object(wizard.android, "find_adb", side_effect=eo.SmokeError("no-device")), \
                mock.patch.object(wizard, "install_emulator", return_value=1):
            ok = wizard.ensure_device(input_fn=_answers(""), printer=lines.append)
        self.assertFalse(ok)


class RunWizardTests(unittest.TestCase):
    """End to end with every edge injected: no device, no network, no disk churn."""

    def _run(self, answers, exports=None, tmp=None):
        calls: list[dict] = []

        def fake_export(**kwargs):
            calls.append(kwargs)
            return 0 if exports is None else exports.pop(0)

        lines: list[str] = []
        with mock.patch.object(wizard, "ensure_device", return_value=True), \
                mock.patch.object(wizard, "ensure_app_installed", return_value=True), \
                mock.patch.object(wizard, "ensure_credentials", return_value=("tok", (_UUID, _UUID2))), \
                mock.patch.object(wizard, "describe_children", return_value=[
                    wizard.ChildSummary(_UUID, "2026-04-30", "做手工", 3),
                    wizard.ChildSummary(_UUID2, "2026-06-12", "毕业彩排", 5),
                ]), \
                mock.patch.object(wizard, "export_one", side_effect=fake_export), \
                mock.patch.object(wizard, "open_folder") as opener:
            rc = wizard.run_wizard(
                input_fn=_answers(*answers),
                printer=lines.append,
                export_root=Path(tmp) if tmp else Path("/tmp/wizard-test"),
            )
        return rc, calls, lines, opener

    def test_happy_path_exports_the_chosen_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, calls, lines, opener = self._run(["2", "小明"], tmp=tmp)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["child_ids"], (_UUID2,))
        self.assertEqual(calls[0]["build_root"].name, "小明")
        self.assertEqual(calls[0]["token"], "tok")
        opener.assert_called_once()

    def test_token_is_never_printed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, lines, _ = self._run(["1", "小明"], tmp=tmp)
        self.assertNotIn("tok", "\n".join(lines))

    def test_all_archives_get_their_own_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, calls, _, _ = self._run(["a", "小明", "小红"], tmp=tmp)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0]["build_root"], calls[1]["build_root"])
        self.assertEqual({c["child_ids"] for c in calls}, {(_UUID,), (_UUID2,)})

    def test_multi_export_says_which_archive_each_name_is_for(self):
        """Asked twice for a name with no context, nobody can tell them apart."""
        with tempfile.TemporaryDirectory() as tmp:
            _, calls, lines, _ = self._run(["a", "小明", "小红"], tmp=tmp)
        text = "\n".join(lines)
        # Each archive's identifying detail must appear next to its prompt.
        self.assertIn("2026-04-30", text)
        self.assertIn("2026-06-12", text)
        self.assertIn("第 1/2", text)
        self.assertIn("第 2/2", text)

    def test_single_export_does_not_add_counter_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, lines, _ = self._run(["1", "小明"], tmp=tmp)
        self.assertNotIn("第 1/1", "\n".join(lines))

    def test_a_failed_export_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, _, opener = self._run(["1", "小明"], exports=[1], tmp=tmp)
        self.assertEqual(rc, 1)

    def test_stops_when_the_app_is_not_installed(self):
        """No app means the login instructions would be a dead end."""
        with mock.patch.object(wizard, "ensure_device", return_value=True), \
                mock.patch.object(wizard, "ensure_app_installed", return_value=False), \
                mock.patch.object(wizard, "ensure_credentials") as creds_step, \
                mock.patch.object(wizard, "export_one") as export:
            rc = wizard.run_wizard(input_fn=_answers(), printer=lambda m: None)
        self.assertEqual(rc, 1)
        creds_step.assert_not_called()
        export.assert_not_called()

    def test_stops_cleanly_when_the_user_quits_at_login(self):
        lines: list[str] = []
        with mock.patch.object(wizard, "ensure_device", return_value=True), \
                mock.patch.object(wizard, "ensure_app_installed", return_value=True), \
                mock.patch.object(wizard, "ensure_credentials", return_value=None), \
                mock.patch.object(wizard, "export_one") as export:
            rc = wizard.run_wizard(input_fn=_answers(), printer=lines.append)
        self.assertEqual(rc, 1)
        export.assert_not_called()

    def test_stops_when_there_is_no_device(self):
        with mock.patch.object(wizard, "ensure_device", return_value=False), \
                mock.patch.object(wizard, "export_one") as export:
            rc = wizard.run_wizard(input_fn=_answers(), printer=lambda m: None)
        self.assertEqual(rc, 1)
        export.assert_not_called()
