import json
import os
import hashlib
import shutil
import threading
import logging

MEMORY_FILE = "translation_memory.json"
BACKUP_FILE = "translation_memory.json.bak"
SHORT_TEXT_THRESHOLD = 30

_global_cache = {}
_modpack_cache = {}
_lock = threading.Lock()
_global_loaded = False
_modpack_loaded = False
_current_modpack_id = None
_current_modpack_file = None


def set_current_modpack(modpack_path):
    """모드팩 컨텍스트를 설정. 짧은 문장은 이 모드팩 전용 메모리에 저장/조회됨."""
    global _current_modpack_id, _current_modpack_file, _modpack_cache, _modpack_loaded
    with _lock:
        if modpack_path:
            modpack_name = os.path.basename(modpack_path.rstrip(os.sep))
            _current_modpack_id = hashlib.md5(modpack_name.encode('utf-8')).hexdigest()[:8]
            _current_modpack_file = f"translation_memory_modpack_{_current_modpack_id}.json"
        else:
            _current_modpack_id = None
            _current_modpack_file = None
        _modpack_cache = {}
        _modpack_loaded = False


def get_current_modpack_id():
    """현재 설정된 모드팩 ID 반환 (없으면 None)."""
    return _current_modpack_id


def _is_short_text(text):
    return isinstance(text, str) and len(text.strip()) < SHORT_TEXT_THRESHOLD


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


def _read_file(filepath):
    if not filepath or not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
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


def _find_lang_key(cache, target_lang_norm):
    """캐시 딕셔너리에서 공백을 무시하고 언어 키를 찾음."""
    for k in cache.keys():
        if k.replace(" ", "") == target_lang_norm:
            return k
    return None


def _lookup(cache, text, target_lang_norm):
    """캐시에서 번역 결과를 조회."""
    lang_key = _find_lang_key(cache, target_lang_norm)
    if not lang_key:
        return None
    return cache[lang_key].get(text)


def _store(cache, text, translated_text, target_lang_norm):
    """캐시에 번역 결과를 저장."""
    lang_key = _find_lang_key(cache, target_lang_norm) or target_lang_norm
    if lang_key not in cache:
        cache[lang_key] = {}
    cache[lang_key][text] = translated_text


def load_memory():
    """글로벌 메모리 로드."""
    global _global_cache, _global_loaded
    with _lock:
        if _global_loaded:
            return
        data = _read_file(MEMORY_FILE)
        data = _migrate_legacy_keys(data)
        _global_cache = data
        _global_loaded = True


def _load_modpack_memory():
    """모드팩별 메모리 로드 (락 내부에서 호출)."""
    global _modpack_cache, _modpack_loaded
    if _modpack_loaded or not _current_modpack_file:
        return
    data = _read_file(_current_modpack_file)
    data = _migrate_legacy_keys(data)
    _modpack_cache = data
    _modpack_loaded = True


def get_cached_translation(text, target_lang="한국어(Korean)"):
    """
    하이브리드 캐시 조회:
    - 짧은 문장 (30자 미만) + 모드팩 설정됨 → 모드팩 전용 캐시만 조회
    - 긴 문장 (30자 이상) 또는 모드팩 미설정 → 글로벌 캐시 조회
    """
    if not _global_loaded:
        load_memory()

    target_lang_norm = target_lang.replace(" ", "")

    if _is_short_text(text) and _current_modpack_id:
        with _lock:
            _load_modpack_memory()
        return _lookup(_modpack_cache, text, target_lang_norm)
    else:
        return _lookup(_global_cache, text, target_lang_norm)


def add_to_memory(text, translated_text, target_lang="한국어(Korean)"):
    """
    하이브리드 캐시 저장:
    - 짧은 문장 (30자 미만) + 모드팩 설정됨 → 모드팩 전용 캐시에 저장
    - 긴 문장 (30자 이상) 또는 모드팩 미설정 → 글로벌 캐시에 저장
    """
    if not _global_loaded:
        load_memory()

    if not text or not translated_text:
        return

    target_lang_norm = target_lang.replace(" ", "")

    with _lock:
        if _is_short_text(text) and _current_modpack_id:
            _load_modpack_memory()
            _store(_modpack_cache, text, translated_text, target_lang_norm)
        else:
            _store(_global_cache, text, translated_text, target_lang_norm)


def _safe_save_file(filepath, data):
    """디스크 기존 데이터와 병합 후 안전하게 저장."""
    if not filepath:
        return None
    try:
        disk_data = _read_file(filepath)
        merged = _merge_caches(disk_data, data)

        if os.path.exists(filepath):
            try:
                shutil.copy2(filepath, filepath + ".bak")
            except Exception:
                pass

        tmp_file = filepath + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, filepath)
        return merged
    except Exception as e:
        tmp_file = filepath + ".tmp"
        if os.path.exists(tmp_file):
            try:
                os.remove(tmp_file)
            except Exception:
                pass
        logging.error(f"Failed to save memory file {filepath}: {e}")
        return data


def save_memory():
    """글로벌 + 모드팩별 메모리를 모두 안전하게 저장."""
    with _lock:
        # 글로벌 캐시 저장
        merged_global = _safe_save_file(MEMORY_FILE, _global_cache)
        if merged_global is not None:
            _global_cache.clear()
            _global_cache.update(merged_global)

        # 모드팩 캐시 저장
        if _current_modpack_file and _modpack_cache:
            merged_modpack = _safe_save_file(_current_modpack_file, _modpack_cache)
            if merged_modpack is not None:
                _modpack_cache.clear()
                _modpack_cache.update(merged_modpack)
