import json
import re
import time
import requests
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from constants import LANG_CODES

try:
    from deep_translator import GoogleTranslator
except Exception:
    GoogleTranslator = None

try:
    from google import genai
    from google.genai import types
except Exception:
    genai = None
    types = None


_google_cache = {}
_GOOGLE_CACHE_MAX = 4000
_gemini_cache = {}
_gemini_cache_lock = threading.Lock()
_GEMINI_CACHE_MAX = 8000

_last_gemini_api_call = 0
_gemini_api_rate_lock = threading.Lock()

# ============================================================
# Mock Mode: API 호출 없이 테스트할 수 있는 모의 번역 모드
# ============================================================
MOCK_MODE = False

def _mock_translate(text):
    """Mock translation: wraps text with [번역됨] prefix for testing."""
    if not text or not str(text).strip():
        return text
    return f"[번역됨] {text}"

def _mock_translate_batch(text_list):
    """Mock batch translation for testing."""
    return [_mock_translate(t) for t in text_list]

def _mock_extract_glossary():
    """Mock glossary extraction for testing."""
    return {
        "creeper": "크리퍼 # [Auto-Extracted]",
        "ender dragon": "엔더 드래곤 # [Auto-Extracted]",
        "redstone": "레드스톤 # [Auto-Extracted]",
    }


class QuotaExceededError(Exception):
    pass


class TranslationCancelledError(Exception):
    pass


def normalize_reference_text(text):
    if text is None:
        return ""
    normalized = re.sub(r"\s+", " ", str(text)).strip()
    return normalized


def get_reference_translation(text, reference_map):
    if not reference_map:
        return None
    key = normalize_reference_text(text)
    if not key:
        return None
    if key in reference_map:
        return reference_map[key]
    lower_key = key.lower()
    return reference_map.get(lower_key)


def apply_builtin_quest_style_translation(text):
    if not isinstance(text, str):
        return text
    
    mapping = {
        "Quests": "퀘스트",
        "Quest": "퀘스트",
        "Rewards": "보상",
        "Reward": "보상",
        "Tasks": "과제",
        "Task": "과제",
        "Dependencies": "선행 조건",
        "Items": "아이템",
        "Item": "아이템",
        "Loot": "전리품",
        "Title": "제목",
        "Subtitle": "부제목",
        "Description": "설명"
    }
    
    # Preserve formatting if any, but replace the exact word
    stripped = text.strip()
    if stripped in mapping:
        return text.replace(stripped, mapping[stripped])
    return text


import translation_memory

def _translate_wrapper(text, api_key, translate_fn, reference_map=None, target_lang="한국어 (Korean)", is_item=False, is_book=False):
    if not text or not str(text).strip():
        return text
    if MOCK_MODE:
        return _mock_translate(text)

    cached_translation = get_reference_translation(text, reference_map)
    if cached_translation is not None:
        return cached_translation
        
    if is_item:
        global_cached = translation_memory.get_cached_item_translation(text, target_lang)
    elif is_book:
        global_cached = translation_memory.get_cached_book_translation(text, target_lang)
    else:
        global_cached = translation_memory.get_cached_translation(text, target_lang)
        
    if global_cached is not None:
        return global_cached

    translated = translate_fn(text, api_key)
    if translated is None or not str(translated).strip():
        return text

    if str(translated).strip() == str(text).strip():
        if target_lang == "한국어 (Korean)":
            return apply_builtin_quest_style_translation(text)
        return text
        
    if is_item:
        translation_memory.add_item_to_memory(text, translated, target_lang)
    elif is_book:
        translation_memory.add_book_to_memory(text, translated, target_lang)
    else:
        translation_memory.add_to_memory(text, translated, target_lang)

    return translated


def is_code_or_id(text):
    if not text or not str(text).strip():
        return True
    t = str(text).strip()
    if (":" in t and " " not in t) or t.isdigit() or bool(re.match(r'^[0-9A-Fa-f]{8,}$', t)):
        return True
    # 영단어(2글자 이상)가 전혀 없는 기호/숫자/포맷 문자열 (%s/t, %s°C, :(, 1/8 등)
    words = re.findall(r'[a-zA-Z]{2,}', t)
    if not words:
        return True
    # 단일 문자나 아주 짧은 대문자 기호/약어 (W, R, D, GUI, ID 등)
    if len(t) <= 3 and not re.search(r'[가-힣]', t) and t.isupper():
        return True
    return False


def translate_deepl(text, api_key, is_pro=False, reference_map=None, target_lang="한국어 (Korean)", is_item=False, is_book=False):
    def do_translate(t, key):
        url = "https://api.deepl.com/v2/translate" if is_pro else "https://api-free.deepl.com/v2/translate"
        headers = {"Authorization": f"DeepL-Auth-Key {key}", "Content-Type": "application/json"}
        lang_code = "KO" if "Korean" in target_lang or "한국어" in target_lang else "EN-US"
        data = {"text": [t], "target_lang": lang_code, "tag_handling": "xml"}
        response = requests.post(url, headers=headers, json=data, timeout=10)
        response.raise_for_status()
        return response.json()["translations"][0]["text"]
    return _translate_wrapper(text, api_key, do_translate, reference_map, target_lang, is_item, is_book)


def translate_google(text, api_key, reference_map=None, target_lang="한국어 (Korean)", is_item=False, is_book=False):
    def do_translate(t, key):
        if GoogleTranslator is None:
            return t
        target_code = LANG_CODES.get(target_lang, ("KO", "ko", "natural Korean"))[1]
        translator = GoogleTranslator(source='en', target=target_code)
        translated = translator.translate(t)
        return translated if translated else t
    return _translate_wrapper(text, api_key, do_translate, reference_map, target_lang, is_item, is_book)


def translate_openai(text, api_key, reference_map=None, glossary=None, ai_model=None, target_lang="한국어 (Korean)", is_item=False, is_book=False):
    def do_translate(t, key):
        url = "https://api.openai.com/v1/chat/completions"
        model = ai_model if ai_model else "gpt-4o-mini"
        target_prompt = LANG_CODES.get(target_lang, ("KO", "ko", "natural Korean"))[2]
        system_prompt = (
            f"You are a professional Minecraft quest translator. Translate the given text from English to {target_prompt}.\n"
            "Rules:\n"
            "- Preserve Minecraft formatting and color codes (e.g., &a, §c) exactly without changing them.\n"
            "- NEVER use square brackets [] around translated words (e.g., WRONG: '[철] [검]', RIGHT: '철 검').\n"
            "- Preserve game abbreviations and formats exactly without translating (e.g., 'Lv.', 'HP', 'MP', 'ATK', 'DEF', 'x2', '+10%').\n"
            "- Output ONLY the translated text without explanation."
        )
        if glossary:
            clean_glossary = [f"'{k}' as '{v.split('#')[0].strip()}'" for k, v in glossary.items()]
            glossary_text = ", ".join(clean_glossary)
            system_prompt += f"\nGlossary (Strictly replace these words): {glossary_text}"

        try:
            from translation_memory import find_few_shot_examples
            few_shots = find_few_shot_examples([t], target_lang, max_examples=2)
            if few_shots:
                system_prompt += "\nStyle Reference Examples from previous quests (Follow this tone and style):\n"
                for src_ex, tgt_ex in few_shots:
                    system_prompt += f"- \"{src_ex}\" -> \"{tgt_ex}\"\n"
        except Exception:
            pass

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": t}
            ],
        }
        if any(model.startswith(p) for p in ("o1", "o3", "o4")):
            payload["reasoning_effort"] = "low"
        else:
            payload["temperature"] = 0.2
        res = requests.post(url, headers={"Authorization": f"Bearer {key}"}, json=payload, timeout=15)
        if res.status_code == 429:
            raise QuotaExceededError("OpenAI API 할당량이 초과되었거나 요청이 너무 빠릅니다.")
        elif res.status_code == 401:
            raise Exception("OpenAI API 키가 유효하지 않습니다.")
        elif res.status_code == 200:
            return res.json()["choices"][0]["message"]["content"].strip()
        return t
    return _translate_wrapper(text, api_key, do_translate, reference_map, target_lang, is_item, is_book)


def translate_gemini_batch(text_list, api_key, is_paid=False, log_callback=None, cancel_checker=None, reference_map=None, glossary=None, ai_model=None, target_lang="한국어 (Korean)"):
    if not text_list:
        return []
    if MOCK_MODE:
        if log_callback:
            log_callback(f"🧪 [Mock Mode] {len(text_list)}개 텍스트를 모의 번역합니다.")
        time.sleep(0.1)  # 약간의 지연으로 실제 번역처럼 느끼게
        return _mock_translate_batch(text_list)
    resolved = []
    unresolved_indices = []
    unresolved_raw = []

    for text in text_list:
        cached = get_reference_translation(text, reference_map)
        if cached is not None:
            resolved.append(cached)
        else:
            # 파일 기반 영구 캐시(translation_memory) 조회 - API 비용 절감 핵심!
            mem_cached = translation_memory.get_cached_translation(text, target_lang)
            if mem_cached is not None:
                resolved.append(mem_cached)
            else:
                cache_key = (text, target_lang)
                with _gemini_cache_lock:
                    gemini_cached = _gemini_cache.get(cache_key)
                if gemini_cached is not None:
                    resolved.append(gemini_cached)
                else:
                    resolved.append(None)
                    unresolved_indices.append(len(resolved) - 1)
                    unresolved_raw.append(text)

    if not unresolved_raw:
        return resolved

    unique_unresolved = []
    unique_map = {}
    unresolved_to_unique = []
    for text in unresolved_raw:
        pos = unique_map.get(text)
        if pos is None:
            pos = len(unique_unresolved)
            unique_map[text] = pos
            unique_unresolved.append(text)
        unresolved_to_unique.append(pos)

    if genai is None:
        return [item if item is not None else text for item, text in zip(resolved, text_list)]

    max_retries = 3
    client = genai.Client(api_key=api_key)
    input_json = json.dumps(unique_unresolved, ensure_ascii=False)
    
    target_prompt = LANG_CODES.get(target_lang, ("KO", "ko", "natural Korean"))[2]
    
    prompt = (
        f"Translate the input JSON array from English to {target_prompt} for Minecraft quests.\n"
        "Rules:\n"
        "- Return ONLY a valid JSON array (no explanation, no markdown).\n"
        "- Keep array order and length exactly the same.\n"
        "- Preserve formatting/placeholders exactly (&a, §c, {color:...}, %s, {0}, <...>).\n"
        "- NEVER use square brackets [] around translated words (e.g., WRONG: '[철] [검]', RIGHT: '철 검').\n"
        "- Preserve game abbreviations and formats exactly without translating (e.g., 'Lv.', 'HP', 'MP', 'ATK', 'DEF', 'x2', '+10%').\n"
    )
    if glossary:
        input_text_lower = " ".join(unique_unresolved).lower()
        clean_glossary = []
        for k, v in glossary.items():
            if k.lower() in input_text_lower:
                clean_glossary.append(f"'{k}' as '{v.split('#')[0].strip()}'")
        
        if clean_glossary:
            glossary_text = ", ".join(clean_glossary)
            prompt += f"- Glossary (Strictly replace these words): {glossary_text}\n"

    try:
        from translation_memory import find_few_shot_examples
        few_shots = find_few_shot_examples(unique_unresolved, target_lang, max_examples=2)
        if few_shots:
            prompt += "- Style Reference Examples from previous quests (Follow this tone, formatting, and style):\n"
            for src_ex, tgt_ex in few_shots:
                prompt += f"  * \"{src_ex}\" -> \"{tgt_ex}\"\n"
    except Exception:
        pass

    prompt += f"Input JSON: {input_json}"

    model_name = ai_model if ai_model else 'gemini-3.5-flash-lite'

    for attempt in range(1, max_retries + 1):
        if cancel_checker and cancel_checker():
            raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")

        if not is_paid:
            with _gemini_api_rate_lock:
                global _last_gemini_api_call
                elapsed = time.time() - _last_gemini_api_call
                if elapsed < 6.5:
                    sleep_time = 6.5 - elapsed
                    for _ in range(int(sleep_time * 10)):
                        if cancel_checker and cancel_checker():
                            raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")
                        time.sleep(0.1)
                _last_gemini_api_call = time.time()

        try:
            config = None
            if types:
                config_params = {"response_mime_type": "application/json"}
                if hasattr(types, "ThinkingConfig") and ("2.5" in model_name or "thinking" in model_name or "2.0-flash" in model_name):
                    try:
                        config_params["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
                    except Exception:
                        pass
                try:
                    config = types.GenerateContentConfig(**config_params)
                except Exception:
                    config = None

            try:
                res = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=config,
                )
            except Exception as call_err:
                err_msg = str(call_err)
                if ("400" in err_msg or "INVALID_ARGUMENT" in err_msg or "not supported" in err_msg) and config is not None:
                    # 설정 호환성 문제일 경우 설정 없이 기본 호출로 자동 재시도
                    res = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                    )
                else:
                    raise call_err

            raw_text = res.text.strip() if res.text else ""
            if raw_text.startswith("```"):
                raw_text = re.sub(r"^```[a-zA-Z]*\s*", "", raw_text)
                raw_text = re.sub(r"\s*```$", "", raw_text).strip()
            match = re.search(r'\[.*\]', raw_text, re.DOTALL)
            if match:
                raw_text = match.group(0)
            
            try:
                translated_array = json.loads(raw_text, strict=False)
            except Exception:
                # 불완전한 제어 문자나 이스케이프 오류 방어
                cleaned = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', raw_text)
                translated_array = json.loads(cleaned, strict=False)
            if isinstance(translated_array, list) and len(translated_array) == len(unique_unresolved):
                with _gemini_cache_lock:
                    if len(_gemini_cache) >= _GEMINI_CACHE_MAX:
                        _gemini_cache.clear()
                    for src, dst in zip(unique_unresolved, translated_array):
                        _gemini_cache[(src, target_lang)] = str(dst)

                merged = list(resolved)
                for original_pos, resolved_idx in enumerate(unresolved_indices):
                    unique_pos = unresolved_to_unique[original_pos]
                    merged[resolved_idx] = translated_array[unique_pos]
                return merged
            return [item if item is not None else text for item, text in zip(resolved, text_list)]

        except json.JSONDecodeError as e:
            if attempt < max_retries:
                if log_callback:
                    log_callback(f"⚠️ [응답 파싱 실패] Gemini 응답이 올바른 JSON 형식이 아닙니다. 재시도합니다... ({attempt}/{max_retries})")
                for _ in range(20):
                    if cancel_checker and cancel_checker():
                        raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")
                    time.sleep(0.1)
            else:
                if log_callback:
                    log_callback(f"⚠️ [번역 건너뜀] 응답 파싱에 계속 실패해 이 구간은 원문 그대로 둡니다. ({str(e)})")
                return [item if item is not None else text for item, text in zip(resolved, text_list)]

        except Exception as e:
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                if attempt < max_retries:
                    wait_time = 15 * attempt if is_paid else 65 * attempt
                    if log_callback:
                        log_callback(f"⚠️ [요청 제한 발생] {wait_time}초 동안 대기 후 자동 재시도합니다... ({attempt}/{max_retries})")
                    for _ in range(wait_time * 10):
                        if cancel_checker and cancel_checker():
                            raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")
                        time.sleep(0.1)
                else:
                    raise QuotaExceededError("Gemini API 한도(RPM/RPD) 초과로 더 이상 진행할 수 없습니다. 작업을 중단합니다.")
            elif "API_KEY_INVALID" in err or "401" in err or "UNAUTHENTICATED" in err:
                raise Exception("Gemini API 키가 유효하지 않습니다. 올바른 API 키인지 확인해주세요.")
            elif "404" in err or "NOT_FOUND" in err:
                raise Exception(f"선택한 AI 모델({model_name})을 찾을 수 없습니다. 지원되는 모델명을 선택해주세요.")
            else:
                if attempt == max_retries:
                    raise Exception(f"Gemini 번역 실패 ({err})")

    return [item if item is not None else text for item, text in zip(resolved, text_list)]


def translate_local_ai(text_list, base_url, model_name, api_key=None, log_callback=None, cancel_checker=None, reference_map=None, glossary=None, target_lang="한국어 (Korean)"):
    if not text_list:
        return []
    if MOCK_MODE:
        if log_callback:
            log_callback(f"🧪 [Mock Mode] {len(text_list)}개 텍스트를 모의 번역합니다.")
        time.sleep(0.1)
        return _mock_translate_batch(text_list)

    resolved = []
    unresolved_indices = []
    unresolved_raw = []

    for text in text_list:
        cached = get_reference_translation(text, reference_map)
        if cached is not None:
            resolved.append(cached)
        else:
            mem_cached = translation_memory.get_cached_translation(text, target_lang)
            if mem_cached is not None:
                resolved.append(mem_cached)
            else:
                cache_key = (text, target_lang)
                with _gemini_cache_lock:
                    cached_local = _gemini_cache.get(cache_key)
                if cached_local is not None:
                    resolved.append(cached_local)
                else:
                    resolved.append(None)
                    unresolved_indices.append(len(resolved) - 1)
                    unresolved_raw.append(text)

    if not unresolved_raw:
        return resolved

    unique_unresolved = []
    unique_map = {}
    unresolved_to_unique = []
    for text in unresolved_raw:
        pos = unique_map.get(text)
        if pos is None:
            pos = len(unique_unresolved)
            unique_map[text] = pos
            unique_unresolved.append(text)
        unresolved_to_unique.append(pos)

    input_json = json.dumps(unique_unresolved, ensure_ascii=False)
    target_prompt = LANG_CODES.get(target_lang, ("KO", "ko", "natural Korean"))[2]
    
    system_prompt = (
        f"Translate the input JSON array from English to {target_prompt} for Minecraft quests.\n"
        "Rules:\n"
        "- Return ONLY a valid JSON array matching the exact order and length of the input.\n"
        "- Output MUST start with '[' and end with ']'.\n"
        "- Preserve formatting/placeholders exactly (&a, §c, {{color:...}}, %s, {{0}}, <...>).\n"
        "- NEVER use square brackets [] around translated words.\n"
        "- Preserve abbreviations like 'Lv.', 'HP', 'MP', 'ATK', 'x2'.\n"
    )
    if glossary:
        clean_glossary = [f"'{k}' as '{v.split('#')[0].strip()}'" for k, v in glossary.items()]
        glossary_text = ", ".join(clean_glossary)
        system_prompt += f"- Glossary (Strictly apply): {glossary_text}\n"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Input JSON: {input_json}"}
    ]
    
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": 0.1
    }
    
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    
    url = base_url.strip().rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"

    try:
        if cancel_checker and cancel_checker():
            raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")
            
        response = requests.post(url, headers=headers, json=payload, timeout=300)
        response.raise_for_status()
        data = response.json()
        raw_text = data['choices'][0]['message']['content'].strip()
        
        match = re.search(r'\[.*\]', raw_text, re.DOTALL)
        if match:
            raw_text = match.group(0)
            
        translated_array = json.loads(raw_text)
        if isinstance(translated_array, list) and len(translated_array) == len(unique_unresolved):
            with _gemini_cache_lock:
                if len(_gemini_cache) >= _GEMINI_CACHE_MAX:
                    _gemini_cache.clear()
                for src, dst in zip(unique_unresolved, translated_array):
                    _gemini_cache[(src, target_lang)] = str(dst)

            merged = list(resolved)
            for original_pos, resolved_idx in enumerate(unresolved_indices):
                unique_pos = unresolved_to_unique[original_pos]
                merged[resolved_idx] = translated_array[unique_pos]
            return merged
            
        if log_callback:
            log_callback("⚠️ [응답 파싱 실패] 로컬 AI 응답이 올바른 JSON 형식이 아니어 원문으로 대체됩니다.")
        return [item if item is not None else text for item, text in zip(resolved, text_list)]

    except Exception as e:
        if log_callback:
            log_callback(f"⚠️ [통신/파싱 에러] 로컬 AI 호출 실패: {str(e)}")
        return [item if item is not None else text for item, text in zip(resolved, text_list)]


ENGINES = {
    "Gemini Lite (배치 번역)": "gemini_batch",
    "Custom API (OpenAI 호환, Local AI 등)": "local_ai",
    "DeepL": "deepl",
    "Google Translate": "google",
    "OpenAI": "openai",
    "🧪 테스트 모드 (Mock)": "mock",
}


def auto_extract_glossary(text_samples, engine_key, api_key, ai_model=None, target_lang="한국어 (Korean)", custom_url=None):
    if not text_samples:
        return {}
    if MOCK_MODE or engine_key == "mock":
        return _mock_extract_glossary()
        
    combined_text = "\n".join(text_samples)
    if len(combined_text) > 12000:
        combined_text = combined_text[:12000]
        
    prompt = (
        "You are an expert Minecraft translator.\n"
        "Analyze the following Minecraft modpack texts and extract the top 20 most important game terms, items, or proper nouns.\n"
        "RULES:\n"
        "- DO NOT extract generic words like 'Chest', 'Block', 'Item', 'Sword'. ONLY extract highly specific proper nouns (e.g., 'Botania', 'Mekanism', 'Draconium').\n"
        "- Return ALL English keys in lowercase (e.g., 'iron ingot' instead of 'Iron Ingot') to prevent fragmentation.\n"
        f"- Translate them into natural {target_lang}.\n"
        "Return ONLY a valid JSON dictionary where the key is the English term and the value is the translated term.\n"
        "Example output: {\"draconium ingot\": \"드라코늄 주괴\", \"mana pool\": \"마나 풀\"}\n\n"
        f"Input text:\n{combined_text}"
    )

    extracted = {}
    try:
        if engine_key == "gemini_batch":
            client = genai.Client(api_key=api_key)
            model_name = ai_model if ai_model else 'gemini-3.5-flash-lite'
            config = None
            if types:
                config_params = {"response_mime_type": "application/json"}
                if hasattr(types, "ThinkingConfig"):
                    try:
                        config_params["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
                    except Exception:
                        pass
                try:
                    config = types.GenerateContentConfig(**config_params)
                except Exception:
                    config = None
            try:
                res = client.models.generate_content(model=model_name, contents=prompt, config=config)
            except Exception as call_err:
                err_msg = str(call_err)
                if ("400" in err_msg or "INVALID_ARGUMENT" in err_msg or "not supported" in err_msg) and config is not None:
                    res = client.models.generate_content(model=model_name, contents=prompt)
                else:
                    raise call_err
            raw_text = res.text.strip() if res.text else ""
        elif engine_key in ("openai", "local_ai"):
            url = custom_url.strip().rstrip("/") + "/chat/completions" if engine_key == "local_ai" and custom_url else "https://api.openai.com/v1/chat/completions"
            model_name = ai_model if ai_model else ("gpt-4o-mini" if engine_key == "openai" else "llama")
            headers = {"Content-Type": "application/json"}
            if api_key: headers["Authorization"] = f"Bearer {api_key}"
            payload = {"model": model_name, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3}
            res = requests.post(url, headers=headers, json=payload, timeout=30)
            res.raise_for_status()
            raw_text = res.json()['choices'][0]['message']['content'].strip()
        else:
            return {}

        match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if match:
            raw_text = match.group(0)
        parsed = json.loads(raw_text)
        if isinstance(parsed, dict):
            for k, v in parsed.items():
                if isinstance(k, str) and isinstance(v, str) and len(k) < 40 and len(v) < 40:
                    clean_k = re.sub(r'[^a-zA-Z0-9\s]', '', k).lower().strip()
                    if clean_k:
                        extracted[clean_k] = v.strip()
    except Exception as e:
        import logging
        logging.warning(f"Auto glossary extraction failed: {e}")
        
    return extracted
