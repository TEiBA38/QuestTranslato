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
except Exception:
    genai = None


_google_cache = {}
_GOOGLE_CACHE_MAX = 4000
_gemini_cache = {}
_gemini_cache_lock = threading.Lock()
_GEMINI_CACHE_MAX = 8000

_last_gemini_api_call = 0
_gemini_api_rate_lock = threading.Lock()


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


def translate_with_builtin_fallback(text, api_key, reference_map, translate_fn, target_lang="한국어 (Korean)"):
    if text is None:
        return text

    cached_translation = get_reference_translation(text, reference_map)
    if cached_translation is not None:
        return cached_translation

    translated = translate_fn(text, api_key)
    if translated is None or not str(translated).strip():
        return text

    if str(translated).strip() == str(text).strip():
        if target_lang == "한국어 (Korean)":
            return apply_builtin_quest_style_translation(text)
        return text

    return translated


def is_code_or_id(text):
    if not text or not str(text).strip():
        return True
    t = str(text).strip()
    return (":" in t and " " not in t) or t.isdigit() or bool(re.match(r'^[0-9A-Fa-f]{8,}$', t))


def translate_deepl(text, api_key, reference_map=None, target_lang="한국어 (Korean)"):
    return translate_with_builtin_fallback(text, api_key, reference_map, lambda value, key: _translate_deepl_request(value, key, target_lang), target_lang)


def _translate_deepl_request(text, api_key, target_lang="한국어 (Korean)"):
    url = "https://api-free.deepl.com/v2/translate" if api_key.endswith(":fx") else "https://api.deepl.com/v2/translate"
    target_code = LANG_CODES.get(target_lang, ("KO", "ko", "natural Korean"))[0]
    res = requests.post(
        url,
        headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
        data={"text": [text], "source_lang": "EN", "target_lang": target_code},
        timeout=10,
    )
    if res.status_code in (456, 429):
        raise QuotaExceededError("DeepL API 사용량 한도가 초과되었습니다.")
    elif res.status_code == 403:
        raise Exception("DeepL API 키가 유효하지 않습니다.")
    elif res.status_code == 200:
        return res.json()["translations"][0]["text"]
    return text


def translate_google(text, _, reference_map=None, target_lang="한국어 (Korean)"):
    return translate_with_builtin_fallback(text, _, reference_map, lambda value, key: _translate_google_request(value, target_lang), target_lang)


def _translate_google_request(text, target_lang="한국어 (Korean)"):
    if GoogleTranslator is None:
        return text

    target_code = LANG_CODES.get(target_lang, ("KO", "ko", "natural Korean"))[1]
    cache_key = (text, target_lang)

    cached = _google_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        translator = GoogleTranslator(source='en', target=target_code)
        translated = translator.translate(text)
        result = translated if translated else text

        if len(_google_cache) >= _GOOGLE_CACHE_MAX:
            _google_cache.clear()
        _google_cache[cache_key] = result

        return result
    except Exception:
        return text


def translate_openai(text, api_key, reference_map=None, glossary=None, ai_model=None, target_lang="한국어 (Korean)"):
    return translate_with_builtin_fallback(text, api_key, reference_map, lambda value, key: _translate_openai_request(value, key, glossary, ai_model, target_lang), target_lang)


def _translate_openai_request(text, api_key, glossary=None, ai_model=None, target_lang="한국어 (Korean)"):
    url = "https://api.openai.com/v1/chat/completions"
    model = ai_model if ai_model else "gpt-4o-mini"
    target_prompt = LANG_CODES.get(target_lang, ("KO", "ko", "natural Korean"))[2]
    system_prompt = f"You are a professional Minecraft quest translator. Translate the given text from English to {target_prompt}. Preserve Minecraft formatting and color codes (e.g., &a, §c) without changing them. Output ONLY the translated text."
    if glossary:
        glossary_text = ", ".join([f"'{k}' as '{v}'" for k, v in glossary.items()])
        system_prompt += f"\nGlossary (Strictly replace these words): {glossary_text}"
        
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt
            },
            {"role": "user", "content": text}
        ],
        "temperature": 0.3,
    }
    res = requests.post(url, headers={"Authorization": f"Bearer {api_key}"}, json=payload, timeout=12)
    if res.status_code == 429:
        raise QuotaExceededError("OpenAI API 할당량이 초과되었거나 요청이 너무 빠릅니다.")
    elif res.status_code == 401:
        raise Exception("OpenAI API 키가 유효하지 않습니다.")
    elif res.status_code == 200:
        return res.json()["choices"][0]["message"]["content"].strip()
    return text


def translate_gemini_batch(text_list, api_key, is_paid=False, log_callback=None, cancel_checker=None, reference_map=None, glossary=None, ai_model=None, target_lang="한국어 (Korean)"):
    if not text_list:
        return []

    resolved = []
    unresolved_indices = []
    unresolved_raw = []

    for text in text_list:
        cached = get_reference_translation(text, reference_map)
        if cached is not None:
            resolved.append(cached)
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
    )
    if glossary:
        glossary_text = ", ".join([f"'{k}' as '{v}'" for k, v in glossary.items()])
        prompt += f"- Glossary (Strictly replace these words): {glossary_text}\n"
    prompt += f"Input JSON: {input_json}"

    model_name = ai_model if ai_model else 'gemini-3.5-flash-lite'

    for attempt in range(1, max_retries + 1):
        if cancel_checker and cancel_checker():
            raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")

        if not is_paid:
            with _gemini_api_rate_lock:
                import time
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
            res = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            raw_text = res.text.strip() if res.text else ""
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
            elif "API_KEY_INVALID" in err or "400" in err:
                raise Exception("Gemini API 키가 유효하지 않거나 모델 권한이 없습니다.")
            else:
                if attempt == max_retries:
                    raise Exception(f"Gemini 번역 연속 실패: {err}")

    return [item if item is not None else text for item, text in zip(resolved, text_list)]


ENGINES = {
    "Gemini Lite (배치 번역)": "gemini_batch",
    "DeepL": "deepl",
    "Google Translate": "google",
    "OpenAI": "openai",
}
