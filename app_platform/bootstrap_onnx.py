"""
PyInstaller exe: onnxruntime·기타 네이티브 DLL 검색 경로 정리.
onedir 에서 numpy.libs / Shapely.libs / cv2 등에만 있는 DLL 을
add_dll_directory 에 안 넣으면 onnxruntime_pybind11_state.pyd 초기화가 실패할 수 있음.
"""
from __future__ import annotations

import os
import sys

_applied = False


def apply() -> None:
    global _applied
    if _applied:
        return
    if not getattr(sys, "frozen", False):
        return
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return
    base = os.path.normpath(os.path.abspath(base))

    if sys.platform != "win32":
        os.environ["PATH"] = base + os.pathsep + os.environ.get("PATH", "")
        _applied = True
        return

    try:
        import ctypes
    except ImportError:
        ctypes = None  # type: ignore

    try:
        add = os.add_dll_directory  # type: ignore[attr-defined]
    except AttributeError:
        add = None

    seen_dirs: set[str] = set()
    path_prefix: list[str] = []

    def register_dir(d: str) -> None:
        ap = os.path.normpath(os.path.abspath(d))
        if ap in seen_dirs or not os.path.isdir(ap):
            return
        seen_dirs.add(ap)
        path_prefix.append(ap)
        if add is not None:
            try:
                add(ap)
            except OSError:
                pass

    capi = os.path.join(base, "onnxruntime", "capi")
    if os.path.isdir(capi):
        register_dir(capi)
    register_dir(base)

    _skip_subdirs = frozenset(
        {"torch", "torchvision", "torchaudio", "functorch"}
    )

    extra_dll_dirs: list[str] = []
    for root, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d.lower() not in _skip_subdirs]
        if any(fn.lower().endswith(".dll") for fn in filenames):
            ap = os.path.normpath(os.path.abspath(root))
            if ap not in seen_dirs:
                extra_dll_dirs.append(root)

    for root in sorted(extra_dll_dirs, key=lambda p: p.replace("\\", "/").lower()):
        register_dir(root)

    new_head = os.pathsep.join(path_prefix)
    if new_head:
        os.environ["PATH"] = new_head + os.pathsep + os.environ.get("PATH", "")

    if ctypes is not None:
        for name in ("onnxruntime_providers_shared.dll", "onnxruntime.dll"):
            for d in list(seen_dirs):
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    try:
                        ctypes.WinDLL(p)
                    except OSError:
                        pass

    _applied = True
