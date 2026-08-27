# QuestTranslatorPro (퀘스트 번역기 프로) 🍄

<div align="center">

<a href="https://buymeacoffee.com/teiba" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" style="height: 45px !important;" ></a>

**마인크래프트 모드팩 퀘스트, 아이템, 가이드북 전체 통합 AI 번역 솔루션**  
*복잡한 파일 구조와 코드(색상 코드, 아이템 NBT, 매크로)를 손상시키지 않고 텍스트만 안전하고 완벽하게 번역합니다.*

[![GitHub Release](https://img.shields.io/github/v/release/TEiBA38/QuestTranslato?color=orange&logo=github)](https://github.com/TEiBA38/QuestTranslato/releases/latest)
[![Platform](https://img.shields.io/badge/Platform-Windows-blue?logo=windows)](https://github.com/TEiBA38/QuestTranslato/releases/latest)

</div>

---

## ✨ 핵심 기능 (v1.5.0 대형 업데이트)

- **⚡ 하이브리드 마스터 초압축 아카이빙 (0.3초 로딩)**: 34만 개(50MB)에 달하는 대용량 번역 데이터베이스를 12MB로 초압축하여, 앱 실행 시 0.3초 만에 전체 번역 메모리를 즉시 로딩합니다.
- **🎮 인게임 실시간 오역 수정기 (Memory Editor & Hotpatching)**: 게임을 끄지 않고 앱에서 번역을 수정하면 리소스팩이 실시간 업데이트되어, 마인크래프트 게임 내에서 **`F3 + T`만 누르면 즉시 수정된 번역이 적용**됩니다!
- **📦 모드팩 전체 한글화 (아이템 & 가이드북)**: 수백 개의 모드 JAR에서 `.lang` 및 `Patchouli` 책 데이터를 자동 추출하여 원클릭으로 100% 한글 리소스팩을 생성합니다.
- **🏷️ 아이템 괄호 병기 최적화**: "철 곡괭이 (Iron Pickaxe)"처럼 한글 번역 뒤에 원본 영어를 괄호로 달아주어 검색/조합 편의성을 극대화합니다.
- **🚀 초고속 묶음(Batch) 번역 엔진**: 무료 API에서도 토큰을 꽉꽉 눌러 담아 **기존 대비 3배 이상의 초고속 번역**을 자랑합니다.
- **🌐 글로벌 클라우드 번역 메모리**: 실시간으로 유저들의 번역 데이터가 공유되어, 이미 누군가 번역한 문장은 0.001초 만에 0원(API 비용 없음)으로 즉시 적용됩니다.
- **🤖 다중 AI 번역 엔진 지원**: Gemini, OpenAI(ChatGPT), DeepL, Google Translate, Local AI(Ollama 등)
- **📂 스마트 모드팩 자동 인식**: CurseForge, Prism Launcher, MultiMC의 인스턴스 폴더를 자동 스캔하여 모드팩 썸네일과 함께 한눈에 관리합니다.
- **🎨 스마트 서식 & 색상 코드 보존**: `&a`, `§c`, Patchouli 매크로(`$(item)`, `<br>`) 등 마인크래프트 특수 서식을 완벽하게 파악하여 텍스트 깨짐 없이 안전하게 번역합니다.

---

## 1) 🚀 다운로드 및 실행 방법

1. [최신 릴리즈 페이지(Releases)](https://github.com/TEiBA38/QuestTranslato/releases/latest)에서 `QuestTranslatorPro-v1.5.0-Windows.zip`을 다운로드합니다.
2. 압축을 풀고 **`QuestTranslatorPro.exe`**를 실행합니다.
3. *(Windows SmartScreen 경고가 뜰 경우 "추가 정보 -> 실행"을 선택합니다.)*

---

## 2) 🎮 번역 사용법

### A. 간편 번역 (모드팩 자동 스캔)
1. 상단의 **인스턴스 루트 경로 찾기** 버튼을 눌러 모드팩들이 설치된 폴더(`Instances` 등)를 선택합니다.
2. 하단에 스캔된 모드팩 카드 중 번역할 모드팩을 클릭합니다.
3. 팝업된 번역 옵션 창에서 사용할 AI 엔진과 API 키를 입력하고 번역을 시작합니다.
4. 번역 완료 후 모드팩에 바로 덮어쓰거나 별도의 압축 파일로 저장할 수 있습니다.

### B. 수동 번역 (드래그 앤 드롭)
앱 상단의 **퀵 번역 / 단일 파일 번역** 영역에 번역하고 싶은 `.zip` 파일이나 단일 퀘스트 파일(`.snbt`, `.json`, `.hqm` 등)을 드래그하여 떨어뜨리면 즉시 번역이 시작됩니다.

### C. 🛠️ 인게임 실시간 오역 수정기 사용법
1. 앱 메인 화면의 **`[✏️ 인게임 오역 수정기]`** 버튼을 클릭합니다.
2. 수정하고 싶은 단어/문장을 검색한 후 올바른 번역으로 수정하고 **저장**을 누릅니다.
3. 마인크래프트 게임 화면으로 돌아가 키보드의 **`F3 + T`**를 누르면 리소스팩이 새로고침되며 **게임 재접속 없이 즉시 수정된 번역이 반영**됩니다!

---

## 3) 📁 모드별 퀘스트 파일 위치 및 팁

### 🔹 Better Questing
- **경로**: `[모드팩 폴더]\config\betterquesting`
- `DefaultQuests` 폴더 전체를 압축해서 번역하거나, 폴더 내 `DefaultQuests.json` 파일만 번역하면 됩니다.

### 🔹 FTB Quests
- **경로**: `[모드팩 폴더]\config\ftbquests\quests` 또는 `normal`
- `chapters` 폴더를 압축해서 번역하거나, `lang/en_us.snbt` 언어 파일이 있는 경우 해당 파일 하나만 번역하면 됩니다.

### 🔹 HQM (Hardcore Questing Mode)
- **경로**: `[모드팩 폴더]\config\hqm`
- `quests.hqm` 파일을 찾아 단일 파일로 번역해주면 됩니다.
- ⚠️ **HQM 주의사항**: 번역 적용 전 모드팩을 먼저 실행해 퀘스트북을 한 번 열고, 마인크래프트 게임 언어를 "한국어"로 바꾼 뒤에 적용해야 정상 반영됩니다.

---

## 4) 🧪 검수 리포트

번역 완료 시 화면에 팝업창으로 **번역 검수 리포트**가 즉시 표시되며, `*_review.txt` 파일로도 함께 저장됩니다.
- **total**: 검수 대상 개수
- **changed**: 정상적으로 번역되어 변경된 개수
- **unchanged**: 원문과 동일하게 유지된 개수
- **suspect_untranslated**: (경고) 미번역이 의심되는 항목 개수
