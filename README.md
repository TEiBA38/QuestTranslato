# QuestTranslato 사용법 및 주의사항

## 1) 실행 방법

1. `QuestTranslatorPro_win64.zip` 파일을 원하는 폴더에 압축 해제합니다.
2. 압축 해제한 폴더에서 `QuestTranslatorPro.exe`를 실행합니다.
3. Windows SmartScreen 경고가 뜨면 `추가 정보` -> `실행`을 선택합니다.

## 2) 번역 사용법

1. 번역 엔진을 선택합니다.
2. Google Translate는 API 키 없이 사용 가능합니다.
3. Gemini / OpenAI / DeepL은 앱의 API 키 입력칸에 키를 입력합니다.
4. 퀘스트 압축파일 및 단일 퀘스트 파일을 드래그인 드롭 하거나 선택 번역 버튼을 눌러 선택해서 번역합니다
5. 번역 완료 후 저장 폴더를 선택하면 결과 파일이 저장됩니다.

ex)퀘스트 파일 위치


betterquesting 경우 
D:\MODE\Instances\MeatballCraft Dimensional Ascension\config\betterquesting
<img width="1057" height="239" alt="image" src="https://github.com/user-attachments/assets/b059d6cd-6b30-4942-ae72-ad9479ac3885" />
DefaultQuests 파일 전체를 압축해서 번역 하는 것을 추천합니다(파일 구조 유지됨)

퀘스트 폴더 안에 DefaultQuests.json이 있는 경우 DefaultQuests.json 만 번역하면 됩니다

ftb quest 경우
D:\MODE\Instances\Journey Beyond the Abyss\config\ftbquests\normal
<img width="648" height="141" alt="image" src="https://github.com/user-attachments/assets/bf23034a-92df-4377-b6bd-459fc3459440" />

chapters 파일을 압축해서 번역하면 됩니다

만약 en_us.snbt  en_us.json 파일이 kubejs 폴더 안에 있거나 ftbquest 폴더 안에 있다면 en_us.json을 번역하면 됩니다

ex)D:\MODE\Instances\FTB StoneBlock 4\config\ftbquests\quests\lang
<img width="620" height="37" alt="image" src="https://github.com/user-attachments/assets/0f4b8b2f-ffc2-439e-9d6b-11e3e30e2603" />


HQM 경우

D:\MODE\Instances\Journey to the Core\config\hqm


quests.hqm 을 찾아서 번역해주면 됩니다




## 3) HQM 번역 관련 주의사항

1. HQM 포맷에는 필드 길이 제한이 있습니다.
2. 번역문이 길이 제한을 넘으면 잘린 문장 대신 원문을 유지하도록 처리됩니다.
3. 번역을 적용 하기 전 모드팩을 먼저 실행해 퀘스트북을 한번 열고난후 마크 언어를 한글로 바꾼뒤에 적용 시켜야 합니다



## 4) 검수 리포트

1. 번역 완료 시 자동 검수 리포트(`*_review.txt`)가 함께 생성됩니다.
2. 리포트에는 아래 지표가 포함됩니다.
   - total: 검수 대상 개수
   - changed: 번역되어 변경된 개수
   - unchanged: 원문과 동일한 개수
   - suspect_untranslated: 미번역 의심 개수

## 5) 반드시 지켜야 할 사항

1. `_internal` 폴더와 그 안 파일(예: `base_library.zip`)은 삭제/이동/수정하지 마세요.
2. 압축 파일 안에서 바로 실행하지 말고, 반드시 압축 해제 후 실행하세요.
3. 실행 파일과 `_internal` 폴더는 같은 위치에 있어야 합니다.

