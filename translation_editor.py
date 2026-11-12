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
    query_lower = query.lower()

    def _search_in_cache(cache, cat_name):
        for lang_key, entries in cache.items():
            if lang_key.replace(" ", "") != target_lang_norm:
                continue
            for src, tgt in entries.items():
                match = False
                src_l = src.lower()
                tgt_l = tgt.lower()

                if search_field in ("all", "src"):
                    if query_lower in src_l or ('§' in src or '&' in src) and query_lower in _MC_COLOR_RE.sub('', src_l):
                        match = True
                if not match and search_field in ("all", "tgt"):
                    if query_lower in tgt_l or ('§' in tgt or '&' in tgt) and query_lower in _MC_COLOR_RE.sub('', tgt_l):
                        match = True

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
        translation_memory._save_local_caches_only()
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
        candidates = glob.glob(os.path.join(modpack_dir, f"{mp_name}[*].zip"))
        candidates.extend(glob.glob(os.path.join(modpack_dir, "resourcepacks", f"{mp_name}[*].zip")))
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
        candidates = glob.glob(os.path.join(modpack_dir, f"{mp_name}[*].zip"))
        candidates.extend(glob.glob(os.path.join(modpack_dir, "resourcepacks", f"{mp_name}[*].zip")))
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
            import translation_memory
            worker_url = translation_memory.get_worker_url()
            if not worker_url:
                return

            endpoint = f"{worker_url}/audit/report"
            payload = {
                "category": category,
                "src": src,
                "old_tgt": old_tgt,
                "new_tgt": new_tgt,
                "error_rate": float(error_rate),
                "validity": float(validity),
                "verdict": str(verdict),
                "details": str(details),
                "is_test": is_test
            }
            data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                endpoint,
                data=data_bytes,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "User-Agent": "QuestTranslator-Audit/1.0"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                audit_id = resp_data.get("audit_id")
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
                status_url = f"{worker_url}/audit/status?audit_id={audit_id}"
                req_status = urllib.request.Request(status_url, headers={"User-Agent": "QuestTranslator-Audit/1.0"})
                with urllib.request.urlopen(req_status, timeout=5) as st_resp:
                    st_data = json.loads(st_resp.read().decode("utf-8"))
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


def dispatch_batch_audit_report(batch_changes, error_rate=0.0, validity=0.0, verdict="pass", details=""):
    """
    일괄 치환 시 수십~수백 건의 변경 사항을 단 1개의 디스코드 감사 로그로 묶어 발송합니다.
    디스코드 레이트리밋을 방지하고, 단 한 번의 롤백 클릭으로 전체 항목을 일괄 원복합니다.
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
            import translation_memory
            worker_url = translation_memory.get_worker_url()
            if not worker_url:
                return

            preview_lines = []
            for ch in batch_changes[:6]:
                preview_lines.append(f"• {ch['src']}: '{ch['old_tgt']}' ➡️ '{ch['new_tgt']}'")
            if len(batch_changes) > 6:
                preview_lines.append(f"...외 {len(batch_changes) - 6}개 항목")

            combined_details = f"{details}\n" + "\n".join(preview_lines)

            endpoint = f"{worker_url}/audit/report"
            payload = {
                "category": f"일괄 치환 ({len(batch_changes)}건)",
                "src": f"일괄 치환 {len(batch_changes)}개 항목",
                "old_tgt": "일괄 치환",
                "new_tgt": f"총 {len(batch_changes)}개 항목 변경",
                "error_rate": float(error_rate),
                "validity": float(validity),
                "verdict": str(verdict),
                "details": combined_details,
                "is_test": is_test
            }
            data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                endpoint,
                data=data_bytes,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "User-Agent": "QuestTranslator-Audit/1.0"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                audit_id = resp_data.get("audit_id")
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
                status_url = f"{worker_url}/audit/status?audit_id={audit_id}"
                req_status = urllib.request.Request(status_url, headers={"User-Agent": "QuestTranslator-Audit/1.0"})
                with urllib.request.urlopen(req_status, timeout=5) as st_resp:
                    st_data = json.loads(st_resp.read().decode("utf-8"))
                    if st_data.get("rolled_back"):
                        admin_user = st_data.get("rolled_back_by", "관리자")
                        import translation_memory
                        with translation_memory._lock:
                            for ch in batch_changes:
                                c_dict = (
                                    translation_memory._items_cache if ch["category"] == "items" else
                                    (translation_memory._books_cache if ch["category"] == "books" else translation_memory._global_cache)
                                )
                                translation_memory._store(c_dict, ch["src"], ch["old_tgt"], "한국어(Korean)")
                        translation_memory.save_memory(flush_now=True)

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

    # 엔진별 재번역 호출
    if engine_key == "local_ai":
        raw_res = translation_engines.translate_local_ai(
            texts, custom_url, ai_model, api_key=api_key,
            glossary=glossary, target_lang=target_lang, log_callback=log_callback
        )
    elif engine_key == "deepseek":
        raw_res = translation_engines.translate_local_ai(
            texts, "https://api.deepseek.com/v1", ai_model or "deepseek-flash", api_key=api_key,
            glossary=glossary, target_lang=target_lang, log_callback=log_callback
        )
    elif engine_key == "claude":
        raw_res = translation_engines.translate_local_ai(
            texts, "https://api.anthropic.com/v1", ai_model or "claude-haiku-5-5", api_key=api_key,
            glossary=glossary, target_lang=target_lang, log_callback=log_callback
        )
    elif engine_key == "deepl":
        raw_res = [translation_engines.translate_deepl(t, api_key, is_pro=is_paid, target_lang=target_lang) for t in texts]
    elif engine_key == "openai":
        raw_res = [translation_engines.translate_openai(t, api_key, glossary=glossary, ai_model=ai_model, target_lang=target_lang) for t in texts]
    elif engine_key == "google":
        raw_res = [translation_engines.translate_google(t, api_key, target_lang=target_lang) for t in texts]
    else:
        raw_res = translation_engines.translate_gemini_batch(
            texts, api_key, is_paid=is_paid, glossary=glossary,
            ai_model=ai_model, target_lang=target_lang, log_callback=log_callback
        )

    cleaned_res = []
    for orig, res in zip(texts, raw_res):
        if res is None:
            res = orig
        cleaned_res.append(sanitize_minecraft_formatting(orig, str(res).strip()))

    return cleaned_res
