import json
import os
import hashlib
import shutil
import threading
import logging
import re
import requests
from concurrent.futures import ThreadPoolExecutor
from constants import has_hangul

MEMORY_FILE = "translation_memory.json"
BACKUP_FILE = "translation_memory.json.bak"
ITEMS_MEMORY_FILE = "translation_memory_items.json"
BOOKS_MEMORY_FILE = "translation_memory_books.json"
SHORT_TEXT_THRESHOLD = 30

# ====================================================================
# Supabase 클라우드 설정
# ====================================================================
SUPABASE_URL = "https://oanjweqyvvdrbmvqoqrs.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im9hbmp3ZXF5dnZkcmJtdnFvcXJzIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODc3NTkwMDgsImV4cCI6MjEwMzMzNTAwOH0.3HbUVkupPoyMfzjMPSkAmGQ0qydp6yjDrxfSoGAghC8"

TABLE_MAP = {
    "general": "translation_memory",
    "items": "translation_memory_items",
    "books": "translation_memory_books",
}

_global_cache = {}
_modpack_cache = {}
_items_cache = {}
_books_cache = {}
_lock = threading.Lock()
_global_loaded = False
_modpack_loaded = False
_items_loaded = False
_books_loaded = False
_current_modpack_id = None
_current_modpack_file = None


def set_current_modpack(modpack_path):
    """모드팩 컨텍스트 설정 (짧은 문장 격리)"""
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
    return _current_modpack_id


def _is_short_text(text):
    return isinstance(text, str) and len(text.strip()) < SHORT_TEXT_THRESHOLD


def is_valid_translation(src, tgt, target_lang="한국어 (Korean)"):
    """번역문 유효성 검증: 원문과 같거나 한글이 누락된 오염 데이터 차단"""
    if not src or not tgt:
        return False
    s_clean = str(src).strip()
    t_clean = str(tgt).strip()
    if s_clean == t_clean:
        return False
    if "한국어" in target_lang or "Korean" in target_lang:
        if re.search(r'[A-Za-z]', s_clean) and not has_hangul(t_clean):
            return False
    return True


def _read_and_clean_file(filepath):
    """파일 로드 시 오염된 항목(원문==번역문) 자동 제거"""
    if not filepath or not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        cleaned = {}
        for lang, translations in data.items():
            if not isinstance(translations, dict):
                continue
            cleaned[lang] = {}
            for src, tgt in translations.items():
                if is_valid_translation(src, tgt, lang):
                    cleaned[lang][src] = tgt
        return cleaned
    except Exception:
        return {}


def _lookup_safe(cache, text, target_lang_norm):
    """캐시 조회 및 오염된 데이터 발견 시 즉시 자가 치유(삭제)"""
    for lang_key, entries in cache.items():
        if lang_key.replace(" ", "") == target_lang_norm:
            if text in entries:
                val = entries[text]
                if is_valid_translation(text, val, lang_key):
                    return val
                else:
                    with _lock:
                        entries.pop(text, None)
            break
    return None


def _store(cache, text, translated_text, target_lang_norm):
    target_lang_key = target_lang_norm
    for k in cache.keys():
        if k.replace(" ", "") == target_lang_norm:
            target_lang_key = k
            break
    if target_lang_key not in cache:
        cache[target_lang_key] = {}
    cache[target_lang_key][text] = translated_text


def load_memory():
    """로컬 메모리 로드 및 Supabase 최신 데이터 동기화"""
    global _global_cache, _global_loaded, _items_cache, _items_loaded, _books_cache, _books_loaded
    with _lock:
        if not _global_loaded:
            _global_cache = _read_and_clean_file(MEMORY_FILE)
            _global_loaded = True
        if not _items_loaded:
            _items_cache = _read_and_clean_file(ITEMS_MEMORY_FILE)
            _items_loaded = True
        if not _books_loaded:
            _books_cache = _read_and_clean_file(BOOKS_MEMORY_FILE)
            _books_loaded = True

    sync_from_supabase()


def _load_modpack_memory():
    global _modpack_cache, _modpack_loaded
    if _modpack_loaded or not _current_modpack_file:
        return
    _modpack_cache = _read_and_clean_file(_current_modpack_file)
    _modpack_loaded = True


def get_cached_translation(text, target_lang="한국어 (Korean)"):
    if not _global_loaded:
        load_memory()
    target_lang_norm = target_lang.replace(" ", "")

    if _is_short_text(text) and _current_modpack_id:
        with _lock:
            _load_modpack_memory()
        result = _lookup_safe(_modpack_cache, text, target_lang_norm)
        if result is not None:
            return result

    result = _lookup_safe(_global_cache, text, target_lang_norm)
    if result is not None:
        return result

    result = _lookup_safe(_items_cache, text, target_lang_norm)
    if result is not None:
        return result

    return _lookup_safe(_books_cache, text, target_lang_norm)


def add_to_memory(text, translated_text, target_lang="한국어 (Korean)"):
    if not is_valid_translation(text, translated_text, target_lang):
        return

    if not _global_loaded:
        load_memory()

    target_lang_norm = target_lang.replace(" ", "")
    with _lock:
        if _is_short_text(text) and _current_modpack_id:
            _load_modpack_memory()
            _store(_modpack_cache, text, translated_text, target_lang_norm)
        else:
            _store(_global_cache, text, translated_text, target_lang_norm)


def get_cached_item_translation(text, target_lang="한국어 (Korean)"):
    if not _items_loaded:
        load_memory()
    target_lang_norm = target_lang.replace(" ", "")
    result = _lookup_safe(_items_cache, text, target_lang_norm)
    if result is not None:
        return result
    result = _lookup_safe(_global_cache, text, target_lang_norm)
    if result is not None:
        return result
    return _lookup_safe(_books_cache, text, target_lang_norm)


def add_item_to_memory(text, translated_text, target_lang="한국어 (Korean)"):
    if not is_valid_translation(text, translated_text, target_lang):
        return
    if not _items_loaded:
        load_memory()
    target_lang_norm = target_lang.replace(" ", "")
    with _lock:
        _store(_items_cache, text, translated_text, target_lang_norm)


def get_cached_book_translation(text, target_lang="한국어 (Korean)"):
    if not _books_loaded:
        load_memory()
    target_lang_norm = target_lang.replace(" ", "")
    result = _lookup_safe(_books_cache, text, target_lang_norm)
    if result is not None:
        return result
    result = _lookup_safe(_global_cache, text, target_lang_norm)
    if result is not None:
        return result
    return _lookup_safe(_items_cache, text, target_lang_norm)


def add_book_to_memory(text, translated_text, target_lang="한국어 (Korean)"):
    if not is_valid_translation(text, translated_text, target_lang):
        return
    if not _books_loaded:
        load_memory()
    target_lang_norm = target_lang.replace(" ", "")
    with _lock:
        _store(_books_cache, text, translated_text, target_lang_norm)


def _safe_save_file(filepath, data):
    if not filepath:
        return None
    try:
        disk_data = _read_and_clean_file(filepath)
        merged = {}
        all_keys = set(list(disk_data.keys()) + list(data.keys()))
        for lang in all_keys:
            merged[lang] = {}
            if lang in disk_data: merged[lang].update(disk_data[lang])
            if lang in data: merged[lang].update(data[lang])

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
        logging.error(f"메모리 파일 저장 실패 ({filepath}): {e}")
        return data


def save_memory():
    """로컬 4개 파일 저장 및 Supabase 3개 개별 테이블에 비동기 업로드 (UPSERT)"""
    with _lock:
        merged_global = _safe_save_file(MEMORY_FILE, _global_cache)
        if merged_global is not None:
            _global_cache.clear()
            _global_cache.update(merged_global)

        if _current_modpack_file and _modpack_cache:
            merged_modpack = _safe_save_file(_current_modpack_file, _modpack_cache)
            if merged_modpack is not None:
                _modpack_cache.clear()
                _modpack_cache.update(merged_modpack)

        merged_items = _safe_save_file(ITEMS_MEMORY_FILE, _items_cache)
        if merged_items is not None:
            _items_cache.clear()
            _items_cache.update(merged_items)

        merged_books = _safe_save_file(BOOKS_MEMORY_FILE, _books_cache)
        if merged_books is not None:
            _books_cache.clear()
            _books_cache.update(merged_books)

    # Supabase 3개 개별 테이블로 자동 업로드
    upload_to_supabase(_global_cache, "general")
    upload_to_supabase(_items_cache, "items")
    upload_to_supabase(_books_cache, "books")


# ====================================================================
# Supabase 클라우드 동기화 (3개 테이블 분리 REST API 통신)
# ====================================================================

def sync_from_supabase():
    """Supabase의 3개 개별 테이블에서 번역 메모리 고속 병렬 다운로드 및 로컬 캐시 병합"""
    if "YOUR_PROJECT_REF" in SUPABASE_URL:
        return

    def _sync_single_table(category, table_name, target_cache):
        headers = {
            "apikey": SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
            "Prefer": "count=exact",
        }
        try:
            # 1. 전체 행 수 확인
            res = requests.get(f"{SUPABASE_URL}/rest/v1/{table_name}?select=src&limit=1", headers=headers, timeout=10)
            if res.status_code not in (200, 206):
                return
            content_range = res.headers.get("content-range", "")
            total_count = int(content_range.split('/')[-1]) if '/' in content_range else 0
            if total_count <= 0:
                return

            def fetch_page(offset):
                url = f"{SUPABASE_URL}/rest/v1/{table_name}?select=lang,src,tgt&limit=1000&offset={offset}"
                r = requests.get(url, headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {SUPABASE_ANON_KEY}"}, timeout=15)
                return r.json() if r.status_code == 200 else []

            offsets = list(range(0, total_count, 1000))
            with ThreadPoolExecutor(max_workers=8) as executor:
                for page in executor.map(fetch_page, offsets):
                    with _lock:
                        for row in page:
                            lang = row.get("lang", "한국어 (Korean)")
                            src = row.get("src")
                            tgt = row.get("tgt")

                            if not is_valid_translation(src, tgt, lang):
                                continue

                            if lang not in target_cache:
                                target_cache[lang] = {}
                            if src not in target_cache[lang]:
                                target_cache[lang][src] = tgt
        except Exception as e:
            logging.warning(f"Supabase [{table_name}] 동기화 실패: {e}")

    def _async_download_all():
        with ThreadPoolExecutor(max_workers=3) as executor:
            executor.submit(_sync_single_table, "general", TABLE_MAP["general"], _global_cache)
            executor.submit(_sync_single_table, "items", TABLE_MAP["items"], _items_cache)
            executor.submit(_sync_single_table, "books", TABLE_MAP["books"], _books_cache)

        # 동기화 완료 후 로컬 파일에도 안전하게 자동 저장
        save_memory()

    threading.Thread(target=_async_download_all, daemon=True).start()


def upload_to_supabase(cache_dict, category="general"):
    """Supabase 해당 테이블에 번역 데이터 일괄 업로드 (중복 자동 병합 - UPSERT)"""
    if "YOUR_PROJECT_REF" in SUPABASE_URL or not cache_dict:
        return

    table_name = TABLE_MAP.get(category, "translation_memory")

    def _async_upload():
        records = []
        for lang, entries in cache_dict.items():
            if not isinstance(entries, dict):
                continue
            for src, tgt in entries.items():
                if is_valid_translation(src, tgt, lang):
                    rec = {
                        "lang": lang,
                        "src": src,
                        "tgt": tgt,
                    }
                    if category == "general":
                        rec["category"] = "general"
                    records.append(rec)

        if not records:
            return

        headers = {
            "apikey": SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",  # 충돌 시 자동 업데이트 (UPSERT)
        }
        
        # PostgREST UPSERT는 on_conflict 매개변수가 필수입니다.
        if category == "general":
            url = f"{SUPABASE_URL}/rest/v1/{table_name}?on_conflict=category,lang,src"
        else:
            url = f"{SUPABASE_URL}/rest/v1/{table_name}?on_conflict=lang,src"

        # 1000개씩 청크 단위로 분할 업로드
        batch_size = 1000
        for i in range(0, len(records), batch_size):
            chunk = records[i:i + batch_size]
            try:
                requests.post(url, headers=headers, json=chunk, timeout=20)
            except Exception as e:
                logging.warning(f"Supabase 업로드 실패 ({table_name}): {e}")

    threading.Thread(target=_async_upload, daemon=True).start()
