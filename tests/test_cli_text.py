"""Unit tests for explicit Windows-console text sanitization.

Model output routinely contains Unicode punctuation (notably the
non-breaking hyphen U+2011). A bare cp1252 errors="replace" encode
turns each such character into "?", corrupting readable prose
("auto-scaling" rendered "auto?scaling"). The shared sanitizer maps
common punctuation to ASCII equivalents first; only genuinely
unmappable characters (emoji, CJK) take the documented "?" last
resort instead of crashing the CLI.

Run: python -m unittest discover tests (with the project .venv)
"""
import unittest

from ai_architect.cli.text import sanitize_console_text


class ConsoleTextTests(unittest.TestCase):
    def test_non_breaking_hyphen_maps_to_hyphen(self):
        self.assertEqual(
            sanitize_console_text("auto‑scaling"), "auto-scaling"
        )
        self.assertEqual(
            sanitize_console_text("sub‑200‑ms"), "sub-200-ms"
        )
        self.assertEqual(
            sanitize_console_text("20 writes/s"), "20 writes/s"
        )

    def test_dashes_and_quotes_mapped(self):
        self.assertEqual(sanitize_console_text("a—b"), "a--b")
        self.assertEqual(sanitize_console_text("a–b"), "a-b")
        self.assertEqual(
            sanitize_console_text("“quoted”"), '"quoted"'
        )
        self.assertEqual(sanitize_console_text("it’s"), "it's")

    def test_arrows_and_marks_mapped(self):
        self.assertEqual(sanitize_console_text("a→b"), "a->b")
        self.assertEqual(sanitize_console_text("ok ✓"), "ok ok")

    def test_plain_ascii_unchanged(self):
        text = "Amazon S3 -> bucket (50 GB/mo) 99.9% 'ok'"
        self.assertEqual(sanitize_console_text(text), text)

    def test_unmappable_falls_back_to_replace_not_crash(self):
        # Emoji has no cp1252 mapping: explicit, tested last resort.
        self.assertEqual(sanitize_console_text("done 😀"), "done ?")

    def test_non_string_input_coerced(self):
        self.assertEqual(sanitize_console_text(42), "42")
        self.assertEqual(sanitize_console_text(None), "None")


if __name__ == "__main__":
    unittest.main()
