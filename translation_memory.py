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

    # 🧪 [테스트 모드]: 원본 캐시를 완벽 보호하기 위해 dist/temp/test_cache 격리 폴더 사용
    try:
        import temp_session_logger
        if temp_session_logger.is_debug_mode():
            test_cache_dir = os.path.join(base, "temp", "test_cache")
            os.makedirs(test_cache_dir, exist_ok=True)
            for fname in os.listdir(base):
                src_path = os.path.join(base, fname)
                if os.path.isfile(src_path) and fname.endswith(".json"):
                    dst_path = os.path.join(test_cache_dir, fname)
                    if not os.path.exists(dst_path):
                        shutil.copy2(src_path, dst_path)
            return test_cache_dir
    except Exception:
        pass

    return base

CACHE_DIR = _get_cache_dir()
MODPACK_MEMORY_DIR = os.path.join(CACHE_DIR, "translation_modpack")
os.makedirs(MODPACK_MEMORY_DIR, exist_ok=True)
MEMORY_FILE = os.path.join(CACHE_DIR, "translation_memory.json")
BACKUP_FILE = os.path.join(CACHE_DIR, "translation_memory.json.bak")
ITEMS_MEMORY_FILE = os.path.join(CACHE_DIR, "translation_memory_items.json")
BOOKS_MEMORY_FILE = os.path.join(CACHE_DIR, "translation_memory_books.json")
KO_PACK_KEYS_FILE = os.path.join(CACHE_DIR, "ko_pack_keys.json")
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
_ko_pack_keys = {
    "general": set(),
    "items": set(),
    "books": set()
}
_ko_pack_keys_dirty = False
_ko_pack_loaded = False
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
    """파일 로드 시 언어 키 표준화 및 고속 딕셔너리 구성 (정규식 오버헤드 최소화)"""
    if not filepath or not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        # 파일 손상 시 압축 백업(.bak.gz) 또는 비압축 백업(.bak)에서 자동 안전 복구
        logging.warning(f"메모리 파일 로드 실패 ({filepath}), 백업 파일 복원 시도: {e}")
        try:
            from backup_utils import read_backup_json
            data = read_backup_json(filepath + ".bak.gz")
            if data is None:
                data = read_backup_json(filepath + ".bak")
            if data is None:
                return {}
        except Exception:
            return {}

    try:
        cleaned = {}
        clean_tag_pat = re.compile(r'\s*#\s*\[(?:Auto-Extracted|자동\s*추출됨)\]')
        for lang, translations in data.items():
            if not isinstance(translations, dict):
                continue
            std_lang = "한국어 (Korean)" if ("Korean" in lang or "한국어" in lang) else lang
            if std_lang not in cleaned:
                cleaned[std_lang] = {}
            target_dict = cleaned[std_lang]
            # 이미 검증/정제되어 디스크에 캐싱된 데이터이므로 무효 항목(None, 빈문자열, 원문==번역문)만 고속 필터링
            for src, tgt in translations.items():
                if not src or not tgt:
                    continue
                if isinstance(src, str) and "#" in src:
                    src = clean_tag_pat.sub('', src).strip()
                if isinstance(tgt, str) and "#" in tgt:
                    tgt = clean_tag_pat.sub('', tgt).strip()
                if not src or not tgt or src == tgt:
                    continue
                target_dict[src] = tgt
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

def _load_ko_pack_keys():
    """모드 내장 공식 한글패치(ko_pack) 불변 보호 키 목록 로드"""
    global _ko_pack_loaded
    if _ko_pack_loaded:
        return
    if os.path.exists(KO_PACK_KEYS_FILE):
        try:
            with open(KO_PACK_KEYS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    for cat in ("general", "items", "books"):
                        val = data.get(cat)
                        if isinstance(val, list):
                            _ko_pack_keys[cat] = set(val)
                        elif isinstance(val, dict):
                            _ko_pack_keys[cat] = set(val.keys())
        except Exception as e:
            logging.debug(f"ko_pack_keys 로드 스킵: {e}")
    _ko_pack_loaded = True


def _save_ko_pack_keys(sync_cloud=True):
    """모드 내장 공식 한글패치(ko_pack) 불변 보호 키 목록 안전 저장"""
    global _ko_pack_keys_dirty
    if not _ko_pack_keys_dirty:
        return
    try:
        dump_data = {
            cat: sorted(list(keys))
            for cat, keys in _ko_pack_keys.items()
        }
        tmp_file = KO_PACK_KEYS_FILE + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(dump_data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, KO_PACK_KEYS_FILE)
        _ko_pack_keys_dirty = False
        if sync_cloud:
            upload_ko_pack_keys_to_cloudflare()
    except Exception as e:
        logging.error(f"ko_pack_keys 저장 실패: {e}")


_ko_pack_keys_etag = ""
_is_uploading_ko_pack_keys = False


def upload_ko_pack_keys_to_cloudflare(sync_wait=False):
    """
    공식 한글패치 불변 보호 키 목록(dist/ko_pack_keys.json)을 Gzip 압축하여
    Cloudflare R2(PUT /ko-pack-keys)에 안전하게 업로드합니다.
    """
    global _is_uploading_ko_pack_keys
    if not os.path.exists(KO_PACK_KEYS_FILE):
        return

    try:
        import temp_session_logger
        if temp_session_logger.is_debug_mode():
            return
        worker_url = get_worker_url()
        if not worker_url:
            return
        safe, _ = is_cloud_upload_safe()
        if not safe:
            return
    except Exception:
        return

    with _lock:
        if _is_uploading_ko_pack_keys:
            return
        _is_uploading_ko_pack_keys = True

    def _do_upload():
        global _is_uploading_ko_pack_keys, _ko_pack_keys_etag
        try:
            with open(KO_PACK_KEYS_FILE, "r", encoding="utf-8") as f:
                content = f.read()
            if len(content) < 15:
                return
            gz_bytes = gzip.compress(content.encode("utf-8"), compresslevel=6)
            headers = {
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            }
            r = requests.put(f"{worker_url}/ko-pack-keys", headers=headers, data=gz_bytes, timeout=25)
            if r.status_code in (200, 201):
                res_data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                new_etag = res_data.get("etag", "")
                if new_etag:
                    _ko_pack_keys_etag = new_etag
                logging.info(f"☁️ [Cloudflare R2] ko_pack_keys.json.gz 공식 보호 키 업로드 완료 ({len(gz_bytes)/1024:.1f} KB)")
        except Exception as e:
            logging.debug(f"Cloudflare ko-pack-keys 업로드 스킵: {e}")
        finally:
            _is_uploading_ko_pack_keys = False

    t = threading.Thread(target=_do_upload, daemon=True)
    t.start()
    if sync_wait:
        t.join(timeout=10.0)


def sync_ko_pack_keys_from_cloudflare(force=False):
    """
    Cloudflare R2(GET /ko-pack-keys)에서 최신 공식 보호 키 목록을 Gzip으로 다운로드하여
    로컬 ko_pack_keys.json 및 메모리 캐시와 병합(Union)합니다 (ETag 304 지원).
    """
    global _ko_pack_keys_etag
    try:
        worker_url = get_worker_url()
        if not worker_url:
            return False
    except Exception:
        return False

    headers = {}
    if _ko_pack_keys_etag and os.path.exists(KO_PACK_KEYS_FILE) and not force:
        headers["If-None-Match"] = _ko_pack_keys_etag

    try:
        r = requests.get(f"{worker_url}/ko-pack-keys", headers=headers, timeout=15)
        if r.status_code == 304:
            return False
        if r.status_code == 200 and r.content:
            raw = r.content
            if r.headers.get("content-encoding") == "gzip" or (len(raw) >= 2 and raw[:2] == b'\x1f\x8b'):
                raw = gzip.decompress(raw)
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict):
                _load_ko_pack_keys()
                added_count = 0
                with _lock:
                    for cat in ("general", "items", "books"):
                        remote_keys = data.get(cat, [])
                        if isinstance(remote_keys, list):
                            cur_set = _ko_pack_keys.setdefault(cat, set())
                            b_len = len(cur_set)
                            cur_set.update(remote_keys)
                            added_count += len(cur_set) - b_len
                    if added_count > 0:
                        global _ko_pack_keys_dirty
                        _ko_pack_keys_dirty = True
                        _save_ko_pack_keys(sync_cloud=False)

                new_etag = r.headers.get("ETag", "")
                if new_etag:
                    _ko_pack_keys_etag = new_etag
                logging.info(f"☁️ [Cloudflare R2] ko_pack_keys 최신 공식 보호 키 동기화 완료 (+{added_count}개 갱신)")
                return True
    except Exception as e:
        logging.debug(f"Cloudflare ko-pack-keys 동기화 스킵: {e}")
    return False


def _store(cache, text, translated_text, target_lang_norm, force_override=False, tier=None):
    if isinstance(translated_text, str) and "#" in translated_text:
        translated_text = re.sub(r'\s*#\s*\[(?:Auto-Extracted|자동\s*추출됨)\]', '', translated_text).strip()
    if isinstance(text, str) and "#" in text:
        text = re.sub(r'\s*#\s*\[(?:Auto-Extracted|자동\s*추출됨)\]', '', text).strip()
    target_lang_key = target_lang_norm
    for k in cache.keys():
        if k.replace(" ", "") == target_lang_norm:
            target_lang_key = k
            break
    if target_lang_key not in cache:
        cache[target_lang_key] = {}
    prev_val = cache[target_lang_key].get(text)

    # 🔒 [Tier 0: 바닐라 마인크래프트 공식 번역 절대 불변 보호]
    # 바닐라 공식 번역은 게임 무결성을 위해 force_override 여부와 무관하게(오역수정기 포함) 절대로 수정/덮어쓰기 불가!
    if prev_val is not None:
        cat = "general"
        if cache is _items_cache:
            cat = "items"
        elif cache is _books_cache:
            cat = "books"

        if is_vanilla_official_key(text, cat):
            return

    # 🔒 [Tier 1: 공식 한글패치 및 티어 역전 방지 보호]
    if not force_override and prev_val is not None:
        cat = "general"
        if cache is _items_cache:
            cat = "items"
        elif cache is _books_cache:
            cat = "books"

        # 1) 공식 한글패치 및 사용자 수정(Tier 1) 불변 보호
        if text in _ko_pack_keys.get(cat, set()) or (cat == "general" and text in _ko_pack_keys.get("items", set())):
            return

        # 2) 티어 보호: 신규 번역의 품질 티어가 기존 캐시의 티어보다 낮으면(숫자가 크면) 덮어쓰기 원천 차단
        if tier is not None:
            existing_tier, _, _ = get_translation_tier(text, cat)
            if tier > existing_tier:
                return

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
    _load_ko_pack_keys()
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


def add_to_memory(text, translated_text, target_lang="한국어 (Korean)", force_override=False, tier=None):
    translated_text = sanitize_minecraft_formatting(text, translated_text)
    if not is_valid_translation(text, translated_text, target_lang):
        return

    if not _global_loaded:
        load_memory()

    target_lang_norm = target_lang.replace(" ", "")
    with _lock:
        if _current_modpack_id:
            _load_modpack_memory()
            _store(_modpack_cache, text, translated_text, target_lang_norm, force_override=force_override, tier=tier)
        # 클라우드 동기화 및 전역 재사용을 위해 _global_cache에도 동시 보존
        _store(_global_cache, text, translated_text, target_lang_norm, force_override=force_override, tier=tier)


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


def add_item_to_memory(text, translated_text, target_lang="한국어 (Korean)", force_override=False, tier=None):
    if not is_valid_translation(text, translated_text, target_lang):
        return
    if not _items_loaded:
        load_memory()
    target_lang_norm = target_lang.replace(" ", "")
    with _lock:
        _store(_items_cache, text, translated_text, target_lang_norm, force_override=force_override, tier=tier)
        # 아이템/블록/기계 명사구가 퀘스트 본문/설명/가이드북에서도 1차 완전일치로 즉시 치환되도록 _global_cache에도 동시 보존
        _store(_global_cache, text, translated_text, target_lang_norm, force_override=force_override, tier=tier)



def add_official_ko_to_memory(text, translated_text, target_lang="한국어 (Korean)", is_item=False):
    translated_text = sanitize_minecraft_formatting(text, translated_text)
    """
    모드 내장 공식 한글패치(ko_kr)에서 불러온 데이터를 기존 AI 번역 캐시보다 항상 최우선으로 덮어씁니다.
    이미 다른 캐시(_global_cache, _items_cache, _modpack_cache, _books_cache)에 이전 번역 데이터가 남아 있더라도
    모두 공식 한글패치 데이터로 일괄 덮어씌워 구버전 번역이 튀어나오는 현상을 원천 차단합니다.
    또한, ko_pack_keys에 등록하여 차후 AI 번역이나 클라우드 풀에 의해 덮어씌워지지 않도록 불변 보호합니다.
    """
    if not is_valid_translation(text, translated_text, target_lang):
        return
    if not _global_loaded or not _items_loaded or not _books_loaded:
        load_memory()

    _load_ko_pack_keys()
    target_lang_norm = target_lang.replace(" ", "")
    with _lock:
        global _ko_pack_keys_dirty
        if is_item:
            _ko_pack_keys["items"].add(text)
            _ko_pack_keys["general"].add(text)
        else:
            _ko_pack_keys["general"].add(text)
        _ko_pack_keys_dirty = True

        if _current_modpack_id:
            _load_modpack_memory()
            _store(_modpack_cache, text, translated_text, target_lang_norm, force_override=True)

        if is_item:
            _store(_items_cache, text, translated_text, target_lang_norm, force_override=True)
            _store(_global_cache, text, translated_text, target_lang_norm, force_override=True)
        else:
            _store(_global_cache, text, translated_text, target_lang_norm, force_override=True)
            # 만약 _items_cache에 동일 원문의 기존 AI 번역이 이미 존재한다면 공식 한글패치로 함께 덮어쓰기
            for k in list(_items_cache.keys()):
                if k.replace(" ", "") == target_lang_norm and text in _items_cache[k]:
                    _store(_items_cache, text, translated_text, target_lang_norm, force_override=True)
                    break

        # 만약 _books_cache에 동일 원문의 기존 AI 번역이 이미 존재한다면 공식 한글패치로 함께 덮어쓰기
        for k in list(_books_cache.keys()):
            if k.replace(" ", "") == target_lang_norm and text in _books_cache[k]:
                _store(_books_cache, text, translated_text, target_lang_norm, force_override=True)
                break

    # 공식 한글패치에서 발견된 단어/명사구는 translation_word 단어사전에도 최우선(is_official=True)으로 반영
    if "한국어" in target_lang or "Korean" in target_lang:
        try:
            import translation_word
            translation_word.learn_from_pair(text, translated_text, is_official=True)
        except Exception:
            pass


def is_official_ko_key(text, category="general"):
    """특정 텍스트가 모드 공식 한글패치(ko_pack)로 보호되고 있는지 확인"""
    _load_ko_pack_keys()
    if category == "all":
        return any(text in s for s in _ko_pack_keys.values())
    return text in _ko_pack_keys.get(category, set())


def get_official_ko_keys_count():
    """보호 중인 공식 한글패치 키 개수 반환"""
    _load_ko_pack_keys()
    return {
        cat: len(keys) for cat, keys in _ko_pack_keys.items()
    }


# 바닐라 마인크래프트 공식 어휘 세트 (Tier 0: 절대 불변, 오역수정기로도 수정 불가)
VANILLA_OFFICIAL_ITEMS_BASE = {
    # Minerals & Items
    "Stone", "Cobblestone", "Granite", "Diorite", "Andesite",
    "Grass Block", "Dirt", "Coarse Dirt", "Podzol",
    "Bedrock", "Sand", "Red Sand", "Gravel",
    "Coal Ore", "Iron Ore", "Gold Ore", "Redstone Ore",
    "Diamond Ore", "Lapis Lazuli Ore", "Emerald Ore", "Nether Quartz Ore",
    "Deepslate", "Cobbled Deepslate", "Ancient Debris",
    "Coal", "Charcoal", "Diamond", "Iron Ingot",
    "Gold Ingot", "Copper Ingot", "Netherite Ingot",
    "Netherite Scrap", "Stick", "Bowl", "String",
    "Feather", "Gunpowder", "Wheat Seeds", "Wheat",
    "Bread", "Flint", "Bucket", "Water Bucket",
    "Lava Bucket", "Milk Bucket", "Compass", "Clock",
    "Glowstone Dust", "Bone", "Sugar", "Rotten Flesh",
    "Ender Pearl", "Blaze Rod", "Ghast Tear",
    "Nether Wart", "Glass Bottle", "Spider Eye",
    "Fermented Spider Eye", "Blaze Powder", "Magma Cream",
    "Brewing Stand", "Cauldron", "Eye of Ender",
    "Glistering Melon Slice", "Experience Bottle", "Fire Charge",
    "Book and Quill", "Written Book", "Item Frame",
    "Flower Pot", "Carrot", "Potato", "Baked Potato",
    "Poisonous Potato", "Golden Carrot", "Skeleton Skull",
    "Wither Skeleton Skull", "Zombie Head", "Creeper Head",
    "Dragon Head", "Nether Star", "Pumpkin Pie",
    "Enchanted Book", "Nether Brick", "Prismarine Shard",
    "Prismarine Crystals", "Armor Stand", "Iron Nugget",
    "Gold Nugget", "Raw Iron", "Raw Copper", "Raw Gold",
    # Tools & Armor
    "Wooden Sword", "Wooden Shovel", "Wooden Pickaxe", "Wooden Axe", "Wooden Hoe",
    "Stone Sword", "Stone Shovel", "Stone Pickaxe", "Stone Axe", "Stone Hoe",
    "Golden Sword", "Golden Shovel", "Golden Pickaxe", "Golden Axe", "Golden Hoe",
    "Iron Sword", "Iron Shovel", "Iron Pickaxe", "Iron Axe", "Iron Hoe",
    "Diamond Sword", "Diamond Shovel", "Diamond Pickaxe", "Diamond Axe", "Diamond Hoe",
    "Netherite Sword", "Netherite Shovel", "Netherite Pickaxe", "Netherite Axe", "Netherite Hoe",
    "Leather Cap", "Leather Tunic", "Leather Pants", "Leather Boots",
    "Iron Helmet", "Iron Chestplate", "Iron Leggings", "Iron Boots",
    "Diamond Helmet", "Diamond Chestplate", "Diamond Leggings", "Diamond Boots",
    "Netherite Helmet", "Netherite Chestplate", "Netherite Leggings", "Netherite Boots",
    "Shield", "Totem of Undying", "Elytra", "Bow", "Crossbow", "Trident", "Arrow",
    # Blocks & Machines
    "Crafting Table", "Furnace", "Blast Furnace", "Smoker",
    "Chest", "Trapped Chest", "Ender Chest", "Barrel",
    "Anvil", "Chipped Anvil", "Damaged Anvil",
    "Smithing Table", "Fletching Table", "Cartography Table",
    "Loom", "Stonecutter", "Grindstone", "Enchanting Table",
    "Beacon", "Conduit", "Campfire", "Soul Campfire",
    "Shulker Box", "Hopper", "Dropper", "Dispenser",
    "Observer", "Piston", "Sticky Piston", "Redstone Lamp",
    "Daylight Detector", "Tripwire Hook", "Lever",
    "Oak Log", "Spruce Log", "Birch Log", "Jungle Log",
    "Acacia Log", "Dark Oak Log", "Mangrove Log", "Cherry Log",
    "Oak Planks", "Spruce Planks", "Birch Planks", "Jungle Planks",
    "Acacia Planks", "Dark Oak Planks", "Mangrove Planks", "Cherry Planks",
    "Glass", "Tinted Glass", "Torch", "Soul Torch", "Lantern", "Soul Lantern"
}

VANILLA_OFFICIAL_GENERAL_BASE = {
    "Minecraft", "Options", "Video Settings", "Audio Settings",
    "Singleplayer", "Multiplayer", "Game Menu",
    "Achievements", "Advancements", "Statistics",
    "Creeper", "Zombie", "Skeleton", "Spider",
    "Cave Spider", "Enderman", "Witch", "Slime",
    "Magma Cube", "Ghast", "Blaze", "Wither Skeleton",
    "Ender Dragon", "Wither", "Warden", "Piglin",
    "Piglin Brute", "Zombified Piglin", "Hoglin", "Zoglin",
    "Villager", "Iron Golem", "Snow Golem", "Wolf",
    "Cat", "Horse", "Donkey", "Mule", "Llama",
    "Fox", "Panda", "Polar Bear", "Bee", "Bat",
    "Parrot", "Dolphin", "Sea Turtle", "Glow Squid",
    "Squid", "Strider", "Allay", "Frog", "Tadpole",
    "Camel", "Sniffer", "Breeze",
    # Effects
    "Speed", "Slowness", "Haste", "Mining Fatigue",
    "Strength", "Instant Health", "Instant Damage", "Jump Boost",
    "Nausea", "Regeneration", "Resistance", "Fire Resistance",
    "Water Breathing", "Invisibility", "Blindness", "Night Vision",
    "Hunger", "Weakness", "Poison", "Wither", "Health Boost",
    "Absorption", "Saturation", "Glowing", "Levitation",
    "Luck", "Bad Luck", "Slow Falling", "Conduit Power",
    "Dolphins Grace", "Bad Omen", "Hero of the Village", "Darkness"
}


def is_vanilla_official_key(text: str, category: str = "general") -> bool:
    """바닐라 마인크래프트 공식 번역(Tier 0) 키인지 확인 (오역수정기로도 수정 절대 불가)"""
    if not text:
        return False
    _load_cache_tiers()
    if _cache_tiers_data:
        t0 = _cache_tiers_data.get("tier_0_keys", {})
        if category == "all":
            if any(text in t0.get(c, []) for c in ("general", "items", "books")):
                return True
        elif text in t0.get(category, []) or text in t0.get("general", []):
            return True

    # 티어 파일이 없는 로컬 환경용 기본 바닐라 세트 검사
    if category in ("items", "all") and text in VANILLA_OFFICIAL_ITEMS_BASE:
        return True
    if category in ("general", "all") and (text in VANILLA_OFFICIAL_GENERAL_BASE or text in VANILLA_OFFICIAL_ITEMS_BASE):
        return True
    return False


TIERS_CACHE_FILE = os.path.join(CACHE_DIR, "cache_tiers.json")
_cache_tiers_data = None
_cache_tiers_loaded = False

MODEL_TIER_MAPPING = {
    # Tier 2: 고품질 AI
    "claude-sonnet-5-5": 2,
    "gpt-6.1-terra": 2,
    "gemini-3.8-flash": 2,
    "deepseek-v4-pro": 2,
    # Tier 3: 중품질 / 표준 AI
    "gpt-5-mini": 3,
    "gpt-4.1": 3,
    "gemini-3.6-flash": 3,
    "claude-haiku-5-5": 3,
    # Tier 4: 가성비 / 저품질 AI (기본값)
    "gemini-3.5-flash-lite": 4,
    "gemini-3.1-flash-lite": 4,
    "gpt-6-luna": 4,
    "deepseek-flash": 4,
}


def get_model_tier(model_name: str) -> int:
    """
    AI 모델명에 대응하는 번역 품질 티어(2~4)를 반환합니다.
    - Tier 2 (고품질 AI): claude-sonnet-5-5, gpt-6.1-terra, gemini-3.8-flash, deepseek-v4-pro 등
    - Tier 3 (중품질/표준 AI): gpt-5-mini, gpt-4.1, gemini-3.6-flash, claude-haiku-5-5 등
    - Tier 4 (저품질/가성비 AI / 기본값): gemini-3.5-flash-lite, gpt-6-luna, deepseek-flash, Local AI 등
    """
    if not model_name:
        return 4
    m = model_name.lower().strip()
    for name, tier in MODEL_TIER_MAPPING.items():
        if name in m:
            return tier
    if any(k in m for k in ("sonnet", "terra", "3.8-flash", "v4-pro")):
        return 2
    if any(k in m for k in ("mini", "4.1", "3.6-flash", "haiku")):
        return 3
    return 4


def _load_cache_tiers():
    global _cache_tiers_data, _cache_tiers_loaded
    if _cache_tiers_loaded:
        return
    if os.path.exists(TIERS_CACHE_FILE):
        try:
            with open(TIERS_CACHE_FILE, "r", encoding="utf-8") as f:
                _cache_tiers_data = json.load(f)
        except Exception:
            _cache_tiers_data = None
    _cache_tiers_loaded = True


def get_translation_tier(text: str, category: str = "general") -> tuple:
    """
    특정 텍스트의 캐시 티어를 반환합니다.
    Returns: (tier_level: int, source_desc: str, model_name: str)
      - Tier 0: (0, "바닐라 공식 번역 (수정 불가)", "vanilla")
      - Tier 1: (1, "모드 공식 한글패치/사용자 수정", "official_ko_pack")
      - Tier 2: (2, "고품질 AI 번역", "high_ai")
      - Tier 3: (3, "중품질 AI 번역", "mid_ai")
      - Tier 4: (4, "가성비 AI 번역 (미등록/기본값)", "gemini-3.5-flash-lite")
    
    ※ 다른 로컬 환경이라 cache_tiers.json 파일이 없거나 티어 정보가 누락된 경우:
      - 바닐라 공식 번역은 무조건 Tier 0 (수정 불가) 반환
      - 모드 공식 한글패치(ko_pack) 등록 키라면 Tier 1 유지
      - 그 외 모든 캐시 항목은 안전하게 최하위인 Tier 4(가성비 AI gemini-3.5-flash-lite)로 취급합니다.
    """
    # 🔒 Tier 0 판정: 바닐라 마인크래프트 공식 번역 (절대 불변)
    if is_vanilla_official_key(text, category):
        return 0, "바닐라 공식 번역 (수정 불가)", "vanilla"

    _load_cache_tiers()
    if _cache_tiers_data:
        t0 = _cache_tiers_data.get("tier_0_keys", {})
        if text in t0.get(category, []) or text in t0.get("general", []):
            return 0, "바닐라 공식 번역 (수정 불가)", "vanilla"
        t1 = _cache_tiers_data.get("tier_1_keys", {})
        if text in t1.get(category, []) or text in t1.get("general", []):
            return 1, "모드 공식 한글패치", "official_ko_pack"
        t2 = _cache_tiers_data.get("tier_2_keys", {})
        if text in t2.get(category, []) or text in t2.get("general", []):
            return 2, "고품질 AI 번역", "high_ai"
        t3 = _cache_tiers_data.get("tier_3_keys", {})
        if text in t3.get(category, []) or text in t3.get("general", []):
            return 3, "중품질 AI 번역", "mid_ai"

    # 로컬에 cache_tiers.json 파일이 없거나 파일 내에 미등록된 키:
    if is_official_ko_key(text, category):
        return 1, "공식 한글패치", "official_ko_pack"

    # 티어 데이터가 없는 캐시 데이터는 무조건 4티어(gemini-3.5-flash-lite)로 안전하게 취급
    return 4, "가성비 AI 번역 (미등록 기본값)", "gemini-3.5-flash-lite"



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


def add_book_to_memory(text, translated_text, target_lang="한국어 (Korean)", force_override=False, tier=None):
    if not is_valid_translation(text, translated_text, target_lang):
        return
    if not _books_loaded:
        load_memory()
    target_lang_norm = target_lang.replace(" ", "")
    with _lock:
        _store(_books_cache, text, translated_text, target_lang_norm, force_override=force_override, tier=tier)
        # 가이드북 텍스트가 퀘스트 본문/설명에서도 1차 완전일치로 즉시 치환되도록 _global_cache에도 동시 보존
        _store(_global_cache, text, translated_text, target_lang_norm, force_override=force_override, tier=tier)



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
                from backup_utils import create_compressed_backup
                create_compressed_backup(filepath)
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

        _save_ko_pack_keys()


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
COMMIT_FILES_COMPACT_THRESHOLD = 100  # 누적 커밋 파일이 100개(약 50,000문장)에 도달하면 마스터 압축 및 정리 자동 실행
SYNC_COOLDOWN_SECONDS = 180.0  # 짧은 시간 내 중복 Pull 방지 (3분 쿨타임)

_commit_queue = {}  # (category, lang, src) -> tgt
_commit_timer = None
_is_committing = False
_active_commit_thread = None
_active_sync_thread = None
_active_pack_thread = None
_session_committed_count = 0
_session_committed_chunks = 0
_last_pull_complete = False
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

    global _commit_timer, _is_committing, _active_commit_thread

    with _lock:
        if _commit_timer is not None:
            _commit_timer.cancel()
            _commit_timer = None
        if _is_committing:
            existing_thread = _active_commit_thread
        else:
            existing_thread = None

    if existing_thread is not None:
        if sync_wait and existing_thread.is_alive():
            existing_thread.join(timeout=20.0)
        return

    with _lock:
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

    def _do_commit():
        global _is_committing, _consecutive_upload_errors, _circuit_breaker_until, _session_committed_count, _session_committed_chunks
        # Cloudflare Worker 무료 플랜 CPU 제한(10ms) 및 타임아웃 방지를 위해 500개 단위로 안전 분할 전송
        chunk_size = 500

        try:
            while True:
                with _lock:
                    if not _commit_queue:
                        break
                    snapshot_items = [
                        {"cat": cat, "lang": lang, "src": src, "tgt": tgt}
                        for (cat, lang, src), tgt in _commit_queue.items()
                    ]
                    _commit_queue.clear()

                committed_total = 0
                failed_items = []

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
                            _session_committed_count += len(chunk)
                            _session_committed_chunks += 1
                            try:
                                res_json = r.json()
                                new_last_id = res_json.get("latest_id") or res_json.get("key")
                                if new_last_id:
                                    # 주의: last_commit_id는 오직 /pull 증분 수신 시점에만 갱신해야
                                    # 다른 사람이 직전에 올린 커밋(C3)을 건너뛰지 않고 완벽히 병합할 수 있음!
                                    sync_state = _load_sync_state()
                                    sync_state["last_pushed_commit_id"] = str(new_last_id)
                                    sync_state["uncompacted_chunks"] = sync_state.get("uncompacted_chunks", 0) + 1
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
                    break

                # 누적 커밋 파일이 임계치(100개 = 약 50,000문장)에 도달하면 백그라운드에서 마스터 압축 및 정리 실행
                sync_state = _load_sync_state()
                total_uncompacted = max(_session_committed_chunks, sync_state.get("uncompacted_chunks", 0))
                if total_uncompacted >= COMMIT_FILES_COMPACT_THRESHOLD:
                    logging.info(
                        f"📦 [Auto-Compact] 누적 커밋 파일이 {total_uncompacted}개에 도달하여 "
                        f"마스터 스냅샷 자동 압축 업로드 및 정리를 백그라운드에서 실행합니다."
                    )
                    _session_committed_chunks = 0
                    sync_state["uncompacted_chunks"] = 0
                    _save_sync_state(sync_state)
                    upload_snapshots_to_cloudflare(force=True, purge_commits=True)
        finally:
            with _lock:
                _is_committing = False

    t = threading.Thread(target=_do_commit, daemon=True)
    _active_commit_thread = t
    t.start()
    if sync_wait:
        t.join(timeout=20.0)


def upload_snapshots_to_cloudflare(force=False, purge_commits=True, min_entries=5000, sync_wait=False):
    """
    현재 로컬 3대 마스터 캐시를 Gzip 압축하여 Cloudflare R2 스냅샷(/snapshot/:cat)으로 업로드하고,
    스냅샷에 포함된 과거 커밋 파일들을 Worker에서 일괄 삭제(Purge)하여 저장소를 압축 통합합니다.
    (업로드 직전 최신 커밋을 먼저 전부 Pull하여 병합하므로 덮어쓰기 유실 0%)
    """
    if not force:
        return
    try:
        import temp_session_logger
        if temp_session_logger.is_debug_mode():
            return
    except Exception:
        pass
    worker_url = get_worker_url()
    if not worker_url:
        return

    global _active_pack_thread
    import urllib.parse

    with _lock:
        existing_pack = _active_pack_thread if (_active_pack_thread is not None and _active_pack_thread.is_alive()) else None

    if existing_pack is not None:
        if sync_wait:
            existing_pack.join(timeout=30.0)
        return

    def _async_pack():
        global _session_committed_count, _session_committed_chunks
        # 1. 혹시 남아있는 로컬 커밋 큐가 있다면 먼저 100% 전송 완료 보장
        try:
            flush_commit_queue(sync_wait=True)
        except Exception:
            pass

        # 2. 압축 직전 R2의 모든 최신 커밋(내 커밋 + 다른 유저 커밋 C3, C4)을 끝까지 Pull하여 로컬에 합침 (사전 머지)
        try:
            sync_from_cloudflare(force_wait=True, bypass_cooldown=True)
        except Exception:
            pass

        sync_state = _load_sync_state()
        last_commit_id = str(sync_state.get("last_commit_id", "") or "").strip()
        last_compacted_id = str(sync_state.get("last_compacted_commit_id", "") or "").strip()
        uncompacted = max(_session_committed_chunks, sync_state.get("uncompacted_chunks", 0))

        # 새로 병합할 커밋이 없고 이미 최신 상태까지 마스터 압축이 완료된 경우 불필요한 대용량 재업로드 생략
        if not last_commit_id and _session_committed_count == 0 and uncompacted == 0:
            return
        if last_commit_id and last_commit_id == last_compacted_id and _session_committed_count == 0 and uncompacted == 0:
            return

        all_succeeded = True
        conflict_detected = False
        server_newer_commit = ""

        for cat, (gz_name, cache) in MASTER_FILES.items():
            try:
                with _lock:
                    data_copy = {k: v.copy() for k, v in cache.items() if isinstance(v, dict)}
                total_items = sum(len(v) for v in data_copy.values())
                # 최소 항목 수 검증 (빈 캐시/손상된 캐시로 마스터 덮어쓰기 원천 방지)
                if total_items < min_entries:
                    logging.warning(f"스냅샷 업로드 스킵: [{cat}] 항목 수({total_items})가 안전 기준({min_entries}) 미달")
                    all_succeeded = False
                    continue

                raw_json = json.dumps(data_copy, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                gz_bytes = gzip.compress(raw_json, compresslevel=6)

                query_str = f"?commit_id={urllib.parse.quote(last_commit_id)}" if last_commit_id else ""

                r = requests.put(
                    f"{worker_url}/snapshot/{cat}{query_str}",
                    headers={"Content-Type": "application/gzip"},
                    data=gz_bytes,
                    timeout=90,
                )
                if r.status_code in (200, 201):
                    logging.info(f"✅ Cloudflare R2 스냅샷 업로드 성공 [{gz_name}] ({len(gz_bytes)/1024:.1f} KB)")
                elif r.status_code == 409:
                    conflict_detected = True
                    all_succeeded = False
                    try:
                        res_j = r.json()
                        server_newer_commit = str(res_j.get("server_last_commit", "") or "").strip()
                    except Exception:
                        pass
                    logging.info(f"ℹ️ Cloudflare R2 스냅샷 동시성 방어 [{gz_name}]: 서버에 이미 더 최신 마스터가 존재하여 덮어쓰기가 안전하게 방지되었습니다.")
                    break
                else:
                    all_succeeded = False
                    logging.warning(f"⚠️ Cloudflare R2 스냅샷 업로드 실패 [{gz_name}]: {r.status_code} {r.text}")
            except Exception as e:
                all_succeeded = False
                logging.warning(f"스냅샷 업로드 오류 ({cat}): {e}")

        if conflict_detected and server_newer_commit:
            sync_state = _load_sync_state()
            sync_state["last_compacted_commit_id"] = server_newer_commit
            _save_sync_state(sync_state)
            return

        # 3. 3대 마스터 스냅샷이 모두 정상 업로드되었고 모든 커밋이 로컬에 완벽히 병합된 경우에만 과거 커밋 일괄 정리(Purge)
        if all_succeeded and purge_commits and last_commit_id and _last_pull_complete:
            try:
                purge_url = f"{worker_url}/commits/purge?until={urllib.parse.quote(last_commit_id)}"
                pr = requests.post(purge_url, timeout=30)
                if pr.status_code == 200:
                    pj = pr.json() if pr.headers.get("content-type", "").startswith("application/json") else {}
                    purged = pj.get("purged", 0)
                    logging.info(f"🧹 Cloudflare R2 병합 완료 커밋 {purged}개 자동 정리 완료 (기준: {last_commit_id})")
                    sync_state = _load_sync_state()
                    sync_state["last_compacted_commit_id"] = last_commit_id
                    sync_state["uncompacted_chunks"] = 0
                    _save_sync_state(sync_state)
                    _session_committed_count = 0
                    _session_committed_chunks = 0
            except Exception as pe:
                logging.warning(f"커밋 자동 정리 요청 실패: {pe}")

    t = threading.Thread(target=_async_pack, daemon=True)
    _active_pack_thread = t
    t.start()
    if sync_wait:
        t.join(timeout=30.0)


def finalize_translation_session(modpack_path=None, sync_wait=False):
    """
    번역 작업 종료(완료 또는 중단) 시 호출되는 통합 동기화 파이프라인:
    1. 로컬 캐시(마스터 3종 + 개별 모드팩 캐시) 디스크 영구 저장
    2. 대기열에 남은 모든 신규 번역(30개, 50개 등 수량 무관)을 Gzip 커밋으로 즉시 100% 클라우드 전송
    3. 클라우드 최신 커밋(다른 사람 커밋 포함)을 로컬에 전부 당겨와서 합친 뒤(사전 머지),
       마스터 스냅샷 압축 갱신 & 병합 완료된 이전 커밋 정리(Purge)
    """
    try:
        import temp_session_logger
        if temp_session_logger.is_debug_mode():
            return
    except Exception:
        pass

    # 1. 로컬 저장 및 남은 큐 즉시 커밋 전송 (자투리 수량에 상관없이 100% 즉시 전송)
    save_memory(flush_now=True)

    # 2. 마스터 스냅샷 갱신 및 누적 커밋 정리 (사전 머지 후 업로드)
    upload_snapshots_to_cloudflare(force=True, purge_commits=True, sync_wait=sync_wait)

    # 3. 공식 한글패치 보호 키(ko_pack_keys) 및 단어사전(words) 클라우드 업로드
    upload_ko_pack_keys_to_cloudflare(sync_wait=sync_wait)
    try:
        import translation_word
        translation_word.upload_words_to_cloudflare(sync_wait=sync_wait)
    except Exception:
        pass


def wait_for_cloud_sync(timeout=6.0):
    """클라우드 동기화(Pull)가 완료될 때까지 대기"""
    if _cloud_sync_done.is_set():
        return True
    return _cloud_sync_done.wait(timeout=timeout)


def sync_from_cloudflare(force_wait=False, bypass_cooldown=False):
    """
    Cloudflare R2 순수 스토리지 기반 Git-Pull 증분 동기화:
    1) 이미 최근 3분 이내에 동기화했다면 중복 네트워크 호출 없이 즉시 통과 (0 Byte 소모)
    2) 로컬 캐시가 비어 있거나 스냅샷 ETag가 변경된 경우에만 R2에서 master_*.json.gz 다운로드 (304 Not Modified 지원)
    3) 내 마지막 커밋 키(last_commit_id) 이후에 다른 유저들이 올린 신규 델타 커밋만 /pull 로 가볍게 병합
    """
    global _is_syncing, _active_sync_thread
    worker_url = get_worker_url()
    if not worker_url:
        _cloud_sync_done.set()
        return

    now = time.time()
    if _cloud_sync_done.is_set() and (now - _last_sync_timestamp < SYNC_COOLDOWN_SECONDS) and not force_wait and not bypass_cooldown:
        return

    with _lock:
        if _is_syncing:
            existing_sync = _active_sync_thread
        else:
            existing_sync = None

    if existing_sync is not None:
        if force_wait and existing_sync.is_alive():
            existing_sync.join(timeout=20.0)
        if not bypass_cooldown:
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

            r = requests.get(f"{worker_url}/master/{category}", headers=headers, timeout=25)
            snap_commit = str(r.headers.get("X-Last-Commit-Id", "") or "").strip()
            if snap_commit:
                with _lock:
                    cur_last = str(sync_state.get("last_commit_id", "") or "").strip()
                    if not cur_last or snap_commit > cur_last:
                        sync_state["last_commit_id"] = snap_commit
                    cur_compacted = str(sync_state.get("last_compacted_commit_id", "") or "").strip()
                    if not cur_compacted or snap_commit > cur_compacted:
                        sync_state["last_compacted_commit_id"] = snap_commit

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
        global _last_pull_complete
        import urllib.parse
        last_id = str(sync_state.get("last_commit_id", "") or "").strip()
        if last_id.isdigit() and len(last_id) < 10:
            last_id = ""
        pulled_total = 0
        _last_pull_complete = False
        try:
            while True:
                encoded_since = urllib.parse.quote(last_id)
                r = requests.get(f"{worker_url}/pull?since={encoded_since}&limit=30", timeout=15)
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

                            # 🔒 [공식 한글패치 보호]: 클라우드 커밋이 로컬 공식 번역(ko_pack)을 덮어쓰지 못하도록 방어
                            if src in _ko_pack_keys.get(cat, set()) or src in _ko_pack_keys.get("items", set()):
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
                    _last_pull_complete = True
                    break

            if pulled_total > 0:
                logging.info(f"🔄 [Cloudflare R2 Pull] 신규 커밋 {pulled_total:,}개 증분 병합 완료 (최신: {last_id})")
                estimated_chunks = max(1, pulled_total // 500)
                sync_state["uncompacted_chunks"] = sync_state.get("uncompacted_chunks", 0) + estimated_chunks
        except Exception as e:
            logging.debug(f"Cloudflare 증분 Pull 실패: {e}")
        return pulled_total > 0

    def _async_cloudflare_sync():
        global _is_syncing, _last_sync_timestamp, _session_committed_chunks
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

            # 2.5단계: 누적 커밋 파일이 100개에 도달했을 경우 백그라운드 자동 마스터 압축 트리거
            current_uncompacted = max(_session_committed_chunks, sync_state.get("uncompacted_chunks", 0))
            if current_uncompacted >= COMMIT_FILES_COMPACT_THRESHOLD:
                logging.info(f"📦 [Auto-Compact] 누적 커밋 파일이 {current_uncompacted}개에 도달하여 마스터 스냅샷 자동 압축을 시작합니다.")
                _session_committed_chunks = 0
                sync_state["uncompacted_chunks"] = 0
                _save_sync_state(sync_state)
                upload_snapshots_to_cloudflare(force=True, purge_commits=True)

            # 3단계: 단어사전(translation_memory_words.json) 무압축 원본 동기화
            try:
                import translation_word
                translation_word.sync_words_from_cloudflare()
            except Exception:
                pass

            # 3.5단계: 공식 한글패치 보호 키(ko_pack_keys.json) 압축 동기화
            try:
                sync_ko_pack_keys_from_cloudflare()
            except Exception:
                pass

            # 4단계: 변경사항이 있었을 때만 로컬 파일 업데이트
            if changed_master or changed_pull:
                _save_local_caches_only()

            # 5단계: 이전에 오프라인이라 못 보낸 대기 커밋이 있으면 자동 푸시
            if _commit_queue:
                flush_commit_queue()
        finally:
            with _lock:
                _is_syncing = False
            _cloud_sync_done.set()

    t = threading.Thread(target=_async_cloudflare_sync, daemon=True)
    _active_sync_thread = t
    t.start()
    if force_wait:
        t.join(timeout=25.0)





# ====================================================================
# Memory Editor API & 오역 감지 엔진 (translation_editor 모듈 위임 및 Re-export)
# ====================================================================

from translation_editor import (
    search_memory,
    update_memory_entry,
    delete_memory_entry,
    patch_modpack_files,
    patch_modpack_batch_files,
    _decompose_korean,
    _compare_syllables_phonetic,
    _CORE_MINECRAFT_DICT,
    calculate_translation_error_rate,
    dispatch_audit_report,
    dispatch_batch_audit_report,
    retranslate_texts,
)
