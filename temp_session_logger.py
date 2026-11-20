import datetime
import json
import logging
import os
import sys
import threading

# ============================================================================
# 💡 샌드박스 디버그 모드 스위치 (ON / OFF)
# ============================================================================
# True : 원본 캐시(translation_memory_*.json) 및 클라우드 업로드를 100% 보호하고,
#        이번 세션에서 새로 합성/번역된 데이터만 dist/temp/ 폴더에 안전하게 기록합니다.
# False: 실전 모드 (원본 캐시 파일 갱신 및 Cloudflare 자동 커밋)
# ============================================================================
DEBUG_TEMP_MODE = False


_lock = threading.Lock()

_session_start_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
_session_timestamp_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

_session_data = {
    "session_id": _session_timestamp_id,
    "start_time": _session_start_time,
    "last_updated": _session_start_time,
    "summary": {
        "exact_cache_hits": 0,
        "case_a_assembled_count": 0,
        "case_b_llm_count": 0,
        "newly_learned_words_count": 0,
    },
    "case_a_assembled_details": [],
    "case_b_llm_details": [],
    "new_learned_words": {
        "proper_nouns": {},
        "common_words": {},
    },
    "new_memory_entries": {
        "items": {},
        "general": {},
        "books": {},
    },
}


def is_debug_mode() -> bool:
    """샌드박스 디버그 모드 활성화 여부를 반환합니다."""
    return DEBUG_TEMP_MODE


def set_debug_mode(enabled: bool):
    """샌드박스 디버그 모드를 켜거나 끕니다."""
    global DEBUG_TEMP_MODE
    DEBUG_TEMP_MODE = bool(enabled)


def get_temp_dir() -> str:
    """dist/temp 폴더 경로를 반환합니다."""
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        base = os.path.join(exe_dir, "dist", "temp")
    else:
        repo_root = os.path.dirname(os.path.abspath(__file__))
        base = os.path.join(repo_root, "dist", "temp")
    os.makedirs(base, exist_ok=True)
    return base


def record_case_a(orig_text: str, assembled_text: str, category: str = "items"):
    """[Case A] 단어사전으로 로컬 합성된 명사구를 기록합니다."""
    if not DEBUG_TEMP_MODE:
        return
    with _lock:
        _session_data["summary"]["case_a_assembled_count"] += 1
        _session_data["case_a_assembled_details"].append({
            "category": category,
            "orig": orig_text,
            "assembled": assembled_text,
        })
        _session_data["new_memory_entries"].setdefault(category, {})[orig_text] = assembled_text
        _session_data["last_updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def record_case_b(orig_text: str, translated_text: str, glossary_used: dict = None, category: str = "general"):
    """[Case B] LLM/API 엔진으로 번역된 문장 및 주입된 고유명사 힌트를 기록합니다."""
    if not DEBUG_TEMP_MODE:
        return
    with _lock:
        _session_data["summary"]["case_b_llm_count"] += 1
        _session_data["case_b_llm_details"].append({
            "category": category,
            "orig": orig_text,
            "translated": translated_text,
            "glossary_used": glossary_used or {},
        })
        _session_data["new_memory_entries"].setdefault(category, {})[orig_text] = translated_text
        _session_data["last_updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def record_new_word(en_word: str, ko_word: str, category: str = "proper_nouns"):
    """새롭게 발견/학습된 단어 매핑을 기록합니다."""
    if not DEBUG_TEMP_MODE:
        return
    with _lock:
        target_dict = _session_data["new_learned_words"].setdefault(category, {})
        if en_word not in target_dict:
            target_dict[en_word] = ko_word
            _session_data["summary"]["newly_learned_words_count"] += 1
            _session_data["last_updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def record_memory_entry(category: str, src: str, tgt: str):
    """메모리에 저장 요청된 엔트리를 기록합니다."""
    if not DEBUG_TEMP_MODE:
        return
    with _lock:
        cat_key = "items" if "item" in category else ("books" if "book" in category else "general")
        _session_data["new_memory_entries"].setdefault(cat_key, {})[src] = tgt
        _session_data["last_updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 🧪 [테스트 모드 전용] 인게임 오역 수정기 1-클릭 테스트용 시드 데이터 (총 100종: items 50 / general 30 / books 20)
# DEBUG_TEMP_MODE = True 일 때만 메모리 캐시에 주입되며, 실전 모드(False)에서는 절대 로드되지 않습니다.
DEBUG_EDITOR_SEED_ENTRIES = {
    "items": {
        # 1~10: 동음의이어 'Log' (통나무 vs 기록/로그) — 펼치기 확인창 개별 체크 해제 테스트
        "[TEST] Oak Log Block": "참나무 통나무 블록",
        "[TEST] Spruce Log Block": "가문비나무 통나무 블록",
        "[TEST] Birch Log Block": "자작나무 통나무 블록",
        "[TEST] Jungle Log Block": "정글 통나무 블록",
        "[TEST] Dark Oak Log Block": "짙은 참나무 통나무 블록",
        "[TEST] System Error Log": "시스템 오류 통나무",
        "[TEST] Mission Quest Log": "미션 퀘스트 통나무",
        "[TEST] Reactor Crash Log": "원자로 충돌 통나무",
        "[TEST] Trade History Log": "거래 내역 통나무",
        "[TEST] Debug Console Log": "디버그 콘솔 통나무",
        # 11~20: 고유명사 음운/표기 통일 ('Mekanism' 메카니즘/메커니즘, 'Thermal' 써멀/서멀)
        "[TEST] Mekanism Quantum Pump": "메카니즘 양자 펌프",
        "[TEST] Mekanism Energy Core": "메커니즘 에너지 코어",
        "[TEST] Mekanism Steel Casing": "메카니즘 강철 케이싱",
        "[TEST] Mekanism Digital Miner": "메커니즘 디지털 광부",
        "[TEST] Mekanism Atomic Disassembler": "메카니즘 원자 분해기",
        "[TEST] Thermal Expansion Dynamo": "서멀 익스팬션 다이나모",
        "[TEST] Thermal Pulverizer Frame": "써멀 분쇄기 프레임",
        "[TEST] Thermal Induction Smelter": "서멀 유도 제련소",
        "[TEST] Thermal Cryo Stabilizer": "써멀 극저온 안정기",
        "[TEST] Thermal Flux Capacitor": "서멀 플럭스 축전기",
        # 21~30: 동음의이어 'Cell' (전지/셀 vs 감옥) & 'Charge' (충전 vs 돌진)
        "[TEST] ME 1k Storage Cell": "ME 1k 저장 감옥",
        "[TEST] ME 4k Fluid Cell": "ME 4k 액체 감옥",
        "[TEST] ME 16k Item Cell": "ME 16k 아이템 감옥",
        "[TEST] ME 64k Crafting Cell": "ME 64k 조합 감옥",
        "[TEST] Prison Dungeon Cell": "감옥 던전 셀",
        "[TEST] Energy Charge Pad": "에너지 돌진 패드",
        "[TEST] Shield Charge Crystal": "방패 돌진 수정",
        "[TEST] Battery Charge Station": "배터리 돌진 충전소",
        "[TEST] Bull Charge Totem": "황소 충전 토템",
        "[TEST] Cavalry Charge Banner": "기병 충전 깃발",
        # 31~40: 동음의이어 'Lead' (납 vs 이끌다), 'Spring' (봄 vs 용수철), 'Bat' (박쥐 vs 방망이)
        "[TEST] Raw Lead Ingot": "원시 이끌다 주괴",
        "[TEST] Refined Lead Plate": "정제된 이끌다 판",
        "[TEST] Lead Dust Pile": "이끌다 가루 더미",
        "[TEST] Lead Shielding Block": "이끌다 차폐 블록",
        "[TEST] Steel Coil Spring": "강철 코일 봄",
        "[TEST] Mechanical Piston Spring": "기계 피스톤 봄",
        "[TEST] Hot Water Spring": "온천 용수철",
        "[TEST] Wooden Baseball Bat": "나무 야구 박쥐",
        "[TEST] Vampire Cave Bat Wing": "흡혈 동굴 방망이 날개",
        "[TEST] Spiked Iron Bat": "가시 철 박쥐",
        # 41~50: 마인크래프트 서식 코드(&6, §b, %s) 포함 아이템 & 모드 기계
        "[TEST] &6Create Mechanical Press": "&6크리에이트 기계 프레스",
        "[TEST] &bCreate Water Wheel": "&b크리에이트 물레방아",
        "[TEST] §dBotania Mana Spreader": "§d보타니아 마나 살포기",
        "[TEST] §aBotania Terrasteel Blade": "§a보타니아 테라스틸 검",
        "[TEST] Tinkers Smeltery Controller": "팅커스 제련소 컨트롤러",
        "[TEST] Tinkers Cleaver Blade": "팅커스 도끼날 검",
        "[TEST] Immersive Blast Furnace": "이머시브 용광로",
        "[TEST] Immersive Arc Electrode": "이머시브 아크 전극",
        "[TEST] Applied Quantum Ring": "어플라이드 양자 반지",
        "[TEST] Refined Storage Grid (%s)": "리파인드 스토리지 그리드 (%s)",
    },
    "general": {
        # 51~60: 퀘스트 제목 & 설명 (단건 수정 · 결과 내 이동 · 단어 삭제 테스트)
        "[TEST] Craft the Quantum Pump to collect water.": "물을 모으기 위해 양자 펌프를 제작하세요.",
        "[TEST] Check your Mission Quest Log for next steps.": "다음 단계는 미션 퀘스트 통나무를 확인하세요.",
        "[TEST] Open the System Error Log to inspect reactor faults.": "원자로 오류를 점검하려면 시스템 오류 통나무를 여세요.",
        "[TEST] Build a Mekanism Steel Casing first.": "먼저 메카니즘 강철 케이싱을 만드세요.",
        "[TEST] Upgrade your Mekanism Digital Miner with speed modules.": "속도 모듈로 메커니즘 디지털 광부를 업그레이드하세요.",
        "[TEST] Connect the Thermal Dynamo to a Flux Capacitor.": "서멀 다이나모를 플럭스 축전기에 연결하세요.",
        "[TEST] Insert a 16k Storage Cell into the ME Drive.": "ME 드라이브에 16k 저장 감옥을 삽입하세요.",
        "[TEST] Recharge your jetpack on the Energy Charge Pad.": "에너지 돌진 패드에서 제트팩을 재충전하세요.",
        "[TEST] Smelt Raw Lead Ore into Lead Ingots.": "원시 이끌다 광석을 제련해 이끌다 주괴를 얻으세요.",
        "[TEST] Replace the broken Steel Coil Spring inside the press.": "프레스 내부의 고장 난 강철 코일 봄을 교체하세요.",
        # 61~70: 서식 코드 & 변수(%s, %d, &e, §c) 포함 퀘스트 문장
        "[TEST] &eReward: %d Experience Levels": "&e보상: %d 경험치 레벨",
        "[TEST] §cWarning: Reactor temperature exceeded %s K!": "§c경고: 원자로 온도가 %s K를 초과했습니다!",
        "[TEST] &aCompleted %d / %d optional tasks.": "&a선택 과제 %d / %d개를 완료했습니다.",
        "[TEST] Press &bShift + Right-Click&r to bind the wrench.": "&bShift + 우클릭&r으로 렌치를 연동하세요.",
        "[TEST] Requires at least %s RF/t to operate continuously.": "연속 가동하려면 최소 %s RF/t가 필요합니다.",
        "[TEST] Collect 16 Oak Logs and 8 Iron Ingots.": "참나무 통나무 16개와 철 주괴 8개를 모으세요.",
        "[TEST] Defeat the Nether Boss without taking fire damage.": "화염 피해를 입지 않고 네더 보스를 처치하세요.",
        "[TEST] Craft a Wireless Crafting Terminal for remote access.": "원격 접근을 위해 무선 조합 터미널을 제작하세요.",
        "[TEST] Automate ore doubling using the Crusher and Enrichment Chamber.": "분쇄기와 농축 챔버를 사용해 광석 2배 공정을 자동화하세요.",
        "[TEST] Place a Chunk Loader to keep your base active.": "기지를 계속 활성화하려면 청크 로더를 설치하세요.",
        # 71~80: 동음의이어 'Block'(블록 vs 차단), 'Frame'(프레임 vs 틀), 'Pipe'(파이프 vs 관)
        "[TEST] Use a shield to Block incoming arrows.": "방패를 사용해 날아오는 화살을 블록하세요.",
        "[TEST] Place a Redstone Block beneath the beacon.": "신호기 아래에 레드스톤 차단을 배치하세요.",
        "[TEST] Craft a Machine Frame using four Iron Plates.": "철판 4개로 기계 틀을 제작하세요.",
        "[TEST] Item Frame can display rare relics on walls.": "아이템 프레임은 벽에 희귀 유물을 전시할 수 있습니다.",
        "[TEST] Connect Fluid Pipes from the pump to the tank.": "펌프에서 탱크까지 액체 관을 연결하세요.",
        "[TEST] Upgrade Basic Logistical Pipe to Ultimate tier.": "기본 물류 관을 얼티밋 등급으로 업그레이드하세요.",
        "[TEST] Swing the Iron Bat to knock back enemies.": "철 박쥐를 휘둘러 적을 밀쳐내세요.",
        "[TEST] Beware of Cave Bats spawning in dark tunnels.": "어두운 터널에서 스폰되는 동굴 방망이를 조심하세요.",
        "[TEST] Spring has arrived in the Overworld biome.": "오버월드 바이옴에 용수철이 찾아왔습니다.",
        "[TEST] Lead your villagers safely back to the town hall.": "주민들을 마을 회관으로 안전하게 납하세요.",
    },
    "books": {
        # 81~100: 가이드북 문장 (의도적 어색한 직역 포함 — AI 재번역 O/X 비교창 테스트용)
        "[TEST] Ancient arcane tome containing forgotten recipes.": "고대 아케인 톰 잊혀진 레시피 포함.",
        "[TEST] Press right-click while sneaking to open the manual.": "스니킹 하면서 우클릭 눌러 매뉴얼 열기.",
        "[TEST] Mana flows from the spreader into nearby mana pools.": "마나는 살포기에서 근처 마나 풀로 흐른다.",
        "[TEST] Never let the fission reactor coolant drop below twenty percent.": "절대 핵분열 원자로 냉각수 20퍼센트 아래 떨어지게 하지마.",
        "[TEST] Kinetic stress capacity increases when adding more water wheels.": "더 많은 물레방아 추가하면 운동 스트레스 용량 증가함.",
        "[TEST] This multiblock structure requires a hollow three by three core.": "이 멀티블록 구조는 속이 빈 3바이3 코어 필요함.",
        "[TEST] Channels are limited to eight per normal ME glass cable.": "채널은 일반 ME 유리 케이블당 8개로 제한됨.",
        "[TEST] Dense cables can carry up to thirty-two channels simultaneously.": "고밀도 케이블은 동시에 최대 32채널 운반 가능함.",
        "[TEST] Right-click the altar with a wand to initiate the ritual.": "지팡이로 제단 우클릭해서 의식 시작하기.",
        "[TEST] Infusing redstone dust into iron yields enriched alloy.": "철에 레드스톤 가루 주입하면 강화 합금 나옴.",
        "[TEST] Thermal generators produce more power when placed adjacent to lava.": "열 발전기는 용암 옆에 두면 더 많은 전력 생산함.",
        "[TEST] Always wear a hazmat suit before handling radioactive waste barrels.": "방사성 폐기물 배럴 다루기 전에 항상 방호복 입기.",
        "[TEST] Use a configurator to toggle input and output sides on machines.": "기계의 입력 출력 면을 바꾸려면 설정기 사용.",
        "[TEST] The smeltery must have a controller, drain, and seared fuel tank.": "제련소는 컨트롤러, 배수구, 그을린 연료 탱크 있어야함.",
        "[TEST] Pour molten gold over a stone pickaxe head to create a cast.": "주형 만들려면 돌 곡괭이 머리 위에 녹은 금 붓기.",
        "[TEST] Wireless transmitters consume extra energy based on distance.": "무선 송신기는 거리 따라 추가 에너지 소비함.",
        "[TEST] Check the quest book regularly for progression hints and rewards.": "진행 힌트와 보상을 위해 퀘스트 북 정기적으로 확인.",
        "[TEST] Combining two damaged tools on an anvil restores durability.": "모루에서 손상된 도구 두 개 합치면 내구도 회복됨.",
        "[TEST] Ender chests with identical color codes share the same inventory.": "같은 색 코드 가진 엔더 상자는 같은 인벤토리 공유함.",
        "[TEST] Keep a backup of your world before updating major modpack versions.": "대규모 모드팩 버전 업데이트 전에 월드 백업 보관하기.",
    },
}


def get_debug_editor_seeds() -> dict:
    """테스트 모드(DEBUG_TEMP_MODE=True)일 때만 오역 수정기 테스트용 임시 단어 시드를 반환합니다."""
    if not DEBUG_TEMP_MODE:
        return {}
    return {cat: dict(entries) for cat, entries in DEBUG_EDITOR_SEED_ENTRIES.items()}


def save_temp_session() -> str:
    """
    현재까지 누적된 세션 디버그 데이터를 dist/temp/session_summary.json 에 저장합니다.
    동시에 타임스탬프가 붙은 영구 보존용 파일도 함께 기록합니다.
    """
    temp_dir = get_temp_dir()
    summary_path = os.path.join(temp_dir, "session_summary.json")
    history_path = os.path.join(temp_dir, f"session_{_session_timestamp_id}.json")

    with _lock:
        payload = json.dumps(_session_data, ensure_ascii=False, indent=2)

    try:
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(payload)
        with open(history_path, "w", encoding="utf-8") as f:
            f.write(payload)
        logging.info(f"🛠️ [샌드박스 디버그 모드] 세션 데이터 기록 완료: {summary_path}")
        return summary_path
    except Exception as e:
        logging.warning(f"⚠️ temp 세션 저장 실패: {e}")
        return summary_path

