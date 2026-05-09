"""
로컬 캡처 프레임을 WebRTC로 송출하는 경량 서버.

- 신호 교환(signaling): aiohttp HTTP 엔드포인트
- 비디오: 앱 캡처 프레임(BGR ndarray) 공유
- 오디오: Windows WASAPI loopback(시스템 출력) 공유
"""

from __future__ import annotations

import asyncio
import json
import ssl
import sys
import threading
import time
from fractions import Fraction
from pathlib import Path
from typing import Optional

import av
import cv2
import numpy as np
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import AudioStreamTrack, VideoStreamTrack

from web_log import log_web_event


def build_web_stream_ssl_context(
    *,
    enabled: bool,
    certfile: str | None,
    keyfile: str | None,
) -> ssl.SSLContext | None:
    """HTTPS 송출용 TLS 서버 컨텍스트. 비활성이면 None(평문 HTTP)."""
    if not enabled:
        return None
    c = (certfile or "").strip()
    k = (keyfile or "").strip()
    if not c or not k:
        raise ValueError(
            "HTTPS를 사용하려면 인증서(cert)와 개인키(key) 파일 경로를 모두 지정하세요."
        )
    cp = Path(c).expanduser()
    kp = Path(k).expanduser()
    if not cp.is_file():
        raise ValueError(f"인증서 파일을 찾을 수 없습니다: {cp}")
    if not kp.is_file():
        raise ValueError(f"개인키 파일을 찾을 수 없습니다: {kp}")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cp), str(kp))
    return ctx


def _web_print(message: str) -> None:
    """콘솔 실행 시 접속 여부를 터미널에서도 확인할 수 있게 한다."""
    try:
        print(f"[웹송출] {message}", flush=True)
    except OSError:
        pass


def _resize_bgr_max_side(bgr: np.ndarray, max_side: int) -> np.ndarray:
    """긴 변이 max_side 를 넘지 않게 비율 유지 축소. max_side<=0 이면 원본."""
    if bgr.size == 0 or max_side <= 0:
        return bgr
    h, w = bgr.shape[:2]
    long_edge = max(w, h)
    if long_edge <= max_side:
        return bgr
    scale = float(max_side) / float(long_edge)
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    return cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_AREA)


def _even_dims_bgr(bgr: np.ndarray) -> np.ndarray:
    """VP8/H.264 인코더가 가로·세로 짝수를 요구하는 경우가 있어 맞춘다."""
    if bgr.size == 0:
        return bgr
    h, w = bgr.shape[:2]
    eh, ew = h - (h % 2), w - (w % 2)
    if eh == h and ew == w:
        return bgr
    if eh < 2 or ew < 2:
        return bgr
    return bgr[:eh, :ew]


_VIEWER_HTML = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <meta name="mobile-web-app-capable" content="yes" />
  <title></title>
  <style>
    html, body {
      margin: 0;
      width: 100%;
      min-height: 100%;
      min-height: 100dvh;
      min-height: -webkit-fill-available;
      background: #111;
      color: #ddd;
      font-family: sans-serif;
    }
    /* 모바일 크롬: html 자체 전체화면 시 주소줄까지 접히며 실제 폰 화면에 가깝게 */
    html:fullscreen,
    html:-webkit-full-screen {
      background: #000;
    }
    html:fullscreen body,
    html:-webkit-full-screen body {
      margin: 0;
      min-height: 100%;
      min-height: 100dvh;
      background: #000;
    }
    html:fullscreen #fsShell,
    html:-webkit-full-screen #fsShell {
      min-height: 100%;
      height: 100%;
      position: relative;
      box-sizing: border-box;
    }
    #fsShell {
      position: relative;
      width: 100%;
      min-height: 100vh;
      min-height: 100dvh;
      min-height: -webkit-fill-available;
      box-sizing: border-box;
    }
    #fsShell:fullscreen,
    #fsShell:-webkit-full-screen {
      width: 100%;
      height: 100%;
      min-height: 100%;
      min-height: 100dvh;
      background: #000;
      padding: env(safe-area-inset-top) env(safe-area-inset-right)
        env(safe-area-inset-bottom) env(safe-area-inset-left);
      box-sizing: border-box;
    }
    #stage {
      width: 100%;
      height: 100vh;
      height: 100dvh;
      background: #000;
      position: relative;
    }
    html:fullscreen #stage,
    html:-webkit-full-screen #stage,
    #fsShell:fullscreen #stage,
    #fsShell:-webkit-full-screen #stage,
    html.pseudo-fs-html #stage,
    #stage.pseudo-fs {
      position: absolute;
      left: 0;
      top: 0;
      right: 0;
      bottom: 0;
      width: 100% !important;
      height: 100% !important;
      height: 100dvh !important;
      max-height: none;
      z-index: 1;
    }
    html:fullscreen #stage,
    html:-webkit-full-screen #stage,
    #fsShell:fullscreen #stage,
    #fsShell:-webkit-full-screen #stage {
      height: 100% !important;
    }
    #stage video {
      display: block;
      width: 100%;
      height: 100%;
      object-fit: contain;
      background: #000;
      cursor: pointer;
      -webkit-transform: translateZ(0);
      transform: translateZ(0);
    }
    /* video 단독 전체화면(모바일 크롬): 웹 탭이 아니라 동영상 레이어에 가깝게 꽉 참 */
    @media (max-width: 900px) {
      video:fullscreen:not(.fs-rotate),
      video:-webkit-full-screen:not(.fs-rotate) {
        width: 100%;
        height: 100%;
        object-fit: cover;
      }
      html:fullscreen #stage:not(.fs-rotate) video,
      html:-webkit-full-screen #stage:not(.fs-rotate) video,
      #fsShell:fullscreen #stage:not(.fs-rotate) video,
      #fsShell:-webkit-full-screen #stage:not(.fs-rotate) video,
      html.pseudo-fs-html #stage:not(.fs-rotate) video,
      #stage.pseudo-fs:not(.fs-rotate) video {
        object-fit: cover;
      }
    }
    /* 세로로 든 기기: 스테이지 기준 전체화면 */
    #stage.fs-rotate video {
      position: absolute;
      left: 50%;
      top: 50%;
      width: 100vh;
      width: 100dvh;
      height: 100vw;
      max-width: none;
      max-height: none;
      transform: translate(-50%, -50%) rotate(90deg);
      object-fit: contain;
    }
    /* 세로로 든 기기: video 요소 자체가 전체화면 루트일 때 */
    video:fullscreen.fs-rotate,
    video:-webkit-full-screen.fs-rotate {
      position: absolute;
      left: 50%;
      top: 50%;
      width: 100vh;
      width: 100dvh;
      height: 100vw;
      max-width: none;
      max-height: none;
      transform: translate(-50%, -50%) rotate(90deg);
      object-fit: contain;
    }
    #stage.pseudo-fs {
      position: fixed !important;
      left: 0 !important;
      top: 0 !important;
      right: 0 !important;
      bottom: 0 !important;
      z-index: 150;
    }
    html.pseudo-fs-html {
      overflow: hidden;
      height: 100%;
    }
    html.pseudo-fs-html body {
      overflow: hidden;
      margin: 0;
      min-height: 100dvh;
      touch-action: none;
    }
    #fsbtn {
      position: fixed;
      top: max(10px, env(safe-area-inset-top));
      right: max(10px, env(safe-area-inset-right));
      z-index: 200;
      padding: 8px 12px;
      font: 14px sans-serif;
      border: 1px solid #555;
      border-radius: 8px;
      background: rgba(0, 0, 0, 0.55);
      color: #eee;
      cursor: pointer;
      -webkit-tap-highlight-color: transparent;
    }
    #fsbtn:hover { background: rgba(40, 40, 40, 0.88); }
  </style>
</head>
<body>
  <div id="fsShell">
    <button type="button" id="fsbtn" aria-label="전체화면">전체화면</button>
    <div id="stage">
      <video id="v" autoplay muted playsinline webkit-playsinline></video>
    </div>
    <p id="sndhint" style="position:fixed;bottom:max(8px,env(safe-area-inset-bottom));left:0;right:0;margin:0;text-align:center;font:12px sans-serif;color:#888;pointer-events:none;z-index:180;">
      첫 클릭: 소리 허용 · 이후 클릭: 재생 ↔ 일시정지
    </p>
  </div>
  <audio id="a" autoplay muted playsinline style="width:0;height:0;opacity:0;position:fixed;pointer-events:none"></audio>
  <script>
    async function start() {
      const pc = new RTCPeerConnection({
        iceServers: [{ urls: ["stun:stun.l.google.com:19302"] }]
      });
      const video = document.getElementById("v");
      const audio = document.getElementById("a");
      const sndHint = document.getElementById("sndhint");
      const fsBtn = document.getElementById("fsbtn");
      const fsShell = document.getElementById("fsShell");
      const stage = document.getElementById("stage");
      let pseudoFs = false;
      let prevImmersive = false;

      video.muted = true;
      video.volume = 1.0;
      video.setAttribute("playsinline", "");
      video.setAttribute("webkit-playsinline", "");
      video.playsInline = true;
      audio.muted = true;
      audio.volume = 1.0;
      audio.setAttribute("playsinline", "");
      let soundUnlocked = false;
      let userHeldPause = false;

      async function tryPlay() {
        try {
          await video.play();
        } catch (e) {
          console.warn("v play", e);
        }
        try {
          await audio.play();
        } catch (e) {
          console.warn("a play", e);
        }
      }

      function tryResumePipeline() {
        if (!userHeldPause) tryPlay();
      }

      /* 모바일에서 전체화면 해제 직후 브라우저가 잠깐 멈출 때만 보강 (진입 시에는 호출 안 함) */
      function resumePlaybackAfterMobileExitFullscreen() {
        if (userHeldPause) return;
        const kick = () => {
          if (!userHeldPause) tryPlay();
        };
        kick();
        requestAnimationFrame(kick);
        setTimeout(kick, 0);
        setTimeout(kick, 60);
        setTimeout(kick, 180);
        setTimeout(kick, 450);
        setTimeout(kick, 900);
      }

      function isMobileLike() {
        if (!window.matchMedia("(max-width: 900px)").matches) return false;
        return "ontouchstart" in window || (navigator.maxTouchPoints || 0) > 0;
      }

      function tryUnlockOrientation() {
        try {
          const o = screen.orientation;
          if (o && typeof o.unlock === "function") o.unlock();
        } catch (_) {}
      }

      async function tryLockLandscape() {
        try {
          const o = screen.orientation;
          if (o && typeof o.lock === "function") {
            await o.lock("landscape");
            return true;
          }
        } catch (_) {}
        return false;
      }

      function immersiveActive() {
        return !!(
          document.fullscreenElement ||
          document.webkitFullscreenElement ||
          pseudoFs
        );
      }

      function updateStageRotate() {
        if (!immersiveActive()) {
          stage?.classList.remove("fs-rotate");
          video?.classList.remove("fs-rotate");
          return;
        }
        const fsEl =
          document.fullscreenElement || document.webkitFullscreenElement;
        const portrait =
          isMobileLike() &&
          window.matchMedia("(orientation: portrait)").matches;
        if (!portrait) {
          stage?.classList.remove("fs-rotate");
          video?.classList.remove("fs-rotate");
          return;
        }
        if (fsEl === video) {
          video.classList.add("fs-rotate");
          stage?.classList.remove("fs-rotate");
        } else {
          stage?.classList.add("fs-rotate");
          video.classList.remove("fs-rotate");
        }
      }

      function syncFsLabel() {
        if (!fsBtn) return;
        fsBtn.textContent = immersiveActive()
          ? "전체화면 종료"
          : "전체화면";
      }

      function onFullscreenEvent() {
        syncFsLabel();
        updateStageRotate();
        const fsEl =
          document.fullscreenElement || document.webkitFullscreenElement;
        if (!fsEl) tryUnlockOrientation();
        const now = immersiveActive();
        if (isMobileLike() && prevImmersive && !now) {
          resumePlaybackAfterMobileExitFullscreen();
        }
        prevImmersive = now;
      }

      async function toggleFullscreen(ev) {
        if (ev) {
          ev.preventDefault();
          ev.stopPropagation();
        }
        try {
          const nativeFs = !!(
            document.fullscreenElement || document.webkitFullscreenElement
          );
          const on = nativeFs || pseudoFs;
          if (!on) {
            const pickFsTarget = () => {
              if (isMobileLike()) {
                const portrait = window.matchMedia(
                  "(orientation: portrait)"
                ).matches;
                /*
                  세로(portrait): 영상 90deg 회전은 <video> 네이티브 전체화면 레이어에서
                  CSS transform 이 무시되는 경우가 많음 → 셸/#stage 경로에서만 안정적.
                  가로(landscape): 탭 UI 없이 꽉 채우려면 video 단독 전체화면이 유리.
                */
                if (portrait) {
                  if (fsShell && fsShell.requestFullscreen) return fsShell;
                  if (fsShell && fsShell.webkitRequestFullscreen) return fsShell;
                  const root = document.documentElement;
                  if (root.requestFullscreen) return root;
                  if (root.webkitRequestFullscreen) return root;
                } else {
                  if (video && video.requestFullscreen) return video;
                  if (video && video.webkitRequestFullscreen) return video;
                  const root = document.documentElement;
                  if (root.requestFullscreen) return root;
                  if (root.webkitRequestFullscreen) return root;
                }
              }
              if (fsShell) {
                if (fsShell.requestFullscreen) return fsShell;
                if (fsShell.webkitRequestFullscreen) return fsShell;
              }
              if (stage) {
                if (stage.requestFullscreen) return stage;
                if (stage.webkitRequestFullscreen) return stage;
              }
              return null;
            };
            const fsTarget = pickFsTarget();
            if (fsTarget) {
              try {
                if (fsTarget.requestFullscreen) {
                  await fsTarget.requestFullscreen();
                } else if (fsTarget.webkitRequestFullscreen) {
                  fsTarget.webkitRequestFullscreen();
                }
                if (
                  isMobileLike() &&
                  !window.matchMedia("(orientation: portrait)").matches
                ) {
                  await tryLockLandscape();
                }
              } catch (err) {
                console.warn("fullscreen", err);
                if (isMobileLike() && stage) {
                  pseudoFs = true;
                  stage.classList.add("pseudo-fs");
                  document.documentElement.classList.add("pseudo-fs-html");
                }
              }
            } else if (
              isMobileLike() &&
              video &&
              typeof video.webkitEnterFullscreen === "function"
            ) {
              try {
                video.webkitEnterFullscreen();
              } catch (err) {
                console.warn("webkitEnterFullscreen", err);
              }
            } else if (isMobileLike() && stage) {
              pseudoFs = true;
              stage.classList.add("pseudo-fs");
              document.documentElement.classList.add("pseudo-fs-html");
            }
            updateStageRotate();
            prevImmersive = immersiveActive();
          } else {
            const wasPseudoOnly = pseudoFs && !nativeFs;
            tryUnlockOrientation();
            if (pseudoFs && stage) {
              pseudoFs = false;
              stage.classList.remove("pseudo-fs", "fs-rotate");
              document.documentElement.classList.remove("pseudo-fs-html");
            }
            if (nativeFs) {
              if (document.exitFullscreen) {
                await document.exitFullscreen();
              } else if (document.webkitExitFullscreen) {
                document.webkitExitFullscreen();
              }
            }
            updateStageRotate();
            /* native 종료는 fullscreenchange → onFullscreenEvent 에서 모바일 재생 복구 */
            /* pseudo 전체화면은 fullscreenchange가 없어 여기서만 복구 */
            if (wasPseudoOnly && isMobileLike()) {
              resumePlaybackAfterMobileExitFullscreen();
            }
            prevImmersive = immersiveActive();
          }
        } catch (err) {
          console.warn("fullscreen", err);
        }
        syncFsLabel();
      }

      if (fsBtn) {
        fsBtn.addEventListener("click", toggleFullscreen);
        document.addEventListener("fullscreenchange", onFullscreenEvent);
        document.addEventListener("webkitfullscreenchange", onFullscreenEvent);
      }
      window.addEventListener("orientationchange", () => {
        setTimeout(updateStageRotate, 80);
      });
      window.addEventListener("resize", () => {
        setTimeout(updateStageRotate, 50);
      });

      video.addEventListener("play", () => {
        userHeldPause = false;
        audio.play().catch(() => {});
      });
      video.addEventListener("pause", () => {
        audio.pause();
      });

      video.addEventListener("volumechange", () => {
        audio.muted = video.muted;
        audio.volume = video.volume;
      });

      video.addEventListener("canplay", () => { tryResumePipeline(); });
      audio.addEventListener("canplay", () => { tryResumePipeline(); });

      ["stalled", "waiting", "suspend"].forEach((ev) => {
        video.addEventListener(ev, () => {
          if (!userHeldPause && !video.paused) tryPlay();
        });
        audio.addEventListener(ev, () => {
          if (!userHeldPause && !video.paused) tryPlay();
        });
      });

      document.addEventListener("visibilitychange", () => {
        if (!document.hidden && !userHeldPause && !video.paused) tryPlay();
      });

      function onWindowClick() {
        if (!soundUnlocked) {
          soundUnlocked = true;
          video.muted = false;
          audio.muted = false;
          video.volume = 1.0;
          audio.volume = 1.0;
          userHeldPause = false;
          if (sndHint) {
            sndHint.textContent = "클릭/탭: 재생 ↔ 일시정지";
            sndHint.style.color = "#aaa";
          }
          tryPlay();
          return;
        }
        if (video.paused) {
          userHeldPause = false;
          tryPlay();
        } else {
          userHeldPause = true;
          video.pause();
          audio.pause();
        }
      }

      pc.addTransceiver("video", { direction: "recvonly" });
      pc.addTransceiver("audio", { direction: "recvonly" });
      /* 단일 MediaStream: iOS Safari 등에서 오디오 트랙 라우팅이 더 안정적 */
      const inbound = new MediaStream();
      pc.ontrack = (ev) => {
        ev.track.enabled = true;
        inbound.addTrack(ev.track);
        video.srcObject = inbound;
        audio.srcObject = inbound;
        tryPlay();
        requestAnimationFrame(() => tryPlay());
      };
      pc.onconnectionstatechange = () => {
        if (pc.connectionState === "connected") tryResumePipeline();
      };
      pc.oniceconnectionstatechange = () => {
        const s = pc.iceConnectionState;
        if (s === "connected" || s === "completed") tryResumePipeline();
      };

      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      const resp = await fetch("/offer", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ sdp: offer.sdp, type: offer.type })
      });
      if (!resp.ok) throw new Error("offer failed: " + resp.status);
      const answer = await resp.json();
      await pc.setRemoteDescription(answer);
      tryResumePipeline();

      window.addEventListener("click", onWindowClick);
      syncFsLabel();
    }
    start().catch((e) => {
      console.error(e);
      alert("연결 실패: " + e.message);
    });
  </script>
</body>
</html>
"""


class SharedVideoBuffer:
    """캡처 스레드가 최신 BGR 프레임을 기록하고 트랙이 읽는 공유 버퍼."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: Optional[np.ndarray] = None

    def push(self, frame: np.ndarray) -> None:
        with self._lock:
            self._latest = frame.copy()

    def snapshot(self) -> Optional[np.ndarray]:
        with self._lock:
            if self._latest is None:
                return None
            return self._latest.copy()


class SharedAudioBuffer:
    """오디오 캡처 스레드가 PCM 청크를 퍼블리시하는 구독형 버퍼."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: list[asyncio.Queue[np.ndarray]] = []

    def subscribe(self) -> asyncio.Queue[np.ndarray]:
        q: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=8)
        with self._lock:
            self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[np.ndarray]) -> None:
        with self._lock:
            if q in self._queues:
                self._queues.remove(q)

    def publish(self, chunk: np.ndarray) -> None:
        with self._lock:
            queues = list(self._queues)
        for q in queues:
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                pass


class SharedVideoTrack(VideoStreamTrack):
    """공유 버퍼의 최신 프레임을 일정 FPS로 송출하는 WebRTC 비디오 트랙."""

    def __init__(
        self,
        shared: SharedVideoBuffer,
        fps: float = 20.0,
        *,
        max_stream_side: int = 0,
    ) -> None:
        super().__init__()
        self._shared = shared
        self._fps = max(5.0, min(60.0, float(fps)))
        self._max_stream_side = max(0, int(max_stream_side))
        self._last = np.zeros((360, 640, 3), dtype=np.uint8)
        self._pts = 0
        self._time_base = Fraction(1, 90000)
        self._pts_step = max(1, int(round(90000.0 / self._fps)))

    async def recv(self) -> av.VideoFrame:
        await asyncio.sleep(1.0 / self._fps)
        snap = self._shared.snapshot()
        if snap is not None and snap.size:
            resized = _resize_bgr_max_side(snap, self._max_stream_side)
            self._last = np.ascontiguousarray(_even_dims_bgr(resized))
        rgb = cv2.cvtColor(self._last, cv2.COLOR_BGR2RGB)
        vf = av.VideoFrame.from_ndarray(
            np.ascontiguousarray(rgb), format="rgb24"
        )
        vf.pts = self._pts
        vf.time_base = self._time_base
        self._pts += self._pts_step
        return vf


class SharedAudioTrack(AudioStreamTrack):
    """구독 큐에서 PCM 청크를 받아 송출하는 WebRTC 오디오 트랙."""

    def __init__(self, shared: SharedAudioBuffer, sample_rate: int = 48000) -> None:
        super().__init__()
        self._shared = shared
        self._q = shared.subscribe()
        self._sample_rate = int(sample_rate)
        self._pts = 0

    async def recv(self) -> av.AudioFrame:
        # 오디오 loopback 실패·지연 시 큐가 비면 MediaStreamError 로 세션이 끊겨 영상도 까맣게 보일 수 있음 → 무음 유지
        try:
            chunk = await asyncio.wait_for(self._q.get(), timeout=1.0)
        except asyncio.TimeoutError:
            chunk = np.zeros((960, 2), dtype=np.float32)
        if chunk.ndim != 2 or chunk.shape[1] != 2:
            chunk = np.zeros((960, 2), dtype=np.float32)
        pcm = np.clip(chunk, -1.0, 1.0)
        # 모노 Opus: Safari·모바일 WebRTC 호환에 유리
        mono = np.mean(pcm, axis=1)
        pcm_s16 = (mono * 32767.0).astype(np.int16)
        af = av.AudioFrame.from_ndarray(
            pcm_s16.reshape(1, -1), format="s16", layout="mono"
        )
        af.sample_rate = self._sample_rate
        af.pts = self._pts
        af.time_base = Fraction(1, self._sample_rate)
        self._pts += int(pcm_s16.shape[0])
        return af

    def stop(self) -> None:  # type: ignore[override]
        self._shared.unsubscribe(self._q)
        super().stop()


# loopback 블록당 ~20ms(960/48k). 워밍업 후에만 무음 경고(오탐 방지).
_AUDIO_LOOPBACK_WARMUP_CHUNKS = 300  # ~6s
_AUDIO_SILENT_STREAK_CHUNKS = 200  # ~4s 연속 무음


def list_web_stream_audio_outputs() -> list[str]:
    """Windows WASAPI 재생(출력) 장치 이름 목록. 웹 송출 루프백 소스 선택용."""
    if sys.platform != "win32":
        return []
    try:
        import soundcard as sc
    except ImportError:
        return []
    try:
        speakers = list(sc.all_speakers())  # type: ignore[attr-defined]
    except Exception:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for sp in speakers:
        n = str(getattr(sp, "name", "")).strip()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    out.sort(key=lambda s: s.casefold())
    return out


def _pick_loopback_microphone(
    sc: object,
    *,
    output_speaker_name: Optional[str] = None,
) -> tuple[object | None, Optional[str]]:
    """재생 장치 이름에 대응하는 loopback 마이크 선택. 이름 없으면 Windows 기본 출력."""
    explicit = (output_speaker_name or "").strip()
    if explicit:
        try:
            all_sp = list(sc.all_speakers())  # type: ignore[attr-defined]
        except Exception as e:
            return None, f"스피커 열거 실패: {e}"
        if not any(str(sp.name) == explicit for sp in all_sp):
            return (
                None,
                "선택한 출력 장치를 찾을 수 없습니다(목록 새로고침 또는 다른 장치 선택).",
            )
        anchor_name = explicit
    else:
        try:
            speaker = sc.default_speaker()  # type: ignore[attr-defined]
        except Exception as e:
            return None, f"default_speaker 실패: {e}"
        if speaker is None:
            return None, "기본 스피커를 찾을 수 없습니다."
        anchor_name = str(speaker.name)

    mic = None
    try:
        mic = sc.get_microphone(anchor_name, include_loopback=True)  # type: ignore[attr-defined]
    except Exception:
        mic = None
    if mic is not None:
        return mic, None
    try:
        all_m = list(sc.all_microphones(include_loopback=True))  # type: ignore[attr-defined]
    except Exception as e:
        return None, f"마이크 열거 실패: {e}"
    if not all_m:
        return None, "WASAPI loopback 장치를 찾을 수 없습니다."
    name_l = anchor_name.lower()
    tokens = [
        t
        for t in anchor_name.replace("(", " ").replace(")", " ").split()
        if len(t) >= 4
    ]
    loopbacks = [
        m
        for m in all_m
        if getattr(m, "isloopback", False)
        or "loopback" in str(m.name).lower()
    ]
    pool = loopbacks if loopbacks else all_m
    for m in pool:
        mn = str(m.name).lower()
        if name_l in mn or mn in name_l:
            return m, None
    for t in tokens:
        tl = t.lower()
        for m in pool:
            if tl in str(m.name).lower():
                return m, None
    return (loopbacks[0] if loopbacks else all_m[0]), None


class WebStreamServer:
    """외부 접속 가능한 WebRTC 수신 페이지와 트랙 송출을 제공한다."""

    @staticmethod
    def _peer_display(req: web.Request) -> str:
        xff = req.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        peer = req.remote
        if peer:
            return f"{peer[0]}:{peer[1]}"
        return "?"

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 8787,
        fps: float = 20.0,
        max_stream_side: int = 720,
        audio_output_name: Optional[str] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
    ) -> None:
        self.host = str(host).strip() or "0.0.0.0"
        self.port = int(port)
        self.fps = float(fps)
        self.max_stream_side = max(0, min(4096, int(max_stream_side)))
        aon = (audio_output_name or "").strip()
        self._audio_output_name: Optional[str] = aon if aon else None
        self._ssl_context: Optional[ssl.SSLContext] = ssl_context
        self.video = SharedVideoBuffer()
        self.audio = SharedAudioBuffer()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._pcs: set[RTCPeerConnection] = set()
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.BaseSite] = None
        self._audio_stop = threading.Event()
        self._audio_thread: Optional[threading.Thread] = None
        self._audio_error: Optional[str] = None
        self._audio_mic_name: str = ""
        self._audio_low_signal: bool = False
        self._audio_low_signal_logged: bool = False
        self._viewer_lock = threading.Lock()
        self._viewer_count: int = 0

    @property
    def uses_tls(self) -> bool:
        return self._ssl_context is not None

    def get_connected_viewer_count(self) -> int:
        """WebRTC `connectionState === connected` 인 피어 수 (스레드 안전)."""
        with self._viewer_lock:
            return int(self._viewer_count)

    def _sync_viewer_count(self) -> None:
        try:
            n = sum(
                1
                for p in list(self._pcs)
                if getattr(p, "connectionState", "") == "connected"
            )
        except Exception:
            n = 0
        with self._viewer_lock:
            self._viewer_count = n

    def push_video_frame(self, frame: np.ndarray) -> None:
        self.video.push(frame)

    def get_audio_error(self) -> Optional[str]:
        return self._audio_error

    def get_audio_status_line(self) -> str:
        """설정 창용: 루프백 장치·무음 의심 등 한 줄 상태."""
        err = self._audio_error
        if err:
            return f"오디오: 오류 — {err}"
        if self._audio_mic_name:
            if self._audio_low_signal:
                return (
                    f"오디오: 신호 매우 약함 ({self._audio_mic_name}) — "
                    "이 PC에서 소리가 재생 중인지, 기본 출력 장치를 확인하세요."
                )
            return f"오디오: 루프백 캡처 중 ({self._audio_mic_name})"
        return "오디오: 루프백 초기화 중…"

    def start(self) -> None:
        if self._thread is not None:
            if self._thread.is_alive():
                return
            # 이전 스레드가 예외로 종료되어 포트만 점유된 상태일 수 있음 → 정리 후 재바인드
            try:
                self.stop()
            except Exception:
                pass
            time.sleep(0.2)
        self._audio_error = None
        self._audio_mic_name = ""
        self._audio_low_signal = False
        self._audio_low_signal_logged = False
        self._audio_stop.clear()
        self._audio_thread = threading.Thread(
            target=self._audio_capture_loop,
            daemon=True,
            name="Oddments-WebAudio",
        )
        self._audio_thread.start()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="Oddments-WebRTC",
        )
        self._thread.start()
        timeout_at = time.time() + 4.0
        while time.time() < timeout_at:
            if self._loop is not None:
                return
            time.sleep(0.03)
        raise RuntimeError("웹 송출 서버 시작에 실패했습니다.")

    def stop(self) -> None:
        self._audio_stop.set()
        if self._audio_thread is not None and self._audio_thread.is_alive():
            self._audio_thread.join(timeout=1.0)
        self._audio_thread = None
        loop = self._loop
        if loop is not None:
            fut = asyncio.run_coroutine_threadsafe(self._shutdown_async(), loop)
            try:
                fut.result(timeout=4.0)
            except Exception:
                pass
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        self._loop = None
        with self._viewer_lock:
            self._viewer_count = 0
        self._audio_mic_name = ""
        self._audio_low_signal = False
        self._audio_low_signal_logged = False

    def _audio_capture_loop(self) -> None:
        try:
            import soundcard as sc
        except Exception as e:
            self._audio_error = f"soundcard import 실패: {e}"
            return
        try:
            mic, pick_err = _pick_loopback_microphone(
                sc,
                output_speaker_name=self._audio_output_name,
            )
            if mic is None:
                self._audio_error = pick_err or "WASAPI loopback 장치를 찾을 수 없습니다."
                return
            mic_name = str(getattr(mic, "name", "?"))
            self._audio_mic_name = mic_name
            if self._audio_output_name:
                msg = (
                    f"오디오 루프백 캡처 장치: {mic_name} "
                    f"(선택한 재생 출력: {self._audio_output_name})"
                )
            else:
                msg = (
                    f"오디오 루프백 캡처 장치: {mic_name} "
                    "(Windows 기본 재생 출력)"
                )
            log_web_event(msg)
            _web_print(msg)
            silent_run = 0
            chunk_i = 0
            with mic.recorder(samplerate=48000, channels=2, blocksize=960) as rec:
                while not self._audio_stop.is_set():
                    data = rec.record(numframes=960)
                    if data is None:
                        continue
                    arr = np.asarray(data, dtype=np.float32)
                    if arr.ndim == 1:
                        arr = np.repeat(arr[:, None], 2, axis=1)
                    if arr.shape[1] == 1:
                        arr = np.repeat(arr, 2, axis=1)
                    if arr.shape[1] > 2:
                        arr = arr[:, :2]
                    chunk_i += 1
                    peak = float(np.max(np.abs(arr)))
                    rms = float(np.sqrt(np.mean(arr * arr)))
                    # RMS·피크 둘 다 매우 낮을 때만 무음으로 간주(아주 작은 소리는 오탐 방지)
                    near_silence = rms < 8e-5 and peak < 3e-4
                    past_warmup = chunk_i > _AUDIO_LOOPBACK_WARMUP_CHUNKS
                    if not near_silence:
                        silent_run = 0
                        self._audio_low_signal = False
                    elif past_warmup:
                        silent_run += 1
                    if (
                        past_warmup
                        and silent_run >= _AUDIO_SILENT_STREAK_CHUNKS
                        and not self._audio_low_signal_logged
                    ):
                        self._audio_low_signal_logged = True
                        self._audio_low_signal = True
                        warn = (
                            "오디오 루프백 레벨이 거의 없습니다. "
                            "이 PC에서 소리가 실제로 재생 중인지, "
                            "Windows 기본 출력(스피커/헤드폰)이 맞는지 확인하세요."
                        )
                        log_web_event(warn)
                        _web_print(warn)
                    self.audio.publish(arr.copy())
        except Exception as e:
            self._audio_error = f"오디오 loopback 시작 실패: {e}"

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._startup_async())
        except OSError as e:
            msg = f"웹 송출 포트 바인드 실패 ({self.host}:{self.port}): {e}"
            log_web_event(msg)
            _web_print(msg)
            try:
                loop.run_until_complete(self._shutdown_async())
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None
            return
        self._loop = loop
        loop.run_forever()
        loop.run_until_complete(self._shutdown_async())
        loop.close()

    async def _startup_async(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._handle_index)
        app.router.add_post("/offer", self._handle_offer)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        last_os_err: OSError | None = None
        for attempt in range(6):
            self._site = web.TCPSite(
                self._runner,
                host=self.host,
                port=self.port,
                reuse_address=True,
                ssl_context=self._ssl_context,
            )
            try:
                await self._site.start()
                return
            except OSError as e:
                last_os_err = e
                if self._site is not None:
                    try:
                        await self._site.stop()
                    except Exception:
                        pass
                self._site = None
                await asyncio.sleep(0.2 + 0.15 * attempt)
        if last_os_err is not None:
            raise last_os_err

    async def _shutdown_async(self) -> None:
        pcs = list(self._pcs)
        self._pcs.clear()
        for pc in pcs:
            try:
                await pc.close()
            except Exception:
                pass
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
        self._runner = None
        self._site = None

    async def _handle_index(self, req: web.Request) -> web.Response:
        ua = (req.headers.get("User-Agent") or "").replace("\n", " ")[:80]
        extra = f" {ua}" if ua else ""
        msg = f"뷰어 접속 GET / peer={self._peer_display(req)}{extra}"
        log_web_event(msg)
        _web_print(msg)
        return web.Response(text=_VIEWER_HTML, content_type="text/html")

    async def _handle_offer(self, req: web.Request) -> web.Response:
        peer_s = self._peer_display(req)
        msg0 = f"offer(signaling) peer={peer_s}"
        log_web_event(msg0)
        _web_print(msg0)
        params = await req.json()
        offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

        pc = RTCPeerConnection()
        self._pcs.add(pc)

        @pc.on("connectionstatechange")
        async def _on_state_change() -> None:
            st = pc.connectionState
            if st == "connected":
                msg1 = f"시청 연결됨 peer={peer_s}"
                log_web_event(msg1)
                _web_print(msg1)
            if st in ("failed", "closed", "disconnected"):
                self._pcs.discard(pc)
                try:
                    await pc.close()
                except Exception:
                    pass
            self._sync_viewer_count()

        # Offer의 m-line·mid와 맞추려면 반드시 setRemoteDescription 후 addTrack (aiortc는 빈 sender 슬롯에 replace)
        await pc.setRemoteDescription(offer)
        pc.addTrack(
            SharedVideoTrack(
                self.video,
                fps=self.fps,
                max_stream_side=self.max_stream_side,
            )
        )
        pc.addTrack(SharedAudioTrack(self.audio, sample_rate=48000))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        self._sync_viewer_count()
        return web.Response(
            text=json.dumps(
                {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
            ),
            content_type="application/json",
        )
