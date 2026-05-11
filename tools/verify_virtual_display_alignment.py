#!/usr/bin/env python3
"""macOS 전용: 가상 디스플레이 ↔ mss 좌표계 정합 검증.

  프로젝트 루트에서:
    python tools/verify_virtual_display_alignment.py
    python tools/verify_virtual_display_alignment.py --preset qhd_16_9

  성공 시 CGDisplayBounds 와 mss 모니터 rect 가 허용 오차 안에서 일치하는지 출력하고,
  가상 디스플레이를 제거한다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 프로젝트 루트를 path 에 넣는다.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:
    if sys.platform != "darwin":
        print("이 스크립트는 macOS 에서만 실행할 수 있습니다.", file=sys.stderr)
        return 2

    ap = argparse.ArgumentParser(description="가상 디스플레이·mss 정합 검증")
    ap.add_argument(
        "--preset",
        default="fhd_16_9",
        help="streaming.remote_presets 의 preset id (기본 fhd_16_9)",
    )
    ap.add_argument(
        "--tol",
        type=int,
        default=16,
        help="좌표·크기 비교 허용 픽셀 오차",
    )
    args = ap.parse_args()

    from capture.thread import enumerate_monitors
    from streaming.remote_presets import normalize_preset_id, preset_dimensions

    from app_platform.darwin_virtual_display import (
        DarwinVirtualDisplayError,
        cg_display_bounds,
        create_virtual_display,
        release_virtual_display,
    )

    pid = normalize_preset_id(args.preset, fallback="fhd_16_9")

    def _host_native() -> tuple[int, int]:
        try:
            from AppKit import NSScreen  # type: ignore[import-untyped]

            scr = NSScreen.mainScreen()
            if scr is None:
                return 1920, 1080
            f = scr.frame()
            s = float(scr.backingScaleFactor())
            return (
                max(320, int(round(float(f.size.width) * s))),
                max(240, int(round(float(f.size.height) * s))),
            )
        except Exception:
            return 1920, 1080

    w, h = preset_dimensions(pid, host_native=_host_native)
    print(f"preset={pid} → 생성 크기 {w}×{h}")

    vd = None
    did = 0
    try:
        vd, did = create_virtual_display(w, h, refresh_hz=60.0)
    except DarwinVirtualDisplayError as exc:
        print(f"가상 디스플레이 생성 실패: {exc}", file=sys.stderr)
        return 1

    try:
        bx, by, bw, bh = cg_display_bounds(int(did))
        left = int(round(bx))
        top = int(round(by))
        gw = max(1, int(round(bw)))
        gh = max(1, int(round(bh)))
        print(f"CGDisplayBounds({did}): left={left} top={top} w={gw} h={gh}")

        tol = max(0, int(args.tol))
        hit = None
        for m in enumerate_monitors():
            try:
                if (
                    abs(int(m.get("left", 0)) - left) <= tol
                    and abs(int(m.get("top", 0)) - top) <= tol
                    and abs(int(m.get("width", 0)) - gw) <= tol
                    and abs(int(m.get("height", 0)) - gh) <= tol
                ):
                    hit = m
                    break
            except (TypeError, ValueError, KeyError):
                continue

        if hit is None:
            print("FAIL: mss 모니터 목록에서 동일 rect 를 찾지 못했습니다.", file=sys.stderr)
            print("mss monitors:", enumerate_monitors(), file=sys.stderr)
            return 1

        print(
            f"OK: mss index={hit.get('index')} "
            f"rect=({hit['left']},{hit['top']},{hit['width']}×{hit['height']})"
        )

        # 원격 호스트와 동일: 이 rect 로 정규 좌표 0..1 → 절대 픽셀 매핑이 일관됨.
        print(
            "원격 입력 매핑: nx,ny ∈ [0,1] → "
            f"x=left+nx*gw={left}+nx*{gw}, y=top+ny*gh={top}+ny*{gh} "
            "(가상 VD 모드에서는 pointer_scale=1.0 유지 권장)"
        )
        return 0
    finally:
        release_virtual_display(vd)


if __name__ == "__main__":
    raise SystemExit(main())
