"""Tests for per-guild FFmpeg profiles."""
from __future__ import annotations

import unittest

import arken_audio as a


class ArkenAudioTests(unittest.TestCase):
    def test_normalize_region(self):
        self.assertEqual(a.normalize_region("eu"), "eu")
        self.assertEqual(a.normalize_region("bogus"), "us")

    def test_guild_setting_override(self):
        def gs(_gid, key, default=None):
            return "eu" if key == "ffmpeg_region" else default

        opts = a.ffmpeg_options_for_guild(123, gs)
        self.assertIn("44100", opts["options"])


if __name__ == "__main__":
    unittest.main()