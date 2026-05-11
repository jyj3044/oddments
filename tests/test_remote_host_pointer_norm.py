"""호스트 쪽 정규 좌표 → 모니터 픽셀 매핑 (송출 해상도와 무관하게 gw×gh 스팬)."""

from __future__ import annotations

import unittest

from streaming.remote_host import _norm_to_monitor_pixel_floats


class RemoteHostPointerNormTests(unittest.TestCase):
    def test_corners_full_hd_monitor(self) -> None:
        rect = (0, 0, 1920, 1080)
        self.assertEqual(_norm_to_monitor_pixel_floats(0.0, 0.0, rect), (0.0, 0.0))
        self.assertEqual(_norm_to_monitor_pixel_floats(1.0, 1.0, rect), (1920.0, 1080.0))

    def test_span_uses_geom_not_encoded_width(self) -> None:
        """인코더가 3840→1920으로 줄여도 정규 좌표는 여전히 전체 모니터 폭에 매핑된다."""
        rect = (0, 0, 3840, 2160)
        ax, ay = _norm_to_monitor_pixel_floats(1.0, 0.5, rect)
        self.assertEqual(ax, 3840.0)
        self.assertEqual(ay, 1080.0)

    def test_secondary_monitor_offset(self) -> None:
        rect = (1920, 0, 1920, 1080)
        ax, ay = _norm_to_monitor_pixel_floats(0.0, 1.0, rect)
        self.assertEqual(ax, 1920.0)
        self.assertEqual(ay, 1080.0)


if __name__ == "__main__":
    unittest.main()
