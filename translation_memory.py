import json
import os
import shutil
import threading
import logging

MEMORY_FILE = "translation_memory.json"
BACKUP_FILE = "translation_memory.json.bak"
_memory_cache = {}
_memory_lock = threading.Lock()
_is_loaded = False

def _normalize_key(key):
    """언어 키의 공백을 제거하여 정규화"""
    return key.replace(" ", "")

def _migrate_legacy_keys(data):
    """하위 호환성: '한국어 (Korean)' → '한국어(Korean)' 마이그레이션"""
    old_key = "한국어 (Korean)"
    new_key = "한국어(Korean)"
    if old_key in data:
        if new_key not in data:
            data[new_key] = {}
        data[new_key].update(data[old_key])
        del data[old_key]
    return data

def _read_disk_file():
    """디스크에서 현재 메모리 파일을 읽어서 반환 (없으면 빈 dict)"""
    if not os.path.exists(MEMORY_FILE):
        return {}
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _merge_caches(base, overlay):
    """base 위에 overlay를 병합. 기존 데이터는 절대 삭제되지 않음."""
    merged = {}
    all_keys = set(list(base.keys()) + list(overlay.keys()))
    for lang in all_keys:
        merged[lang] = {}
        if lang in base and isinstance(base[lang], dict):
            merged[lang].update(base[lang])
        if lang in overlay and isinstance(overlay[lang], dict):
            merged[lang].update(overlay[lang])
    return merged

def load_memory():
    global _memory_cache, _is_loaded
    with _memory_lock:
        if _is_loaded:
            return
        disk_data = _read_disk_file()
        disk_data = _migrate_legacy_keys(disk_data)
        _memory_cache = disk_data
        _is_loaded = True

def save_memory():
    """
    안전한 저장: 디스크의 기존 데이터와 RAM 캐시를 병합한 뒤 저장.
    - 기존 데이터가 절대 삭제되지 않음 (병합 방식)
    - 저장 전 자동 백업 (.bak) 생성
    """
    with _memory_lock:
        try:
            # 1. 디스크에 있는 기존 데이터 읽기
            disk_data = {}
            if os.path.exists(MEMORY_FILE):
                try:
                    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                        disk_data = json.load(f)
                except Exception:
                    disk_data = {}

            # 2. 기존 디스크 데이터 + RAM 캐시 병합 (기존 데이터 보존, 새 데이터 추가)
            merged = _merge_caches(disk_data, _memory_cache)

            # 3. 저장 전 백업 생성
            if os.path.exists(MEMORY_FILE):
                try:
                    shutil.copy2(MEMORY_FILE, BACKUP_FILE)
                except Exception:
                    pass  # 백업 실패해도 저장은 진행

            # 4. 임시 파일에 쓰고 원자적 교체
            tmp_file = MEMORY_FILE + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, MEMORY_FILE)

            # 5. RAM 캐시도 병합된 최신 상태로 갱신
            _memory_cache.clear()
            _memory_cache.update(merged)

        except Exception as e:
            tmp_file = MEMORY_FILE + ".tmp"
            if os.path.exists(tmp_file):
                try:
                    os.remove(tmp_file)
                except Exception:
                    pass
            logging.error(f"Failed to save translation memory: {e}")

def get_cached_translation(text, target_lang="한국어(Korean)"):
    if not _is_loaded:
        load_memory()
    
    # 띄어쓰기 등 불일치 방지 정규화
    target_lang = target_lang.replace(" ", "")
    
    # 캐시 딕셔너리의 키도 공백이 제거된 형태로 확인
    cache_key = None
    for k in _memory_cache.keys():
        if k.replace(" ", "") == target_lang:
            cache_key = k
            break
            
    if not cache_key:
        return None
        
    return _memory_cache[cache_key].get(text)

def add_to_memory(text, translated_text, target_lang="한국어(Korean)"):
    if not _is_loaded:
        load_memory()
        
    if not text or not translated_text:
        return
        
    target_lang = target_lang.replace(" ", "")
        
    with _memory_lock:
        cache_key = target_lang
        for k in _memory_cache.keys():
            if k.replace(" ", "") == target_lang:
                cache_key = k
                break
                
        if cache_key not in _memory_cache:
            _memory_cache[cache_key] = {}
        _memory_cache[cache_key][text] = translated_text
