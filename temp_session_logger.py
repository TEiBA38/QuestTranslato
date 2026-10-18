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

