import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

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


if __name__ == "__main__":
    unittest.main()
