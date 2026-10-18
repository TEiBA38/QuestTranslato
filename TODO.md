# 📋 Quest Translator Pro - TODO List

## 🚀 구현 예정 기능 (Up Next)

- [ ] **사용자 피드백(Feedback) 전송 기능 추가**
  - 앱 상단 헤더(또는 메뉴)에 `💬 피드백 / 버그 제보` 버튼 및 모달 다이얼로그 추가
  - 사용자 의견/건의사항 입력 후 **Discord Webhook**을 통해 지정된 디스코드 채널로 즉시 전송
- [ ] **버그 리포트(Bug Report) & 로그 자동 첨부 기능 추가**
  - 버그 제보 시 현재 실행 중인 앱의 로그 파일(`%APPDATA%/QuestTranslatorPro/logs/quest_translator_YYYYMMDD.log`) 및 화면 콘솔 로그(`log_box`) 자동 수집
  - 앱 버전(`APP_VERSION`), 선택한 번역 엔진/모델, 대상 모드팩 이름 등 환경 정보 함께 패키징
- [ ] **UI 성능 최적화 및 렉 개선**
  - **1단계 (즉시 체감 최적화 - CustomTkinter 병목 수술)**:
    - 모드팩 카드 스캔 및 썸네일 이미지 비동기/지연 로딩(Lazy Loading) 적용
    - 번역 메모리 편집기 검색 결과 렌더링 최적화 (대량 위젯 생성 대신 경량 가상 리스트/테이블 적용)
    - 실시간 로그창(`log_box`) 갱신 배칭(Batching) 처리 (초당 수십 번 I/O 호출을 100ms 단위로 묶어 UI 프리징 방지)
    - 창 크기 조절(`<Configure>`) 리사이징 스로틀링/디바운싱 강화
  - **2단계 (장기 대안 - 고성능 프레임워크 전환 검토)**:
    - **PySide6 (Qt6 for Python)** 또는 **PyQt-Fluent-Widgets** 기반 GPU 가속 네이티브 UI 마이그레이션 검토 (디스코드/VS Code 스타일의 60fps 부드러운 다크 테마)

---

## ✅ 최근 완료된 작업 (Completed)

- [x] Cloudflare R2 순수 스토리지 기반 큐/커밋 아키텍처 전환 (D1 행 한도 문제 영구 해결 및 동시 업로드 덮어쓰기 방지)
- [x] 모드 내장 공식 한글패치(`ko_kr.lang`, `ko_kr.json`) 최우선 적용 및 번역 메모리 자동 적재
- [x] 전체 Python 파일 문법/스코프/중복 임포트 정리 및 6단계 번역 파이프라인 검증

