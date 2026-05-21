from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app_platform import settings_paths as sp


class SettingsPathTests(unittest.TestCase):
    def test_under_assets_stored_as_relative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp) / "assets"
            sounds = assets / "sounds"
            sounds.mkdir(parents=True)
            wav = sounds / "alert.wav"
            wav.write_bytes(b"RIFF")

            with mock.patch.object(sp, "user_assets_dir", return_value=assets):
                stored = sp.to_settings_storage_path(str(wav))
                resolved = sp.resolve_settings_path(stored)

            self.assertEqual(stored, "sounds/alert.wav")
            self.assertEqual(resolved, str(wav.resolve()))

    def test_outside_assets_stored_as_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp) / "assets"
            assets.mkdir()
            external = Path(tmp) / "other" / "tone.mp3"
            external.parent.mkdir()
            external.write_bytes(b"x")

            with mock.patch.object(sp, "user_assets_dir", return_value=assets):
                stored = sp.to_settings_storage_path(str(external))
                resolved = sp.resolve_settings_path(stored)

            self.assertEqual(stored, str(external.resolve()))
            self.assertEqual(resolved, str(external.resolve()))

    def test_resolve_ignores_process_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp) / "assets"
            tpl = assets / "tpl.png"
            assets.mkdir(parents=True)
            tpl.write_bytes(b"\x89PNG")

            with (
                mock.patch.object(sp, "user_assets_dir", return_value=assets),
                mock.patch.object(sp, "app_install_dir", return_value=Path(tmp)),
            ):
                stored = sp.to_settings_storage_path(str(tpl))
                old_cwd = Path.cwd()
                try:
                    os.chdir(tempfile.gettempdir())
                    resolved = sp.resolve_settings_path(stored)
                finally:
                    os.chdir(old_cwd)

            self.assertEqual(resolved, str(tpl.resolve()))

    def test_already_relative_path_resolves_under_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp) / "assets"
            img = assets / "a.png"
            assets.mkdir(parents=True)
            img.write_bytes(b"x")

            with mock.patch.object(sp, "user_assets_dir", return_value=assets):
                self.assertEqual(
                    sp.resolve_settings_path("a.png"),
                    str(img.resolve()),
                )

    def test_absolute_under_assets_migrates_to_relative_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp) / "assets"
            wav = assets / "alert.wav"
            assets.mkdir(parents=True)
            wav.write_bytes(b"RIFF")

            with mock.patch.object(sp, "user_assets_dir", return_value=assets):
                stored = sp.to_settings_storage_path(str(wav.resolve()))

            self.assertEqual(stored, "alert.wav")
            with mock.patch.object(sp, "user_assets_dir", return_value=assets):
                self.assertEqual(
                    sp.resolve_settings_path(stored),
                    str(wav.resolve()),
                )


if __name__ == "__main__":
    unittest.main()
