"""원격 뷰어 포인터 수학(main.remote_viewer_main 과 동일 공식) 단위 검증.

실제 Flet/GUI는 여기서 돌리지 않고, 레이아웃 불변식만 검증한다.
"""

from __future__ import annotations

import unittest


def contain_fit_disp_xy(
    view_size: tuple[float, float],
    stream_wh: tuple[float, float],
) -> tuple[float, float, float, float]:
    cw = max(1.0, view_size[0])
    ch = max(1.0, view_size[1])
    sw = max(1.0, stream_wh[0])
    sh = max(1.0, stream_wh[1])
    ar_s = sw / sh
    ar_c = cw / ch
    if ar_c > ar_s:
        disp_h = ch
        disp_w = disp_h * ar_s
        ox = (cw - disp_w) * 0.5
        oy = 0.0
    else:
        disp_w = cw
        disp_h = disp_w / ar_s
        ox = 0.0
        oy = (ch - disp_h) * 0.5
    return max(1.0, disp_w), max(1.0, disp_h), ox, oy


def inner_image_paint_rect(
    view_size: tuple[float, float],
    stream_wh: tuple[float, float],
) -> tuple[float, float, float, float]:
    disp_w, disp_h, _, _ = contain_fit_disp_xy(view_size, stream_wh)
    iw = max(1.0, stream_wh[0])
    ih = max(1.0, stream_wh[1])
    scale = min(disp_w / iw, disp_h / ih)
    fw = iw * scale
    fh = ih * scale
    ox = (disp_w - fw) * 0.5
    oy = (disp_h - fh) * 0.5
    return ox, oy, fw, fh


def norm_xy_in_video_local(
    lx: float,
    ly: float,
    view_size: tuple[float, float],
    stream_wh: tuple[float, float],
) -> tuple[float, float] | None:
    _, _, fw, fh = inner_image_paint_rect(view_size, stream_wh)
    if lx < 0.0 or ly < 0.0 or lx > fw or ly > fh:
        return None
    nx = lx / max(fw, 1.0)
    ny = ly / max(fh, 1.0)
    return max(0.0, min(1.0, nx)), max(0.0, min(1.0, ny))


class RemoteViewerPointerMathTests(unittest.TestCase):
    def test_aspect_match_inner_fills_cell(self) -> None:
        """뷰 셀 종횡비 == 스트림이면 내부 페인트가 셀 전체(회색 패딩 없음)."""
        vs = (800.0, 450.0)
        stream_wh = (1920.0, 1080.0)
        disp_w, disp_h, _, _ = contain_fit_disp_xy(vs, stream_wh)
        iox, ioy, fw, fh = inner_image_paint_rect(vs, stream_wh)
        self.assertAlmostEqual(disp_w / disp_h, stream_wh[0] / stream_wh[1], places=5)
        self.assertAlmostEqual(fw, disp_w, delta=1e-3)
        self.assertAlmostEqual(fh, disp_h, delta=1e-3)
        self.assertAlmostEqual(iox, 0.0, delta=1e-3)
        self.assertAlmostEqual(ioy, 0.0, delta=1e-3)

    def test_aspect_mismatch_letterbox_on_shell_not_inside_inner(self) -> None:
        """셀 ≠ 스트림 종횡비면 표시 영역(disp)이 셀보다 작아지고 contain 의 ox/oy 로 패딩된다.

        inner_image_paint_rect 는 disp 와 같은 종횡비의 스트림을 담으므로 항상 disp 를 꽉 채운다
        (iox=ioy=0, fw=disp_w). 회색은 shell.left/top·크기로만 생긴다 — main._sync_remote_video_rect 와 동일.
        """
        vs = (1000.0, 1000.0)
        stream_wh = (1920.0, 1080.0)
        cw, ch = vs
        disp_w, disp_h, ox, oy = contain_fit_disp_xy(vs, stream_wh)
        iox, ioy, fw, fh = inner_image_paint_rect(vs, stream_wh)
        self.assertLess(disp_w * disp_h, cw * ch - 1.0)
        self.assertGreater(oy, 1.0)
        self.assertAlmostEqual(ox, 0.0, delta=1e-6)
        self.assertAlmostEqual(fw, disp_w, delta=1e-3)
        self.assertAlmostEqual(fh, disp_h, delta=1e-3)
        self.assertAlmostEqual(iox, 0.0, delta=1e-6)
        self.assertAlmostEqual(ioy, 0.0, delta=1e-6)

    def test_norm_corners(self) -> None:
        vs = (800.0, 450.0)
        stream_wh = (1920.0, 1080.0)
        _, _, fw, fh = inner_image_paint_rect(vs, stream_wh)
        self.assertIsNotNone(norm_xy_in_video_local(0.0, 0.0, vs, stream_wh))
        p = norm_xy_in_video_local(fw, fh, vs, stream_wh)
        assert p is not None
        self.assertAlmostEqual(p[0], 1.0, delta=1e-6)
        self.assertAlmostEqual(p[1], 1.0, delta=1e-6)

    def test_norm_rejects_outside_hit_rect(self) -> None:
        """영상 GD 밖 좌표는 None (회색 패딩 구간에 해당)."""
        vs = (1000.0, 1000.0)
        stream_wh = (1920.0, 1080.0)
        _, _, fw, fh = inner_image_paint_rect(vs, stream_wh)
        self.assertIsNone(norm_xy_in_video_local(-1.0, fh / 2, vs, stream_wh))
        self.assertIsNone(norm_xy_in_video_local(fw + 10.0, fh / 2, vs, stream_wh))
        self.assertIsNone(norm_xy_in_video_local(fw / 2, fh + 0.01, vs, stream_wh))

    def test_portrait_stream_landscape_cell_horizontal_shell_margin(self) -> None:
        """세로 스트림을 가로 셀에 넣으면 좌우 패딩은 contain 의 ox (shell 위치)에서 생긴다."""
        vs = (1920.0, 1080.0)
        stream_wh = (1080.0, 1920.0)
        disp_w, disp_h, ox, oy = contain_fit_disp_xy(vs, stream_wh)
        iox, ioy, fw, fh = inner_image_paint_rect(vs, stream_wh)
        self.assertGreater(ox, 1.0)
        self.assertAlmostEqual(oy, 0.0, delta=1e-4)
        self.assertAlmostEqual(fw, disp_w, delta=1e-3)
        self.assertAlmostEqual(fh, disp_h, delta=1e-3)
        self.assertAlmostEqual(iox, 0.0, delta=1e-6)
        self.assertAlmostEqual(ioy, 0.0, delta=1e-6)


if __name__ == "__main__":
    unittest.main()
