# OCR Region Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add multiple independent OCR region rules with per-rule preprocessing, color matching, custom alert sound, and alert cooldown.

**Architecture:** Keep the current main OCR flow intact, then add region-rule runtime config and detection after main detection. Region rules reuse the existing keyword OCR code on cropped frames, with pure helpers for rect clamping, color matching, settings serialization, and cooldown checks.

**Tech Stack:** Python dataclasses, Flet UI, NumPy/OpenCV-style BGR frames, pytest.

---

### Task 1: Pure Region Helpers

**Files:**
- Modify: `detection/common.py`
- Create: `tests/test_region_rules.py`

- [ ] **Step 1: Add failing tests**

Add tests for rectangle clamping, color matching ratio, and overlay coordinate offsetting.

- [ ] **Step 2: Run tests and confirm failure**

Run: `python -m pytest tests/test_region_rules.py -v`
Expected: FAIL because helper types/functions do not exist yet.

- [ ] **Step 3: Implement helpers**

Add region config dataclasses plus helpers:

- `RegionRect`
- `RegionDetectionConfig`
- `clamp_region_rect`
- `offset_overlay_rects`
- `color_match_ratio`
- `region_color_matches`

- [ ] **Step 4: Run tests and confirm pass**

Run: `python -m pytest tests/test_region_rules.py -v`
Expected: PASS.

### Task 2: Settings Persistence

**Files:**
- Modify: `flet_ui/state.py`
- Modify: `detection/common.py`
- Modify: `tests/test_region_rules.py`

- [ ] **Step 1: Add failing tests**

Add tests that serialize and load region rule settings without breaking existing settings.

- [ ] **Step 2: Run tests and confirm failure**

Run: `python -m pytest tests/test_region_rules.py -v`
Expected: FAIL because `RegionRuleSettings` and persistence are missing.

- [ ] **Step 3: Implement settings model and JSON migration**

Add `RegionRuleSettings` to UI state, parse `region_rules`, serialize it, and sync runtime region configs into `DetectionConfig`.

- [ ] **Step 4: Run tests and confirm pass**

Run: `python -m pytest tests/test_region_rules.py -v`
Expected: PASS.

### Task 3: Region Detection Pipeline

**Files:**
- Modify: `detection/pipeline.py`
- Modify: `detection/common.py`
- Modify: `flet_ui/state.py`
- Modify: `tests/test_region_rules.py`

- [ ] **Step 1: Add failing tests**

Add tests for region detection using monkeypatched keyword detection and color matching.

- [ ] **Step 2: Run tests and confirm failure**

Run: `python -m pytest tests/test_region_rules.py -v`
Expected: FAIL because pipeline does not run region rules.

- [ ] **Step 3: Implement region pipeline**

Run main detection first, then loop region configs, crop frames, apply OCR/color checks, offset overlays, and return hit events with stable rule IDs.

- [ ] **Step 4: Run tests and confirm pass**

Run: `python -m pytest tests/test_region_rules.py -v`
Expected: PASS.

### Task 4: Sound and Cooldown Dispatch

**Files:**
- Modify: `app_platform/audio.py`
- Modify: `flet_ui/state.py`
- Modify: `tests/test_region_rules.py`

- [ ] **Step 1: Add failing tests**

Add tests for independent cooldown keys.

- [ ] **Step 2: Run tests and confirm failure**

Run: `python -m pytest tests/test_region_rules.py -v`
Expected: FAIL because cooldown helper is absent.

- [ ] **Step 3: Implement sound dispatch**

Allow `play_alert_sound(path=None)`. In `AppState`, keep separate cooldown timestamps for main and each region rule. Play custom sound for region hits when configured, falling back to default.

- [ ] **Step 4: Run tests and confirm pass**

Run: `python -m pytest tests/test_region_rules.py -v`
Expected: PASS.

### Task 5: OCR Settings UI

**Files:**
- Modify: `flet_ui/pages/page_ocr.py`
- Modify: `flet_ui/state.py`

- [ ] **Step 1: Refactor reusable builders**

Split OCR variant checkbox grid creation into a helper used by main OCR and each region drawer.

- [ ] **Step 2: Add region list UI**

Add `특정영역` card with `+ 특정영역 추가`, collapsed rows, expanded drawers, delete/clear controls, and file picker for custom sounds.

- [ ] **Step 3: Wire settings sync**

Every control updates `state.settings.detection.region_rules`, calls `_sync_cfg_from_settings()`, and updates the current page where needed.

- [ ] **Step 4: Add coordinate fallback**

Until the platform overlay selector is implemented, provide numeric x/y/w/h fields and make `영역 지정 시작` show a message that external drag selection is not wired yet.

### Task 6: Final Verification

**Files:**
- All modified files

- [ ] **Step 1: Run focused tests**

Run: `python -m pytest tests/test_region_rules.py -v`
Expected: PASS.

- [ ] **Step 2: Run existing tests**

Run: `python -m pytest tests -v`
Expected: PASS or report unrelated existing failures with exact output.

- [ ] **Step 3: Compile modified Python**

Run: `python -m compileall detection flet_ui app_platform tests`
Expected: exit code 0.
