# PyInstaller: PATH/DLL 디렉터리 정리 후 onnxruntime 선로드.
import os
import sys
import traceback


def _write_rthook_log(title: str, exc: BaseException) -> None:
    if not getattr(sys, "frozen", False):
        return
    try:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        path = os.path.join(exe_dir, "pyi_rthook_onnx_error.txt")
        tb = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n=== {title} ===\n{tb}")
    except OSError:
        pass


try:
    from app_platform.bootstrap_onnx import apply as _bootstrap_onnx_apply

    _bootstrap_onnx_apply()
except Exception as e:
    _write_rthook_log("bootstrap_onnx.apply", e)

try:
    import onnxruntime  # noqa: F401
except Exception as e:
    _write_rthook_log("import onnxruntime", e)
