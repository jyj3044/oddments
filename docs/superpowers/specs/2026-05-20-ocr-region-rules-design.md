# OCR Region Rules Design

## Goal

Add multiple user-defined OCR region rules to OCR Settings. Each region rule can be selected by dragging on a separate target overlay, can use its own keywords and OCR preprocessing variants, can optionally use color matching, and can play its own custom alert sound with an independent alert cooldown.

## Current Context

The app currently has one OCR detection configuration:

- `flet_ui/pages/page_ocr.py` renders the OCR Settings UI.
- `flet_ui/state.py` stores `DetectionSettings`, loads and saves settings, syncs settings into `DetectionConfig`, and owns alert sound cooldown state.
- `detection/common.py` defines `DetectionConfig` and overlay rectangle types.
- `detection/pipeline.py` runs keyword OCR and template matching for the current capture frame.
- `detection/keywords.py` already accepts `variant_groups`, so the same preprocessing variant mechanism can be reused for main OCR and cropped region OCR.
- `app_platform/audio.py` plays the current default alert sound.

## User-Facing Behavior

OCR Settings will be organized into two main sections: `메인` and `특정영역`.

`메인` keeps the existing OCR behavior:

- Keyword OCR on the current capture source, either window or monitor.
- Main OCR preprocessing variant checkboxes.
- Template matching settings.
- Main alert cooldown.

`특정영역` becomes a list of independent region rules:

- A `+ 특정영역 추가` button adds a new rule.
- Each rule is collapsed by default into a single summary row.
- Clicking a summary row expands it like a drawer.
- Expanded controls include:
  - Rule name.
  - Enabled toggle.
  - Region keywords.
  - Region selection status and buttons.
  - Region-specific OCR preprocessing variant checkboxes.
  - Color matching settings.
  - Region alert cooldown.
  - Custom alert sound path with file picker and a reset-to-default button.

Example layout:

```text
OCR Settings

[ 메인 ]
  ├─ 키워드 설정
  ├─ 전처리 설정
  └─ 템플릿 매칭

[ 특정영역 ]
  ├─ + 특정영역 추가

  ┌─ ▸ 영역 1    ON   키워드 2개   x=100 y=200 w=300 h=80   쿨다운 3.0초   기본음
  ├─ ▾ 영역 2    ON   키워드 1개   영역 미지정              쿨다운 1.5초   custom.wav
  │
  │   이름: [영역 2]
  │   사용: [x]
  │
  │   [키워드 설정]
  │   알림 키워드: [ ... ]
  │
  │   [특정영역]
  │   현재 영역: x, y, w, h
  │   [영역 지정 시작] [영역 비우기]
  │
  │   [전처리 설정]
  │   [x] 원본  [ ] 그레이/대비  [x] 이진화 ...
  │
  │   [색상 매칭 설정]
  │   [x] 색상 매칭 사용
  │   기준 색상: [#ff3030]
  │   허용 오차: [24]
  │   최소 비율: [3.0%]
  │
  │   [알림]
  │   쿨다운(초): [1.5]
  │   커스텀 알림음: [D:/sounds/custom.wav]
  │   [파일 선택] [기본 알림음 사용]
```

## Region Selection

Region selection must not depend on dragging inside the dashboard preview. When the user clicks `영역 지정 시작`, the app enters a separate region selection mode that lets the user drag over the actual target client, window, or monitor.

The saved rectangle is stored in capture-frame coordinates:

- `x`
- `y`
- `w`
- `h`

The region selector should clamp invalid rectangles and reject near-empty selections. If the active capture source changes size, region rules remain saved but detection clamps them to the current frame bounds.

## Data Model

Extend detection settings with a list of region rules.

```python
@dataclass
class RegionRect:
    x: int
    y: int
    w: int
    h: int


@dataclass
class RegionRuleSettings:
    id: str
    name: str
    enabled: bool = True
    keywords: str = ""
    rect: RegionRect | None = None
    ocr_variant_groups: tuple[str, ...] = ()
    color_match_enabled: bool = False
    color_hex: str = "#ff3030"
    color_tolerance: int = 24
    color_min_ratio: float = 3.0
    cooldown_sec: float = ALERT_COOLDOWN_DEFAULT
    custom_sound_path: str = ""
    expanded: bool = False
```

`DetectionSettings` keeps existing main OCR fields and adds:

```python
region_rules: tuple[RegionRuleSettings, ...] = ()
```

`DetectionConfig` should receive normalized runtime-friendly region rules:

- Parsed keywords as tuples.
- Rect values as integer tuples or a small dataclass.
- Region preprocessing groups.
- Color matching values.
- Cooldown and sound path can stay in UI state if sound dispatch stays in `AppState`, but detection results must include enough information to identify which rule triggered.

## Detection Flow

Each detection tick uses the latest capture frame.

```text
1. Run main OCR and template matching.
2. For each enabled region rule:
   - Skip if no rect or no active detection condition.
   - Clamp rect to frame bounds.
   - Crop frame to rect.
   - Run keyword OCR on the crop with the rule's own OCR variant groups.
   - Run color matching on the crop if enabled.
   - Return overlays mapped back to full-frame coordinates.
3. Combine main overlays and region overlays.
4. Trigger alert sound for each logical hit whose own cooldown allows it.
```

Region OCR reuses the existing keyword OCR implementation by passing the cropped frame and the rule-specific `ocr_variant_groups`. Region overlays returned from the cropped frame are offset by the region `x` and `y`.

## Alert Sound and Cooldown

Alert cooldowns are independent:

- Main OCR has its existing cooldown.
- Each region rule has its own cooldown.
- Region 1 triggering does not block Region 2.
- Main OCR triggering does not block any region rule.

When a region rule triggers:

- If `custom_sound_path` is set and playable, play that file.
- If it is empty or playback fails, play the default alert sound.

The default alert sound behavior remains unchanged for main OCR.

## Color Matching

Color matching is an additional condition inside a region rule.

The initial implementation should support:

- Enable/disable checkbox.
- Target color as hex.
- Per-channel tolerance.
- Minimum matching pixel ratio as a percentage of the cropped region.

A color match triggers the region rule even if OCR keywords do not match. If both OCR and color match trigger in one tick, the rule produces one alert event and one cooldown check.

## UI Details

The OCR Settings page should be refactored into smaller builder helpers:

- Main keyword section.
- Main preprocessing section.
- Main template section.
- Region rules list.
- Region rule collapsed row.
- Region rule expanded drawer.
- Shared OCR variant checkbox grid builder.

Collapsed region rows show:

- Expand/collapse indicator.
- Name.
- Enabled state.
- Keyword count.
- Region summary or `영역 미지정`.
- Cooldown.
- Sound summary.

Expanded drawers should be compact and row-based so multiple rules remain manageable.

## Persistence and Compatibility

Existing settings files must continue to load:

- Missing `region_rules` means no region rules.
- Missing main OCR fields keep current defaults.
- Existing `ocr_variant_groups` remains the main OCR preprocessing setting.

Saved JSON should include `region_rules` as a list of dictionaries.

## Testing

Add focused tests for pure logic where possible:

- Settings serialization and loading of region rules.
- Crop rect clamping.
- Region OCR overlay coordinate offsetting.
- Color matching threshold behavior.
- Independent cooldown bookkeeping.

Manual verification:

- Add multiple region rules.
- Expand and collapse drawers.
- Select or enter region rectangles.
- Configure different preprocessing variants for main and region OCR.
- Configure custom sound paths and default fallback.
- Confirm each rule's cooldown is independent.

## Open Implementation Notes

The separate region selection overlay is platform-sensitive. Implement the detection and settings model so manual coordinate entry or stored rectangles work first, then wire the platform selector behind the `영역 지정 시작` button. This keeps the core OCR behavior testable before the selection UI is attached.
