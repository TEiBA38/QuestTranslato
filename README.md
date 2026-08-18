# QuestTranslatorPro (퀘스트 번역기 프로) 🍄

마인크래프트 모드팩의 다양한 퀘스트 모드(FTB Quests, Better Questing, HQM)를 자동으로 한글화해주는 강력한 AI 번역 툴입니다. 복잡한 파일 구조와 코드(색상 코드, 아이템 NBT 등)를 손상시키지 않고 텍스트만 안전하게 번역합니다.

## ✨ 주요 기능 (V1.2.0 대형 업데이트 반영!)
- **모드팩 전체 한글화 (아이템 & 가이드북)**: 수백 개의 모드에서 `.lang` 및 `Patchouli` 책 데이터를 전부 추출하여 단 한 번의 클릭으로 100% 한글 리소스팩을 찍어냅니다!
- **아이템 괄호 병기 최적화**: "철 곡괭이 (Iron Pickaxe)"처럼, 아이템이나 블록 이름은 한글 번역 뒤에 원본 영어를 괄호로 달아주어 검색/조합 편의성을 극대화했습니다.
- **초고속 묶음(Batch) 번역 엔진**: 하루 1500회 제한 무료 API에서도 토큰을 꽉꽉 눌러 담아 **기존 대비 3배 빠른 번역 속도**를 자랑합니다. 
- **글로벌 번역 메모리 (자동 세이브)**: 번역을 하다가 멈춰도 걱정 마세요. 10초 단위로 로컬 하드디스크에 자동 세이브되며, 겹치는 문장은 0.1초 만에 0원으로 불러옵니다!
- **다중 AI 번역 엔진 지원**: Gemini(최신 1.5 Flash 최적화), OpenAI(ChatGPT), DeepL, Google Translate 완벽 지원! 더불어 요금 걱정 없는 **Local AI(Ollama)** 100% 무료 연동!
- **스마트 모드팩 자동 인식**: CurseForge, Prism Launcher, MultiMC의 인스턴스 폴더를 자동으로 스캔하고 모드팩 썸네일과 함께 예쁜 UI로 렌더링합니다.
- **간편 번역 & 덮어쓰기**: 번역된 파일을 원본 모드팩에 원클릭으로 덮어쓸 수 있으며, 번역된 팩은 "한글화됨" 딱지로 쉽게 구분 가능합니다.
- **용어집(Glossary) 기능**: "Creeper=크리퍼"처럼 특정 고유 명사를 원하는 단어로 강제 고정하여 번역 품질을 크게 높일 수 있습니다.
- **안전한 구문 분석**: SNBT, JSON, HQM 포맷은 물론 Patchouli 특수 매크로(`$(item)`, `<br>`)까지 파악해 코드 손상 없이 번역을 수행합니다.

---

## 1) 🚀 실행 방법

2. 압축 해제한 폴더에서 "QuestTranslatorPro.exe"를 실행합니다.
3. Windows SmartScreen 경고가 뜨면 "추가 정보 -> 실행"을 선택합니다.

---

## 2) 🎮 번역 사용법

### A. 간편 번역 (모드팩 스캔)
1. 상단의 **인스턴스 루트 경로 찾기** 버튼을 눌러 모드팩들이 설치된 폴더("Instances" 등)를 선택합니다.
2. 하단에 스캔된 모드팩 카드 중 번역할 모드팩을 클릭합니다.
3. 팝업된 번역 옵션 창에서 사용할 AI 엔진과 API 키를 입력하고 번역을 시작합니다.
4. 번역 완료 후 모드팩에 바로 덮어쓰거나 별도의 압축 파일로 저장할 수 있습니다.

### B. 수동 번역 (드래그 앤 드롭)
앱 상단의 **퀵 번역 / 단일 파일 번역** 영역에 번역하고 싶은 ".zip" 파일이나 단일 퀘스트 파일(".snbt", ".json", ".hqm" 등)을 드래그하여 떨어뜨리면 즉시 번역이 시작됩니다.

---

## B-1) 📁 모드별 퀘스트 파일 위치 및 팁

### 🔹 Better Questing
경로: "D:\MODE\Instances\[모드팩 이름]\config\betterquesting"

<img width="1057" height="239" alt="image" src="https://github.com/user-attachments/assets/b059d6cd-6b30-4942-ae72-ad9479ac3885" />

- "DefaultQuests" 폴더 전체를 압축해서 번역하는 것을 추천합니다. (파일 구조가 그대로 유지됩니다)
- 퀘스트 폴더 안에 "DefaultQuests.json"이 통째로 있는 경우 해당 파일만 번역하면 됩니다.

### 🔹 FTB Quests
경로: "D:\MODE\Instances\[모드팩 이름]\config\ftbquests\normal" or quest

<img width="648" height="141" alt="image" src="https://github.com/user-attachments/assets/bf23034a-92df-4377-b6bd-459fc3459440" />

- "chapters" 폴더를 압축해서 번역하면 됩니다.
- 만약 "en_us.snbt" 또는 "en_us.json" 파일이 "kubejs" 폴더나 "ftbquest/quests/lang" 폴더 안에 있다면 해당 언어 파일 하나만 번역하면 됩니다. (아래 예시 참고)

ex) "D:\MODE\Instances\FTB StoneBlock 4\config\ftbquests\quests\lang"
<img width="620" height="37" alt="image" src="https://github.com/user-attachments/assets/0f4b8b2f-ffc2-439e-9d6b-11e3e30e2603" />

### 🔹 HQM (Hardcore Questing Mode)
경로: "D:\MODE\Instances\[모드팩 이름]\config\hqm"
- "quests.hqm" 파일을 찾아서 단일 파일로 번역해주면 됩니다.

---

## 4) ⚠️ HQM 번역 관련 주의사항

1. HQM 포맷에는 내부적으로 데이터 필드 길이 제한이 있습니다.
2. 번역문이 길이 제한을 넘으면 퀘스트북이 깨지는 것을 방지하기 위해 잘린 문장 대신 원문을 그대로 유지하도록 똑똑하게 처리됩니다.
3. **번역을 적용 하기 전** 모드팩을 먼저 실행해 퀘스트북을 한번 열고난 후, 마인크래프트 게임 언어를 "한국어"로 바꾼 뒤에 적용 시켜야 정상적으로 반영됩니다.

---

## 5) 🧪 검수 리포트

1. 번역 완료 시 화면에 팝업창으로 **번역 검수 리포트**가 즉시 표시되며, "*_review.txt" 파일로도 함께 저장됩니다.
2. 리포트에는 아래 지표가 포함되어 품질을 한눈에 확인할 수 있습니다.
   - **total**: 검수 대상 개수
   - **changed**: 정상적으로 번역되어 변경된 개수
   - **unchanged**: 원문과 동일하게 번역되지 않은 개수
   - **suspect_untranslated**: (경고) 미번역이 강력히 의심되는 항목 개수
