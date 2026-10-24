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
from translation_word import sanitize_minecraft_formatting

def _get_cache_dir():
    """로컬 캐시 파일이 저장될 전용 폴더 (항상 dist 하위 폴더에 저장)"""
    import sys
    if getattr(sys, 'frozen', False):
        # PyInstaller EXE 실행 환경: 실행 파일이 위치한 폴더 기준
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        # 만약 EXE가 이미 dist 폴더 내에 위치한다면 하위에 또 dist를 만들지 않음
        if os.path.basename(exe_dir).lower() == "dist":
            base = exe_dir
        else:
            base = os.path.join(exe_dir, "dist")
    else:
        # 개발 환경: 프로젝트 루트의 dist 폴더
        repo_root = os.path.dirname(os.path.abspath(__file__))
        base = os.path.join(repo_root, "dist")
    
    os.makedirs(base, exist_ok=True)
    return base

CACHE_DIR = _get_cache_dir()
MODPACK_MEMORY_DIR = os.path.join(CACHE_DIR, "translation_modpack")
os.makedirs(MODPACK_MEMORY_DIR, exist_ok=True)
MEMORY_FILE = os.path.join(CACHE_DIR, "translation_memory.json")
BACKUP_FILE = os.path.join(CACHE_DIR, "translation_memory.json.bak")
ITEMS_MEMORY_FILE = os.path.join(CACHE_DIR, "translation_memory_items.json")
BOOKS_MEMORY_FILE = os.path.join(CACHE_DIR, "translation_memory_books.json")
SYNC_STATE_FILE = os.path.join(CACHE_DIR, "cloud_sync_state.json")
PENDING_COMMITS_FILE = os.path.join(CACHE_DIR, "pending_commits.json")
CLOUD_CONFIG_FILE = os.path.join(CACHE_DIR, "cloud_config.json")
SHORT_TEXT_THRESHOLD = 30

# ====================================================================
# Cloudflare R2 (순수 파일 스토리지 큐 & 커밋 방식) 클라우드 설정
# ====================================================================
# cloudflare_worker.js 배포 후 발급받은 Worker URL을 입력하거나,
# dist/cloud_config.json 에 {"worker_url": "https://..."} 형태로 저장하세요.
DEFAULT_CLOUDFLARE_WORKER_URL = "https://quest-translator-sync.kimraon84.workers.dev"
CLOUDFLARE_WORKER_URL = os.environ.get("QUEST_CLOUDFLARE_URL", DEFAULT_CLOUDFLARE_WORKER_URL).strip()

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
    """모드팩 컨텍스트 설정 (translation_modpack 전용 폴더 및 직관적인 모드팩 이름 파일명 적용)"""
    global _current_modpack_id, _current_modpack_file, _modpack_loaded
    with _lock:
        if modpack_path:
            # .zip 확장자만 안전하게 제거 (예: Project Infinity 0.1의 마이너 버전 보존)
            base_part = os.path.basename(modpack_path.rstrip(os.sep))
            if base_part.lower().endswith(".zip"):
                modpack_name = base_part[:-4]
            else:
                modpack_name = base_part

            # 파일명에 부적합한 특수문자 안전 치환
            safe_slug = re.sub(r'[/\\*?:"<>|\s]+', '_', modpack_name).strip('_')
            legacy_hash = hashlib.md5(modpack_name.encode('utf-8')).hexdigest()[:8]
            _current_modpack_id = safe_slug

            # 파일 탐색 우선순위:
            # 1. dist/translation_modpack/translation_memory_modpack_<이름>.json (신규 표준)
            # 2. dist/translation_modpack/translation_memory_modpack_<해시>.json
            # 3. dist/translation_memory_modpack_<해시>.json (구버전 호환)
            candidate_named = os.path.join(MODPACK_MEMORY_DIR, f"translation_memory_modpack_{safe_slug}.json")
            candidate_sub_hash = os.path.join(MODPACK_MEMORY_DIR, f"translation_memory_modpack_{legacy_hash}.json")
            candidate_legacy = os.path.join(CACHE_DIR, f"translation_memory_modpack_{legacy_hash}.json")

            if os.path.exists(candidate_named):
                _current_modpack_file = candidate_named
            elif os.path.exists(candidate_sub_hash):
                _current_modpack_file = candidate_sub_hash
            elif os.path.exists(candidate_legacy):
                _current_modpack_file = candidate_legacy
            else:
                _current_modpack_file = candidate_named
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
                tgt = sanitize_minecraft_formatting(src, tgt)
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
_session_hit_texts = set()
_session_miss_texts = set()
_session_start_time = None
_session_api_calls = 0


def reset_session_stats():
    """번역 작업 시작 시 세션 통계(캐시 적중/미적중, API 호출량, 시작 시간)를 초기화합니다."""
    global _session_start_time, _session_api_calls
    with _lock:
        _session_hit_texts.clear()
        _session_miss_texts.clear()
        _session_api_calls = 0
        _session_start_time = time.time()


def record_api_call(count=1):
    """API 호출 횟수(RPD 추적)를 증가시킵니다."""
    global _session_api_calls
    with _lock:
        _session_api_calls += count


def _record_cache_lookup(text, is_hit):
    if not text or not isinstance(text, str):
        return
    if is_hit:
        if text not in _session_miss_texts:
            _session_hit_texts.add(text)
    else:
        if text not in _session_hit_texts:
            _session_miss_texts.add(text)


def get_cache_stats_summary(target_lang="한국어 (Korean)"):
    """현재 세션의 캐시 적용률, 번역 소요 시간, API 호출량(RPD) 요약 문자열을 반환합니다."""
    hits = len(_session_hit_texts)
    misses = len(_session_miss_texts)
    total_queries = hits + misses
    hit_rate = (hits / total_queries * 100.0) if total_queries > 0 else 100.0

    elapsed_sec = (time.time() - _session_start_time) if _session_start_time else 0.0
    if elapsed_sec >= 60:
        elapsed_str = f"{int(elapsed_sec // 60)}분 {int(elapsed_sec % 60)}초"
    else:
        elapsed_str = f"{elapsed_sec:.1f}초"

    api_calls = _session_api_calls
    if api_calls > 0:
        rpd_info = f"{api_calls:,}회 (Gemini 무료 티어 일일 1,500 RPD 중 {api_calls:,}회 소모)"
    else:
        rpd_info = "0회 (100% 캐시 및 로컬 단어 조립 처리로 API 소모 0회)"

    lines = [
        "==========================================",
        "📊 [번역 통계 요약]",
        f"  • 캐시 적용률: {hit_rate:.1f}% (전체 고유 문장 {total_queries:,}개 중 캐시 적용 {hits:,}개 / 신규 번역 {misses:,}개)",
        f"  • 번역 소요 시간: {elapsed_str}",
        f"  • API 호출량 (RPD): {rpd_info}",
        "==========================================",
    ]
    return "\n".join(lines)

def _store(cache, text, translated_text, target_lang_norm, force_override=False):
    target_lang_key = target_lang_norm
    for k in cache.keys():
        if k.replace(" ", "") == target_lang_norm:
            target_lang_key = k
            break
    if target_lang_key not in cache:
        cache[target_lang_key] = {}
    prev_val = cache[target_lang_key].get(text)
    cache[target_lang_key][text] = translated_text
    
    # 더티 캐시(새로 추가/변경된 데이터 또는 공식 한글패치 강제 덮어쓰기)에 저장하여 클라우드 DB까지 덮어쓰도록 보장
    if force_override or prev_val != translated_text:
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
    """로컬 메모리 로드 및 Cloudflare 최신 데이터 동기화"""
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

    sync_from_cloudflare()


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
            _record_cache_lookup(text, True)
            return result

    result = _lookup_safe(_global_cache, text, target_lang_norm)
    if result is not None:
        _record_cache_lookup(text, True)
        return result

    result = _lookup_safe(_items_cache, text, target_lang_norm)
    if result is not None:
        _record_cache_lookup(text, True)
        return result

    result = _lookup_safe(_books_cache, text, target_lang_norm)
    if result is not None:
        _record_cache_lookup(text, True)
        return result

    # 💡 시스템 코드/명령어/단위/포맷터: 번역 불필요 대상은 원문 그대로 즉시 반환
    from translation_engines import is_code_or_id
    if is_code_or_id(text):
        return text
    _record_cache_lookup(text, False)
    return None


def add_to_memory(text, translated_text, target_lang="한국어 (Korean)", force_override=False):
    translated_text = sanitize_minecraft_formatting(text, translated_text)
    if not is_valid_translation(text, translated_text, target_lang):
        return

    if not _global_loaded:
        load_memory()

    target_lang_norm = target_lang.replace(" ", "")
    with _lock:
        if _current_modpack_id:
            _load_modpack_memory()
            _store(_modpack_cache, text, translated_text, target_lang_norm, force_override=force_override)
        # 클라우드 동기화 및 전역 재사용을 위해 _global_cache에도 동시 보존
        _store(_global_cache, text, translated_text, target_lang_norm, force_override=force_override)


def get_cached_item_translation(text, target_lang="한국어 (Korean)"):
    if not _items_loaded:
        load_memory()
    target_lang_norm = target_lang.replace(" ", "")
    result = _lookup_safe(_items_cache, text, target_lang_norm)
    if result is not None:
        _record_cache_lookup(text, True)
        return result
    result = _lookup_safe(_global_cache, text, target_lang_norm)
    if result is not None:
        _record_cache_lookup(text, True)
        return result
    result = _lookup_safe(_books_cache, text, target_lang_norm)
    if result is not None:
        _record_cache_lookup(text, True)
        return result

    from translation_engines import is_code_or_id
    if is_code_or_id(text):
        return text
    _record_cache_lookup(text, False)
    return None


def add_item_to_memory(text, translated_text, target_lang="한국어 (Korean)", force_override=False):
    if not is_valid_translation(text, translated_text, target_lang):
        return
    if not _items_loaded:
        load_memory()
    target_lang_norm = target_lang.replace(" ", "")
    with _lock:
        _store(_items_cache, text, translated_text, target_lang_norm, force_override=force_override)
        # 아이템/블록/기계 명사구가 퀘스트 본문/설명/가이드북에서도 1차 완전일치로 즉시 치환되도록 _global_cache에도 동시 보존
        _store(_global_cache, text, translated_text, target_lang_norm, force_override=force_override)



def add_official_ko_to_memory(text, translated_text, target_lang="한국어 (Korean)", is_item=False):
    translated_text = sanitize_minecraft_formatting(text, translated_text)
    """
    모드 내장 공식 한글패치(ko_kr)에서 불러온 데이터를 기존 AI 번역 캐시보다 항상 최우선으로 덮어씁니다.
    이미 다른 캐시(_global_cache, _items_cache, _modpack_cache, _books_cache)에 이전 번역 데이터가 남아 있더라도
    모두 공식 한글패치 데이터로 일괄 덮어씌워 구버전 번역이 튀어나오는 현상을 원천 차단합니다.
    """
    if not is_valid_translation(text, translated_text, target_lang):
        return
    if not _global_loaded or not _items_loaded or not _books_loaded:
        load_memory()

    target_lang_norm = target_lang.replace(" ", "")
    with _lock:
        if _current_modpack_id:
            _load_modpack_memory()
            _store(_modpack_cache, text, translated_text, target_lang_norm)

        if is_item:
            _store(_items_cache, text, translated_text, target_lang_norm)
            _store(_global_cache, text, translated_text, target_lang_norm)
        else:
            _store(_global_cache, text, translated_text, target_lang_norm)
            # 만약 _items_cache에 동일 원문의 기존 AI 번역이 이미 존재한다면 공식 한글패치로 함께 덮어쓰기
            for k in list(_items_cache.keys()):
                if k.replace(" ", "") == target_lang_norm and text in _items_cache[k]:
                    _store(_items_cache, text, translated_text, target_lang_norm)
                    break

        # 만약 _books_cache에 동일 원문의 기존 AI 번역이 이미 존재한다면 공식 한글패치로 함께 덮어쓰기
        for k in list(_books_cache.keys()):
            if k.replace(" ", "") == target_lang_norm and text in _books_cache[k]:
                _store(_books_cache, text, translated_text, target_lang_norm)
                break

    # 공식 한글패치에서 발견된 단어/명사구는 translation_word 단어사전에도 최우선(is_official=True)으로 반영
    if "한국어" in target_lang or "Korean" in target_lang:
        try:
            import translation_word
            translation_word.learn_from_pair(text, translated_text, is_official=True)
        except Exception:
            pass



def get_cached_book_translation(text, target_lang="한국어 (Korean)"):
    if not _books_loaded:
        load_memory()
    target_lang_norm = target_lang.replace(" ", "")
    result = _lookup_safe(_books_cache, text, target_lang_norm)
    if result is not None:
        _record_cache_lookup(text, True)
        return result
    result = _lookup_safe(_global_cache, text, target_lang_norm)
    if result is not None:
        _record_cache_lookup(text, True)
        return result
    result = _lookup_safe(_items_cache, text, target_lang_norm)
    if result is not None:
        _record_cache_lookup(text, True)
        return result

    from translation_engines import is_code_or_id
    if is_code_or_id(text):
        return text
    _record_cache_lookup(text, False)
    return None


def add_book_to_memory(text, translated_text, target_lang="한국어 (Korean)", force_override=False):
    if not is_valid_translation(text, translated_text, target_lang):
        return
    if not _books_loaded:
        load_memory()
    target_lang_norm = target_lang.replace(" ", "")
    with _lock:
        _store(_books_cache, text, translated_text, target_lang_norm, force_override=force_override)
        # 가이드북 텍스트가 퀘스트 본문/설명에서도 1차 완전일치로 즉시 치환되도록 _global_cache에도 동시 보존
        _store(_global_cache, text, translated_text, target_lang_norm, force_override=force_override)



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


def _save_local_caches_only():
    """서버 업로드 없이 로컬 4개 캐시 파일만 안전하게 디스크에 병합 저장"""
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


def save_memory(flush_now=False):
    """
    1~2초 단위 호출 시 로컬 4개 파일은 즉시 저장하고,
    서버 전송은 커밋 대기 큐(_commit_queue)에 모아두었다가 일괄 커밋(Commit) 방식으로 전송합니다.
    """
    try:
        import temp_session_logger
        if temp_session_logger.is_debug_mode():
            temp_session_logger.save_temp_session()
            return
    except Exception:
        pass

    _save_local_caches_only()

    with _lock:
        dirty_general_copy = {k: v.copy() for k, v in _dirty_general.items()}
        dirty_items_copy = {k: v.copy() for k, v in _dirty_items.items()}
        dirty_books_copy = {k: v.copy() for k, v in _dirty_books.items()}
        _dirty_general.clear()
        _dirty_items.clear()
        _dirty_books.clear()

    if dirty_general_copy:
        enqueue_commit(dirty_general_copy, "general")
    if dirty_items_copy:
        enqueue_commit(dirty_items_copy, "items")
    if dirty_books_copy:
        enqueue_commit(dirty_books_copy, "books")

    try:
        import translation_word
        translation_word.save_words()
    except Exception:
        pass

    if flush_now:
        flush_commit_queue(sync_wait=True)



# ====================================================================
# Cloudflare R2 순수 스토리지 큐(Queue) & 커밋(Commit) 동기화 엔진
# ====================================================================

MASTER_FILES = {
    "general": ("master_general.json.gz", _global_cache),
    "items": ("master_items.json.gz", _items_cache),
    "books": ("master_books.json.gz", _books_cache),
}

COMMIT_BATCH_THRESHOLD = 500   # 큐에 500문장 이상 모이면 즉시 자동 커밋
COMMIT_DEBOUNCE_SECONDS = 12.0 # 번역 중 1~2초마다 쌓이는 변경분을 12초 단위로 묶어서 1회 커밋
SYNC_COOLDOWN_SECONDS = 180.0  # 짧은 시간 내 중복 Pull 방지 (3분 쿨타임)

_commit_queue = {}  # (category, lang, src) -> tgt
_commit_timer = None
_is_committing = False
_circuit_breaker_until = 0.0
_consecutive_upload_errors = 0
_last_sync_timestamp = 0.0
_cloud_sync_done = threading.Event()
_is_syncing = False


def get_worker_url():
    """환경변수, dist/cloud_config.json, 또는 상수에서 Cloudflare Worker URL을 가져옵니다."""
    if CLOUDFLARE_WORKER_URL and "YOUR_" not in CLOUDFLARE_WORKER_URL:
        return CLOUDFLARE_WORKER_URL.rstrip("/")
    if os.path.exists(CLOUD_CONFIG_FILE):
        try:
            with open(CLOUD_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                url = str(cfg.get("worker_url", "")).strip()
                if url and "YOUR_" not in url:
                    return url.rstrip("/")
        except Exception:
            pass
    return ""


def set_worker_url(worker_url):
    """Cloudflare Worker URL을 설정하고 dist/cloud_config.json에 영구 저장합니다."""
    global CLOUDFLARE_WORKER_URL
    clean_url = str(worker_url or "").strip().rstrip("/")
    CLOUDFLARE_WORKER_URL = clean_url
    try:
        with open(CLOUD_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"worker_url": clean_url}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"Cloudflare 설정 저장 실패: {e}")


def _load_sync_state():
    if os.path.exists(SYNC_STATE_FILE):
        try:
            with open(SYNC_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    last_id = data.get("last_commit_id", "")
                    # 레거시 D1의 정수 ID는 R2 커밋 키 체계와 다르므로 빈 문자열로 정규화
                    if isinstance(last_id, int) or (isinstance(last_id, str) and last_id.isdigit() and len(last_id) < 10):
                        data["last_commit_id"] = ""
                    return data
        except Exception:
            pass
    return {"last_commit_id": "", "etags": {}}


def _save_sync_state(state):
    try:
        tmp = SYNC_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SYNC_STATE_FILE)
    except Exception as e:
        logging.debug(f"동기화 상태 저장 실패: {e}")


def _load_pending_commits_from_disk():
    """앱 재시작 시 미전송된 로컬 커밋 큐 복원"""
    if not os.path.exists(PENDING_COMMITS_FILE):
        return
    try:
        with open(PENDING_COMMITS_FILE, "r", encoding="utf-8") as f:
            items = json.load(f)
        if isinstance(items, list):
            with _lock:
                for row in items:
                    cat = row.get("cat", "general")
                    lang = row.get("lang", "한국어 (Korean)")
                    src = row.get("src")
                    tgt = row.get("tgt")
                    if src and tgt:
                        _commit_queue[(cat, lang, src)] = tgt
    except Exception:
        pass


def _save_pending_commits_to_disk():
    """커밋 대기 큐를 로컬 파일에 안전 백업 (오프라인/종료 시 유실 방지)"""
    try:
        with _lock:
            items = [
                {"cat": cat, "lang": lang, "src": src, "tgt": tgt}
                for (cat, lang, src), tgt in _commit_queue.items()
            ]
        if not items:
            if os.path.exists(PENDING_COMMITS_FILE):
                try:
                    os.remove(PENDING_COMMITS_FILE)
                except Exception:
                    pass
            return
        tmp = PENDING_COMMITS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        os.replace(tmp, PENDING_COMMITS_FILE)
    except Exception as e:
        logging.debug(f"대기 커밋 저장 실패: {e}")


def is_cloud_upload_safe():
    """클라우드 업로드 전 서킷 브레이커 상태 검사"""
    now = time.time()
    if now < _circuit_breaker_until:
        return False, "통신 오류로 인한 안전 차단기(Circuit Breaker) 활성화 중"
    return True, "정상"


def enqueue_commit(cache_dict, category="general"):
    """
    1~2초마다 발생하는 신규 번역을 즉시 HTTP 전송하지 않고
    메모리 큐(_commit_queue)에 병합 적재한 뒤, 일정량/일정시간마다 단일 커밋으로 푸시합니다.
    """
    try:
        import temp_session_logger
        if temp_session_logger.is_debug_mode():
            return
    except Exception:
        pass

    global _commit_timer
    if not cache_dict:
        return

    added = 0
    with _lock:
        for lang, entries in list(cache_dict.items()):
            if not isinstance(entries, dict):
                continue
            for src, tgt in list(entries.items()):
                if is_valid_translation(src, tgt, lang):
                    _commit_queue[(category, lang, src)] = tgt
                    added += 1
        queue_size = len(_commit_queue)

    if added == 0:
        return

    _save_pending_commits_to_disk()

    # 1) 큐에 500개 이상 쌓였으면 즉시 배치 커밋 실행
    if queue_size >= COMMIT_BATCH_THRESHOLD:
        flush_commit_queue()
        return

    # 2) 그렇지 않으면 디바운스 타이머(12초)를 걸어 1~2초 단위 업데이트들을 하나로 묶어서 커밋
    with _lock:
        if _commit_timer is not None:
            _commit_timer.cancel()
        _commit_timer = threading.Timer(COMMIT_DEBOUNCE_SECONDS, flush_commit_queue)
        _commit_timer.daemon = True
        _commit_timer.start()


def flush_commit_queue(sync_wait=False):
    """큐(_commit_queue)에 대기 중인 모든 신규 번역을 Gzip 압축하여 단일 커밋(POST /commit)으로 전송"""
    try:
        import temp_session_logger
        if temp_session_logger.is_debug_mode():
            return
    except Exception:
        pass

    global _commit_timer, _is_committing

    with _lock:
        if _commit_timer is not None:
            _commit_timer.cancel()
            _commit_timer = None
        if not _commit_queue or _is_committing:
            return
        worker_url = get_worker_url()
        if not worker_url:
            # 아직 Cloudflare Worker URL이 설정되지 않은 경우 로컬 pending_commits.json에만 보존
            return
        safe, _ = is_cloud_upload_safe()
        if not safe:
            return

        _is_committing = True
        snapshot_items = [
            {"cat": cat, "lang": lang, "src": src, "tgt": tgt}
            for (cat, lang, src), tgt in _commit_queue.items()
        ]
        _commit_queue.clear()

    def _do_commit():
        global _is_committing, _consecutive_upload_errors, _circuit_breaker_until
        # Cloudflare Worker 무료 플랜 CPU 제한(10ms) 및 타임아웃 방지를 위해 500개 단위로 안전 분할 전송
        chunk_size = 500
        committed_total = 0
        failed_items = []


        try:
            for idx in range(0, len(snapshot_items), chunk_size):
                chunk = snapshot_items[idx:idx + chunk_size]
                try:
                    raw_bytes = json.dumps({"items": chunk}, ensure_ascii=False).encode("utf-8")
                    gz_payload = gzip.compress(raw_bytes, compresslevel=6)
                    headers = {
                        "Content-Type": "application/json",
                        "Content-Encoding": "gzip",
                    }
                    r = requests.post(f"{worker_url}/commit", headers=headers, data=gz_payload, timeout=30)
                    if r.status_code in (200, 201):
                        committed_total += len(chunk)
                        try:
                            res_json = r.json()
                            new_last_id = res_json.get("latest_id") or res_json.get("key")
                            if new_last_id:
                                sync_state = _load_sync_state()
                                sync_state["last_commit_id"] = str(new_last_id)
                                _save_sync_state(sync_state)
                        except Exception:
                            pass
                    else:
                        failed_items.extend(chunk)
                        logging.warning(f"⚠️ Cloudflare 커밋 청크 응답 오류 (HTTP {r.status_code}): {r.text[:150]}")
                except Exception as chunk_err:
                    failed_items.extend(chunk)
                    logging.warning(f"⚠️ Cloudflare 커밋 전송 보류 (다음 주기에 재시도): {chunk_err}")

            if not failed_items:
                _consecutive_upload_errors = 0
                _save_pending_commits_to_disk()
                logging.info(f"✅ [Cloudflare Commit] 신규 번역 {committed_total:,}개 일괄 커밋 완료!")
            else:
                _consecutive_upload_errors += 1
                with _lock:
                    for item in failed_items:
                        key = (item["cat"], item["lang"], item["src"])
                        if key not in _commit_queue:
                            _commit_queue[key] = item["tgt"]
                _save_pending_commits_to_disk()
                if committed_total > 0:
                    logging.info(f"✅ [Cloudflare Commit] {committed_total:,}개 커밋 완료 (보류 {len(failed_items):,}개는 다음 주기에 재시도)")
                if _consecutive_upload_errors >= 3:
                    _circuit_breaker_until = time.time() + 180
        finally:
            _is_committing = False

    t = threading.Thread(target=_do_commit, daemon=True)
    t.start()
    if sync_wait:
        t.join(timeout=15.0)


def upload_snapshots_to_cloudflare(force=False):
    """
    force=True 호출 시 현재 로컬 3대 마스터 캐시를 Gzip 압축하여 Cloudflare R2 스냅샷(/snapshot/:cat)으로 업로드합니다.
    평상시(force=False)에는 매번 50MB 스냅샷을 재업로드하지 않고 가벼운 /commit 큐 방식만 사용합니다.
    """
    if not force:
        return
    worker_url = get_worker_url()
    if not worker_url:
        return

    def _async_pack():
        for cat, (gz_name, cache) in MASTER_FILES.items():
            try:
                with _lock:
                    data_copy = {k: v.copy() for k, v in cache.items() if isinstance(v, dict)}
                if not data_copy:
                    continue
                raw_json = json.dumps(data_copy, ensure_ascii=False, indent=2).encode("utf-8")
                gz_bytes = gzip.compress(raw_json, compresslevel=9)
                r = requests.put(
                    f"{worker_url}/snapshot/{cat}",
                    headers={"Content-Type": "application/gzip"},
                    data=gz_bytes,
                    timeout=60,
                )
                if r.status_code in (200, 201):
                    logging.info(f"✅ Cloudflare R2 스냅샷 업로드 성공 [{gz_name}] ({len(gz_bytes)/1024:.1f} KB)")
                else:
                    logging.warning(f"⚠️ Cloudflare R2 스냅샷 업로드 실패 [{gz_name}]: {r.status_code} {r.text}")
            except Exception as e:
                logging.warning(f"스냅샷 업로드 오류 ({cat}): {e}")

    threading.Thread(target=_async_pack, daemon=True).start()


def wait_for_cloud_sync(timeout=6.0):
    """클라우드 동기화(Pull)가 완료될 때까지 대기"""
    if _cloud_sync_done.is_set():
        return True
    return _cloud_sync_done.wait(timeout=timeout)


def sync_from_cloudflare(force_wait=False):
    """
    Cloudflare R2 순수 스토리지 기반 Git-Pull 증분 동기화:
    1) 이미 최근 3분 이내에 동기화했다면 중복 네트워크 호출 없이 즉시 통과 (0 Byte 소모)
    2) 로컬 캐시가 비어 있거나 스냅샷 ETag가 변경된 경우에만 R2에서 master_*.json.gz 다운로드 (304 Not Modified 지원)
    3) 내 마지막 커밋 키(last_commit_id) 이후에 다른 유저들이 올린 신규 델타 커밋만 /pull 로 가볍게 병합
    """
    global _is_syncing
    worker_url = get_worker_url()
    if not worker_url:
        _cloud_sync_done.set()
        return

    now = time.time()
    if _cloud_sync_done.is_set() and (now - _last_sync_timestamp < SYNC_COOLDOWN_SECONDS) and not force_wait:
        return

    with _lock:
        if _is_syncing:
            return
        _is_syncing = True

    def _download_r2_master_if_needed(category, target_cache, sync_state):
        """로컬 캐시가 비어있거나 스냅샷이 갱신되었을 때만 ETag 기반으로 R2 마스터 다운로드"""
        try:
            etags = sync_state.setdefault("etags", {})
            saved_etag = etags.get(category, "")
            has_local_data = bool(target_cache)

            headers = {}
            if has_local_data and saved_etag:
                headers["If-None-Match"] = saved_etag

            r = requests.get(f"{worker_url}/master/{category}", headers=headers, timeout=15)
            if r.status_code == 304:
                return False
            if r.status_code == 200 and r.content:
                raw_bytes = gzip.decompress(r.content)
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
                    new_etag = r.headers.get("ETag", "")
                    if new_etag:
                        etags[category] = new_etag
                    logging.info(f"✅ Cloudflare R2 마스터 스냅샷 [{category}] 로드 완료")
                    return True
        except Exception as e:
            logging.debug(f"R2 마스터 [{category}] 확인 스킵: {e}")
        return False

    def _pull_incremental_commits(sync_state):
        """마지막 커밋 키(last_commit_id) 이후에 추가된 신규 델타 커밋만 Pull (R2 Git Pull 방식)"""
        import urllib.parse
        last_id = str(sync_state.get("last_commit_id", "") or "").strip()
        if last_id.isdigit() and len(last_id) < 10:
            last_id = ""
        pulled_total = 0
        try:
            while True:
                encoded_since = urllib.parse.quote(last_id)
                r = requests.get(f"{worker_url}/pull?since={encoded_since}&limit=20", timeout=12)
                if r.status_code != 200:
                    break
                data = r.json()
                commits = data.get("commits", [])
                latest_id = str(data.get("latest_id", last_id) or last_id).strip()

                if commits:
                    with _lock:
                        for row in commits:
                            cat = row.get("cat") or row.get("category") or "general"
                            lang = row.get("lang", "한국어 (Korean)")
                            src = row.get("src")
                            tgt = row.get("tgt")
                            if not is_valid_translation(src, tgt, lang):
                                continue
                            target_cache = _global_cache if cat == "general" else (_items_cache if cat == "items" else _books_cache)
                            if lang not in target_cache:
                                target_cache[lang] = {}
                            target_cache[lang][src] = tgt
                            _index_template(src, tgt, lang.replace(" ", ""))
                            pulled_total += 1

                if latest_id and latest_id != last_id:
                    last_id = latest_id
                    sync_state["last_commit_id"] = last_id

                if not data.get("has_more"):
                    break

            if pulled_total > 0:
                logging.info(f"🔄 [Cloudflare R2 Pull] 신규 커밋 {pulled_total:,}개 증분 병합 완료 (최신: {last_id})")
        except Exception as e:
            logging.debug(f"Cloudflare 증분 Pull 실패: {e}")
        return pulled_total > 0

    def _async_cloudflare_sync():
        global _is_syncing, _last_sync_timestamp
        try:
            _load_pending_commits_from_disk()
            sync_state = _load_sync_state()

            # 1단계: 필요한 경우에만 R2 마스터 스냅샷 로드
            changed_master = False
            with ThreadPoolExecutor(max_workers=3) as executor:
                f1 = executor.submit(_download_r2_master_if_needed, "general", _global_cache, sync_state)
                f2 = executor.submit(_download_r2_master_if_needed, "items", _items_cache, sync_state)
                f3 = executor.submit(_download_r2_master_if_needed, "books", _books_cache, sync_state)
                changed_master = any([f1.result(), f2.result(), f3.result()])

            # 2단계: 마지막 커밋 이후 추가된 신규 델타 커밋만 Pull
            changed_pull = _pull_incremental_commits(sync_state)

            _save_sync_state(sync_state)
            _last_sync_timestamp = time.time()

            # 3단계: 단어사전(translation_memory_words.json) 무압축 원본 동기화
            try:
                import translation_word
                translation_word.sync_words_from_cloudflare()
            except Exception:
                pass

            # 4단계: 변경사항이 있었을 때만 로컬 파일 업데이트
            if changed_master or changed_pull:
                _save_local_caches_only()

            # 5단계: 이전에 오프라인이라 못 보낸 대기 커밋이 있으면 자동 푸시
            if _commit_queue:
                flush_commit_queue()
        finally:
            _is_syncing = False
            _cloud_sync_done.set()


    t = threading.Thread(target=_async_cloudflare_sync, daemon=True)
    t.start()
    if force_wait:
        t.join(timeout=8.0)


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
    """캐시 업데이트 후 로컬 저장 및 클라우드 커밋 큐에 반영합니다."""
    target_lang_norm = target_lang.replace(" ", "")
    cache_to_update = None
    
    if category == "items": cache_to_update = _items_cache
    elif category == "general": cache_to_update = _global_cache
    elif category == "books": cache_to_update = _books_cache
    else: return False

    with _lock:
        _store(cache_to_update, original_src, new_tgt, target_lang_norm)
        
    save_memory(flush_now=True)
    return True

def delete_memory_entry(category, original_src, target_lang="한국어 (Korean)"):
    """로컬 캐시에서 항목을 삭제합니다."""
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
        _save_local_caches_only()
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

