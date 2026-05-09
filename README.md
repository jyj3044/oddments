# Alert (cyj)

Windows에서 **특정 창 화면을 캡처**하고, **키워드 OCR**과 **템플릿 이미지 매칭**으로 이벤트를 감지하면 **알림음**을 재생하는 데스크톱 앱입니다.  
미리보기·설정은 **Tkinter** GUI로 제공합니다.

- **OCR 엔진**: Tesseract, EasyOCR, RapidOCR 중 UI에서 선택(복수 선택 가능).  
  한글 인식 보조를 위해 RapidOCR 사용 시 ONNX·사전 파일을 최초에 자동으로 내려받을 수 있습니다.
- **실행 파일 이름·창 제목**: 기본 `cyj` (`main.py`의 `APP_NAME`).
- **설정 저장**: `oddments_settings.json` (소스 실행 시 프로젝트 루트, **exe 실행 시 exe와 같은 폴더**). 예전 `alert_settings.json`은 첫 로드 시 자동으로 읽힌 뒤 이후 저장은 새 파일명으로 합니다.

---

## 필요 환경

- **OS**: Windows (캡처·알림은 Win32 기준으로 동작)
- **Python**: 3.10 이상 권장 (개발 시 3.12 등에서 검증)

---

## 개발 실행 및 빌드

가상환경·패키지 설치·`python main.py` 실행, Windows **exe**·macOS 번들 만드는 방법, 빌드 관련 파일 목록은 **[BUILD.md](BUILD.md)** 를 참고하세요.

---

## 라이선스

저장소에 별도 라이선스 파일이 없다면, 프로젝트 관리자에게 문의하세요.
