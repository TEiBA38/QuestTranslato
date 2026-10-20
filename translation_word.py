import json
import logging
import os
import re
import sys
import threading
from collections import Counter, defaultdict

from constants import has_hangul

_lock = threading.Lock()
_loaded = False
_dirty = False

# In-memory dictionaries (lowercase English word -> Korean translation)
_proper_nouns = {}
_common_words = {}

# Candidate voting buffer for newly discovered AI translations (prevents 1-off hallucination poisoning)
# {(en_lower, ko_word): count}
_candidate_votes = Counter()
VOTE_THRESHOLD = 2

# Optional NLTK WordNet integration (graceful fallback if nltk_data is absent on end-user PC)
_has_wordnet = False
_lemmatizer = None
_wn = None

try:
    from nltk.corpus import wordnet as _wn_mod
    from nltk.stem import WordNetLemmatizer as _WNL
    # Trigger a quick lookup to verify corpus data is available
    _wn_mod.synsets("stone")
    _wn = _wn_mod
    _lemmatizer = _WNL()
    _has_wordnet = True
except Exception:
    _has_wordnet = False


# Prepositions, articles, conjunctions, and pronouns that disqualify simple 1:1 left-to-right noun assembly
_DISQUALIFYING_GRAMMAR_TOKENS = {
    "of", "in", "on", "at", "to", "for", "with", "from", "by", "about", "into",
    "through", "after", "over", "between", "out", "against", "during", "without",
    "before", "under", "around", "among", "the", "a", "an", "and", "or", "but",
    "if", "when", "while", "how", "why", "where", "what", "who", "which", "that",
    "this", "these", "those", "your", "you", "my", "our", "their", "his", "her",
    "its", "they", "them", "we", "us", "i", "me", "he", "him", "she",
    "than", "then", "else", "per", "so", "as", "via",
    "is", "are", "was", "were", "be", "been", "have", "has", "had", "do",
    "does", "did", "can", "could", "will", "would", "shall", "should", "may",
    "might", "must", "not", "no", "yes", "all", "any", "some", "many", "much",
}

# Imperative / sentence-starter verbs that indicate a quest instruction rather than an item noun phrase
_IMPERATIVE_STARTERS = {
    "click", "craft", "find", "use", "kill", "make", "collect", "gather", "place",
    "build", "open", "break", "get", "obtain", "reach", "visit", "defeat", "talk",
    "interact", "complete", "submit", "check", "go", "enter", "mine", "smelt",
    "cook", "brew", "combine", "mix", "fill", "empty", "charge", "power", "connect",
    "attach", "remove", "upgrade", "insert", "extract", "press", "hold", "shift",
    "right-click", "left-click", "sneak", "jump", "throw", "drop", "pick", "equip",
    "wear", "eat", "drink", "feed", "breed", "tame", "summon", "spawn", "travel",
    "explore", "locate", "search", "unlock", "activate", "enable", "disable",
    "turn", "set", "put", "take", "give", "bring", "buy", "sell", "trade", "read",
    "look", "watch", "listen", "wait", "sleep", "survive", "protect", "defend",
    "destroy", "clear", "clean", "repair", "fix", "heal", "cure", "plant", "harvest",
    "grow", "cut", "chop", "dig", "fish", "catch", "ride", "fly", "swim", "climb",
}

_PHONETIC_ENDINGS = (
    "라이트", "스톤", "사이트", "리움", "니움", "늄", "드", "트", "크", "프",
    "볼트", "셀", "박스", "체스트", "바", "플레이트", "블록", "코어", "큐브",
)

_SEMANTIC_ENDINGS = (
    "암", "갑", "문", "벽", "알", "통", "틀", "줄", "가루", "주괴", "광석", "판", "검", "도끼", "곡괭이", "삽", "괭이",
)


def _get_words_file_path():
    """dist/translation_memory_words.json 경로를 반환합니다."""
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        base = os.path.join(exe_dir, "dist")
    else:
        repo_root = os.path.dirname(os.path.abspath(__file__))
        base = os.path.join(repo_root, "dist")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "translation_memory_words.json")


def load_words(force=False):
    """dist/translation_memory_words.json 에서 고유명사 및 일반 어휘 사전을 메모리에 로드합니다."""
    global _loaded, _proper_nouns, _common_words
    with _lock:
        if _loaded and not force:
            return
        filepath = _get_words_file_path()
        if not os.path.exists(filepath):
            # Fallback check in dist/dist or old filename
            alt_path = os.path.join(os.path.dirname(filepath), "dist", "translation_memory_words.json")
            old_path = os.path.join(os.path.dirname(filepath), "translation_words.json")
            if os.path.exists(alt_path):
                filepath = alt_path
            elif os.path.exists(old_path):
                filepath = old_path

        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                p_dict = data.get("proper_nouns", {})
                c_dict = data.get("common_words", {})
                if isinstance(p_dict, dict):
                    _proper_nouns = {str(k).lower().strip(): str(v).strip() for k, v in p_dict.items() if k and v}
                if isinstance(c_dict, dict):
                    _common_words = {str(k).lower().strip(): str(v).strip() for k, v in c_dict.items() if k and v}
                logging.info(
                    f"📚 [단어사전 로드] 고유명사 {len(_proper_nouns):,}개, 일반어휘 {len(_common_words):,}개 로드 완료"
                )
            except Exception as e:
                logging.warning(f"⚠️ translation_memory_words.json 로드 실패: {e}")
        _loaded = True


import shutil
import requests

_words_etag = ""
_is_uploading_words = False


def upload_words_to_cloudflare(sync_wait=False):
    """
    translation_memory_words.json 파일을 압축 없이 원본 JSON 그대로 Cloudflare R2(PUT /words)에 업로드합니다.
    """
    global _is_uploading_words
    filepath = _get_words_file_path()
    if not os.path.exists(filepath):
        return

    try:
        import temp_session_logger
        if temp_session_logger.is_debug_mode():
            return
        import translation_memory
        worker_url = translation_memory.get_worker_url()
        if not worker_url:
            return
        safe, _ = translation_memory.is_cloud_upload_safe()
        if not safe:
            return
    except Exception:
        return

    with _lock:
        if _is_uploading_words:
            return
        _is_uploading_words = True

    def _do_upload():
        global _is_uploading_words, _words_etag
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            if len(content) < 20:
                return
            headers = {"Content-Type": "application/json; charset=utf-8"}
            r = requests.put(f"{worker_url}/words", headers=headers, data=content.encode("utf-8"), timeout=25)
            if r.status_code in (200, 201):
                res_data = r.json()
                new_etag = res_data.get("etag", "")
                if new_etag:
                    _words_etag = new_etag
                logging.info(f"☁️ [Cloudflare Words] translation_memory_words.json 원본 업로드 완료 (ETag: {new_etag})")
            else:
                logging.debug(f"Cloudflare words 업로드 응답: {r.status_code}")
        except Exception as e:
            logging.debug(f"Cloudflare words 업로드 스킵: {e}")
        finally:
            _is_uploading_words = False

    t = threading.Thread(target=_do_upload, daemon=True)
    t.start()
    if sync_wait:
        t.join(timeout=10.0)


def sync_words_from_cloudflare(force=False):
    """
    Cloudflare R2(GET /words)에서 translation_memory_words.json 원본을 압축 없이 그대로 다운로드하여
    로컬 파일 및 메모리 캐시로 동기화합니다 (ETag 캐싱 지원).
    """
    global _words_etag, _proper_nouns, _common_words
    try:
        import translation_memory
        worker_url = translation_memory.get_worker_url()
        if not worker_url:
            return False
    except Exception:
        return False

    filepath = _get_words_file_path()
    headers = {}
    if _words_etag and os.path.exists(filepath) and not force:
        headers["If-None-Match"] = _words_etag

    try:
        r = requests.get(f"{worker_url}/words", headers=headers, timeout=15)
        if r.status_code == 304:
            return False
        if r.status_code == 200 and r.text:
            data = r.json()
            if isinstance(data, dict):
                # 기존 로컬 파일이 있으면 .bak 백업 생성
                if os.path.exists(filepath):
                    try:
                        shutil.copy2(filepath, filepath + ".bak")
                    except Exception:
                        pass

                temp_path = filepath + ".tmp"
                with open(temp_path, "w", encoding="utf-8") as f:
                    f.write(r.text)
                os.replace(temp_path, filepath)

                p_dict = data.get("proper_nouns", {})
                c_dict = data.get("common_words", {})
                with _lock:
                    if isinstance(p_dict, dict):
                        _proper_nouns.update({str(k).lower().strip(): str(v).strip() for k, v in p_dict.items() if k and v})
                    if isinstance(c_dict, dict):
                        _common_words.update({str(k).lower().strip(): str(v).strip() for k, v in c_dict.items() if k and v})
                new_etag = r.headers.get("ETag", "")
                if new_etag:
                    _words_etag = new_etag
                logging.info(f"☁️ [Cloudflare Words] 최신 translation_memory_words.json 동기화 완료 ({len(_proper_nouns):,}개 고유명사)")
                return True
    except Exception as e:
        logging.debug(f"Cloudflare words 동기화 스킵: {e}")
    return False


def save_words(force=False, sync_cloud=True):
    """
    변경된 단어 사전을 dist/translation_memory_words.json 에 안전하게 저장하고 .bak 백업을 생성합니다.
    sync_cloud=True일 경우 Cloudflare R2에도 압축 없이 원본 그대로 자동 업로드합니다.
    """
    try:
        import temp_session_logger
        if temp_session_logger.is_debug_mode():
            temp_session_logger.save_temp_session()
            return
    except Exception:
        pass

    global _dirty
    with _lock:
        if not _dirty and not force:
            return
        filepath = _get_words_file_path()
        bak_path = filepath + ".bak"
        temp_path = filepath + ".tmp"

        try:
            # 1. 기존 파일이 존재하면 .bak 백업 생성
            if os.path.exists(filepath):
                try:
                    shutil.copy2(filepath, bak_path)
                except Exception as bak_err:
                    logging.debug(f"단어사전 .bak 백업 생성 스킵: {bak_err}")

            payload = {
                "proper_nouns": _proper_nouns,
                "common_words": _common_words,
                "meta": {
                    "proper_noun_count": len(_proper_nouns),
                    "common_word_count": len(_common_words),
                },
            }
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, filepath)
            _dirty = False
        except Exception as e:
            logging.warning(f"⚠️ translation_memory_words.json 저장 실패: {e}")
            return

    # 2. 클라우드에 압축 없이 원본 JSON 그대로 백그라운드 업로드
    if sync_cloud:
        upload_words_to_cloudflare()



def is_base_english_word(word: str) -> bool:
    """표준 영어 단어(WordNet 또는 기존 일반어휘 사전) 여부를 판별합니다."""
    w = word.lower().strip()
    if len(w) < 2:
        return False
    if w in _common_words or w in _DISQUALIFYING_GRAMMAR_TOKENS or w in _IMPERATIVE_STARTERS:
        return True
    if not _has_wordnet:
        return False
    if _wn.synsets(w):
        return True
    for pos in ("n", "v", "a", "r"):
        try:
            base = _lemmatizer.lemmatize(w, pos=pos)
            if _wn.synsets(base):
                return True
        except Exception:
            pass
    return False


def split_compound_word(word: str):
    """합성어(예: chestplate -> chest + plate, deepslate -> deep + slate) 분해 여부를 확인합니다."""
    w = word.lower().strip()
    if len(w) < 6:
        return None
    for i in range(3, len(w) - 2):
        left = w[:i]
        right = w[i:]
        if is_base_english_word(left) and is_base_english_word(right):
            return left, right
    return None


def _is_likely_phonetic(en: str, ko: str) -> bool:
    """한국어 번역어가 의미 번역(심층암, 흉갑)인지 음차 표기(레드스톤, 네더라이트)인지 판별합니다."""
    en_l = en.lower()
    for end in ("ite", "ium", "um", "us", "ix", "on", "craft", "ism"):
        if en_l.endswith(end):
            return True
    if any(ko.endswith(pe) for pe in _PHONETIC_ENDINGS):
        if any(ko.endswith(sm) for sm in _SEMANTIC_ENDINGS):
            return False
        return True
    return False


def classify_word_category(en_word: str, ko_word: str = "") -> str:
    """
    단어를 분석하여 'COMMON'(일반어휘) 또는 'PROPER'(고유명사)를 반환합니다.
    1. 이미 사전에 있으면 기존 분류 우선
    2. 표준 영어사전(WordNet) 등재 시 COMMON
    3. 합성어(Compound) 분해 시 의미 번역이면 COMMON, 음차면 PROPER
    4. 그 외 미등재 조어는 PROPER
    """
    w = en_word.lower().strip()
    if w in _common_words:
        return "COMMON"
    if w in _proper_nouns:
        return "PROPER"

    if is_base_english_word(w):
        return "COMMON"

    compound = split_compound_word(w)
    if compound:
        if ko_word and _is_likely_phonetic(w, ko_word) and any(
            w.endswith(x) for x in ("stone", "ite", "ium", "craft")
        ):
            return "PROPER"
        return "COMMON"

    return "PROPER"


def _extract_formatting(text: str):
    """마인크래프트 색상/서식 코드(§a, &6 등)를 앞뒤로 분리합니다."""
    if not isinstance(text, str) or not text.strip():
        return "", text, ""
    prefix_match = re.match(r"^([&§][0-9a-fk-orA-FK-OR]|\s)+", text)
    prefix = prefix_match.group(0) if prefix_match else ""
    suffix_match = re.search(r"([&§][0-9a-fk-orA-FK-OR]|\s)+$", text)
    suffix = suffix_match.group(0) if suffix_match else ""
    clean = re.sub(r"[&§][0-9a-fk-orA-FK-OR]", "", text).strip()
    return prefix, clean, suffix


def is_noun_phrase_candidate(text: str, is_item_hint: bool = False) -> bool:
    """
    텍스트가 Case A(단어/명사구 조립 대상)인지 Case B(자연어 문장)인지 판별합니다.
    - 문장 부호(., !, ?, ;, :)나 줄바꿈이 있으면 False (문장)
    - 전치사/관사/접속사(of, in, the, and 등)가 포함되면 False (문맥 번역 필요)
    - 첫 단어가 명령형 동사(Click, Craft, Find 등)이고 아이템 플래그가 아니면 False
    - 순수 영단어 1~5개로 구성된 명사구면 True
    """
    if not isinstance(text, str):
        return False
    _, clean, _ = _extract_formatting(text)
    if not clean or "\n" in clean or "\r" in clean:
        return False

    # 문장 부호나 특수 포맷터(%s, {0}, <...>)가 있으면 문장 파이프라인으로 위임
    if re.search(r"[.!?;:\"%\{\}<>\[\]=+]", clean):
        return False

    tokens = clean.split()
    if not (1 <= len(tokens) <= 5):
        return False

    for idx, tok in enumerate(tokens):
        # 괄호나 숫자가 섞인 토큰 제외 (예: "Tier 1", "(WIP)")
        if not re.match(r"^[A-Za-z]+$", tok):
            return False
        tok_lower = tok.lower()
        # 전치사/관사/대명사 등이 하나라도 있으면 어순 변화 가능성이 있으므로 조립 제외
        if tok_lower in _DISQUALIFYING_GRAMMAR_TOKENS:
            return False
        # 첫 단어가 동사 명령문이고 명확한 아이템이 아니면 문장으로 분류
        if idx == 0 and not is_item_hint and len(tokens) >= 2 and tok_lower in _IMPERATIVE_STARTERS:
            return False

    return True


def try_assemble_noun_phrase(text: str, target_lang: str = "한국어 (Korean)", is_item_hint: bool = False):
    """
    [Case A] 명사구를 단어 단위로 분해하여 translation_words 사전만으로 즉시 조립합니다.
    모든 구성 단어가 사전에 존재할 때만 조립된 한국어 문자열을 반환하며,
    단 하나라도 불확실하거나 사전에 없으면 None을 반환하여 LLM으로 안전하게 넘깁니다.
    """
    if "한국어" not in target_lang and "Korean" not in target_lang:
        return None

    if not _loaded:
        load_words()

    if not is_noun_phrase_candidate(text, is_item_hint=is_item_hint):
        return None

    prefix, clean, suffix = _extract_formatting(text)
    tokens = clean.split()
    if not tokens:
        return None

    assembled_ko = []
    with _lock:
        for tok in tokens:
            tok_l = tok.lower()
            # 1순위: 고유명사 사전 조회
            if tok_l in _proper_nouns:
                assembled_ko.append(_proper_nouns[tok_l])
            # 2순위: 일반 어휘 사전 조회
            elif tok_l in _common_words:
                assembled_ko.append(_common_words[tok_l])
            else:
                # 사전에 없는 단어가 하나라도 있으면 조립 중단 -> LLM에 위임
                return None

    deduped_ko = []
    for w in assembled_ko:
        if not deduped_ko or deduped_ko[-1] != w:
            deduped_ko.append(w)

    result_core = " ".join(deduped_ko)
    return f"{prefix}{result_core}{suffix}"


def extract_proper_noun_glossary(texts, max_terms: int = 30) -> dict:
    """
    [Case B] 번역할 문장 배열(또는 단일 문자열)을 스캔하여,
    문장 내에 실제로 등장하는 '순수 고유명사(_proper_nouns)'만 핀포인트로 추출합니다.
    (일반 어휘는 동음이의어/문맥 오염 방지를 위해 절대 포함하지 않습니다!)
    """
    if not _loaded:
        load_words()

    if isinstance(texts, str):
        texts = [texts]

    found_glossary = {}
    with _lock:
        if not _proper_nouns:
            return found_glossary

        for text in texts:
            if not isinstance(text, str) or not text:
                continue
            # 영단어 토큰 추출
            words = re.findall(r"[A-Za-z]{2,}", text)
            for w in words:
                w_low = w.lower()
                if w_low in _proper_nouns and w_low not in found_glossary:
                    # 일반 어휘 사전에 중복 등재된 다의어가 아닌 순수 고유명사만 주입
                    if w_low not in _common_words and w_low not in _DISQUALIFYING_GRAMMAR_TOKENS:
                        found_glossary[w_low] = _proper_nouns[w_low]
                        if len(found_glossary) >= max_terms:
                            return found_glossary

    return found_glossary


def register_word(en_word: str, ko_word: str, is_official: bool = False):
    """
    단일 영단어와 한국어 번역 쌍을 분류하여 사전에 등록합니다.
    - is_official=True (한글패치 원본 데이터): 즉시 최우선 덮어쓰기 등록
    - is_official=False (AI 신규 번역 데이터): 최소 VOTE_THRESHOLD(2회) 이상 일관되게 등장할 때만 미등록 단어로 추가
    """
    global _dirty
    if not _loaded:
        load_words()

    en_clean = en_word.strip().lower()
    ko_clean = re.sub(r"#.*$", "", ko_word).strip()

    if not re.match(r"^[a-z]{2,}$", en_clean):
        return False
    if not ko_clean or not has_hangul(ko_clean) or " " in ko_clean:
        return False
    if en_clean in _DISQUALIFYING_GRAMMAR_TOKENS:
        return False

    with _lock:
        if is_official:
            cat = classify_word_category(en_clean, ko_clean)
            if cat == "COMMON":
                if _common_words.get(en_clean) != ko_clean:
                    _common_words[en_clean] = ko_clean
                    _dirty = True
                # 만약 이전에 고유명사에 잘못 들어가 있었다면 제거
                _proper_nouns.pop(en_clean, None)
            else:
                if _proper_nouns.get(en_clean) != ko_clean:
                    _proper_nouns[en_clean] = ko_clean
                    _dirty = True
            return True
        else:
            # 이미 등록된 단어는 AI 번역으로 덮어쓰지 않음 (기존 사전/한글패치 보호)
            if en_clean in _proper_nouns or en_clean in _common_words:
                return False

            vote_key = (en_clean, ko_clean)
            _candidate_votes[vote_key] += 1
            if _candidate_votes[vote_key] >= VOTE_THRESHOLD:
                cat = classify_word_category(en_clean, ko_clean)
                if cat == "COMMON":
                    _common_words[en_clean] = ko_clean
                else:
                    _proper_nouns[en_clean] = ko_clean
                _dirty = True
                return True

    return False


def learn_from_pair(en_text: str, ko_text: str, is_official: bool = False):
    """
    번역 완료된 명사구(en_text <-> ko_text)에서 단어 매핑을 역산(소거법)하여 사전에 자동 학습합니다.
    1. 1단어 <-> 1단어 매칭: 즉시 학습 후보 등록
    2. N단어 <-> N단어 매칭 (2~4단어):
       - 이미 알고 있는 단어를 소거하여 모르는 단어 1개가 남으면 정확히 1:1 역산 등록
       - 또는 어순이 1:1 대응하는 명사구에서 각 위치별 단어 학습
    """
    if not isinstance(en_text, str) or not isinstance(ko_text, str):
        return
    _, en_clean, _ = _extract_formatting(en_text)
    _, ko_clean, _ = _extract_formatting(ko_text)
    ko_clean = re.sub(r"#.*$", "", ko_clean).strip()

    if not is_noun_phrase_candidate(en_clean, is_item_hint=True):
        return
    if not has_hangul(ko_clean):
        return

    en_tokens = en_clean.split()
    ko_tokens = ko_clean.split()

    # Case 1: 단일 단어 1:1 매칭
    if len(en_tokens) == 1 and len(ko_tokens) == 1:
        register_word(en_tokens[0], ko_tokens[0], is_official=is_official)
        return

    # Case 2: N:N 단어 수가 동일할 때 (2~4단어)
    if len(en_tokens) == len(ko_tokens) and 2 <= len(en_tokens) <= 4:
        if not _loaded:
            load_words()

        # 소거법(Elimination) 확인: N개 중 N-1개를 이미 알고 있는 경우, 남은 1개는 확실한 1:1 매칭!
        unknown_indices = []
        with _lock:
            for idx, (e_tok, k_tok) in enumerate(zip(en_tokens, ko_tokens)):
                e_low = e_tok.lower()
                known_ko = _proper_nouns.get(e_low) or _common_words.get(e_low)
                if known_ko == k_tok:
                    continue
                unknown_indices.append(idx)

        if len(unknown_indices) == 1:
            u_idx = unknown_indices[0]
            # 소거법으로 도출된 단일 미지 단어는 신뢰도가 높으므로 즉시/가중 등록
            if not is_official:
                _candidate_votes[(en_tokens[u_idx].lower(), ko_tokens[u_idx])] += 1
            register_word(en_tokens[u_idx], ko_tokens[u_idx], is_official=is_official)
        else:
            for e_tok, k_tok in zip(en_tokens, ko_tokens):
                register_word(e_tok, k_tok, is_official=is_official)


def post_validate_proper_nouns(orig_text: str, translated_text: str, chunk_glossary: dict = None) -> str:
    """
    번역 결과문(translated_text)에서 고유명사가 영문 그대로 남았거나(예: 'Certus 석영')
    지정된 고유명사 표기와 다를 경우 안전하게 정제합니다.
    """
    if not orig_text or not translated_text or not isinstance(translated_text, str):
        return translated_text

    if not _loaded:
        load_words()

    active_glossary = chunk_glossary if chunk_glossary is not None else extract_proper_noun_glossary(orig_text)
    if not active_glossary:
        return translated_text

    result = translated_text
    for en_low, ko_target in active_glossary.items():
        # 번역문 안에 영문 고유명사가 그대로 남아있는 경우 (단, 코드/플레이스홀더 내부는 제외)
        pattern = re.compile(rf"(?<![A-Za-z0-9_/:])({re.escape(en_low)})(?![A-Za-z0-9_/:])", re.IGNORECASE)
        if pattern.search(result):
            result = pattern.sub(ko_target, result)

    return result


def sanitize_minecraft_formatting(src: str, tgt: str) -> str:
    """
    & 또는 § 뒤에 0~9, a~z, A~Z, 또는 헥스(#)가 오지 않는 모든 비정상 서식을 감지하여,
    원문(src)의 누락된 서식 코드를 상대 위치(Proximity) 기반으로 100% 정밀 복구하거나 불량 기호를 제거합니다.
    """
    if not isinstance(tgt, str) or not tgt:
        return tgt
    if not isinstance(src, str):
        src = ""

    # 원문 서식 코드와 상대 위치(0.0 ~ 1.0) 추출 (표준 2글자 서식 및 1.16+ 헥스 컬러 서식 지원)
    format_code_pattern = r'([&§](?:#[0-9a-fA-F]{6}|[0-9a-zA-Z]))'
    src_len = max(len(src), 1)
    src_codes_with_pos = [
        (m.group(1), m.start() / src_len)
        for m in re.finditer(format_code_pattern, src)
    ]

    # 번역문에 이미 존재하는 정상 서식 코드들
    tgt_valid_codes = re.findall(format_code_pattern, tgt)

    # 원문 코드 중 번역문에서 누락된 코드들 추적
    remaining_src_codes = list(src_codes_with_pos)
    for tc in tgt_valid_codes:
        for idx, (sc, pos) in enumerate(remaining_src_codes):
            if sc == tc:
                remaining_src_codes.pop(idx)
                break

    tgt_len = max(len(tgt), 1)

    def fix_invalid_code(match):
        prefix = match.group(1)
        char_after = match.group(2)

        # 원문에 동일한 기호 시퀀스가 원래 있었던 경우(예: '&&', '& ' 등) 원본 유지
        if match.group(0) in src:
            return match.group(0)

        # 공백 분리 일반 앰퍼샌드('A & B') 유지
        start_idx = match.start()
        if char_after in ' \t' and start_idx > 0 and match.string[start_idx - 1] in ' \t':
            return match.group(0)

        match_rel_pos = start_idx / tgt_len
        is_closing_punctuation = bool(char_after and char_after in ".,;:?!\"')]}~")

        # 후보군 필터링
        candidates = []
        if is_closing_punctuation or not char_after:
            candidates = [
                (idx, code, pos) for idx, (code, pos) in enumerate(remaining_src_codes)
                if code[0] == prefix and code[1].lower() == 'r'
            ]

        if not candidates:
            candidates = [
                (idx, code, pos) for idx, (code, pos) in enumerate(remaining_src_codes)
                if code[0] == prefix and code[1].lower() != 'r'
            ]

        if not candidates:
            candidates = [
                (idx, code, pos) for idx, (code, pos) in enumerate(remaining_src_codes)
                if code[0] == prefix
            ]

        if candidates:
            # 상대 위치(거리)가 가장 가까운 최적의 원문 코드 매칭
            best_idx, best_code, _ = min(candidates, key=lambda c: abs(c[2] - match_rel_pos))
            remaining_src_codes.pop(best_idx)
            return f"{best_code}{char_after}"
        else:
            return char_after

    result = re.sub(r'([&§])([^0-9a-zA-Z#]|$)', fix_invalid_code, tgt)
    return result


