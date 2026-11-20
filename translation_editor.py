# -*- coding: utf-8 -*-
"""
translation_editor.py - 인게임 오역 수정기 백엔드 모듈

오역 수정기 UI에서 사용하는 다음 핵심 기능들을 전담 관리합니다:
1. 번역 메모리 검색, 수정, 삭제 (Memory Editor API)
2. WordNet 및 어휘 사전(_common_words, _proper_nouns) 연동
3. 영-한 자음/모음 음운 분석 및 오역률/분탕 감지 알고리즘 (Two-Pillar Validation)
4. 일반 명사의 음차 번역 지원 (예: iron -> 아이언) 및 WordNet Lemmatization
"""

import os
import re
import sys
import time
import logging
from constants import has_hangul

# WordNet 및 어휘 사전 모듈 (지연 임포트 및 안전한 폴백)
try:
    import translation_word
except Exception:
    translation_word = None


# ====================================================================
# Memory Editor API (검색, 수정, 삭제)
# ====================================================================

_MC_COLOR_RE = re.compile(r'[§&][0-9a-fk-orA-FK-OR]')

def search_memory(query, category="all", target_lang="한국어 (Korean)", limit=100, search_field="all"):
    """
    모든 캐시(일반, 아이템, 책)에서 주어진 검색어(query)를 포함하는 원문/번역문을 초고속으로 찾습니다.
    사전 컴파일 정규식과 단축 평가(Short-circuit)로 불필요한 regex 실행을 최소화합니다.
    Args:
        search_field (str): 'all' (원문+번역문 통합), 'src' (영어 원문만), 'tgt' (한글 번역문만)
    반환 형태: [{"category": "items", "src": "...", "tgt": "..."}]
    """
    import translation_memory
    if not translation_memory._global_loaded:
        translation_memory.load_memory()

    target_lang_norm = target_lang.replace(" ", "")
    results = []
    query_lower = query.lower().strip()
    terms = [t.strip() for t in query_lower.split(",") if t.strip()]
    if not terms:
        return results

    def _term_in_text(term, raw_text, lower_text):
        if term in lower_text:
            return True
        if ('§' in raw_text or '&' in raw_text) and term in _MC_COLOR_RE.sub('', lower_text):
            return True
        return False

    def _search_in_cache(cache, cat_name):
        for lang_key, entries in cache.items():
            if lang_key.replace(" ", "") != target_lang_norm:
                continue
            for src, tgt in entries.items():
                src_l = src.lower()
                tgt_l = tgt.lower()

                if search_field == "src":
                    match = all(_term_in_text(t, src, src_l) for t in terms)
                elif search_field == "tgt":
                    match = all(_term_in_text(t, tgt, tgt_l) for t in terms)
                else:
                    match = all((_term_in_text(t, src, src_l) or _term_in_text(t, tgt, tgt_l)) for t in terms)

                if match:
                    is_vanilla = translation_memory.is_vanilla_official_key(src, cat_name)
                    is_off = translation_memory.is_official_ko_key(src, cat_name) or is_vanilla
                    results.append({"category": cat_name, "src": src, "tgt": tgt, "is_official": is_off, "is_vanilla": is_vanilla})
                    if len(results) >= limit:
                        return True
        return False

    with translation_memory._lock:
        if category in ("all", "items"):
            if _search_in_cache(translation_memory._items_cache, "items"):
                return results
        if category in ("all", "general"):
            if _search_in_cache(translation_memory._global_cache, "general"):
                return results
        if category in ("all", "books"):
            if _search_in_cache(translation_memory._books_cache, "books"):
                return results

    return results


def update_memory_entry(category, original_src, new_tgt, target_lang="한국어 (Korean)"):
    """캐시 업데이트 후 로컬 저장 및 클라우드 커밋 큐에 반영합니다."""
    import translation_memory
    if translation_memory.is_vanilla_official_key(original_src, category):
        logging.warning(f"🔒 [Tier 0 절대 불변] '{original_src}' 항목은 바닐라 마인크래프트 공식 번역이므로 수정할 수 없습니다.")
        return False

    target_lang_norm = target_lang.replace(" ", "")
    cache_to_update = None

    if category == "items":
        cache_to_update = translation_memory._items_cache
    elif category == "general":
        cache_to_update = translation_memory._global_cache
    elif category == "books":
        cache_to_update = translation_memory._books_cache
    else:
        return False

    with translation_memory._lock:
        translation_memory._store(cache_to_update, original_src, new_tgt, target_lang_norm, force_override=True)

    # CACHE_DIR가 테스트 모드일 때는 자동으로 dist/temp/test_cache 로 격리되어 저장됨
    translation_memory.save_memory(flush_now=True)
    return True


def delete_memory_entry(category, original_src, target_lang="한국어 (Korean)"):
    """로컬 캐시에서 항목을 삭제합니다."""
    import translation_memory
    if translation_memory.is_vanilla_official_key(original_src, category):
        logging.warning(f"🔒 [Tier 0 절대 불변] '{original_src}' 항목은 바닐라 마인크래프트 공식 번역이므로 삭제할 수 없습니다.")
        return False
    target_lang_norm = target_lang.replace(" ", "")
    cache_to_update = None

    if category == "items":
        cache_to_update = translation_memory._items_cache
    elif category == "general":
        cache_to_update = translation_memory._global_cache
    elif category == "books":
        cache_to_update = translation_memory._books_cache
    else:
        return False

    with translation_memory._lock:
        deleted = False
        for k in cache_to_update.keys():
            if k.replace(" ", "") == target_lang_norm:
                if original_src in cache_to_update[k]:
                    del cache_to_update[k][original_src]
                    deleted = True
                break

    if deleted:
        translation_memory._save_local_caches_only(deleted_keys={original_src})
    return True


def patch_modpack_files(modpack_dir, old_tgt, new_tgt):
    """
    모드팩 폴더 내 퀘스트, 리소스팩/언어, 가이드북 파일들을 초고속 스캔하여
    텍스트를 인플레이스(In-place) 치환하고 카테고리별 수정 파일 개수를 반환합니다.
    """
    if not modpack_dir or not os.path.isdir(modpack_dir) or not old_tgt or not new_tgt:
        return {"modpack_name": "", "total": 0, "quests": 0, "lang_resources": 0, "books": 0}

    import glob
    mp_name = os.path.basename(modpack_dir.rstrip(os.sep))
    results = {"modpack_name": mp_name, "total": 0, "quests": 0, "lang_resources": 0, "books": 0}

    # 1. 퀘스트 파일 탐색 (.snbt, .json)
    quest_patterns = [
        os.path.join(modpack_dir, "config", "ftbquests", "quests", "**", "*.snbt"),
        os.path.join(modpack_dir, "defaultconfigs", "ftbquests", "quests", "**", "*.snbt"),
        os.path.join(modpack_dir, "config", "betterquesting", "**", "*.json"),
        os.path.join(modpack_dir, "config", "hqm", "**", "*.json"),
    ]
    quest_files = set()
    for p in quest_patterns:
        quest_files.update(glob.glob(p, recursive=True))

    # 2. 리소스팩 및 언어 파일 탐색 (.json, .lang)
    lang_patterns = [
        os.path.join(modpack_dir, "kubejs", "assets", "**", "lang", "*.json"),
        os.path.join(modpack_dir, "resourcepacks", "**", "lang", "*.json"),
        os.path.join(modpack_dir, "resourcepacks", "**", "lang", "*.lang"),
    ]
    lang_files = set()
    for p in lang_patterns:
        lang_files.update(glob.glob(p, recursive=True))

    # 3. 가이드북 파일 탐색 (patchouli_books)
    book_patterns = [
        os.path.join(modpack_dir, "patchouli_books", "**", "*.json"),
        os.path.join(modpack_dir, "config", "patchouli_books", "**", "*.json"),
    ]
    book_files = set()
    for p in book_patterns:
        book_files.update(glob.glob(p, recursive=True))

    def _patch_file(filepath):
        try:
            if not os.path.isfile(filepath):
                return False
            if filepath.lower().endswith(('.zip', '.jar', '.png', '.ogg', '.bak', '.bak.gz')):
                return False
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            if old_tgt in content:
                new_content = content.replace(old_tgt, new_tgt)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(new_content)
                return True
        except Exception as e:
            logging.debug(f"파일 핫패치 오류 ({filepath}): {e}")
        return False

    for qf in quest_files:
        if _patch_file(qf):
            results["quests"] += 1
            results["total"] += 1

    for lf in lang_files:
        if _patch_file(lf):
            results["lang_resources"] += 1
            results["total"] += 1

    for bf in book_files:
        if _patch_file(bf):
            results["books"] += 1
            results["total"] += 1

    # 4. 리소스팩 ZIP 파일 내부 핫패치
    try:
        import mod_jar_extractor
        escaped_mp_dir = glob.escape(modpack_dir)
        escaped_mp_name = glob.escape(mp_name)
        zip_glob_name = f"{escaped_mp_name}[[]*[]].zip"
        candidates = glob.glob(os.path.join(escaped_mp_dir, zip_glob_name))
        candidates.extend(glob.glob(os.path.join(escaped_mp_dir, "resourcepacks", zip_glob_name)))
        legacy = os.path.join(modpack_dir, "QuestTranslatorPro_Pack.zip")
        if os.path.exists(legacy):
            candidates.append(legacy)

        for pz in set(candidates):
            try:
                changed = mod_jar_extractor.hotpatch_resource_pack(pz, old_tgt, new_tgt)
                if changed:
                    results["lang_resources"] += 1
                    results["total"] += 1
            except Exception as ze:
                logging.debug(f"ZIP 리소스팩 핫패치 오류 ({pz}): {ze}")
    except Exception as e:
        logging.debug(f"리소스팩 핫패치 모듈 호출 실패: {e}")

    return results


def patch_modpack_batch_files(modpack_dir, batch_changes):
    """
    일괄 치환 시 모드팩 파일들을 단 1회 순회하여 모든 치환 대상(batch_changes)을
    동시에 일괄 치환(In-place)하고 카테고리별 수정 파일 개수를 반환합니다.
    """
    if not modpack_dir or not os.path.isdir(modpack_dir) or not batch_changes:
        return {"modpack_name": "", "total": 0, "quests": 0, "lang_resources": 0, "books": 0}

    import glob
    mp_name = os.path.basename(modpack_dir.rstrip(os.sep))
    results = {"modpack_name": mp_name, "total": 0, "quests": 0, "lang_resources": 0, "books": 0}

    pairs = []
    for ch in batch_changes:
        o = ch.get("old_tgt", "")
        n = ch.get("new_tgt", "")
        if o and n is not None and o != n:
            pairs.append((o, n))

    if not pairs:
        return results

    # 1. 퀘스트 파일 탐색
    quest_patterns = [
        os.path.join(modpack_dir, "config", "ftbquests", "quests", "**", "*.snbt"),
        os.path.join(modpack_dir, "defaultconfigs", "ftbquests", "quests", "**", "*.snbt"),
        os.path.join(modpack_dir, "config", "betterquesting", "**", "*.json"),
        os.path.join(modpack_dir, "config", "hqm", "**", "*.json"),
    ]
    quest_files = set()
    for p in quest_patterns:
        quest_files.update(glob.glob(p, recursive=True))

    # 2. 리소스팩 및 언어 파일 탐색
    lang_patterns = [
        os.path.join(modpack_dir, "kubejs", "assets", "**", "lang", "*.json"),
        os.path.join(modpack_dir, "resourcepacks", "**", "lang", "*.json"),
        os.path.join(modpack_dir, "resourcepacks", "**", "lang", "*.lang"),
    ]
    lang_files = set()
    for p in lang_patterns:
        lang_files.update(glob.glob(p, recursive=True))

    # 3. 가이드북 파일 탐색
    book_patterns = [
        os.path.join(modpack_dir, "patchouli_books", "**", "*.json"),
        os.path.join(modpack_dir, "config", "patchouli_books", "**", "*.json"),
    ]
    book_files = set()
    for p in book_patterns:
        book_files.update(glob.glob(p, recursive=True))

    def _patch_file_multi(filepath):
        try:
            if not os.path.isfile(filepath):
                return False
            if filepath.lower().endswith(('.zip', '.jar', '.png', '.ogg', '.bak', '.bak.gz')):
                return False
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

            modified = False
            for old_val, new_val in pairs:
                if old_val in content:
                    content = content.replace(old_val, new_val)
                    modified = True

            if modified:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
                return True
        except Exception as e:
            logging.debug(f"일괄 핫패치 오류 ({filepath}): {e}")
        return False

    for qf in quest_files:
        if _patch_file_multi(qf):
            results["quests"] += 1
            results["total"] += 1

    for lf in lang_files:
        if _patch_file_multi(lf):
            results["lang_resources"] += 1
            results["total"] += 1

    for bf in book_files:
        if _patch_file_multi(bf):
            results["books"] += 1
            results["total"] += 1

    # ZIP 리소스팩 핫패치
    try:
        import mod_jar_extractor
        escaped_mp_dir = glob.escape(modpack_dir)
        escaped_mp_name = glob.escape(mp_name)
        zip_glob_name = f"{escaped_mp_name}[[]*[]].zip"
        candidates = glob.glob(os.path.join(escaped_mp_dir, zip_glob_name))
        candidates.extend(glob.glob(os.path.join(escaped_mp_dir, "resourcepacks", zip_glob_name)))
        legacy = os.path.join(modpack_dir, "QuestTranslatorPro_Pack.zip")
        if os.path.exists(legacy):
            candidates.append(legacy)

        for pz in set(candidates):
            zip_changed = False
            for old_val, new_val in pairs:
                try:
                    if mod_jar_extractor.hotpatch_resource_pack(pz, old_val, new_val):
                        zip_changed = True
                except Exception:
                    pass
            if zip_changed:
                results["lang_resources"] += 1
                results["total"] += 1
    except Exception as e:
        logging.debug(f"ZIP 리소스팩 일괄 패치 오류: {e}")

    return results


# ====================================================================
# 오역률 및 음운 분석 엔진 (Two-Pillar Validation + WordNet)
# ====================================================================

_CHOSUNG_LIST = ['ㄱ', 'ㄲ', 'ㄴ', 'ㄷ', 'ㄸ', 'ㄹ', 'ㅁ', 'ㅂ', 'ㅃ', 'ㅅ', 'ㅆ', 'ㅇ', 'ㅈ', 'ㅉ', 'ㅊ', 'ㅋ', 'ㅌ', 'ㅍ', 'ㅎ']
_JUNGSUNG_LIST = ['ㅏ', 'ㅐ', 'ㅑ', 'ㅒ', 'ㅓ', 'ㅔ', 'ㅕ', 'ㅖ', 'ㅗ', 'ㅘ', 'ㅙ', 'ㅚ', 'ㅛ', 'ㅜ', 'ㅝ', 'ㅞ', 'ㅟ', 'ㅠ', 'ㅡ', 'ㅢ', 'ㅣ']
_JONGSUNG_LIST = ['', 'ㄱ', 'ㄲ', 'ㄳ', 'ㄴ', 'ㄵ', 'ㄶ', 'ㄷ', 'ㄹ', 'ㄺ', 'ㄻ', 'ㄼ', 'ㄽ', 'ㄾ', 'ㄿ', 'ㅀ', 'ㅁ', 'ㅂ', 'ㅄ', 'ㅅ', 'ㅆ', 'ㅇ', 'ㅈ', 'ㅊ', 'ㅋ', 'ㅌ', 'ㅍ', 'ㅎ']


_KOREAN_JOSA_SUFFIXES = (
    "에서는", "에서도", "으로는", "으로도", "에게는", "에게서",
    "에서", "으로", "에게", "한테", "까지", "부터", "처럼", "같이", "보다",
    "은", "는", "이", "가", "을", "를", "의", "에", "로", "와", "과", "도", "만",
)


_HOMONYM_WARNING_MAP = {
    "log": "나무(통나무/원목) ↔ 기록/일지(로그)",
    "lead": "금속(납) ↔ 끈/이끌다(리드)",
    "chest": "보관함(상자) ↔ 갑옷(흉갑/가슴)",
    "bow": "무기(활) ↔ 인사/뱃머리",
    "bat": "동물(박쥐) ↔ 무기(방망이/배트)",
    "mine": "채굴/광산 ↔ 폭발물(지뢰) ↔ 나의 것",
    "sign": "블록(표지판) ↔ 기호/서명",
    "spring": "부품(스프링/용수철) ↔ 계절(봄) ↔ 샘물",
    "bar": "주괴/막대(바) ↔ 상태바(게이지)",
    "block": "건축 블록 ↔ 막다(차단/방어)",
    "stick": "아이템(막대기) ↔ 붙다(고정)",
    "charge": "전력(충전) ↔ 돌격/비용",
    "cell": "저장소(셀/전지) ↔ 감방/세포",
    "key": "열쇠(키) ↔ 단축키/핵심",
    "watch": "시계 ↔ 지켜보다",
    "match": "성냥 ↔ 일치/시합",
    "staff": "무기(지팡이) ↔ 직원",
    "tank": "액체 탱크(저장조) ↔ 전차(탱크)",
    "well": "우물 ↔ 잘(부사)",
    "iron": "금속(철) ↔ 다리미",
    "로그": "기록/일지(log) ↔ 원목/통나무 오역(log)",
    "통나무": "원목/통나무(log) ↔ 시스템 로그(log)와 영단어 공유",
    "원목": "통나무/원목(log) ↔ 시스템 로그(log)와 영단어 공유",
    "리드": "납(lead) ↔ 끈/이끌다(lead)",
    "납": "금속(lead) ↔ 리드(lead)와 영단어 공유",
    "상자": "보관함(chest) ↔ 흉갑(chestplate) 문맥 주의",
}


def check_homonym_warning(term: str) -> str:
    """검색어나 치환어가 동음의이어/다의어로 감지되면 경고 메시지를 반환하고, 아니면 빈 문자열을 반환합니다."""
    if not term:
        return ""
    clean = str(term).strip()
    low = clean.lower()

    if low in _HOMONYM_WARNING_MAP:
        return (
            f"⚠️ [동음의이어 주의] '{clean}'은(는) 문맥에 따라 여러 뜻({_HOMONYM_WARNING_MAP[low]})으로 쓰입니다. "
            f"데이터 오염을 막으려면 '분류: items' 필터를 쓰거나 치환 전 목록을 꼭 확인하세요."
        )

    # 영단어인 경우 단어 사전/캐시에 서로 다른 한글 뜻 후보가 2개 이상 등록돼 있어도 동음의이어 주의 안내
    if re.search(r'[A-Za-z]', clean) and not has_hangul(clean) and " " not in clean:
        cands = _get_known_korean_candidates(clean)
        if len(cands) >= 2:
            preview = ", ".join(cands[:3])
            return (
                f"⚠️ [다의어 감지] '{clean}'은(는) 기존 데이터에서 여러 뜻({preview})으로 매칭됩니다. "
                f"치환 시 다른 문맥이 섞이지 않는지 확인하세요."
            )
    return ""


def _has_batchim(word: str) -> bool:
    """마지막 한글 글자의 받침(종성) 유무를 반환합니다."""
    if not word:
        return False
    ch = word[-1]
    code = ord(ch)
    if 0xAC00 <= code <= 0xD7A3:
        return ((code - 0xAC00) % 28) != 0
    return False


def _adjust_korean_josa(new_stem: str, josa_and_trail: str) -> str:
    """새 어간의 받침 유무에 따라 뒤따르는 한국어 조사(은/는, 이/가, 을/를, 와/과)를 자연스럽게 보정합니다."""
    if not new_stem or not josa_and_trail:
        return josa_and_trail
    pairs = [
        ("은", "는"), ("이", "가"), ("을", "를"), ("과", "와"),
    ]
    has_bat = _has_batchim(new_stem)
    for with_bat, no_bat in pairs:
        if josa_and_trail.startswith(with_bat) or josa_and_trail.startswith(no_bat):
            chosen = with_bat if has_bat else no_bat
            return chosen + josa_and_trail[1:]
    return josa_and_trail


def _split_token_affixes(token: str):
    """어절에서 앞/뒤 문장부호와 한국어 조사를 분리하여 (접두부호, 어간, 조사+접미부호)를 반환합니다."""
    m = re.match(r'^([^\w가-힣]*)(.*?)([^\w가-힣]*)$', token)
    if not m:
        return "", token, ""
    lead, core, trail = m.group(1), m.group(2), m.group(3)
    josa = ""
    if core and has_hangul(core):
        for j in _KOREAN_JOSA_SUFFIXES:
            if len(core) > len(j) and core.endswith(j):
                core = core[:-len(j)]
                josa = j
                break
    return lead, core, josa + trail


_candidates_memo_cache = {}  # en_low -> (timestamp, list_of_candidates)


def _get_known_korean_candidates(en_term: str) -> list:
    """영단어(en_term)에 대응하는 기존 한국어 번역 후보들(예: log -> ['통나무', '로그'])을
    사전(_CORE_MINECRAFT_DICT, translation_word, DEFAULT_GLOSSARY, translation_memory)에서 수집합니다.
    (대량 일괄 치환 시 반복 전체 스캔 병목을 방지하기 위해 3초 TTL 메모 캐시 적용)"""
    en_low = en_term.lower().strip()
    now_ts = time.monotonic()
    cached_entry = _candidates_memo_cache.get(en_low)
    if cached_entry is not None and (now_ts - cached_entry[0]) < 3.0:
        return cached_entry[1]
    candidates = []
    seen = set()

    def _add(val):
        if not val:
            return
        v = str(val).split("#")[0].strip()
        if v and has_hangul(v) and v not in seen:
            seen.add(v)
            candidates.append(v)

    # 1. 내장 마인크래프트 핵심 단어 사전
    for ko_item in _CORE_MINECRAFT_DICT.get(en_low, []):
        _add(ko_item)

    # 2. 단어 학습 사전 (translation_word._proper_nouns / _common_words / _candidate_votes)
    if translation_word is not None:
        try:
            with translation_word._lock:
                _add(translation_word._proper_nouns.get(en_low))
                _add(translation_word._common_words.get(en_low))
                for (w_en, w_ko), _cnt in translation_word._candidate_votes.items():
                    if w_en == en_low:
                        _add(w_ko)
        except Exception:
            pass

    # 3. 기본 용어집 (DEFAULT_GLOSSARY)
    try:
        from constants import DEFAULT_GLOSSARY
        for g_k, g_v in DEFAULT_GLOSSARY.items():
            if g_k.lower() == en_low:
                _add(g_v)
    except Exception:
        pass

    # 4. 번역 메모리 캐시 직접 일치 (단독 단어 항목)
    try:
        import translation_memory
        for cache_obj in (translation_memory._items_cache, translation_memory._global_cache):
            for entries in cache_obj.values():
                for key_variant in (en_term, en_low, en_low.capitalize(), en_low.title()):
                    if key_variant in entries:
                        _add(entries[key_variant])
    except Exception:
        pass

    # 긴 후보부터 먼저 매칭되도록 길이 내림차순 정렬
    candidates.sort(key=len, reverse=True)
    _candidates_memo_cache[en_low] = (now_ts, candidates)
    return candidates


def replace_term_in_translation(src_text: str, tgt_text: str, find_term: str, repl_term: str, is_deletion: bool = False) -> str:
    """
    일괄 치환용 스마트 단어 치환기:
    1) 번역문(tgt_text)에 find_term이 직접 존재하면(한글 오역 단어 또는 미번역 영단어) 대소문자 무시로 즉시 치환합니다.
    2) find_term이 영단어일 때는 어순이 다른 긴 문장에서도 오동작하지 않도록 아래 순서로 정밀 매칭합니다:
       - 1순위(단어 사전 매칭): log -> ['통나무', '로그'] 등 영한 단어 매핑 후보를 찾아 어순과 무관하게 정확히 치환
       - 2순위(음운 유사도 매칭): Mekanism -> '매카니즘/메카니슴' 등 외래어 표기 편차 어절을 조사 유지하며 치환
       - 3순위(짧은 명사구 위치 매칭): 3어절 이하 짧은 아이템/블록명(어절 수 일치)에 한해 위치 기반 치환
    """
    if not tgt_text or not find_term:
        return tgt_text

    pattern = re.compile(re.escape(find_term), re.IGNORECASE)
    if pattern.search(tgt_text):
        res = pattern.sub(lambda _m: "" if is_deletion else repl_term, tgt_text)
        return re.sub(r'\s+', ' ', res).strip() if is_deletion else res

    # 영단어 -> 한국어 정밀 매칭 치환
    if re.search(r'[A-Za-z]', find_term) and not has_hangul(find_term) and src_text:
        word_pat = re.compile(r'\b' + re.escape(find_term) + r'\b', re.IGNORECASE)
        if not word_pat.search(src_text):
            return tgt_text

        repl_clean = "" if is_deletion else repl_term

        # 1순위: 단어 매핑 사전(log -> 통나무 등) 기반 매칭 (어순이 달라도 정확히 해당 단어만 치환)
        known_candidates = _get_known_korean_candidates(find_term)
        for cand_ko in known_candidates:
            if not is_deletion and cand_ko == repl_clean:
                continue
            if cand_ko in tgt_text:
                if is_deletion:
                    res = tgt_text.replace(cand_ko, "")
                else:
                    def _sub_with_josa(m):
                        tail = m.group(1) or ""
                        return repl_clean + (_adjust_korean_josa(repl_clean, tail) if tail else "")
                    # 조사([은는이가을를과와])는 뒤에 공백/문장끝/비한글 기호가 올 때만 보정하여 복합어('통나무가공기') 훼손 방지
                    res = re.sub(re.escape(cand_ko) + r'(?:([은는이가을를과와])(?=\s|$|[^\w가-힣]))?', _sub_with_josa, tgt_text)
                return re.sub(r'\s+', ' ', res).strip() if is_deletion else res

        src_tokens = src_text.split()
        tgt_tokens = tgt_text.split()
        if not src_tokens or not tgt_tokens:
            return tgt_text

        # 2순위: 음운 유사도 매칭 (Mekanism -> 매카니즘/메카니슴 등 외래어 표기 편차 통일)
        updated_tokens = list(tgt_tokens)
        if repl_clean and has_hangul(repl_clean):
            best_idx = -1
            best_score = 0.0
            for t_i, t_tok in enumerate(updated_tokens):
                _, core, _ = _split_token_affixes(t_tok)
                if core and has_hangul(core):
                    _, _, score = _compare_syllables_phonetic(repl_clean, core)
                    if score > best_score:
                        best_score = score
                        best_idx = t_i
            if best_idx >= 0 and best_score >= 0.40:
                lead, _, suffix = _split_token_affixes(updated_tokens[best_idx])
                updated_tokens[best_idx] = "" if is_deletion else f"{lead}{repl_clean}{_adjust_korean_josa(repl_clean, suffix)}"
                joined = " ".join(tok for tok in updated_tokens if tok != "")
                return re.sub(r'\s+', ' ', joined).strip()

        # 3순위: 3어절 이하 짧은 아이템/블록명(어절 수 일치, 문장 부호 없음)에 한해 안전하게 위치 매칭
        if len(src_tokens) <= 3 and len(src_tokens) == len(tgt_tokens) and not any(p in src_text for p in ('.', '!', '?', ',')):
            src_indices = [
                idx for idx, tok in enumerate(src_tokens)
                if re.sub(r'^[^\w]+|[^\w]+$', '', tok).lower() == find_term.lower()
            ]
            changed = False
            for s_i in src_indices:
                lead, _, suffix = _split_token_affixes(updated_tokens[s_i])
                updated_tokens[s_i] = "" if is_deletion else f"{lead}{repl_clean}{_adjust_korean_josa(repl_clean, suffix)}"
                changed = True
            if changed:
                joined = " ".join(tok for tok in updated_tokens if tok != "")
                return re.sub(r'\s+', ' ', joined).strip()

    return tgt_text


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
    'r': ['ㄹ', 'ㅇ'], 'l': ['ㄹ'], 's': ['ㅅ', 'ㅆ', 'ㅈ'], 'p': ['ㅍ', 'ㅃ', 'ㅂ'], 'b': ['ㅂ', 'ㅃ'],
    'm': ['ㅁ'], 'd': ['ㄷ', 'ㄸ'], 'g': ['ㄱ', 'ㄲ'], 'f': ['ㅍ'], 'v': ['ㅂ', 'ㅍ'],
    'z': ['ㅈ', 'ㅉ', 'ㅅ'], 'j': ['ㅈ', 'ㅉ'], 'h': ['ㅎ'], 'w': ['ㅇ', 'ㅂ'], 'y': ['ㅇ']
}

_ENG_KO_VOWELS = {
    'i': ['ㅣ', 'ㅔ', 'ㅏ', 'ㅐ'],
    'e': ['ㅔ', 'ㅓ', 'ㅐ', 'ㅣ'],
    'a': ['ㅏ', 'ㅐ', 'ㅔ', 'ㅓ', 'ㅗ'],
    'o': ['ㅗ', 'ㅓ', 'ㅏ', 'ㅜ'],
    'u': ['ㅓ', 'ㅜ', 'ㅠ', 'ㅡ'],
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
    'log': ['통나무', '원목', '로그'],
    'plank': ['판자', '목재'],
    'planks': ['판자', '목재'],
    'mekanism': ['메카니즘'],
    'farmer\'s delight': ['파머스 딜라이트'],
}


def calculate_translation_error_rate(src_word, tgt_word, reference_hint=None, target_lang="한국어 (Korean)"):
    """
    원문(영어)과 번역문(한글) 간의 연관성 및 오역률(Error Rate, 0~100%)을 정밀 계산합니다.
    - WordNet 및 Lemmatizer(원형 복원) 연동
    - 일반 명사의 의미역/음차역(예: iron -> 철 / 아이언) 양방향 유효성 인정
    - 캐시 DB 대조 유지
    - 영-한 직접 발음 분석(자음/모음 음운 일치)

    Args:
        src_word (str): 영어 원문 단어
        tgt_word (str): 사용자가 입력/수정한 한글 번역문
        reference_hint (str, optional): 기존 번역문 또는 찾을 단어 힌트
        target_lang (str): 번역 대상 언어

    Returns:
        tuple: (error_rate_pct, validity_pct, verdict_type, details_str)
               - verdict_type: 'pass' (오역률 <= 35%), 'warning' (35% < 오역률 < 70%),
                               'block' (오역률 >= 70%), 'hard_block' (기등록 단어 명백한 왜곡)
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

    src_lower = src_clean.lower()

    # ------------------------------------------------------------------
    # 0. WordNet Lemmatization 및 표준 어휘 원형 복원
    # ------------------------------------------------------------------
    lemmas = [src_lower]
    # NLTK WordNet 형태소 분석기 연동
    if translation_word is not None:
        try:
            if not getattr(translation_word, "_loaded", False):
                translation_word.load_words()

            lemmatizer = getattr(translation_word, "_lemmatizer", None)
            if lemmatizer is not None and getattr(translation_word, "_has_wordnet", False):
                for pos in ("n", "v", "a"):
                    try:
                        base = lemmatizer.lemmatize(src_lower, pos=pos)
                        if base and base != src_lower and base not in lemmas:
                            lemmas.append(base)
                    except Exception:
                        pass
        except Exception as e:
            logging.debug(f"[WordNet] Lemmatizer 참조 중 예외 (무시): {e}")

    # 규칙 기반 영어 원형(lemma) 폴백 (NLTK 데이터 부재 시에도 복수형/변형 완벽 지원)
    if src_lower.endswith("ies") and len(src_lower) > 4:
        lemmas.append(src_lower[:-3] + "y")
    elif src_lower.endswith("es") and len(src_lower) > 3:
        lemmas.append(src_lower[:-2])
        lemmas.append(src_lower[:-1])
    elif src_lower.endswith("s") and not src_lower.endswith("ss") and len(src_lower) > 3:
        lemmas.append(src_lower[:-1])
    if src_lower.endswith("ed") and len(src_lower) > 4:
        lemmas.append(src_lower[:-2])
        lemmas.append(src_lower[:-1])
    if src_lower.endswith("ing") and len(src_lower) > 5:
        lemmas.append(src_lower[:-3])


    # ------------------------------------------------------------------
    # 1. 마인크래프트 기본 핵심 어휘 사전 및 WordNet/일반 어휘 사전 대조
    # ------------------------------------------------------------------
    core_accepted_candidates = []
    for k in lemmas:
        if k in _CORE_MINECRAFT_DICT:
            accepted_list = _CORE_MINECRAFT_DICT[k]
            core_accepted_candidates.extend(accepted_list)
            for accepted in accepted_list:
                if accepted == tgt_clean:
                    return 0.0, 100.0, "pass", f"핵심 사전 공식 번역 일치 ('{tgt_clean}')"
                if accepted in tgt_clean or tgt_clean in accepted:
                    return 0.0, 100.0, "pass", f"핵심 사전 복합어/파생어 일치 ('{accepted}')"

    # translation_word의 일반 어휘 사전(_common_words) 및 고유명사 사전(_proper_nouns) 대조
    if translation_word is not None:
        common_words = getattr(translation_word, "_common_words", {})
        proper_nouns = getattr(translation_word, "_proper_nouns", {})
        for k in lemmas:
            if k in common_words:
                c_val = common_words[k]
                if c_val == tgt_clean:
                    return 0.0, 100.0, "pass", f"WordNet 일반 어휘 사전 일치 ('{tgt_clean}')"
                if c_val in tgt_clean or tgt_clean in c_val:
                    return 5.0, 95.0, "pass", f"WordNet 일반 어휘 파생어 일치 ('{c_val}')"
            if k in proper_nouns:
                p_val = proper_nouns[k]
                if p_val == tgt_clean:
                    return 0.0, 100.0, "pass", f"고유명사 사전 일치 ('{tgt_clean}')"
                if p_val in tgt_clean or tgt_clean in p_val:
                    return 5.0, 95.0, "pass", f"고유명사 파생어 일치 ('{p_val}')"

    # ------------------------------------------------------------------
    # 2. 캐시 DB 의미 사전 (Semantic Memory) 대조 유지
    # ------------------------------------------------------------------
    target_lang_norm = target_lang.replace(" ", "")
    cached_candidates = []
    try:
        import translation_memory
        with translation_memory._lock:
            for cache in (translation_memory._items_cache, translation_memory._global_cache, translation_memory._books_cache):
                for lang_key, c_dict in cache.items():
                    if lang_key.replace(" ", "") == target_lang_norm:
                        if src_clean in c_dict:
                            cached_tgt = c_dict[src_clean]
                            if cached_tgt == tgt_clean:
                                return 0.0, 100.0, "pass", f"캐시 DB 사전 일치 ('{cached_tgt}')"
                            if tgt_clean in cached_tgt or cached_tgt in tgt_clean:
                                return 10.0, 90.0, "pass", f"캐시 DB 파생어 일치 ('{cached_tgt}')"
                            cached_candidates.append(cached_tgt)
    except Exception:
        pass

    # ------------------------------------------------------------------
    # 3. reference_hint, 핵심 사전 또는 캐시 내 기존 번역과 음절 단위 교차 검증
    # ------------------------------------------------------------------
    best_cache_validity = 0.0
    best_cache_c = 0.0
    best_cache_v = 0.0

    targets_to_compare = []
    targets_to_compare.extend(core_accepted_candidates)
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

    # ------------------------------------------------------------------
    # 4. 직접 영-한 발음 분석 (일반 명사의 음차 번역 및 신규 음차 완벽 지원)
    # ------------------------------------------------------------------
    cand_syllables = _decompose_korean(tgt_clean)
    letters = re.findall(r'[a-z]', src_lower)
    eng_cons = [c for c in letters if c not in ('a', 'e', 'i', 'o', 'u')]
    eng_vowels = [c for c in letters if c in ('a', 'e', 'i', 'o', 'u')]

    ko_cons = []
    ko_vowels = []
    for s in cand_syllables:
        # 한글 초성 'ㅇ'은 모음으로 시작하는 음절의 무음(null onset)이므로 자음 매칭 카운트에서 과도한 페널티 방지
        if s[0] != 'ㅇ':
            ko_cons.append(s[0])
        elif not ko_cons and not eng_cons:
            # 원문에도 자음이 전혀 없고 초성 'ㅇ'만 있는 순수 모음 단어일 때
            ko_cons.append('ㅇ')

        if s[2]:  # 받침(종성)
            ko_cons.append(s[2])
        if s[1] != 'ㅡ':  # ㅡ는 보조 모음(외래어 음차 받침용)이므로 영문 모음 매칭 대상에서 제외
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
            c_matches += 0.8  # 영어 묵음 r 발음 보정

    effective_cons = max(1, len([c for c in eng_cons if c != 'r']))
    direct_c = c_matches / max(effective_cons, max(1, len(ko_cons)))

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

    # 기존 캐시 대조와 직접 영-한 발음 분석 중 더 높은 점수 채택
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

    has_established = bool(core_accepted_candidates or cached_candidates)

    if error_rate_pct <= 35.0:
        return error_rate_pct, validity_pct, "pass", f"자음/모음 음운 정상 일치 (자음 {final_c*100:.0f}%, 모음 {final_v*100:.0f}%)"
    elif error_rate_pct < 70.0:
        return error_rate_pct, validity_pct, "warning", f"발음 유사도 다소 낮음 (자음 {final_c*100:.0f}%, 모음 {final_v*100:.0f}%)"
    else:
        if has_established:
            return error_rate_pct, validity_pct, "hard_block", f"기존 데이터/표준어 왜곡 분탕 감지 (자음 {final_c*100:.0f}%, 모음 {final_v*100:.0f}%)"
        else:
            return error_rate_pct, validity_pct, "block", "원문과 연관성 없는 왜곡 감지 (신규 미등록 단어)"


# ====================================================================
# 오역 수정 감사 로그 발송 (Cloudflare Worker 위임 & 비밀변수 보안)
# ====================================================================

def dispatch_audit_report(category, src, old_tgt, new_tgt, error_rate=0.0, validity=100.0, verdict="pass", details=""):
    """
    단어 수정/치환 발생 시 Cloudflare Worker(POST /audit/report)로 비동기 감사 로그를 발송합니다.
    - [테스트 모드]: 원본 캐시/클라우드 파일을 보호하고, dist/temp/test_memory_edit.json 에만 임시 기록
    - [롤백 감지]: 디스코드에서 관리자가 [⏪ 롤백] 버튼을 누르면 dist/temp/test_memory_edit.json을 이전 값(old_tgt)으로 즉시 자동 원복!
    - [보안]: 웹훅 URL은 클라이언트에 노출하지 않고 Cloudflare Worker의 비밀변수로만 안전하게 통신
    """
    import threading
    import urllib.request
    import json
    import time
    import datetime

    is_test = False
    try:
        import temp_session_logger
        is_test = temp_session_logger.is_debug_mode()
    except Exception:
        pass

    # 테스트 모드일 때 dist/temp/test_memory_edit.json에 임시 파일 생성
    temp_edit_path = None
    if is_test:
        try:
            import temp_session_logger
            temp_dir = temp_session_logger.get_temp_dir()
            temp_edit_path = os.path.join(temp_dir, "test_memory_edit.json")
            edit_record = {
                "category": category,
                "src": src,
                "old_tgt": old_tgt,
                "new_tgt": new_tgt,
                "current_tgt": new_tgt,
                "status": "modified",
                "error_rate": float(error_rate),
                "verdict": str(verdict),
                "modified_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            with open(temp_edit_path, "w", encoding="utf-8") as f:
                json.dump(edit_record, f, ensure_ascii=False, indent=2)
            logging.info(f"🧪 [테스트 모드] 임시 파일에 단어 수정 기록: {temp_edit_path} ('{old_tgt}' ➡️ '{new_tgt}')")
        except Exception as e:
            logging.debug(f"[Test Edit Temp Log Error]: {e}")

    def _send_and_listen():
        audit_id = None
        try:
            import requests
            import translation_memory
            worker_url = translation_memory.get_worker_url()
            if not worker_url:
                return

            endpoint = f"{worker_url}/audit/report"
            payload = {
                "category": str(category)[:100],
                "src": str(src)[:900],
                "old_tgt": str(old_tgt)[:900],
                "new_tgt": str(new_tgt)[:900],
                "error_rate": float(error_rate),
                "validity": float(validity),
                "verdict": str(verdict),
                "details": str(details)[:950],
                "is_test": is_test
            }
            resp = requests.post(
                endpoint,
                json=payload,
                headers={"User-Agent": "QuestTranslator-Audit/1.0"},
                timeout=12
            )
            resp.raise_for_status()
            resp_data = resp.json()
            audit_id = resp_data.get("audit_id")
            if resp_data.get("discord_ok") is False:
                logging.warning(f"⚠️ [감사 로그] Worker 수신 완료(ID: {audit_id})했으나 디스코드 전달 실패: {resp_data.get('discord_error', '')}")
            else:
                logging.info(f"📤 [감사 로그] 디스코드 감사 로그 전송 완료 (ID: {audit_id})")
        except Exception as e:
            logging.warning(f"⚠️ [감사 로그 발송 실패]: {e}")
            return

        if not audit_id:
            return

        # 디스코드에서 관리자가 [⏪ 롤백] 버튼을 누르는지 백그라운드 리스닝 (최대 3분간 폴링)
        poll_count = 0
        while poll_count < 60:
            time.sleep(3)
            poll_count += 1
            try:
                import requests
                status_url = f"{worker_url}/audit/status?audit_id={audit_id}"
                st_resp = requests.get(status_url, headers={"User-Agent": "QuestTranslator-Audit/1.0"}, timeout=5)
                if st_resp.status_code == 200:
                    st_data = st_resp.json()
                    if st_data.get("rolled_back"):
                        admin_user = st_data.get("rolled_back_by", "관리자")
                        # 메모리 캐시도 이전 번역으로 원복 동기화 및 클라우드 커밋 큐 반영
                        import translation_memory
                        with translation_memory._lock:
                            cache_dict = translation_memory._items_cache if category == "items" else (
                                translation_memory._books_cache if category == "books" else translation_memory._global_cache
                            )
                            translation_memory._store(cache_dict, src, old_tgt, "한국어(Korean)", force_override=True)
                        translation_memory.save_memory(flush_now=True)

                        if is_test and temp_edit_path and os.path.exists(temp_edit_path):
                            # 테스트 모드: temp 파일의 번역을 즉시 old_tgt로 원복!
                            with open(temp_edit_path, "r", encoding="utf-8") as f:
                                rec = json.load(f)
                            rec["current_tgt"] = old_tgt
                            rec["status"] = "rolled_back"
                            rec["rolled_back_by"] = admin_user
                            rec["rolled_back_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            with open(temp_edit_path, "w", encoding="utf-8") as f:
                                json.dump(rec, f, ensure_ascii=False, indent=2)
                            logging.info(f"⏪ [테스트 롤백 감지!] 디스코드 버튼 클릭 확인됨! {temp_edit_path} 파일이 '{new_tgt}' ➡️ '{old_tgt}'(으)로 복구되었습니다! (롤백자: {admin_user})")
                        else:
                            logging.info(f"⏪ [실시간 롤백 완료] 관리자({admin_user})에 의해 '{src}' 번역이 '{old_tgt}'(으)로 롤백되었습니다.")
                        break
            except Exception:
                pass

    threading.Thread(target=_send_and_listen, daemon=True).start()


def dispatch_batch_audit_report(batch_changes, error_rate=0.0, validity=0.0, verdict="pass", details="", find_term="", repl_term=""):
    """
    일괄 치환 시 수십~수천 건의 변경 사항을 단 1개의 디스코드 감사 로그로 묶어 발송하며,
    전체 상세 수정 내역은 피드백 로그와 동일하게 .txt 첨부 파일로 함께 전송합니다.
    """
    if not batch_changes:
        return

    import threading
    import urllib.request
    import json
    import time
    import datetime

    is_test = False
    try:
        import temp_session_logger
        is_test = temp_session_logger.is_debug_mode()
    except Exception:
        pass

    temp_edit_path = None
    if is_test:
        try:
            import temp_session_logger
            temp_dir = temp_session_logger.get_temp_dir()
            temp_edit_path = os.path.join(temp_dir, "test_memory_edit.json")
            edit_record = {
                "type": "batch_replace",
                "count": len(batch_changes),
                "items": batch_changes[:50],
                "status": "modified",
                "details": details,
                "modified_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            with open(temp_edit_path, "w", encoding="utf-8") as f:
                json.dump(edit_record, f, ensure_ascii=False, indent=2)
            logging.info(f"🧪 [테스트 모드] 임시 파일에 일괄 치환 {len(batch_changes)}건 기록: {temp_edit_path}")
        except Exception as e:
            logging.debug(f"[Test Batch Edit Temp Log Error]: {e}")

    def _send_and_listen():
        audit_id = None
        try:
            import requests
            import translation_memory
            worker_url = translation_memory.get_worker_url()
            if not worker_url:
                return

            preview_lines = []
            for ch in batch_changes[:3]:
                s_short = str(ch['src']).replace('\n', ' ')[:60]
                o_short = str(ch['old_tgt']).replace('\n', ' ')[:40]
                n_short = str(ch['new_tgt']).replace('\n', ' ')[:40]
                preview_lines.append(f"• {s_short}: '{o_short}' ➡️ '{n_short}'")
            if len(batch_changes) > 3:
                preview_lines.append(f"...외 {len(batch_changes) - 3:,}건 (전체 상세 내역은 첨부 .txt 파일 참조)")

            combined_details = (f"{details}\n" + "\n".join(preview_lines))[:950]

            # 전체 상세 수정 내역 .txt 로그 생성 (디스코드 첨부파일 전송용)
            log_header = [
                "============================================================",
                f"QuestTranslator Pro 일괄 치환 상세 로그 ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')})",
                f"요약: {details}",
                f"찾을 단어: {find_term or '(없음)'}  ->  바꿀 단어: {repl_term or '(없음)'}",
                f"총 변경 건수: {len(batch_changes):,}건",
                "============================================================",
                ""
            ]
            detail_rows = []
            for idx_ch, ch in enumerate(batch_changes, 1):
                cat_s = str(ch.get("category", "general"))
                src_s = str(ch.get("src", "")).replace("\r", " ").replace("\n", "\\n")
                old_s = str(ch.get("old_tgt", "")).replace("\r", " ").replace("\n", "\\n")
                new_s = str(ch.get("new_tgt", "")).replace("\r", " ").replace("\n", "\\n")
                detail_rows.append(f"{idx_ch}. [{cat_s}] {src_s}\n   OLD: {old_s}\n   NEW: {new_s}")
            full_log_text = "\n".join(log_header + detail_rows)
            # Worker 페이로드 안전 상한 (약 900KB) 초과 시 안전 절삭
            if len(full_log_text.encode("utf-8")) > 900_000:
                full_log_text = full_log_text[:300_000] + f"\n\n... (용량 제한으로 일부 생략, 총 {len(batch_changes):,}건)"

            clean_find = str(find_term or "").strip()
            clean_repl = str(repl_term or "").strip()
            src_display = clean_find if clean_find else f"일괄 치환 {len(batch_changes)}개 항목"
            old_display = clean_find if clean_find else "일괄 치환"
            new_display = clean_repl if clean_repl else f"총 {len(batch_changes)}개 항목 변경"

            endpoint = f"{worker_url}/audit/report"
            payload = {
                "category": f"일괄 치환 ({len(batch_changes)}건)",
                "src": src_display,
                "old_tgt": old_display,
                "new_tgt": new_display,
                "error_rate": float(error_rate),
                "validity": float(validity),
                "verdict": str(verdict),
                "details": combined_details,
                "log_text": full_log_text,
                "batch_items": [
                    {
                        "cat": str(ch.get("category", "general")),
                        "src": str(ch.get("src", "")),
                        "old_tgt": str(ch.get("old_tgt", "")),
                    }
                    for ch in batch_changes
                ],
                "is_test": is_test
            }
            resp = requests.post(
                endpoint,
                json=payload,
                headers={"User-Agent": "QuestTranslator-Audit/1.0"},
                timeout=12
            )
            resp.raise_for_status()
            resp_data = resp.json()
            audit_id = resp_data.get("audit_id")
            if resp_data.get("discord_ok") is False:
                logging.warning(f"⚠️ [일괄 감사 로그] Worker 수신 완료(ID: {audit_id})했으나 디스코드 전달 실패: {resp_data.get('discord_error', '')}")
            else:
                logging.info(f"📤 [일괄 감사 로그] 디스코드 일괄 감사 로그 전송 완료 (총 {len(batch_changes)}건 / ID: {audit_id})")
        except Exception as e:
            logging.warning(f"⚠️ [일괄 감사 로그 발송 실패]: {e}")
            return

        if not audit_id:
            return

        # 디스코드 원클릭 롤백 감지 리스너 (최대 3분간 폴링)
        poll_count = 0
        while poll_count < 60:
            time.sleep(3)
            poll_count += 1
            try:
                import requests
                status_url = f"{worker_url}/audit/status?audit_id={audit_id}"
                st_resp = requests.get(status_url, headers={"User-Agent": "QuestTranslator-Audit/1.0"}, timeout=5)
                if st_resp.status_code == 200:
                    st_data = st_resp.json()
                    if st_data.get("rolled_back"):
                        admin_user = st_data.get("rolled_back_by", "관리자")
                        import translation_memory
                        translation_memory.default_memory_store.bulk_update(
                            [(ch["category"], ch["src"], ch["old_tgt"]) for ch in batch_changes],
                            target_lang="한국어(Korean)",
                            force_override=True,
                        )
                        translation_memory.default_memory_store.save(flush_now=True)

                        if is_test and temp_edit_path and os.path.exists(temp_edit_path):
                            with open(temp_edit_path, "r", encoding="utf-8") as f:
                                rec = json.load(f)
                            rec["status"] = "rolled_back"
                            rec["rolled_back_by"] = admin_user
                            rec["rolled_back_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            with open(temp_edit_path, "w", encoding="utf-8") as f:
                                json.dump(rec, f, ensure_ascii=False, indent=2)
                            logging.info(f"⏪ [일괄 롤백 감지!] 관리자({admin_user})에 의해 일괄 치환 {len(batch_changes)}건 전체가 원래 번역으로 자동 복구되었습니다!")
                        else:
                            logging.info(f"⏪ [실시간 일괄 롤백 완료] 관리자({admin_user})에 의해 일괄 치환 {len(batch_changes)}건 전체가 복구되었습니다.")
                        break
            except Exception:
                pass

    threading.Thread(target=_send_and_listen, daemon=True).start()


def retranslate_texts(texts, engine_key, api_key, is_paid=False, ai_model=None, target_lang="한국어 (Korean)", custom_url=None, glossary=None, log_callback=None):
    """
    오역 수정기에서 선택된 텍스트 목록을 메인 화면에 설정된 엔진 및 모델로 일괄 재번역합니다.
    """
    import translation_engines
    from file_processors import sanitize_minecraft_formatting

    if not texts:
        return []

    # Mock mode
    if getattr(translation_engines, "MOCK_MODE", False) or engine_key == "mock":
        return [f"[AI 재번역] {t}" for t in texts]

    # 통합 TranslationEngine + EngineConfig 단일 진입점으로 재번역 호출
    cfg = translation_engines.EngineConfig(
        engine_key=engine_key,
        api_key=api_key or "",
        is_paid=is_paid,
        ai_model=ai_model,
        target_lang=target_lang,
        custom_url=custom_url,
    )
    raw_res = translation_engines.default_engine.translate_batch(
        texts, cfg, glossary=glossary, log_callback=log_callback
    )

    cleaned_res = []
    for orig, res in zip(texts, raw_res):
        if res is None:
            res = orig
        cleaned_res.append(sanitize_minecraft_formatting(orig, str(res).strip()))

    return cleaned_res
