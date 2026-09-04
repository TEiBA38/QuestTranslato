# 🚨 Quest Translator Pro v1.5.3-hotfix 릴리즈 노트

이번 **v1.5.3-hotfix** 업데이트는 대규모 모드팩 번역 및 자동 업데이트 환경에서 발생하던 **"번역 안됨", "파일 잠김", "재시작 보안 오류"를 완벽하게 수정한 긴급 안정화 패치**입니다.

---

## 🛠️ 긴급 수정 사항 (Critical Hotfix Highlights)

### 1. 🛑 "번역 안됨" 오류 완전 해결 (변수 스코프 섀도잉 버그)
* **발생 현상**:
  * 언어 파일(.lang) 또는 커스텀 가이드북(XNet, Forestry, OpenComputers 등) 번역 시작 시 `cannot access local variable 'translation_memory' where it is not associated with a value` 또는 `cannot access free variable 'translation_memory' where it is not associated with a value in enclosing scope` 에러와 함께 번역이 즉시 중단되던 현상.
* **원인 및 조치**:
  * 번역 취소 시 로컬 캐시 보존 핸들러 내부의 중복 `import translation_memory` 선언으로 인해, 파이썬 컴파일러가 전역 캐시 모듈을 아직 할당되지 않은 지역 변수로 오인식(UnboundLocalError)하던 문제를 발견했습니다.
  * 모든 번역 엔진 및 러너 파일 최상단 모듈 레벨로 정식 임포트를 일원화하고 내부 중복 선언을 전면 제거하여 **모든 번역 작업이 에러 없이 즉시 정상 기동**되도록 완벽히 수정했습니다.

---

### 2. 🔒 동시 번역 충돌 및 파일 잠김 에러 완전 해결 (`[WinError 32]`)
* **발생 현상**:
  * 대규모 모드팩(MeatballCraft 등) 번역 시 `[WinError 32] 다른 프로세스가 파일을 사용 중이기 때문에 프로세스가 액세스 할 수 없습니다: '...QuestTranslator_MeatballCraft.zip'` 팝업이 뜨며 번역이 실패하던 현상.
* **원인 및 조치**:
  * **UI 버튼 상호 잠금 누락 수정**: '선택 모드팩 퀘스트 번역' 실행 중 '모드팩 전체 한글화' 버튼이 비활성화되지 않아 두 작업이 동시에 실행되던 결함을 수정했습니다. 이제 어떤 번역이든 시작되면 모든 번역/편집 버튼이 즉각 잠깁니다.
  * **중복 실행 방지 가드 (`is_translating`)**: 버튼 더블 클릭이나 빠른 연타 시에도 다중 스레드가 절대 중복 생성되지 않도록 안전 플래그를 적용했습니다.
  * **임시 ZIP 잠김 자동 우회**: 백신 실시간 감시나 윈도우 탐색기에 의해 임시 파일이 잠겨 있더라도, 에러로 멈추지 않고 고유 타임스탬프 기반의 독립 임시 압축 파일로 자동 우회 생성합니다.
  * **이어 번역(Resume) 100% 보장**: 임시 파일명이 바뀌더라도 기존에 번역해 둔 진행 백업(예: 1,026개 완료 기록)을 손실 없이 정확하게 찾아 이어서 번역할 수 있도록 폴더 정규화 처리를 완료했습니다.

---

### 3. 🧩 필수 모듈 참조 누락 및 문법 오류 교정
* **`re` 정규식 모듈 임포트 추가**:
  * `translation_core.py`에서 모드팩 임시 경로 정규화 시 `re` 모듈 누락으로 발생하던 `"re" is not defined` 오류를 수정했습니다.
* **`logging` 모듈 임포트 추가**:
  * `translation_runner.py` 및 `ui_screens.py`에서 로깅 누락으로 발생하던 `"logging" is not defined` 경고 및 참조 오류를 해결했습니다.
* **1ms 이중 로깅(로그 중복 출력) 제거**:
  * 백그라운드 번역 스레드와 메인 UI 스레드에서 `logging.info`가 0.001초 차이로 중복 기록되던 타이밍 버그를 수정하여 로그 파일 가독성을 대폭 개선했습니다.

---

### 4. 🚀 자동 업데이트 후 재시작 보안 팝업 해결
* **발생 현상**:
  * 구버전에서 새 버전으로 자동 업데이트 완료 후 재실행될 때 `Security validation failure: parent process has different executable!` 에러 창이 발생하던 현상.
* **조치**:
  * PyInstaller 6.9+ 공식 재시작 표준 규격에 맞추어 모든 내부 감시 환경 변수(`_PYI*`, `_MEI*`)를 완전 소거하고 `PYINSTALLER_RESET_ENVIRONMENT = 1`을 적용하여 **보안 에러 팝업 없이 깨끗하게 1초 만에 새 버전으로 재실행**됩니다.

---

## 🌟 v1.5.3 메이저 기능도 함께 포함
* **인게임 오역 수정기 (Memory Editor) 대개편**: 검색 필터 삼원화 (`통합`/`EN`/`KO`), 다중 단어 일괄 치환, 체크박스 다중 선택 및 일괄 삭제
* **2축 정밀 오역률 검증 시스템**: 0% 표준어 사전 하드 블록 + 자음x모음 상호 교차 분탕 차단
* **핵심 고유명사 83종 공식 등재**: 팅커스, 크리에이트, 서투스 석영 표준화 및 기존 오타 41건 전수 교정
* **번역 취소 시 실시간 캐시 100% 영구 보존**: 취소 직전까지 완료된 번역 즉시 로컬 디스크 안전 저장

---

## 🛠️ 권장 환경
* **OS**: Windows 10 / 11 (64-bit)
* **권장 AI 모델**: Gemini 3.5 Flash, Gemini 3.1 Flash-Lite, Local AI (Ollama)
