import json
import os
import gzip
import hashlib
import shutil
import threading
import logging
import re
import requests
import time
from concurrent.futures import ThreadPoolExecutor
from constants import has_hangul

def _get_cache_dir():
    """로컬 캐시 파일이 저장될 전용 폴더 (항상 dist 하위 폴더에 저장)"""
    import sys
    if getattr(sys, 'frozen', False):
        # PyInstaller EXE 실행 환경: 실행 파일이 위치한 폴더의 dist 하위 폴더
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        base = os.path.join(exe_dir, "dist")
    else:
        # 개발 환경: 프로젝트 루트의 dist 폴더
        repo_root = os.path.dirname(os.path.abspath(__file__))
        base = os.path.join(repo_root, "dist")
    
    os.makedirs(base, exist_ok=True)
    return base

CACHE_DIR = _get_cache_dir()
MEMORY_FILE = os.path.join(CACHE_DIR, "translation_memory.json")
BACKUP_FILE = os.path.join(CACHE_DIR, "translation_memory.json.bak")
ITEMS_MEMORY_FILE = os.path.join(CACHE_DIR, "translation_memory_items.json")
BOOKS_MEMORY_FILE = os.path.join(CACHE_DIR, "translation_memory_books.json")
SHORT_TEXT_THRESHOLD = 30

# ====================================================================
# Supabase 클라우드 설정
# ====================================================================
SUPABASE_URL = "https://rwyedqmztbxsflndgsmt.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJ3eWVkcW16dGJ4c2ZsbmRnc210Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODc4MTIzNDksImV4cCI6MjEwMzM4ODM0OX0.8Lrxcp1af5ea6JYMhVIBXZAybkmRumindRUWgcq8Kdc"

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
    """모드팩 컨텍스트 설정 (짧은 문장 격리 및 일관된 해시 보장)"""
    global _current_modpack_id, _current_modpack_file, _modpack_loaded
    with _lock:
        if modpack_path:
            # .zip 확장자 여부와 관계없이 동일한 모드팩 식별자 생성
            base_part = os.path.basename(modpack_path.rstrip(os.sep))
            modpack_name = os.path.splitext(base_part)[0]
            _current_modpack_id = hashlib.md5(modpack_name.encode('utf-8')).hexdigest()[:8]
            _current_modpack_file = os.path.join(CACHE_DIR, f"translation_memory_modpack_{_current_modpack_id}.json")
        else:
            _current_modpack_id = None
            _current_modpack_file = None
        _modpack_cache.clear()
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
    # Mock(모의 번역) 오염 문자열 차단
    if "[번역됨]" in t_clean or t_clean.startswith("[번역됨]") or "[Mock]" in t_clean:
        return False
    if "한국어" in target_lang or "Korean" in target_lang:
        if re.search(r'[A-Za-z]', s_clean) and not has_hangul(t_clean):
            return False
    return True


def _read_and_clean_file(filepath):
    """파일 로드 시 오염된 항목(원문==번역문) 자동 제거 및 언어 키 통합"""
    if not filepath or not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        cleaned = {}
        for lang, translations in data.items():
            if not isinstance(translations, dict):
                continue
            std_lang = "한국어 (Korean)" if ("Korean" in lang or "한국어" in lang) else lang
            if std_lang not in cleaned:
                cleaned[std_lang] = {}
            lang_norm = std_lang.replace(" ", "")
            for src, tgt in translations.items():
                if is_valid_translation(src, tgt, std_lang):
                    cleaned[std_lang][src] = tgt
                    _index_template(src, tgt, lang_norm)
        return cleaned
    except Exception:
        return {}


# ====================================================================
# 스마트 퍼지 & 템플릿 캐싱 헬퍼
# ====================================================================

def _extract_formatting(text):
    """문장 앞뒤 및 내부의 마인크래프트 색상/스타일 서식 코드 분리"""
    if not isinstance(text, str) or not text.strip():
        return "", text, ""
    prefix_match = re.match(r'^([&§][0-9a-fk-orA-FK-OR]|\s)+', text)
    prefix = prefix_match.group(0) if prefix_match else ""
    suffix_match = re.search(r'([&§][0-9a-fk-orA-FK-OR]|\s)+$', text)
    suffix = suffix_match.group(0) if suffix_match else ""
    clean = re.sub(r'[&§][0-9a-fk-orA-FK-OR]', '', text).strip()
    return prefix, clean, suffix


def _restore_formatting(clean_trans, prefix, suffix):
    """분리되었던 원본 서식 코드를 번역문에 다시 씌움"""
    p = prefix.strip()
    s = suffix.strip()
    res = clean_trans
    if p and not res.startswith(p):
        res = f"{p}{res}"
    if s and not res.endswith(s):
        res = f"{res}{s}"
    return res.strip()


_template_index = {}  # (lang_norm, tmpl_src) -> (tmpl_tgt, num_count)

def _index_template(src, tgt, lang_norm):
    """문장 번역 쌍을 숫자 정규화 템플릿 인덱스에 O(1) 저장"""
    if len(src) < 10 or len(tgt) < 4:
        return
    nums_src = re.findall(r'\d+', src)
    if not nums_src or len(nums_src) > 4:
        return
    nums_tgt = re.findall(r'\d+', tgt)
    if len(nums_tgt) != len(nums_src):
        return
    tmpl_src = re.sub(r'\d+', '<NUM>', src)
    tmpl_tgt = re.sub(r'\d+', '<NUM>', tgt)
    _template_index[(lang_norm, tmpl_src)] = (tmpl_tgt, len(nums_src))


def _lookup_template(text, target_lang_norm):
    """숫자만 변경된 문장을 템플릿 인덱스에서 O(1)로 조회 및 자동 치환"""
    nums = re.findall(r'\d+', text)
    if not nums or len(nums) > 4:
        return None
    tmpl = re.sub(r'\d+', '<NUM>', text)
    match = _template_index.get((target_lang_norm, tmpl))
    if not match:
        return None
    tmpl_tgt, count = match
    if count != len(nums):
        return None
    res = tmpl_tgt
    for n in nums:
        res = res.replace('<NUM>', str(n), 1)
    return res


def _lookup_safe(cache, text, target_lang_norm):
    """
    3단계 스마트 캐시 조회:
    1단계: 100% 완전 일치 (Exact Match)
    2단계: 색상/서식 코드 무시 일치 (Formatting-Agnostic Match)
    3단계: O(1) 초고속 숫자 템플릿 일치 (Number-Template Match)
    """
    for lang_key, entries in cache.items():
        if lang_key.replace(" ", "") != target_lang_norm:
            continue

        # 1단계: 완전 일치
        if text in entries:
            val = entries[text]
            if is_valid_translation(text, val, lang_key):
                return val
            else:
                with _lock:
                    entries.pop(text, None)

        # 2단계: 색상 및 서식 코드 무시 매칭
        prefix, clean, suffix = _extract_formatting(text)
        if clean and clean != text and clean in entries:
            clean_val = entries[clean]
            if is_valid_translation(clean, clean_val, lang_key):
                return _restore_formatting(clean_val, prefix, suffix)

        # 3단계: 숫자 템플릿 매칭 (O(1) 인덱스 조회)
        template_res = _lookup_template(clean if clean else text, target_lang_norm)
        if template_res and is_valid_translation(text, template_res, lang_key):
            return _restore_formatting(template_res, prefix, suffix) if clean else template_res

    return None


def find_few_shot_examples(query_texts, target_lang="한국어 (Korean)", max_examples=3):
    """11.5만 개 메모리에서 가장 유사한 이전 번역 예시를 고속 추출하여 AI 프롬프트에 주입"""
    if not query_texts:
        return []
    if not _global_loaded:
        load_memory()
    target_lang_norm = target_lang.replace(" ", "")
    cache = _global_cache.get(target_lang_norm, {})
    if not cache:
        cache = _global_cache.get("한국어(Korean)", {})
    if not cache:
        return []

    # 쿼리에서 핵심 단어 추출
    all_words = set()
    for q in query_texts:
        if isinstance(q, str):
            words = re.findall(r'[a-zA-Z]{3,}', q.lower())
            all_words.update(words)

    stop_words = {'the', 'and', 'for', 'you', 'with', 'this', 'that', 'have', 'from', 'your', 'are', 'can', 'not', 'will', 'all'}
    meaningful_words = all_words - stop_words
    if not meaningful_words:
        return []

    scored = []
    for src, tgt in cache.items():
        if len(src) < 15 or len(src) > 140 or '{' in src or '<' in src:
            continue
        src_lower = src.lower()
        match_count = sum(1 for w in meaningful_words if w in src_lower)
        if match_count >= 2:
            scored.append((match_count, src, tgt))
            if len(scored) >= 20:
                break

    scored.sort(key=lambda x: x[0], reverse=True)
    return [(s, t) for _, s, t in scored[:max_examples]]


_dirty_general = {}
_dirty_items = {}
_dirty_books = {}

def _store(cache, text, translated_text, target_lang_norm):
    target_lang_key = target_lang_norm
    for k in cache.keys():
        if k.replace(" ", "") == target_lang_norm:
            target_lang_key = k
            break
    if target_lang_key not in cache:
        cache[target_lang_key] = {}
    cache[target_lang_key][text] = translated_text
    
    # 더티 캐시(새로 추가된 데이터)에도 저장하여 DB 업로드 시 전체가 아닌 신규 데이터만 업로드되도록 함
    dirty_target = None
    if cache is _global_cache:
        dirty_target = _dirty_general
    elif cache is _items_cache:
        dirty_target = _dirty_items
    elif cache is _books_cache:
        dirty_target = _dirty_books

    if dirty_target is not None:
        if target_lang_key not in dirty_target:
            dirty_target[target_lang_key] = {}
        dirty_target[target_lang_key][text] = translated_text

    _index_template(text, translated_text, target_lang_norm)


def load_memory():
    """로컬 메모리 로드 및 Supabase 최신 데이터 동기화"""
    global _global_loaded, _items_loaded, _books_loaded
    with _lock:
        if not _global_loaded:
            loaded_g = _read_and_clean_file(MEMORY_FILE)
            _global_cache.clear()
            _global_cache.update(loaded_g)
            _global_loaded = True
        if not _items_loaded:
            loaded_i = _read_and_clean_file(ITEMS_MEMORY_FILE)
            _items_cache.clear()
            _items_cache.update(loaded_i)
            _items_loaded = True
        if not _books_loaded:
            loaded_b = _read_and_clean_file(BOOKS_MEMORY_FILE)
            _books_cache.clear()
            _books_cache.update(loaded_b)
            _books_loaded = True

    sync_from_supabase()


def _load_modpack_memory():
    global _modpack_loaded
    if _modpack_loaded or not _current_modpack_file:
        return
    loaded_m = _read_and_clean_file(_current_modpack_file)
    _modpack_cache.clear()
    _modpack_cache.update(loaded_m)
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

    result = _lookup_safe(_books_cache, text, target_lang_norm)
    if result is not None:
        return result

    # 💡 시스템 코드/명령어/단위/포맷터: 번역 불필요 대상은 원문 그대로 즉시 반환
    from translation_engines import is_code_or_id
    if is_code_or_id(text):
        return text
    return None


def add_to_memory(text, translated_text, target_lang="한국어 (Korean)"):
    if not is_valid_translation(text, translated_text, target_lang):
        return

    if not _global_loaded:
        load_memory()

    target_lang_norm = target_lang.replace(" ", "")
    with _lock:
        if _current_modpack_id:
            _load_modpack_memory()
            _store(_modpack_cache, text, translated_text, target_lang_norm)
        # 클라우드 동기화 및 전역 재사용을 위해 _global_cache에도 동시 보존
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
    result = _lookup_safe(_books_cache, text, target_lang_norm)
    if result is not None:
        return result

    from translation_engines import is_code_or_id
    if is_code_or_id(text):
        return text
    return None


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
    result = _lookup_safe(_items_cache, text, target_lang_norm)
    if result is not None:
        return result

    from translation_engines import is_code_or_id
    if is_code_or_id(text):
        return text
    return None


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
    """로컬 4개 파일 저장 및 Supabase 3개 개별 테이블에 신규(Dirty) 데이터만 비동기 업로드 (UPSERT)"""
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

        # 현재 업로드할 더티(신규) 캐시 복사 및 원본 초기화
        dirty_general_copy = {k: v.copy() for k, v in _dirty_general.items()}
        dirty_items_copy = {k: v.copy() for k, v in _dirty_items.items()}
        dirty_books_copy = {k: v.copy() for k, v in _dirty_books.items()}
        _dirty_general.clear()
        _dirty_items.clear()
        _dirty_books.clear()

    # Supabase 3개 개별 테이블로 신규 델타 자동 업로드 (UPSERT)
    if dirty_general_copy: upload_to_supabase(dirty_general_copy, "general")
    if dirty_items_copy: upload_to_supabase(dirty_items_copy, "items")
    if dirty_books_copy: upload_to_supabase(dirty_books_copy, "books")

    # Supabase Storage Master Gzip 자동 패킹 및 동기화 (완전 자동화)
    upload_master_to_storage()

    # DB 테이블 누적 용량 200MB 초과 여부 체크 및 자동 컴팩션
    check_and_auto_compact()


# ====================================================================
# 하이브리드 클라우드 동기화 (Master Gzip Storage + Live Delta DB)
# ====================================================================

STORAGE_BUCKET = "translations"

MASTER_FILES = {
    "general": ("master_general.json.gz", _global_cache),
    "items": ("master_items.json.gz", _items_cache),
    "books": ("master_books.json.gz", _books_cache),
}

# 🛡️ 3단계 안전 차단기 (Safety Circuit Breaker / Shutdown) 설정
AUTO_COMPACT_SIZE_MB = 100  # 1단계: 100MB 도달 시 자동 압축 및 DB 0MB 리셋
AUTO_COMPACT_BYTES_THRESHOLD = AUTO_COMPACT_SIZE_MB * 1024 * 1024

EMERGENCY_SHUTDOWN_SIZE_MB = 150  # 2단계: 150MB 초과 시 서버 폭주 방지를 위해 클라우드 업로드 즉시 셧다운(로컬 전용 모드 전환)
EMERGENCY_SHUTDOWN_BYTES = EMERGENCY_SHUTDOWN_SIZE_MB * 1024 * 1024

_is_compacting = False
_circuit_breaker_until = 0.0
_consecutive_upload_errors = 0
_last_size_check_time = 0.0
_last_known_db_bytes = 0

def _get_server_db_bytes():
    """서버 측 실제 물리 용량을 조회 (30초 캐싱)"""
    global _last_size_check_time, _last_known_db_bytes
    now = time.time()
    if now - _last_size_check_time < 30 and _last_known_db_bytes > 0:
        return _last_known_db_bytes

    if "YOUR_PROJECT_REF" in SUPABASE_URL:
        return 0

    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
    }
    total_db_bytes = 0
    try:
        rpc_res = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/get_translation_memory_size_bytes", headers=headers, json={}, timeout=5)
        if rpc_res.status_code == 200 and rpc_res.text.strip().isdigit():
            total_db_bytes = int(rpc_res.text.strip())
    except Exception:
        pass

    if total_db_bytes <= 0:
        h_count = dict(headers)
        h_count["Prefer"] = "count=planned"
        total_rows = 0
        for cat, table_name in TABLE_MAP.items():
            try:
                res = requests.get(f"{SUPABASE_URL}/rest/v1/{table_name}?select=src&limit=1", headers=h_count, timeout=5)
                cr = res.headers.get("content-range", "")
                cnt = int(cr.split('/')[-1]) if '/' in cr else 0
                total_rows += cnt
            except Exception:
                pass
        total_db_bytes = total_rows * 700

    _last_known_db_bytes = total_db_bytes
    _last_size_check_time = now
    return total_db_bytes


def is_cloud_upload_safe():
    """클라우드 업로드 전 안전성(서킷 브레이커 & 셧다운) 검사"""
    now = time.time()
    if now < _circuit_breaker_until:
        return False, "통신 오류로 인한 안전 차단기(Circuit Breaker) 활성화 중"

    db_bytes = _get_server_db_bytes()
    if db_bytes >= EMERGENCY_SHUTDOWN_BYTES:
        return False, f"DB 용량 한계 도달 ({db_bytes / (1024*1024):.1f}MB >= {EMERGENCY_SHUTDOWN_SIZE_MB}MB) 긴급 셧다운 가동"

    return True, "정상"


def check_and_auto_compact(log_callback=None):
    """DB 3개 테이블의 총 용량이 100MB를 넘으면 Master Gzip으로 완전 통합 압축 후 DB를 0MB로 리셋"""
    global _is_compacting
    if _is_compacting or "YOUR_PROJECT_REF" in SUPABASE_URL:
        return

    def _async_compaction():
        global _is_compacting
        _is_compacting = True
        try:
            total_db_bytes = _get_server_db_bytes()
            total_mb = total_db_bytes / (1024 * 1024)
            if total_db_bytes < AUTO_COMPACT_BYTES_THRESHOLD:
                return

            if log_callback:
                log_callback(f"📦 [자동 컴팩션 감지] DB 총 누적 용량이 {total_mb:.1f} MB에 도달하여(기준: {AUTO_COMPACT_SIZE_MB}MB) 마스터 압축 파일로 통합을 시작합니다...")

            headers = {
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
            }
            # 2. 모든 최신 DB 데이터를 메모리로 다운로드 및 병합
            for cat, table_name in TABLE_MAP.items():
                target_cache = _global_cache if cat == "general" else (_items_cache if cat == "items" else _books_cache)
                try:
                    h_t = dict(headers)
                    h_t["Prefer"] = "count=planned"
                    res = requests.get(f"{SUPABASE_URL}/rest/v1/{table_name}?select=src&limit=1", headers=h_t, timeout=10)
                    cr = res.headers.get("content-range", "")
                    table_count = int(cr.split('/')[-1]) if '/' in cr else 0
                    if table_count <= 0:
                        continue

                    def fetch_p(offset):
                        u = f"{SUPABASE_URL}/rest/v1/{table_name}?select=lang,src,tgt&limit=1000&offset={offset}"
                        r = requests.get(u, headers=headers, timeout=15)
                        return r.json() if r.status_code == 200 else []

                    offsets = list(range(0, table_count, 1000))
                    with ThreadPoolExecutor(max_workers=8) as ex:
                        for page in ex.map(fetch_p, offsets):
                            with _lock:
                                for row in page:
                                    lang = row.get("lang", "한국어 (Korean)")
                                    src = row.get("src")
                                    tgt = row.get("tgt")
                                    if is_valid_translation(src, tgt, lang):
                                        if lang not in target_cache: target_cache[lang] = {}
                                        target_cache[lang][src] = tgt
                except Exception as e:
                    logging.warning(f"컴팩션 중 DB [{table_name}] 다운로드 실패: {e}")
                    return

            # 3. 마스터 압축 파일(master_*.json.gz) 생성 및 Storage 업로드
            all_uploads_success = True
            for cat, (gz_name, cache) in MASTER_FILES.items():
                with _lock:
                    data_copy = {k: v.copy() for k, v in cache.items() if isinstance(v, dict)}
                if not data_copy:
                    continue
                raw_json = json.dumps(data_copy, ensure_ascii=False, indent=2).encode("utf-8")
                gz_bytes = gzip.compress(raw_json, compresslevel=9)
                
                upload_url = f"{SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}/{gz_name}"
                h = {
                    "apikey": SUPABASE_ANON_KEY,
                    "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
                    "Content-Type": "application/gzip",
                    "x-upsert": "true",
                }
                r = requests.post(upload_url, headers=h, data=gz_bytes, timeout=30)
                if r.status_code not in (200, 201):
                    r = requests.put(upload_url, headers=h, data=gz_bytes, timeout=30)
                if r.status_code not in (200, 201):
                    all_uploads_success = False
                    break

            # 4. Storage 마스터 업로드가 100% 성공했을 때만 안전하게 DB 테이블 비우기
            if all_uploads_success:
                try:
                    # 보안 RPC 함수를 호출하여 0.01초 만에 안전하게 TRUNCATE 리셋
                    requests.post(f"{SUPABASE_URL}/rest/v1/rpc/truncate_all_translation_tables", headers=headers, json={}, timeout=15)
                except Exception as e:
                    logging.warning(f"DB 리셋 RPC 호출 실패: {e}")

                save_memory()
                msg = f"✨ [스마트 자동 컴팩션 완료] DB 데이터를 master_memory.json.gz에 통합 압축하고 DB 용량을 0MB로 리셋했습니다!"
                logging.info(msg)
                if log_callback:
                    log_callback(msg)
        finally:
            _is_compacting = False

    threading.Thread(target=_async_compaction, daemon=True).start()

_storage_upload_timer = None

def upload_master_to_storage():
    """인메모리 전체 최신 캐시를 Gzip 초압축하여 Supabase Storage에 100% 자동 백그라운드 동기화"""
    if "YOUR_PROJECT_REF" in SUPABASE_URL:
        return

    def _async_pack():
        try:
            for cat, (gz_name, cache) in MASTER_FILES.items():
                with _lock:
                    data_copy = {k: v.copy() for k, v in cache.items() if isinstance(v, dict)}
                if not data_copy:
                    continue
                raw_json = json.dumps(data_copy, ensure_ascii=False, indent=2).encode("utf-8")
                gz_bytes = gzip.compress(raw_json, compresslevel=9)
                
                upload_url = f"{SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}/{gz_name}"
                h = {
                    "apikey": SUPABASE_ANON_KEY,
                    "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
                    "Content-Type": "application/gzip",
                    "x-upsert": "true",
                }
                r = requests.post(upload_url, headers=h, data=gz_bytes, timeout=30)
                if r.status_code not in (200, 201):
                    requests.put(upload_url, headers=h, data=gz_bytes, timeout=30)
        except Exception as e:
            logging.debug(f"자동 마스터 패킹 백그라운드 실패: {e}")

    # 디바운스: 8초 내 중복 호출 방지
    global _storage_upload_timer
    with _lock:
        if _storage_upload_timer:
            _storage_upload_timer.cancel()
        _storage_upload_timer = threading.Timer(8.0, lambda: threading.Thread(target=_async_pack, daemon=True).start())
        _storage_upload_timer.start()

_cloud_sync_done = threading.Event()
_is_syncing = False

def wait_for_cloud_sync(timeout=8.0):
    """클라우드 마스터 메모리가 완전히 다운로드될 때까지 대기"""
    if _cloud_sync_done.is_set():
        return True
    return _cloud_sync_done.wait(timeout=timeout)


def sync_from_supabase(force_wait=False):
    """
    하이브리드 2단계 고속 동기화:
    1단계: Supabase Storage에서 3MB 마스터 압축 파일(master_*.json.gz)을 0.3초 만에 병렬 다운로드하여 Base Memory 구축
    2단계: Supabase DB 테이블에서 최신 Delta 레코드를 동기화하여 실시간 병합
    """
    global _is_syncing
    if "YOUR_PROJECT_REF" in SUPABASE_URL:
        _cloud_sync_done.set()
        return

    with _lock:
        if _is_syncing:
            return
        _is_syncing = True

    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
    }

    def _download_master_storage(category, filename, target_cache):
        """Storage에서 master_*.json.gz 초고속 단일 다운로드 및 decompress"""
        try:
            url = f"{SUPABASE_URL}/storage/v1/object/public/{STORAGE_BUCKET}/{filename}?t={int(time.time())}"
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 200 and r.content:
                raw_bytes = r.content
                if filename.endswith(".gz"):
                    raw_bytes = gzip.decompress(raw_bytes)
                parsed = json.loads(raw_bytes.decode("utf-8"))
                if isinstance(parsed, dict):
                    with _lock:
                        for lang, entries in parsed.items():
                            if not isinstance(entries, dict):
                                continue
                            if lang not in target_cache:
                                target_cache[lang] = {}
                            target_cache[lang].update(entries)
                            lang_norm = lang.replace(" ", "")
                            for src, tgt in entries.items():
                                _index_template(src, tgt, lang_norm)
                    logging.info(f"✅ Storage Master [{filename}] 로드 성공")
        except Exception as e:
            logging.debug(f"Storage Master [{filename}] 조회 실패 또는 미생성 (DB Fallback): {e}")

    def _sync_single_table(category, table_name, target_cache):
        """DB 테이블에서 최신 변경분(Delta) 동기화"""
        try:
            h = dict(headers)
            h["Prefer"] = "count=planned"
            res = requests.get(f"{SUPABASE_URL}/rest/v1/{table_name}?select=src&limit=1", headers=h, timeout=10)
            if res.status_code not in (200, 206):
                return
            content_range = res.headers.get("content-range", "")
            total_count = int(content_range.split('/')[-1]) if '/' in content_range else 0
            if total_count <= 0:
                return

            def fetch_page(offset):
                url = f"{SUPABASE_URL}/rest/v1/{table_name}?select=lang,src,tgt&limit=1000&offset={offset}"
                r = requests.get(url, headers=headers, timeout=15)
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
                            target_cache[lang][src] = tgt
                            _index_template(src, tgt, lang.replace(" ", ""))
        except Exception as e:
            logging.warning(f"Supabase DB [{table_name}] 동기화 실패: {e}")

    def _async_hybrid_sync():
        global _is_syncing
        try:
            # 1단계: Storage Master 초고속 병렬 로드 (0.3초)
            with ThreadPoolExecutor(max_workers=3) as executor:
                executor.submit(_download_master_storage, "general", "master_general.json.gz", _global_cache)
                executor.submit(_download_master_storage, "items", "master_items.json.gz", _items_cache)
                executor.submit(_download_master_storage, "books", "master_books.json.gz", _books_cache)

            # 2단계: DB Table Live Delta 동기화
            with ThreadPoolExecutor(max_workers=3) as executor:
                executor.submit(_sync_single_table, "general", TABLE_MAP["general"], _global_cache)
                executor.submit(_sync_single_table, "items", TABLE_MAP["items"], _items_cache)
                executor.submit(_sync_single_table, "books", TABLE_MAP["books"], _books_cache)

            # 3단계: 로컬 백업 자동 저장
            save_memory()
        finally:
            _is_syncing = False
            _cloud_sync_done.set()

    t = threading.Thread(target=_async_hybrid_sync, daemon=True)
    t.start()
    if force_wait:
        t.join(timeout=10.0)


def upload_to_supabase(cache_dict, category="general"):
    """Supabase 해당 테이블에 번역 데이터 일괄 업로드 (중복 자동 병합 - UPSERT)"""
    global _consecutive_upload_errors, _circuit_breaker_until
    if "YOUR_PROJECT_REF" in SUPABASE_URL or not cache_dict:
        return

    safe, reason = is_cloud_upload_safe()
    if not safe:
        logging.warning(f"🛡️ [클라우드 안전 셧다운] {reason}. 로컬 디스크에만 안전하게 저장합니다.")
        return

    table_name = TABLE_MAP.get(category, "translation_memory")

    records = []
    for lang, entries in list(cache_dict.items()):
        if not isinstance(entries, dict):
            continue
        for src, tgt in list(entries.items()):
            if is_valid_translation(src, tgt, lang):
                records.append({
                    "lang": lang,
                    "src": src,
                    "tgt": tgt,
                })

    if not records:
        return

    def _async_upload(records_to_upload):
        global _consecutive_upload_errors, _circuit_breaker_until
        headers = {
            "apikey": SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",  # 충돌 시 자동 업데이트 (UPSERT)
        }
        
        # 3개 분리 테이블 모두 on_conflict=lang,src 입니다.
        url = f"{SUPABASE_URL}/rest/v1/{table_name}?on_conflict=lang,src"

        # 1000개씩 청크 단위로 분할 업로드
        batch_size = 1000
        for i in range(0, len(records_to_upload), batch_size):
            chunk = records_to_upload[i:i + batch_size]
            try:
                r = requests.post(url, headers=headers, json=chunk, timeout=20)
                if r.status_code not in (200, 201):
                    _consecutive_upload_errors += 1
                    logging.warning(f"Supabase 업로드 실패 ({table_name}): {r.status_code} {r.text}")
                    if _consecutive_upload_errors >= 3:
                        _circuit_breaker_until = time.time() + 300  # 5분간 차단
                        logging.warning("⚠️ [서킷 브레이커 작동] 연속 통신 실패로 인해 5분간 클라우드 업로드를 중단합니다.")
                else:
                    _consecutive_upload_errors = 0
                    logging.info(f"✅ Supabase [{table_name}] {len(chunk)}개 동기화 성공")
            except Exception as e:
                _consecutive_upload_errors += 1
                logging.warning(f"Supabase 업로드 통신 오류 ({table_name}): {e}")
                if _consecutive_upload_errors >= 3:
                    _circuit_breaker_until = time.time() + 300
                    logging.warning("⚠️ [서킷 브레이커 작동] 연속 통신 실패로 인해 5분간 클라우드 업로드를 중단합니다.")

    threading.Thread(target=_async_upload, args=(records,), daemon=True).start()

# ====================================================================
# Memory Editor API (검색, 수정, 삭제)
# ====================================================================

def search_memory(query, category="all", target_lang="한국어 (Korean)", limit=100, search_field="all"):
    """
    모든 캐시(일반, 아이템, 책)에서 주어진 검색어(query)를 포함하는 원문/번역문을 찾습니다.
    Args:
        search_field (str): 'all' (원문+번역문 통합), 'src' (영어 원문만), 'tgt' (한글 번역문만)
    반환 형태: [{"category": "items", "src": "...", "tgt": "..."}]
    """
    if not _global_loaded: load_memory()
    target_lang_norm = target_lang.replace(" ", "")
    results = []
    
    query_lower = query.lower()

    def _search_in_cache(cache, cat_name):
        nonlocal results
        for lang_key, entries in cache.items():
            if lang_key.replace(" ", "") != target_lang_norm:
                continue
            for src, tgt in entries.items():
                match = False
                if search_field in ("all", "src") and query_lower in src.lower():
                    match = True
                elif search_field in ("all", "tgt") and query_lower in tgt.lower():
                    match = True

                if match:
                    results.append({"category": cat_name, "src": src, "tgt": tgt})
                    if len(results) >= limit:
                        return True
        return False

    with _lock:
        if category in ("all", "items"):
            if _search_in_cache(_items_cache, "items"): return results
        if category in ("all", "general"):
            if _search_in_cache(_global_cache, "general"): return results
        if category in ("all", "books"):
            if _search_in_cache(_books_cache, "books"): return results
            
    return results

def update_memory_entry(category, original_src, new_tgt, target_lang="한국어 (Korean)"):
    """캐시 업데이트 후 로컬/클라우드에 저장합니다."""
    target_lang_norm = target_lang.replace(" ", "")
    cache_to_update = None
    
    if category == "items": cache_to_update = _items_cache
    elif category == "general": cache_to_update = _global_cache
    elif category == "books": cache_to_update = _books_cache
    else: return False

    with _lock:
        target_lang_key = target_lang_norm
        for k in cache_to_update.keys():
            if k.replace(" ", "") == target_lang_norm:
                target_lang_key = k
                break
        if target_lang_key not in cache_to_update:
            cache_to_update[target_lang_key] = {}
            
        cache_to_update[target_lang_key][original_src] = new_tgt
        _index_template(original_src, new_tgt, target_lang_norm)
        
    # 백그라운드로 저장 & 업로드 트리거 (이 파일 내 함수 활용)
    save_memory()
    return True

def delete_memory_entry(category, original_src, target_lang="한국어 (Korean)"):
    """캐시에서 항목을 삭제하고, 클라우드 DB에서도 삭제를 수행합니다."""
    target_lang_norm = target_lang.replace(" ", "")
    cache_to_update = None
    
    if category == "items": cache_to_update = _items_cache
    elif category == "general": cache_to_update = _global_cache
    elif category == "books": cache_to_update = _books_cache
    else: return False

    with _lock:
        deleted = False
        for k in cache_to_update.keys():
            if k.replace(" ", "") == target_lang_norm:
                if original_src in cache_to_update[k]:
                    del cache_to_update[k][original_src]
                    deleted = True
                break
                
    if deleted:
        save_memory()
        
        # Supabase DELETE 요청
        def _async_delete():
            if "YOUR_PROJECT_REF" in SUPABASE_URL: return
            table_name = TABLE_MAP.get(category, "translation_memory")
            headers = {
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {SUPABASE_ANON_KEY}"
            }
            try:
                import urllib.parse
                safe_src = urllib.parse.quote(original_src)
                url = f"{SUPABASE_URL}/rest/v1/{table_name}?src=eq.{safe_src}"
                requests.delete(url, headers=headers, timeout=10)
            except Exception as e:
                logging.warning(f"Supabase 삭제 실패: {e}")
                
        threading.Thread(target=_async_delete, daemon=True).start()
    return True


# ====================================================================
# 오역률 및 분탕 방지 검증 알고리즘 (Two-Pillar Validation)
# ====================================================================

_CHOSUNG_LIST = ['ㄱ', 'ㄲ', 'ㄴ', 'ㄷ', 'ㄸ', 'ㄹ', 'ㅁ', 'ㅂ', 'ㅃ', 'ㅅ', 'ㅆ', 'ㅇ', 'ㅈ', 'ㅉ', 'ㅊ', 'ㅋ', 'ㅌ', 'ㅍ', 'ㅎ']
_JUNGSUNG_LIST = ['ㅏ', 'ㅐ', 'ㅑ', 'ㅒ', 'ㅓ', 'ㅔ', 'ㅕ', 'ㅖ', 'ㅗ', 'ㅘ', 'ㅙ', 'ㅚ', 'ㅛ', 'ㅜ', 'ㅝ', 'ㅞ', 'ㅟ', 'ㅠ', 'ㅡ', 'ㅢ', 'ㅣ']
_JONGSUNG_LIST = ['', 'ㄱ', 'ㄲ', 'ㄳ', 'ㄴ', 'ㄵ', 'ㄶ', 'ㄷ', 'ㄹ', 'ㄺ', 'ㄻ', 'ㄼ', 'ㄽ', 'ㄾ', 'ㄿ', 'ㅀ', 'ㅁ', 'ㅂ', 'ㅄ', 'ㅅ', 'ㅆ', 'ㅇ', 'ㅈ', 'ㅊ', 'ㅋ', 'ㅌ', 'ㅍ', 'ㅎ']

def _decompose_korean(text):
    """한글 음절을 초성, 중성(모음), 종성으로 분해합니다."""
    syllables = []
    for ch in text:
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:
            s_idx = code - 0xAC00
            cho = _CHOSUNG_LIST[s_idx // (21 * 28)]
            jung = _JUNGSUNG_LIST[(s_idx % (21 * 28)) // 28]
            jong = _JONGSUNG_LIST[s_idx % 28]
            syllables.append((cho, jung, jong))
    return syllables

_ENG_KO_CONSONANTS = {
    't': ['ㅌ', 'ㄸ', 'ㄷ'], 'n': ['ㄴ', 'ㅇ', 'ㅁ'], 'k': ['ㅋ', 'ㄲ', 'ㄱ'], 'c': ['ㅋ', 'ㄲ', 'ㄱ', 'ㅅ'],
    'r': ['ㄹ'], 'l': ['ㄹ'], 's': ['ㅅ', 'ㅆ', 'ㅈ'], 'p': ['ㅍ', 'ㅃ', 'ㅂ'], 'b': ['ㅂ', 'ㅃ'],
    'm': ['ㅁ'], 'd': ['ㄷ', 'ㄸ'], 'g': ['ㄱ', 'ㄲ'], 'f': ['ㅍ'], 'v': ['ㅂ', 'ㅍ'],
    'z': ['ㅈ', 'ㅉ', 'ㅅ'], 'j': ['ㅈ', 'ㅉ'], 'h': ['ㅎ'], 'w': ['ㅇ', 'ㅂ'], 'y': ['ㅇ']
}

_ENG_KO_VOWELS = {
    'i': ['ㅣ', 'ㅔ', 'ㅏ'],
    'e': ['ㅔ', 'ㅓ', 'ㅐ', 'ㅣ'],
    'a': ['ㅏ', 'ㅐ', 'ㅔ', 'ㅓ'],
    'o': ['ㅗ', 'ㅓ', 'ㅏ', 'ㅜ'],
    'u': ['ㅓ', 'ㅜ', 'ㅠ'],
}

def _compare_syllables_phonetic(ref_ko, cand_ko):
    """두 한글 단어의 음절 단위 자음 및 모음 일치도를 교차 검증합니다."""
    ref_s = _decompose_korean(ref_ko)
    cand_s = _decompose_korean(cand_ko)
    if not ref_s or not cand_s:
        return 0.0, 0.0, 0.0

    min_len = min(len(ref_s), len(cand_s))
    max_len = max(len(ref_s), len(cand_s))

    c_match = 0.0
    v_match = 0.0
    total_c = 0

    for i in range(min_len):
        r_cho, r_jung, r_jong = ref_s[i]
        c_cho, c_jung, c_jong = cand_s[i]

        # 1. 모음 일치
        if r_jung == c_jung:
            v_match += 1.0
        elif (r_jung, c_jung) in [('ㅓ', 'ㅔ'), ('ㅔ', 'ㅐ'), ('ㅓ', 'ㅗ'), ('ㅗ', 'ㅜ'), ('ㅡ', 'ㅣ'), ('ㅣ', 'ㅔ')]:
            v_match += 0.5

        # 2. 초성 일치
        total_c += 1
        if r_cho == c_cho:
            c_match += 1.0
        elif (r_cho, c_cho) in [('ㅅ', 'ㅈ'), ('ㅌ', 'ㄷ'), ('ㅋ', 'ㄱ'), ('ㄹ', 'ㄴ'), ('ㅂ', 'ㅃ')]:
            c_match += 0.7

        # 3. 종성 일치
        if r_jong or c_jong:
            total_c += 1
            if r_jong == c_jong:
                c_match += 1.0
            elif (r_jong, c_jong) in [('ㄴ', 'ㅇ'), ('ㅅ', 'ㄷ'), ('ㄱ', 'ㅋ')]:
                c_match += 0.7

    c_rate = c_match / (total_c + (max_len - min_len))
    v_rate = v_match / max_len
    # 자음 x 모음 상호곱: 모음이나 자음 중 하나가 0이면 발음 성립 불가!
    combined_score = c_rate * v_rate
    return c_rate, v_rate, combined_score

_CORE_MINECRAFT_DICT = {
    'tin': ['주석', '틴'], 'lead': ['납', '리드'], 'silver': ['은', '실버'], 'copper': ['구리', '코퍼'],
    'iron': ['철', '아이언'], 'gold': ['금', '골드'], 'diamond': ['다이아몬드', '다이아'],
    'emerald': ['에메랄드'], 'nether': ['네더', '지옥'], 'ender': ['엔더'],
    'sword': ['검', '소드', '칼'], 'pickaxe': ['곡괭이'], 'axe': ['도끼'], 'shovel': ['삽'], 'hoe': ['괭이'],
    'ingot': ['주괴', '인곳'], 'ore': ['광석'], 'dust': ['가루', '분말'], 'plate': ['판', '플레이트'],
    'gear': ['톱니바퀴', '기어'], 'rod': ['막대', '로드'], 'block': ['블록'],
    'construct': ['컨스트럭트', '구조물', '건설'], 'resonant': ['공진', '공명', '레조넌트', '공명하는'],
    'tinkers': ['팅커스', '틴커즈', '팅커'], 'tinker': ['팅커', '땜장이'],
    'conduit': ['도관', '콘듀잇'], 'capacitor': ['축전기', '커패시터'], 'pulverizer': ['분쇄기', '펄버라이저'],
    'smelter': ['제련기', '용광로', '스멜터'],
    'certus quartz': ['서투스 석영', '서투스 쿼츠'],
    'charged certus quartz': ['충전된 서투스 석영', '충전된 서투스 쿼츠'],
    'certus quartz dust': ['서투스 석영 가루', '서투스 쿼츠 가루'],
    'create': ['크리에이트'],
    'mekanism': ['메카니즘'],
    'farmer\'s delight': ['파머스 딜라이트'],
}

def calculate_translation_error_rate(src_word, tgt_word, reference_hint=None, target_lang="한국어 (Korean)"):
    """
    원문(영어)과 번역문(한글) 간의 연관성 및 오역률(Error Rate, 0~100%)을 정밀 계산합니다.
    Args:
        src_word (str): 영어 원문 단어
        tgt_word (str): 사용자가 입력/수정한 한글 번역문
        reference_hint (str, optional): 기존 번역문 또는 찾을 단어 힌트
    Returns:
        tuple: (error_rate_pct, validity_pct, verdict_type, details_str)
               - verdict_type: 'pass' (오역률 <= 35%), 'warning' (35% < 오역률 < 70%), 'block' (오역률 >= 70%)
    """
    if not src_word or not tgt_word:
        return 100.0, 0.0, "block", "원문 또는 번역문이 비어있습니다."

    src_clean = src_word.strip()
    tgt_clean = tgt_word.strip()

    # 영문 원문과 동일하게 유지해야 하는 코드/단위/ID인 경우 정상 처리
    if src_clean == tgt_clean:
        return 0.0, 100.0, "pass", "원문 유지(코드/식별자/단위)"

    # 한글이 전혀 없는 경우
    if not has_hangul(tgt_clean):
        if re.match(r'^[\d\s\-_./%+:;]+$', tgt_clean):
            return 0.0, 100.0, "pass", "숫자/기호 서식"
        return 100.0, 0.0, "block", "한글 번역이 누락되었습니다."

    # 1. 마인크래프트 기본 핵심 어휘 사전 대조 (사전 등록 단어는 오역률 0.0% 엄격 기준 적용)
    src_lower = src_clean.lower()
    if src_lower in _CORE_MINECRAFT_DICT:
        accepted_list = _CORE_MINECRAFT_DICT[src_lower]
        for accepted in accepted_list:
            if accepted == tgt_clean:
                return 0.0, 100.0, "pass", f"핵심 사전 공식 번역 일치 ('{tgt_clean}')"
            if accepted in tgt_clean or tgt_clean in accepted:
                return 0.0, 100.0, "pass", f"핵심 사전 복합어/파생어 일치 ('{accepted}')"
        # 사전에 명확히 등록된 단어인데 사전 허용 목록에 없는 단어로 변경하려는 경우: 0% 기준 엄격 적용하여 원천 차단!
        return 100.0, 0.0, "hard_block", f"공식 핵심 사전 등재 단어는 0% 표준어 외 변경 불가 (표준어: {', '.join(accepted_list)})"

    target_lang_norm = target_lang.replace(" ", "")

    # 2. 의미 사전 (Semantic Memory) 대조
    cached_candidates = []
    with _lock:
        for cache in (_items_cache, _global_cache, _books_cache):
            for lang_key, c_dict in cache.items():
                if lang_key.replace(" ", "") == target_lang_norm:
                    if src_clean in c_dict:
                        cached_tgt = c_dict[src_clean]
                        if cached_tgt == tgt_clean:
                            return 0.0, 100.0, "pass", f"캐시 DB 사전 일치 ('{cached_tgt}')"
                        if tgt_clean in cached_tgt or cached_tgt in tgt_clean:
                            return 10.0, 90.0, "pass", f"캐시 DB 파생어 일치 ('{cached_tgt}')"
                        cached_candidates.append(cached_tgt)

    # 3. reference_hint, 핵심 사전 또는 캐시 내 기존 번역과 음절 단위 자음x모음 상호 교차 검증
    best_cache_validity = 0.0
    best_cache_c = 0.0
    best_cache_v = 0.0

    targets_to_compare = []
    if src_lower in _CORE_MINECRAFT_DICT:
        targets_to_compare.extend(_CORE_MINECRAFT_DICT[src_lower])
    if reference_hint and has_hangul(reference_hint):
        targets_to_compare.append(reference_hint.strip())
    targets_to_compare.extend(cached_candidates)

    vowel_distortion_detected = False
    for ref in targets_to_compare:
        c_rate, v_rate, score = _compare_syllables_phonetic(ref, tgt_clean)
        if c_rate >= 0.7 and v_rate == 0.0:
            vowel_distortion_detected = True
        if score > best_cache_validity:
            best_cache_validity = score
            best_cache_c = c_rate
            best_cache_v = v_rate

    # 4. 직접 영-한 발음 분석 (기존 캐시가 전부 오역이더라도 올바른 새 번역을 100% 통과시키도록 항상 병행 검사)
    cand_syllables = _decompose_korean(tgt_clean)
    letters = re.findall(r'[a-z]', src_lower)
    eng_cons = [c for c in letters if c not in ('a', 'e', 'i', 'o', 'u')]
    eng_vowels = [c for c in letters if c in ('a', 'e', 'i', 'o', 'u')]

    ko_cons = []
    ko_vowels = []
    for s in cand_syllables:
        ko_cons.append(s[0])
        if s[2]: ko_cons.append(s[2])
        if s[1] != 'ㅡ': # ㅡ는 보조모음이므로 영문 모음 매칭 대상에서 제외
            ko_vowels.append(s[1])

    c_matches = 0.0
    k_idx = 0
    for ec in eng_cons:
        poss = _ENG_KO_CONSONANTS.get(ec, [])
        matched = False
        for i in range(k_idx, len(ko_cons)):
            if ko_cons[i] in poss:
                c_matches += 1.0
                k_idx = i + 1
                matched = True
                break
        if not matched and ec == 'r':
            c_matches += 0.8

    effective_cons = max(1, len([c for c in eng_cons if c != 'r']))
    direct_c = c_matches / max(effective_cons, len(ko_cons))

    v_matches = 0.0
    v_idx = 0
    for ev in eng_vowels:
        poss_v = _ENG_KO_VOWELS.get(ev, [])
        for i in range(v_idx, len(ko_vowels)):
            if ko_vowels[i] in poss_v:
                v_matches += 1.0
                v_idx = i + 1
                break
    direct_v = v_matches / max(1, max(len(eng_vowels), len(ko_vowels)))
    direct_validity = direct_c * direct_v

    if len(tgt_clean) == 1 and len(src_clean) >= 4:
        direct_validity *= 0.1

    # 기존 캐시 대조와 직접 영-한 발음 분석 중 더 타당한(높은) 점수 채택!
    # 단, 기등록 단어에 대한 명백한 모음 왜곡 분탕(퉁크서, 탕쿠사 등)이 감지된 경우 direct_validity로 우회 불가!
    if vowel_distortion_detected and best_cache_validity == 0.0:
        final_validity = 0.0
        final_c = best_cache_c
        final_v = 0.0
    elif direct_validity >= best_cache_validity:
        final_validity = direct_validity
        final_c = direct_c
        final_v = direct_v
    else:
        final_validity = best_cache_validity
        final_c = best_cache_c
        final_v = best_cache_v

    validity_pct = round(max(0.0, min(100.0, final_validity * 100.0)), 1)
    error_rate_pct = round(100.0 - validity_pct, 1)

    has_established = bool(src_lower in _CORE_MINECRAFT_DICT or cached_candidates)

    if error_rate_pct <= 35.0:
        return error_rate_pct, validity_pct, "pass", f"자음/모음 음운 정상 일치 (자음 {final_c*100:.0f}%, 모음 {final_v*100:.0f}%)"
    elif error_rate_pct < 70.0:
        return error_rate_pct, validity_pct, "warning", f"발음 유사도 다소 낮음 (자음 {final_c*100:.0f}%, 모음 {final_v*100:.0f}%)"
    else:
        if has_established:
            return error_rate_pct, validity_pct, "hard_block", f"기존 데이터/표준어 왜곡 분탕 감지 (자음 {final_c*100:.0f}%, 모음 {final_v*100:.0f}%)"
        else:
            return error_rate_pct, validity_pct, "block", f"원문과 연관성 없는 왜곡 감지 (신규 미등록 단어)"

