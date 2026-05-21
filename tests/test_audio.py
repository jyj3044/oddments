from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app_platform import audio


class ResolveAlertSoundPathTests(unittest.TestCase):
    def test_returns_none_for_missing_file(self) -> None:
        self.assertIsNone(audio.resolve_alert_sound_path(r"C:\no\such\file.mp3"))

    def test_returns_absolute_existing_file(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            path = tmp.name
        try:
            resolved = audio.resolve_alert_sound_path(path)
            self.assertEqual(resolved, os.path.abspath(path))
        finally:
            os.unlink(path)


@unittest.skipUnless(sys.platform == "win32", "Windows playback backend")
class WindowsCustomSoundTests(unittest.TestCase):
    def test_mp3_uses_mci_not_winsound(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            path = tmp.name
        try:
            with (
                mock.patch.object(audio, "_play_custom_sound_windows") as play_custom,
                mock.patch.object(audio, "_play_default_sound_windows") as play_default,
            ):
                play_custom.return_value = True
                audio.play_alert_sound(path)
            play_custom.assert_called_once()
            called_path = play_custom.call_args[0][0]
            self.assertTrue(called_path.lower().endswith(".mp3"))
            play_default.assert_not_called()
        finally:
            os.unlink(path)

    def test_wav_uses_custom_backend(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            path = tmp.name
        try:
            with (
                mock.patch.object(audio, "_play_custom_sound_windows") as play_custom,
                mock.patch.object(audio, "_play_default_sound_windows") as play_default,
            ):
                play_custom.return_value = True
                audio.play_alert_sound(path)
            play_custom.assert_called_once()
            play_default.assert_not_called()
        finally:
            os.unlink(path)

    def test_missing_custom_file_falls_back_to_default(self) -> None:
        with (
            mock.patch.object(audio, "_play_custom_sound_windows") as play_custom,
            mock.patch.object(audio, "_play_default_sound_windows") as play_default,
        ):
            audio.play_alert_sound(r"C:\missing\alert.mp3")
        play_custom.assert_not_called()
        play_default.assert_called_once()


if __name__ == "__main__":
    unittest.main()
