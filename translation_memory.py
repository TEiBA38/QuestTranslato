import json
import os
import threading

MEMORY_FILE = "translation_memory.json"
_memory_cache = {}
_memory_lock = threading.Lock()
_is_loaded = False

def load_memory():
    global _memory_cache, _is_loaded
    with _memory_lock:
        if _is_loaded:
            return
        if os.path.exists(MEMORY_FILE):
            try:
                with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                    _memory_cache = json.load(f)
            except Exception:
                _memory_cache = {}
        else:
            _memory_cache = {}
        _is_loaded = True

def save_memory():
    with _memory_lock:
        try:
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                json.dump(_memory_cache, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

def get_cached_translation(text, target_lang="한국어 (Korean)"):
    if not _is_loaded:
        load_memory()
    
    if target_lang not in _memory_cache:
        return None
        
    return _memory_cache[target_lang].get(text)

def add_to_memory(text, translated_text, target_lang="한국어 (Korean)"):
    if not _is_loaded:
        load_memory()
        
    if not text or not translated_text:
        return
        
    if str(text).strip() == str(translated_text).strip():
        # 원본과 번역본이 완전히 같으면 (번역 실패나 스킵) 저장하지 않음
        return
        
    with _memory_lock:
        if target_lang not in _memory_cache:
            _memory_cache[target_lang] = {}
        _memory_cache[target_lang][text] = translated_text
