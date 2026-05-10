# PyInstaller: 다른 rthook·main 어떤 import 보다 먼저 실행되도록 spec 에서 맨 앞에 둠.
import os

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
