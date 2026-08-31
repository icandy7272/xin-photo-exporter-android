import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import credentials as creds
from tools import export_originals as eo


_UUID = "00000000-0000-4000-8000-000000000000"
_UUID2 = "00000000-0000-4000-8000-000000000001"


def _completed(stdout: str, returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class ExtractPrefStringTests(unittest.TestCase):
    def test_found(self):
        xml = '<map><string name="accessToken">tok123</string></map>'
        self.assertEqual(creds.extract_pref_string(xml, "accessToken"), "tok123")

    def test_missing_returns_none(self):
        self.assertIsNone(creds.extract_pref_string("<map/>", "accessToken"))

    def test_empty_value_returns_none(self):
        xml = '<string name="album_child_id"></string>'
        self.assertIsNone(creds.extract_pref_string(xml, "album_child_id"))


class ExtractChildIdsTests(unittest.TestCase):
    def test_returns_every_child_in_the_account(self):
        # A parent account can hold more than one child record (a sibling, or
        # the same child re-enrolled). Missing one truncates the feed.
        xml = f'<string name="childIds">["{_UUID}","{_UUID2}"]</string>'
        self.assertEqual(creds.extract_child_ids(xml), (_UUID, _UUID2))

    def test_unions_keys_without_dropping_ids(self):
        # album_child_id holds only the open album; childIds holds them all.
        xml = (
            f'<string name="album_child_id">{_UUID2}</string>'
            f'<string name="childIds">["{_UUID}","{_UUID2}"]</string>'
            f'<string name="paChildIds">["{_UUID}","{_UUID2}"]</string>'
        )
        self.assertEqual(set(creds.extract_child_ids(xml)), {_UUID, _UUID2})
        self.assertEqual(len(creds.extract_child_ids(xml)), 2)

    def test_no_children_returns_empty(self):
        self.assertEqual(creds.extract_child_ids("<map/>"), ())


class LoadTokenTests(unittest.TestCase):
    def test_reads_token_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.txt"
            path.write_text("  tok-abc\n", encoding="utf-8")
            self.assertEqual(creds.load_token(token_file=path, env={}), "tok-abc")

    def test_strips_bearer_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.txt"
            path.write_text("Bearer tok-abc", encoding="utf-8")
            self.assertEqual(creds.load_token(token_file=path, env={}), "tok-abc")

    def test_falls_back_to_environment(self):
        self.assertEqual(
            creds.load_token(token_file=None, env={"XIN_ACCESS_TOKEN": "tok-env"}),
            "tok-env",
        )

    def test_file_wins_over_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.txt"
            path.write_text("tok-file", encoding="utf-8")
            self.assertEqual(
                creds.load_token(token_file=path, env={"XIN_ACCESS_TOKEN": "tok-env"}),
                "tok-file",
            )

    def test_no_source_returns_none(self):
        self.assertIsNone(creds.load_token(token_file=None, env={}))

    def test_empty_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.txt"
            path.write_text("\n", encoding="utf-8")
            with self.assertRaises(eo.SmokeError):
                creds.load_token(token_file=path, env={})

    def test_unreadable_file_raises(self):
        with self.assertRaises(eo.SmokeError) as caught:
            creds.load_token(token_file=Path("/nope/token.txt"), env={})
        self.assertEqual(str(caught.exception), "token-file-unreadable")


class NormaliseChildIdsTests(unittest.TestCase):
    def test_accepts_uuids_and_dedupes(self):
        other = "99999999-8888-7777-6666-555555555555"
        self.assertEqual(
            creds.normalise_child_ids([_UUID, other, _UUID]), (_UUID, other)
        )

    def test_rejects_non_uuid(self):
        with self.assertRaises(eo.SmokeError) as caught:
            creds.normalise_child_ids(["not-a-uuid"])
        self.assertEqual(str(caught.exception), "invalid-child-id")

    def test_empty_stays_empty(self):
        self.assertEqual(creds.normalise_child_ids(None), ())


class ResolveCredentialsTests(unittest.TestCase):
    def _prefs(self):
        return f'<string name="accessToken">tok</string><string name="album_child_id">{_UUID}</string>'

    def test_token_and_child_ids_given_skips_the_device(self):
        def read_prefs():
            raise AssertionError("device must not be touched")

        token, children = creds.resolve_credentials(
            token="tok-manual", child_ids=(_UUID,), read_prefs=read_prefs
        )
        self.assertEqual((token, children), ("tok-manual", (_UUID,)))

    def test_child_id_filter_still_reads_the_token_from_the_device(self):
        token, children = creds.resolve_credentials(
            token=None, child_ids=(_UUID,), read_prefs=self._prefs
        )
        self.assertEqual((token, children), ("tok", (_UUID,)))

    def test_manual_child_ids_override_the_prefs(self):
        other = "99999999-8888-7777-6666-555555555555"
        _, children = creds.resolve_credentials(
            token="tok", child_ids=(other,), read_prefs=self._prefs
        )
        self.assertEqual(children, (other,))

    def test_nothing_given_reads_both_from_the_device(self):
        token, children = creds.resolve_credentials(read_prefs=self._prefs)
        self.assertEqual((token, children), ("tok", (_UUID,)))

    def test_no_device_and_incomplete_input_raises(self):
        with self.assertRaises(eo.SmokeError) as caught:
            creds.resolve_credentials(token="tok", child_ids=(), read_prefs=None)
        self.assertEqual(str(caught.exception), "credentials-not-found")



class ReadCredentialsTests(unittest.TestCase):
    def test_reads_token_and_album_child_id(self):
        xml = (
            f'<string name="accessToken">tok</string>'
            f'<string name="album_child_id">{_UUID}</string>'
        )
        token, children = creds.read_app_credentials(
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
        token, children = creds.read_app_credentials(
            eo.Device("s"), run_command=lambda argv: _completed(xml)
        )
        self.assertEqual((token, children), ("tok", (_UUID,)))

    def test_reads_every_child_not_just_the_first(self):
        xml = (
            f'<string name="accessToken">tok</string>'
            f'<string name="childIds">["{_UUID}","{_UUID2}"]</string>'
        )
        _, children = creds.read_app_credentials(
            eo.Device("s"), run_command=lambda argv: _completed(xml)
        )
        self.assertEqual(children, (_UUID, _UUID2))

    def test_missing_child_id_raises(self):
        xml = '<string name="accessToken">tok</string>'
        with self.assertRaises(eo.SmokeError):
            creds.read_app_credentials(eo.Device("s"), run_command=lambda argv: _completed(xml))

    def test_command_failure_raises(self):
        with self.assertRaises(eo.SmokeError):
            creds.read_app_credentials(
                eo.Device("s"), run_command=lambda argv: _completed("", returncode=1)
            )

if __name__ == "__main__":
    unittest.main()
