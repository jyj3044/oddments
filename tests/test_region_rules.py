from __future__ import annotations

import unittest
from unittest import mock

import numpy as np

from detection import pipeline
import flet_ui.state as state_module
from detection.common import (
    DetectionConfig,
    DetectionHitEvent,
    OverlayRect,
    RegionDetectionConfig,
    RegionRect,
    clamp_region_rect,
    color_match_ratio,
    offset_overlay_rects,
    region_color_matches,
)
from flet_ui.state import AppState, RegionRuleSettings
import flet_ui.pages.page_ocr as page_ocr
from app_platform.region_selector import ScreenPoint, ScreenRect


class RegionRuleHelperTests(unittest.TestCase):
    def test_clamp_region_rect_keeps_rect_inside_frame(self) -> None:
        rect = RegionRect(x=-5, y=8, w=20, h=20)

        clamped = clamp_region_rect(rect, frame_w=12, frame_h=15)

        self.assertEqual(clamped, RegionRect(x=0, y=8, w=12, h=7))

    def test_clamp_region_rect_rejects_empty_after_clamping(self) -> None:
        rect = RegionRect(x=20, y=10, w=5, h=5)

        self.assertIsNone(clamp_region_rect(rect, frame_w=12, frame_h=15))

    def test_offset_overlay_rects_maps_crop_overlays_to_full_frame(self) -> None:
        overlays = [
            OverlayRect(3, 4, 10, 12, (1, 2, 3), "키워드"),
            OverlayRect(0, 0, 2, 3, (4, 5, 6), "색상"),
        ]

        shifted = offset_overlay_rects(overlays, dx=100, dy=50, label_prefix="영역 1")

        self.assertEqual(
            shifted,
            [
                OverlayRect(103, 54, 10, 12, (1, 2, 3), "영역 1 키워드"),
                OverlayRect(100, 50, 2, 3, (4, 5, 6), "영역 1 색상"),
            ],
        )

    def test_color_match_ratio_uses_percent_tolerance(self) -> None:
        crop = np.array(
            [
                [[0, 0, 255], [25, 25, 230]],
                [[255, 0, 0], [0, 255, 0]],
            ],
            dtype=np.uint8,
        )

        ratio = color_match_ratio(crop, color_bgr=(0, 0, 255), tolerance=10)

        self.assertAlmostEqual(ratio, 50.0, places=4)

    def test_region_color_matches_when_any_pixel_is_within_tolerance(self) -> None:
        crop = np.zeros((10, 10, 3), dtype=np.uint8)
        crop[0, 0, :] = np.array([0, 0, 255], dtype=np.uint8)
        cfg = RegionDetectionConfig(
            id="r1",
            name="영역 1",
            rect=RegionRect(0, 0, 10, 10),
            color_match_enabled=True,
            color_bgr=(0, 0, 255),
            color_tolerance=0,
        )

        self.assertTrue(region_color_matches(crop, cfg))


class RegionRuleSettingsTests(unittest.TestCase):
    def test_main_custom_sound_round_trip_through_settings_dict(self) -> None:
        state = AppState()
        state.settings.detection.custom_sound_path = "D:/sounds/main.mp3"

        data = state._serialize_settings_dict()
        loaded = AppState()
        loaded._apply_settings_dict(data)

        self.assertEqual(loaded.settings.detection.custom_sound_path, "D:/sounds/main.mp3")
        loaded._sync_cfg_from_settings()
        self.assertEqual(loaded.get_cfg().custom_sound_path, "D:/sounds/main.mp3")

    def test_region_rules_round_trip_through_settings_dict(self) -> None:
        state = AppState()
        state.settings.detection.main_expanded = True
        state.settings.detection.region_rules = (
            RegionRuleSettings(
                id="r1",
                name="체력바",
                enabled=True,
                keywords="위험,경고",
                rect=RegionRect(10, 20, 30, 40),
                ocr_variant_groups=("raw", "otsu"),
                color_match_enabled=True,
                color_hex="#ff3030",
                color_tolerance=120,
                cooldown_sec=1.5,
                custom_sound_path="D:/sounds/warn.wav",
                expanded=True,
            ),
        )

        data = state._serialize_settings_dict()
        loaded = AppState()
        loaded._apply_settings_dict(data)

        self.assertTrue(loaded.settings.detection.main_expanded)
        self.assertEqual(len(loaded.settings.detection.region_rules), 1)
        rule = loaded.settings.detection.region_rules[0]
        self.assertEqual(rule.id, "r1")
        self.assertEqual(rule.name, "체력바")
        self.assertEqual(rule.keywords, "위험,경고")
        self.assertEqual(rule.rect, RegionRect(10, 20, 30, 40))
        self.assertEqual(rule.ocr_variant_groups, ("raw", "otsu"))
        self.assertTrue(rule.color_match_enabled)
        self.assertEqual(rule.color_hex, "#ff3030")
        self.assertEqual(rule.color_tolerance, 100)
        self.assertAlmostEqual(rule.cooldown_sec, 1.5)
        self.assertEqual(rule.custom_sound_path, "D:/sounds/warn.wav")
        self.assertTrue(rule.expanded)
        self.assertNotIn("color_min_ratio", data["region_rules"][0])

    def test_sync_cfg_builds_runtime_region_rules(self) -> None:
        state = AppState()
        state.settings.detection.region_rules = (
            RegionRuleSettings(
                id="r1",
                name="영역 1",
                enabled=True,
                keywords="위험, 경고",
                rect=RegionRect(1, 2, 30, 40),
                ocr_variant_groups=("gray_clahe",),
                color_hex="#112233",
                color_tolerance=-5,
                cooldown_sec=4.0,
                custom_sound_path="D:/a.wav",
            ),
        )

        state._sync_cfg_from_settings()
        cfg = state.get_cfg()

        self.assertEqual(len(cfg.region_rules), 1)
        runtime = cfg.region_rules[0]
        self.assertEqual(runtime.alert_keywords, ("위험", "경고"))
        self.assertEqual(runtime.rect, RegionRect(1, 2, 30, 40))
        self.assertEqual(runtime.ocr_variant_groups, ("gray_clahe",))
        self.assertEqual(runtime.color_bgr, (0x33, 0x22, 0x11))
        self.assertEqual(runtime.color_tolerance, 0)
        self.assertAlmostEqual(runtime.cooldown_sec, 4.0)
        self.assertEqual(runtime.custom_sound_path, "D:/a.wav")


class RegionPipelineTests(unittest.TestCase):
    def test_main_keyword_hit_uses_custom_sound_path(self) -> None:
        frame = np.zeros((40, 40, 3), dtype=np.uint8)
        cfg = DetectionConfig(
            alert_keywords=("위험",),
            ocr_engines=("rapidocr",),
            custom_sound_path="D:/main.mp3",
        )
        original = pipeline.kw.run_keyword_detection

        def fake_run_keyword_detection(*args, **kwargs):
            return True, []

        pipeline.kw.run_keyword_detection = fake_run_keyword_detection
        try:
            result = pipeline.run_detection_detailed(frame, cfg)
        finally:
            pipeline.kw.run_keyword_detection = original

        self.assertTrue(result.triggered)
        self.assertEqual(result.events[0].id, "main")
        self.assertEqual(result.events[0].custom_sound_path, "D:/main.mp3")

    def test_region_keyword_hit_offsets_overlays_and_returns_event(self) -> None:
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        cfg = DetectionConfig(
            alert_keywords=(),
            ocr_engines=("rapidocr",),
            region_rules=(
                RegionDetectionConfig(
                    id="r1",
                    name="영역 1",
                    rect=RegionRect(10, 20, 30, 40),
                    alert_keywords=("위험",),
                    custom_sound_path="D:/warn.wav",
                    cooldown_sec=2.0,
                ),
            ),
        )
        original = pipeline.kw.run_keyword_detection

        def fake_run_keyword_detection(*args, **kwargs):
            crop = args[0]
            self.assertEqual(crop.shape[:2], (40, 30))
            return True, [OverlayRect(1, 2, 3, 4, (9, 8, 7), "키워드")]

        pipeline.kw.run_keyword_detection = fake_run_keyword_detection
        try:
            result = pipeline.run_detection_detailed(frame, cfg)
        finally:
            pipeline.kw.run_keyword_detection = original

        self.assertTrue(result.triggered)
        self.assertEqual(result.reason, "특정영역: 영역 1")
        self.assertEqual(
            result.overlays,
            (OverlayRect(11, 22, 3, 4, (9, 8, 7), "영역 1 키워드"),),
        )
        self.assertEqual(
            result.events,
            (
                DetectionHitEvent(
                    id="region:r1",
                    name="영역 1",
                    cooldown_sec=2.0,
                    custom_sound_path="D:/warn.wav",
                ),
            ),
        )

    def test_region_color_match_triggers_without_keywords(self) -> None:
        frame = np.zeros((20, 20, 3), dtype=np.uint8)
        frame[5:15, 5:15] = np.array([0, 0, 255], dtype=np.uint8)
        cfg = DetectionConfig(
            ocr_engines=(),
            region_rules=(
                RegionDetectionConfig(
                    id="color",
                    name="색상",
                    rect=RegionRect(5, 5, 10, 10),
                    alert_keywords=(),
                    color_match_enabled=True,
                    color_bgr=(0, 0, 255),
                    color_tolerance=0,
                ),
            ),
        )

        result = pipeline.run_detection_detailed(frame, cfg)

        self.assertTrue(result.triggered)
        self.assertEqual(result.reason, "특정영역: 색상")
        self.assertEqual(len(result.overlays), 1)
        self.assertEqual((result.overlays[0].x, result.overlays[0].y), (5, 5))
        self.assertEqual(result.events[0].id, "region:color")


class RegionAlertCooldownTests(unittest.TestCase):
    def test_alert_cooldown_is_independent_per_event_id(self) -> None:
        app = AppState()
        app._sound_armed = True
        calls: list[str | None] = []
        original = state_module.play_alert_sound

        def fake_play_alert_sound(path: str | None = None) -> bool:
            calls.append(path)
            return True

        state_module.play_alert_sound = fake_play_alert_sound
        try:
            app._maybe_play_sound(
                event_id="main",
                cooldown_sec=10.0,
                custom_sound_path="",
            )
            app._maybe_play_sound(
                event_id="region:r1",
                cooldown_sec=10.0,
                custom_sound_path="D:/r1.wav",
            )
            app._maybe_play_sound(
                event_id="region:r1",
                cooldown_sec=10.0,
                custom_sound_path="D:/r1.wav",
            )
            app._maybe_play_sound(
                event_id="region:r2",
                cooldown_sec=10.0,
                custom_sound_path="D:/r2.wav",
            )
        finally:
            state_module.play_alert_sound = original

        self.assertEqual(calls, [None, "D:/r1.wav", "D:/r2.wav"])

    def test_cooldown_starts_only_after_successful_play(self) -> None:
        app = AppState()
        app._sound_armed = True
        calls: list[str | None] = []
        original = state_module.play_alert_sound

        def fake_fail(_path: str | None = None) -> bool:
            calls.append(_path)
            return False

        state_module.play_alert_sound = fake_fail
        try:
            app._maybe_play_sound(event_id="main", cooldown_sec=10.0)
            app._maybe_play_sound(event_id="main", cooldown_sec=10.0)
        finally:
            state_module.play_alert_sound = original

        self.assertEqual(calls, [None, None])

    def test_cooldown_blocks_repeat_until_elapsed(self) -> None:
        app = AppState()
        app._sound_armed = True
        calls: list[str | None] = []
        original = state_module.play_alert_sound
        # 각 _maybe_play_sound: 쿨다운 확인 1회, 재생 성공 시 기록 1회
        mono = iter([0.0, 0.0, 5.0, 11.0, 11.0])

        def fake_play(path: str | None = None) -> bool:
            calls.append(path)
            return True

        state_module.play_alert_sound = fake_play
        try:
            with mock.patch("flet_ui.state.time.monotonic", side_effect=mono):
                app._maybe_play_sound(event_id="main", cooldown_sec=10.0)
                app._maybe_play_sound(event_id="main", cooldown_sec=10.0)
                app._maybe_play_sound(event_id="main", cooldown_sec=10.0)
        finally:
            state_module.play_alert_sound = original

        self.assertEqual(calls, [None, None])

    def test_reset_alert_sound_cooldowns_allows_immediate_play(self) -> None:
        app = AppState()
        app._sound_armed = True
        original = state_module.play_alert_sound
        calls: list[str | None] = []

        def fake_play(path: str | None = None) -> bool:
            calls.append(path)
            return True

        state_module.play_alert_sound = fake_play
        try:
            app._maybe_play_sound(event_id="main", cooldown_sec=10.0)
            app._reset_alert_sound_cooldowns()
            app._maybe_play_sound(event_id="main", cooldown_sec=10.0)
        finally:
            state_module.play_alert_sound = original

        self.assertEqual(calls, [None, None])


class RegionSelectionMappingTests(unittest.TestCase):
    def test_capture_window_pick_round_trips_through_settings_dict(self) -> None:
        state = AppState()
        state.settings.capture.source_mode = "window"
        state.settings.capture.monitor_index = 2
        state.settings.capture.picked_hwnd = 987654
        state.settings.capture.picked_summary = "Target Window"

        data = state._serialize_settings_dict()
        loaded = AppState()
        loaded._apply_settings_dict(data)

        self.assertEqual(loaded.settings.capture.source_mode, "window")
        self.assertEqual(loaded.settings.capture.monitor_index, 2)
        self.assertEqual(loaded.settings.capture.picked_hwnd, 987654)
        self.assertEqual(loaded.settings.capture.picked_summary, "Target Window")

    def test_monitor_screen_rect_maps_to_capture_rect(self) -> None:
        state = AppState()
        state.settings.capture.source_mode = "monitor"
        state.settings.capture.monitor_index = 2
        original = page_ocr.enumerate_monitors
        page_ocr.enumerate_monitors = lambda: [
            {"index": 1, "left": 0, "top": 0, "width": 100, "height": 100},
            {"index": 2, "left": 200, "top": 50, "width": 400, "height": 300},
        ]
        try:
            rect = page_ocr._screen_rect_to_capture_rect(
                state, ScreenRect(250, 70, 100, 80)
            )
        finally:
            page_ocr.enumerate_monitors = original

        self.assertEqual(rect, RegionRect(50, 20, 100, 80))

    def test_monitor_selection_bounds_use_selected_capture_monitor(self) -> None:
        state = AppState()
        state.settings.capture.source_mode = "monitor"
        state.settings.capture.monitor_index = 2
        original = page_ocr.enumerate_monitors
        page_ocr.enumerate_monitors = lambda: [
            {"index": 1, "left": 0, "top": 0, "width": 100, "height": 100},
            {"index": 2, "left": -300, "top": 50, "width": 300, "height": 200},
        ]
        try:
            bounds = page_ocr._capture_selection_bounds(state)
        finally:
            page_ocr.enumerate_monitors = original

        self.assertEqual(bounds, [ScreenRect(-300, 50, 300, 200)])

    def test_window_mapping_uses_window_rect_when_client_rect_misses(self) -> None:
        state = AppState()
        state.settings.capture.source_mode = "window"
        state.settings.capture.picked_hwnd = 123
        original_client = page_ocr._window_client_rect_for_mapping
        original_window = page_ocr._window_capture_rect_for_mapping
        page_ocr._window_client_rect_for_mapping = lambda _hwnd: (120, 120, 100, 100)
        page_ocr._window_capture_rect_for_mapping = lambda _hwnd: (10, 20, 300, 200)
        try:
            rect = page_ocr._screen_rect_to_capture_rect(
                state, ScreenRect(20, 30, 50, 60)
            )
        finally:
            page_ocr._window_client_rect_for_mapping = original_client
            page_ocr._window_capture_rect_for_mapping = original_window

        self.assertEqual(rect, RegionRect(10, 10, 50, 60))

    def test_window_mapping_prefers_rect_matching_latest_frame_size(self) -> None:
        state = AppState()
        state.settings.capture.source_mode = "window"
        state.settings.capture.picked_hwnd = 123
        state.get_latest_preview = lambda: np.zeros((768, 1366, 3), dtype=np.uint8)  # type: ignore[method-assign]
        original_client = page_ocr._window_client_rect_for_mapping
        original_window = page_ocr._window_capture_rect_for_mapping
        page_ocr._window_capture_rect_for_mapping = lambda _hwnd: (100, 100, 1368, 800)
        page_ocr._window_client_rect_for_mapping = lambda _hwnd: (101, 132, 1366, 768)
        try:
            rect = page_ocr._screen_rect_to_capture_rect(
                state, ScreenRect(101, 132, 100, 50)
            )
        finally:
            page_ocr._window_client_rect_for_mapping = original_client
            page_ocr._window_capture_rect_for_mapping = original_window

        self.assertEqual(rect, RegionRect(0, 0, 100, 50))

    def test_window_selection_bounds_use_selected_window_capture_rect(self) -> None:
        state = AppState()
        state.settings.capture.source_mode = "window"
        state.settings.capture.picked_hwnd = 123
        original_effective = page_ocr._effective_capture_hwnd_for_mapping
        original_client = page_ocr._window_client_rect_for_mapping
        original_window = page_ocr._window_capture_rect_for_mapping
        seen: list[int] = []
        page_ocr._effective_capture_hwnd_for_mapping = lambda _hwnd: 456
        page_ocr._window_client_rect_for_mapping = lambda _hwnd: (120, 120, 100, 100)

        def fake_window_rect(hwnd):
            seen.append(hwnd)
            return (-500, 40, 300, 200)

        page_ocr._window_capture_rect_for_mapping = fake_window_rect
        try:
            bounds = page_ocr._capture_selection_bounds(state)
        finally:
            page_ocr._effective_capture_hwnd_for_mapping = original_effective
            page_ocr._window_client_rect_for_mapping = original_client
            page_ocr._window_capture_rect_for_mapping = original_window

        self.assertEqual(bounds, [ScreenRect(-500, 40, 300, 200)])
        self.assertEqual(seen, [456])

    def test_window_selection_bounds_prefer_child_rect_when_root_is_monitor_sized(self) -> None:
        state = AppState()
        state.settings.capture.source_mode = "window"
        state.settings.capture.picked_hwnd = 123
        original_effective = page_ocr._effective_capture_hwnd_for_mapping
        original_candidates = page_ocr._window_selection_rect_candidates
        original_looks_monitor = page_ocr._rect_looks_like_monitor
        page_ocr._effective_capture_hwnd_for_mapping = lambda _hwnd: 123
        page_ocr._rect_looks_like_monitor = lambda _rect: True
        page_ocr._window_selection_rect_candidates = lambda _hwnd: [
            ScreenRect(-1920, 357, 1920, 1032),
            ScreenRect(-1616, 490, 1368, 800),
            ScreenRect(-1500, 510, 300, 200),
        ]
        try:
            bounds = page_ocr._capture_selection_bounds(state)
        finally:
            page_ocr._effective_capture_hwnd_for_mapping = original_effective
            page_ocr._window_selection_rect_candidates = original_candidates
            page_ocr._rect_looks_like_monitor = original_looks_monitor

        self.assertEqual(bounds, [ScreenRect(-1616, 490, 1368, 800)])

    def test_window_display_rect_prefers_nested_same_process_window(self) -> None:
        original_candidates = page_ocr._window_selection_rect_candidates
        page_ocr._window_selection_rect_candidates = lambda _hwnd: [
            ScreenRect(1919, 218, 1368, 800),
            ScreenRect(1920, 249, 1366, 768),
            ScreenRect(2763, 223, 410, 806),
        ]
        try:
            rect = page_ocr._window_display_rect_for_selection(123)
        finally:
            page_ocr._window_selection_rect_candidates = original_candidates

        self.assertEqual(rect, ScreenRect(2763, 223, 410, 806))

    def test_spoide_falls_back_to_screen_pixel_when_capture_mapping_fails(self) -> None:
        state = AppState()
        original = page_ocr._sample_screen_pixel_bgr
        page_ocr._sample_screen_pixel_bgr = lambda _point: (0x33, 0x22, 0x11)
        try:
            color = page_ocr._sample_color_from_screen_point(state, ScreenPoint(10, 20))
        finally:
            page_ocr._sample_screen_pixel_bgr = original

        self.assertEqual(color, "#112233")


if __name__ == "__main__":
    unittest.main()
