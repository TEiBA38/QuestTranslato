import json
import re
import time
import requests
from dataclasses import dataclass
from typing import Optional
from requests.adapters import HTTPAdapter
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from constants import LANG_CODES
import translation_memory

_http_session = None
_http_session_lock = threading.Lock()
_gemini_client_cache = {}
_gemini_client_lock = threading.Lock()


def _get_http_session() -> requests.Session:
    """병렬 스레드에서 TCP/TLS 연결을 재사용할 수 있도록 공용 HTTP 세션을 반환합니다."""
    global _http_session
    if _http_session is None:
        with _http_session_lock:
            if _http_session is None:
                sess = requests.Session()
                adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32)
                sess.mount("https://", adapter)
                sess.mount("http://", adapter)
                _http_session = sess
    return _http_session

try:
    from google import genai
    from google.genai import types
except Exception:
    genai = None
    types = None


_gemini_cache = {}
_gemini_cache_lock = threading.Lock()
_GEMINI_CACHE_MAX = 8000

_last_gemini_api_start = 0.0
_last_gemini_api_call = 0.0
_gemini_backoff_until = 0.0
_gemini_call_timestamps = deque()
_gemini_api_rate_lock = threading.Lock()
_unsupported_config_models = set()


# 무료 계정 기본 보호 파라미터 (미등록 모델용 안전 기본값 / 테스트 오버라이드용)
_GEMINI_FREE_MIN_INTERVAL = 12.8
_GEMINI_FREE_WINDOW_SECONDS = 64.0
_GEMINI_FREE_MAX_CALLS_PER_WINDOW = 5


class GeminiRateLimiter:
    """Gemini 무료 계정의 모델별 RPM 프로파일, 슬라이딩 윈도우 슬롯 예약, 429/503 병렬 백오프 공유를 전담합니다."""

    def get_profile(self, model_name: str):
        """모델별 RPM 한도에 맞춘 (min_start_interval, min_post_cooldown, window_seconds, max_calls) 반환.
        - 3.5-flash-lite / 3.1-flash-lite (무료 15 RPM): 10 RPM 이하 보수적 보호 (시작 간격 5.8초, 응답 후 0.4초, 62초/10회, 2스레드 파이프라이닝)
        - 3.8-flash / 4-hydrogen (무료 5 RPM): 4.5 RPM 이하 보호 (시작 간격 12.8초, 응답 후 1.0초, 64초/5회)"""
        m = str(model_name or "").lower()
        if "flash-lite" in m or "lite" in m:
            return (5.8, 0.4, 62.0, 10)
        if "3.8-flash" in m or "hydrogen" in m or "argon" in m or "pro" in m:
            return (12.8, 1.0, 64.0, 5)
        return (
            _GEMINI_FREE_MIN_INTERVAL,
            min(0.8, _GEMINI_FREE_MIN_INTERVAL),
            _GEMINI_FREE_WINDOW_SECONDS,
            _GEMINI_FREE_MAX_CALLS_PER_WINDOW,
        )

    def notify_backoff(self, wait_seconds: float):
        """병렬 워커 중 하나가 429/503을 수신했을 때 다른 형제 워커들도 동일 쿨다운을 공유하도록 전파합니다."""
        global _gemini_backoff_until
        if wait_seconds <= 0:
            return
        with _gemini_api_rate_lock:
            target = time.monotonic() + float(wait_seconds)
            if target > _gemini_backoff_until:
                _gemini_backoff_until = target

    def acquire_slot(self, model_name: str, cancel_checker=None):
        """락 안에서 다음 호출 슬롯만 예약합니다(실제 HTTP 요청은 락 밖에서 실행해 파이프라이닝 허용)."""
        global _last_gemini_api_start
        min_start_interval, min_post_cooldown, window_sec, max_calls = self.get_profile(model_name)
        with _gemini_api_rate_lock:
            now = time.monotonic()
            while _gemini_call_timestamps and (now - _gemini_call_timestamps[0]) >= window_sec:
                _gemini_call_timestamps.popleft()

            window_wait = 0.0
            if len(_gemini_call_timestamps) >= max_calls:
                window_wait = (_gemini_call_timestamps[0] + window_sec) - now

            start_wait = 0.0
            if _last_gemini_api_start > 0:
                start_wait = (_last_gemini_api_start + min_start_interval) - now
            post_wait = 0.0
            if _last_gemini_api_call > 0:
                post_wait = (_last_gemini_api_call + min_post_cooldown) - now
            backoff_wait = max(0.0, _gemini_backoff_until - now)

            wait_needed = max(window_wait, start_wait, post_wait, backoff_wait)
            if wait_needed > 0:
                _sleep_with_cancel(wait_needed, cancel_checker)

            call_start = time.monotonic()
            _last_gemini_api_start = call_start
            _gemini_call_timestamps.append(call_start)

    def mark_finished(self):
        """응답 완료 시각을 갱신해 최소 응답 후 쿨다운 기준점을 유지합니다."""
        global _last_gemini_api_call
        with _gemini_api_rate_lock:
            _last_gemini_api_call = time.monotonic()


_default_rate_limiter = GeminiRateLimiter()


class _FixedWindowRateLimiter:
    """유료 API 중 Tier 1 RPM이 빡빡한 엔진(예: Claude 50 RPM)의 429 방지를 위한 초경량 슬라이딩 윈도우 리미터."""

    def __init__(self, min_start_interval: float, window_seconds: float, max_calls: int):
        self._lock = threading.Lock()
        self._timestamps = deque()
        self._last_start = 0.0
        self._min_start_interval = float(min_start_interval)
        self._window_seconds = float(window_seconds)
        self._max_calls = int(max_calls)

    def acquire_slot(self, cancel_checker=None):
        with self._lock:
            now = time.monotonic()
            while self._timestamps and (now - self._timestamps[0]) >= self._window_seconds:
                self._timestamps.popleft()

            window_wait = 0.0
            if len(self._timestamps) >= self._max_calls:
                window_wait = (self._timestamps[0] + self._window_seconds) - now

            start_wait = 0.0
            if self._last_start > 0:
                start_wait = (self._last_start + self._min_start_interval) - now

            wait_needed = max(window_wait, start_wait)
            if wait_needed > 0:
                _sleep_with_cancel(wait_needed, cancel_checker)

            call_start = time.monotonic()
            self._last_start = call_start
            self._timestamps.append(call_start)


# Claude Tier 1 (50 RPM) 보수적 보호: 5스레드 기준 시작 간격 1.35초, 62초당 최대 42회 (429 0% 목표)
_claude_rate_limiter = _FixedWindowRateLimiter(min_start_interval=1.35, window_seconds=62.0, max_calls=42)

# OpenAI / Claude 유료 계정 티어 상태 (1: Tier 1, 2: Tier 2, 3: Tier 3+)
_paid_api_tier = 1

# 무료 Gemini 전용 초장문(1,000자 이상) 상위 모델 자동 라우팅 활성화 여부 (유료에서는 자동 비활성)
_free_long_text_upgrade = True


def set_free_long_text_upgrade(enabled: bool):
    global _free_long_text_upgrade
    _free_long_text_upgrade = bool(enabled)


def is_free_long_text_upgrade_enabled() -> bool:
    return _free_long_text_upgrade


def set_paid_api_tier(tier: int):
    """현재 선택된 유료 API 계정 티어(1/2/3)를 설정합니다."""
    global _paid_api_tier
    try:
        t = int(tier)
    except Exception:
        t = 1
    _paid_api_tier = max(1, min(3, t))


def get_paid_api_tier() -> int:
    return _paid_api_tier


def get_paid_worker_limit(engine_key: str) -> int:
    """엔진 및 현재 유료 계정 티어에 맞춘 429-free 최적 병렬 스레드 수를 반환합니다."""
    tier = _paid_api_tier
    if engine_key == "gemini_batch":
        return 10
    if engine_key == "openai":
        return {1: 8, 2: 10, 3: 12}.get(tier, 8)
    if engine_key == "claude":
        return {1: 5, 2: 10, 3: 12}.get(tier, 5)
    if engine_key == "deepseek":
        return 6
    return 5


def _notify_gemini_backoff(wait_seconds: float):
    """병렬 워커 중 하나가 429/503을 수신했을 때 다른 형제 워커들도 동일 쿨다운을 공유하도록 전파합니다."""
    _default_rate_limiter.notify_backoff(wait_seconds)


def _get_gemini_free_rpm_profile(model_name: str):
    """하위 호환용 래퍼: GeminiRateLimiter.get_profile() 위임."""
    return _default_rate_limiter.get_profile(model_name)

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
        "creeper": "크리퍼",
        "ender dragon": "엔더 드래곤",
        "redstone": "레드스톤",
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


KNOWN_GAME_UNITS = {
    'cf', 'cft', 'cf/t', 'rf', 'rft', 'rf/t', 'fe', 'fet', 'fe/t', 'eu', 'eut', 'eu/t',
    'mj', 'mjt', 'mj/t', 'mj/s', 'gp', 'ae', 'mb', 'mb/t', 'mb/s', 't', 'tick', 'ticks',
    's', 'sec', 'secs', 'min', 'mins', 'h', 'hr', 'hrs', 'k', 'm', 'g', 'b', 'kb', 'gb', 'tb',
    'deg', 'c', 'f', 'xp', 'lvl', 'bpm', 'rpm', 'v', 'a', 'w', 'kw', 'mw', 'gw', 'hz', 'khz', 'mhz',
    'ui', 'µi', 'µb', 'µs', 'µ', 'ctrl', 'shift', 'alt', 'esc', 'tab', 'enter', 'space',
    'i', 'ii', 'iii', 'iv', 'v', 'vi', 'vii', 'viii', 'ix', 'x', 'xi', 'xii'
}


def is_code_or_id(text):
    if not text or not str(text).strip():
        return True
    t = str(text).strip()

    # 1. 단일 토큰 콜론 식별자 (modid:item, buildcraftlib:guide 등)
    if (":" in t and " " not in t) or bool(re.match(r'^[0-9A-Fa-f]{8,}$', t)):
        return True

    # 2. 숫자, 소수점, 16진수, 치수/비율 규격 (0.5, 1.12.2, 0x1a4, 1x1, 3x3, 100%, +1, -1, 1-10)
    if re.match(r'^(?:0x[0-9a-fA-F]+|\d+x\d+|[+-]?\d+(?:\.\d+)*(?:-\d+(?:\.\d+)*)?%?)$', t):
        return True

    # 3. 마인크래프트 명령어 (/command ...)
    if t.startswith("/") and not t.startswith("//"):
        return True

    # 4. 마크다운 링크 및 시스템 태그 ($[...](...), <tag...>, {ftbquests.chapter...}, &color&, (PM2.5))
    if (t.startswith("$[") and "](" in t) or (t.startswith("<") and t.endswith(">") and " " not in t):
        return True
    # 중괄호로 감싸진 다국어 번역 키 / 플레이스홀더 (예: {ftbquests.chapter.6.quests0.title}, {image:...})
    if t.startswith("{") and t.endswith("}") and " " not in t and ("." in t or ":" in t or "_" in t):
        return True
    if re.match(r'^(?:[&§][0-9a-fk-orA-FK-OR])+[a-zA-Z0-9]?$', t):
        return True
    if re.match(r'^[(\[{<]*[&%][a-zA-Z0-9_.]+[&%][)\]}>]*$', t):
        return True
    if re.match(r'^\([A-Z0-9._-]+\)$', t):
        return True

    # 5. 수학/논리 함수 표현식 (sin(A), cos(A), sqrt(A), max(A, B) 등)
    if re.match(r'^(?:sin|cos|tan|asin|acos|atan|sqrt|log|ln|abs|min|max|ceil|floor)\([A-Za-z0-9_,\s]*\)$', t):
        return True

    # 6. 언어 코드 (en_US, ko_KR 등)
    if re.match(r'^[a-z]{2}_[A-Z]{2}$', t):
        return True

    # 7. 단일 문자나 아주 짧은 영문 기호/약어 (1글자 영문자 r, j, x, y, z 등 / ESD, NBT, GUI)
    if len(t) == 1 and t.isalpha() and t not in ("I", "A"):
        return True

    # 8. 유니코드 이스케이프 및 포맷터 제거 후 순수 글자 추출
    cleaned = re.sub(r'\\u[0-9a-fA-F]{4}', ' ', t)
    cleaned = re.sub(r'%(?:\d+\$)?[-+#0 ,]*\d*(?:\.\d+)?[a-zA-Z%]', ' ', cleaned)
    cleaned = re.sub(r'\{[0-9]+\}', ' ', cleaned)
    cleaned = re.sub(r'^(?:UUID|UID|URL|ID|IP)\s*:\s*%?s?$', ' ', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\b[xXyYzZ]\s*[:=,]\s*', ' ', cleaned)

    letters = re.findall(r'[^\W\d_]+', cleaned)
    if not letters:
        return True

    # 9. 대문자 약어 (ESD, ESDs, NBT, CF, ME, GUI, UUID 등)
    clean_abbr = t.rstrip('s') if t.endswith('s') and len(t) > 1 and t[:-1].isupper() else t
    if len(clean_abbr) <= 5 and clean_abbr.isupper() and clean_abbr not in ("I", "A"):
        return True

    # 10. 포맷터 및 단위/키보드/좌표계 전용 문자열 (%s, %smb / %smb, %s MJ/t, Ctrl, x, y, z)
    if all(w.lower() in KNOWN_GAME_UNITS or w.lower() in ('of', 'and', 'per', 'x', 'y', 'z', 'v') for w in letters):
        return True

    return False


def _sleep_with_cancel(duration: float, cancel_checker=None):
    """소수점 버림(truncation) 없이 정확한 시간(duration 초)만큼 대기하며 취소 요청을 0.1초 주기로 감지합니다."""
    if duration <= 0:
        return
    deadline = time.monotonic() + duration
    while True:
        if cancel_checker and cancel_checker():
            raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.1, remaining))


def _create_gemini_client(api_key: str):
    """google-genai SDK 자체의 내부 자동 재시도(기본 attempts=5)를 비활성화(attempts=1)하고,
    API 키별 Client 인스턴스를 캐싱하여 병렬 호출 시 HTTP/TLS 커넥션 풀을 재사용합니다."""
    if genai is None:
        return None
    with _gemini_client_lock:
        cached_client = _gemini_client_cache.get(api_key)
        if cached_client is not None:
            return cached_client

        if types is not None and hasattr(types, "HttpOptions") and hasattr(types, "HttpRetryOptions"):
            try:
                http_opts = types.HttpOptions(
                    retry_options=types.HttpRetryOptions(attempts=1)
                )
                client = genai.Client(api_key=api_key, http_options=http_opts)
                _gemini_client_cache[api_key] = client
                return client
            except Exception:
                pass
        client = genai.Client(api_key=api_key)
        _gemini_client_cache[api_key] = client
        return client


def _build_gemini_config(model_name: str):
    """모델별 호환성을 고려하여 GenerateContentConfig를 생성합니다.
    이전에 400(설정 미지원) 에러가 발생한 모델은 캐싱하여 불필요한 이중 호출을 방지합니다."""
    if not types or model_name in _unsupported_config_models:
        return None
    config_params = {"response_mime_type": "application/json"}
    m_low = str(model_name or "").lower()
    if hasattr(types, "ThinkingConfig"):
        try:
            if "2.5" in m_low or "thinking" in m_low:
                config_params["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
            elif any(v in m_low for v in ("gemini-3", "gemini-4", "hydrogen", "argon")):
                if hasattr(types, "ThinkingLevel"):
                    # Pro/Argon 계열은 MINIMAL 미지원 시 LOW 사용, Flash/Hydrogen 계열은 MINIMAL로 초고속 번역
                    lvl = types.ThinkingLevel.LOW if ("pro" in m_low or "argon" in m_low) else types.ThinkingLevel.MINIMAL
                    config_params["thinking_config"] = types.ThinkingConfig(thinking_level=lvl)
                else:
                    config_params["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        except Exception:
            pass
    try:
        return types.GenerateContentConfig(**config_params)
    except Exception:
        return None


def _invoke_gemini_generate_content(client, model_name: str, prompt: str, is_paid: bool = False, cancel_checker=None):
    """단일 Gemini generate_content 호출을 수행하며:
    1) 무료 계정(not is_paid)일 때 _gemini_api_rate_lock 내부에서
       - 직전 응답 완료(_last_gemini_api_call) 이후 최소 6.8초 간격 보장 (소수점 버림 없음)
       - 62초 슬라이딩 윈도우 내 최대 9회 요청 제한 보장 (구글 서버 60초 윈도우 10 RPM 절대 초과 방지)
       - 응답이 완전히 끝난 시점(finally)에 _last_gemini_api_call 및 슬라이딩 윈도우 타임스탬프 갱신
    2) 400(설정 미지원) 에러 발생 시 해당 모델을 _unsupported_config_models에 등록하고,
       재호출 전에도 동일하게 무료 속도 제한 대기를 거친 뒤 안전하게 호출합니다."""
    def _do_single_http_call(use_config: bool):
        cfg = _build_gemini_config(model_name) if use_config else None
        if not is_paid:
            _default_rate_limiter.acquire_slot(model_name, cancel_checker)
            try:
                translation_memory.record_api_call(1)
                if cfg is not None:
                    resp = client.models.generate_content(model=model_name, contents=prompt, config=cfg)
                else:
                    resp = client.models.generate_content(model=model_name, contents=prompt)
                try:
                    um = getattr(resp, "usage_metadata", None)
                    if um is not None:
                        in_t = getattr(um, "prompt_token_count", 0) or 0
                        out_t = (getattr(um, "candidates_token_count", 0) or 0) + (getattr(um, "thoughts_token_count", 0) or 0)
                        translation_memory.record_token_usage(in_t, out_t, model_name=model_name, is_paid=False)
                except Exception:
                    pass
                return resp
            finally:
                _default_rate_limiter.mark_finished()
        else:
            if cancel_checker and cancel_checker():
                raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")
            translation_memory.record_api_call(1)
            if cfg is not None:
                resp = client.models.generate_content(model=model_name, contents=prompt, config=cfg)
            else:
                resp = client.models.generate_content(model=model_name, contents=prompt)
            try:
                um = getattr(resp, "usage_metadata", None)
                if um is not None:
                    in_t = getattr(um, "prompt_token_count", 0) or 0
                    out_t = (getattr(um, "candidates_token_count", 0) or 0) + (getattr(um, "thoughts_token_count", 0) or 0)
                    translation_memory.record_token_usage(in_t, out_t, model_name=model_name, is_paid=True)
            except Exception:
                pass
            return resp

    use_cfg = model_name not in _unsupported_config_models
    try:
        return _do_single_http_call(use_config=use_cfg)
    except Exception as call_err:
        err_msg = str(call_err)
        if use_cfg and ("400" in err_msg or "INVALID_ARGUMENT" in err_msg or "not supported" in err_msg):
            # 해당 모델은 config 미지원으로 캐싱하여 이후 배치에서는 400 재발 원천 차단
            _unsupported_config_models.add(model_name)
            # 폴백 재호출 역시 무료 속도 제한(_do_single_http_call)을 정식으로 통과한 뒤 호출!
            return _do_single_http_call(use_config=False)
        raise call_err


def _is_billing_depleted_text(text: str) -> bool:
    """응답 본문/에러 메시지가 유료 크레딧(잔액) 소진(402/billing)인지 판별합니다."""
    low = str(text or "").lower()
    return any(k in low for k in (
        "prepayment",
        "credits are depleted",
        "insufficient_quota",
        "insufficient balance",
        "credit balance is too low",
        "exceeded your current quota",
        "payment required",
    ))


def _check_http_billing_error(response, engine_label: str):
    """HTTP 응답이 402(결제/크레딧 소진)이거나 잔액 부족 본문을 담고 있으면 즉시 402 예외를 발생시킵니다."""
    code = getattr(response, "status_code", 0)
    body = ""
    if code in (400, 402, 429):
        try:
            body = (response.text or "")[:400]
        except Exception:
            body = ""
    if code == 402 or (code in (400, 429) and _is_billing_depleted_text(body)):
        raise Exception(f"HTTP 402 billing_depleted: {body or engine_label}")


def _execute_batch_with_retry(
    engine_label: str,
    attempt_fn,
    fallback_result,
    *,
    is_paid: bool = False,
    model_name: str = "",
    log_callback=None,
    cancel_checker=None,
    rate_limit_wait_fn=None,
    quota_exceeded_msg: str = "",
    auth_error_msg: str = "",
):
    """모든 클라우드 배치 엔진(Gemini/OpenAI/Claude/DeepSeek)이 공유하는 통합 재시도·백오프·예외 처리 실행기.
    - 취소(TranslationCancelledError) 및 인증/모델 미지원 오류(401/404)는 즉시 상위로 전파
    - 일시적 서버 과부하(500/502/503/504/529/UNAVAILABLE/timeout)는 일반 재시도 횟수를 소모하지 않고 최대 4회 백오프 재시도
    - 429 한도 초과는 대기 후 재시도하고 한도 소진 시 QuotaExceededError 발생
    - 복구 불가 통신/서버 오류는 원문을 슬쩍 통과시키지 않고 예외를 상위로 전파하여 후속 단계 오동작 방지"""
    max_retries = 3
    max_overload_attempts = 4
    overload_attempts = 0
    attempt = 0

    def _diag(status: str, stage: str, detail: str):
        try:
            import session_logger
            session_logger.log_event(status, stage, f"{engine_label}({model_name or 'default'}) {detail}")
        except Exception:
            pass

    while attempt < max_retries:
        attempt += 1
        if cancel_checker and cancel_checker():
            raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")
        try:
            return attempt_fn()
        except TranslationCancelledError:
            raise
        except QuotaExceededError as qe:
            if attempt < max_retries:
                wait_time = rate_limit_wait_fn(attempt) if rate_limit_wait_fn else (8.0 * attempt)
                if engine_label == "Gemini":
                    _notify_gemini_backoff(wait_time)
                _diag("WARN", "API_429", f"retry={attempt}/{max_retries}, wait={wait_time:.0f}s")
                if log_callback:
                    log_callback(f"⚠️ [{engine_label} 요청 제한(429)] {wait_time:.0f}초 동안 대기 후 자동 재시도합니다... ({attempt}/{max_retries})")
                _sleep_with_cancel(wait_time, cancel_checker)
            else:
                _diag("FAIL", "API_QUOTA", str(qe)[:160])
                raise QuotaExceededError(quota_exceeded_msg or str(qe))
        except json.JSONDecodeError as je:
            if attempt < max_retries:
                _diag("WARN", "API_JSON_PARSE", f"retry={attempt}/{max_retries}")
                if log_callback:
                    log_callback(f"⚠️ [응답 파싱 실패] {engine_label} 응답이 올바른 JSON 형식이 아닙니다. 재시도합니다... ({attempt}/{max_retries})")
                _sleep_with_cancel(2.0, cancel_checker)
            else:
                _diag("FAIL", "API_JSON_PARSE", f"fallback_to_source: {str(je)[:120]}")
                if log_callback:
                    log_callback(f"⚠️ [번역 건너뜀] 응답 파싱에 계속 실패해 이 구간은 원문 그대로 둡니다. ({je})")
                return fallback_result
        except Exception as e:
            err = str(e)
            if "API_KEY_INVALID" in err or "401" in err or "UNAUTHENTICATED" in err or "유효하지 않습니다" in err:
                _diag("FAIL", "API_AUTH", "HTTP 401 / invalid key")
                raise Exception(auth_error_msg or f"{engine_label} API 키가 유효하지 않습니다. 올바른 API 키인지 확인해주세요.")
            if "404" in err or "NOT_FOUND" in err:
                _diag("FAIL", "API_MODEL_404", "model not found")
                raise Exception(f"선택한 {engine_label} AI 모델({model_name})을 찾을 수 없습니다. 지원되는 모델명을 선택해주세요.")
            if "402" in err or _is_billing_depleted_text(err):
                _diag("FAIL", "API_BILLING_402", err[:160])
                if log_callback:
                    log_callback(f"❌ [{engine_label} 결제 잔액 부족(402)] 유료 크레딧(잔액)이 모두 소진되었습니다. 결제 페이지에서 충전 후 다시 시도해주세요.")
                raise QuotaExceededError(f"{engine_label} 유료 결제 크레딧(잔액)이 모두 소진되었습니다. 결제 페이지에서 크레딧을 충전한 뒤 다시 시도해주세요.")
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                if attempt < max_retries:
                    wait_time = rate_limit_wait_fn(attempt) if rate_limit_wait_fn else (8.0 * attempt)
                    if engine_label == "Gemini":
                        _notify_gemini_backoff(wait_time)
                    _diag("WARN", "API_429", f"retry={attempt}/{max_retries}, wait={wait_time:.0f}s")
                    if log_callback:
                        log_callback(f"⚠️ [{engine_label} 요청 제한(429)] {wait_time:.0f}초 동안 대기 후 자동 재시도합니다... ({attempt}/{max_retries})")
                    _sleep_with_cancel(wait_time, cancel_checker)
                else:
                    _diag("FAIL", "API_QUOTA", "429 exhausted")
                    raise QuotaExceededError(quota_exceeded_msg or f"{engine_label} API 한도(429) 초과로 작업을 중단합니다.")
            elif any(k in err.lower() for k in ("503", "unavailable", "500", "502", "504", "529", "overloaded", "high demand", "timed out", "timeout", "connection")):
                if overload_attempts < max_overload_attempts:
                    overload_attempts += 1
                    attempt = max(0, attempt - 1)
                    wait_time = min(30, 8 * overload_attempts)
                    if engine_label == "Gemini":
                        _notify_gemini_backoff(wait_time)
                    _diag("WARN", "API_OVERLOAD", f"attempt={overload_attempts}/{max_overload_attempts}, wait={wait_time}s")
                    if log_callback:
                        log_callback(f"⚠️ [{engine_label} 서버 일시 과부하/지연] {wait_time}초 대기 후 자동 재시도합니다... ({overload_attempts}/{max_overload_attempts})")
                    _sleep_with_cancel(wait_time, cancel_checker)
                else:
                    _diag("FAIL", "API_OVERLOAD", f"exhausted: {err[:160]}")
                    raise Exception(f"{engine_label} 서버 일시 과부하/응답 지연으로 계속 실패하고 있습니다. 잠시 후 다시 시도해주세요. ({err})")
            else:
                if attempt < max_retries:
                    wait_time = 4.0 * attempt
                    _diag("WARN", "API_COMM", f"retry={attempt}/{max_retries}, wait={wait_time:.0f}s")
                    if log_callback:
                        log_callback(f"⚠️ [{engine_label} 통신 오류] {wait_time:.0f}초 대기 후 재시도합니다... ({attempt}/{max_retries})")
                    _sleep_with_cancel(wait_time, cancel_checker)
                else:
                    _diag("FAIL", "API_ERROR", err[:160])
                    raise Exception(f"{engine_label} 번역 실패 ({err})")

    return fallback_result


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

    client = _create_gemini_client(api_key)
    indexed_payload = {str(idx): txt for idx, txt in enumerate(unique_unresolved)}
    input_json = json.dumps(indexed_payload, ensure_ascii=False)
    
    target_prompt = LANG_CODES.get(target_lang, ("KO", "ko", "natural Korean"))[2]
    
    prompt = (
        f"Translate every string value in the input JSON object from English to {target_prompt} for Minecraft quests.\n"
        "Rules:\n"
        "- Return ONLY a valid JSON object with the exact same numeric string keys (\"0\", \"1\", ...) (no explanation, no markdown).\n"
        "- Do NOT skip, merge, or renumber any keys.\n"
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
        few_shots = translation_memory.find_few_shot_examples(unique_unresolved, target_lang, max_examples=2)
        if few_shots:
            prompt += "- Style Reference Examples from previous quests (Follow this tone, formatting, and style):\n"
            for src_ex, tgt_ex in few_shots:
                prompt += f"  * \"{src_ex}\" -> \"{tgt_ex}\"\n"
    except Exception:
        pass

    prompt += f"Input JSON: {input_json}"

    model_name = ai_model if ai_model else 'gemini-3.5-flash-lite'

    def _parse_gemini_json_response(raw_text: str):
        text_clean = (raw_text or "").strip()
        if text_clean.startswith("```"):
            text_clean = re.sub(r"^```[a-zA-Z]*\s*", "", text_clean)
            text_clean = re.sub(r"\s*```$", "", text_clean).strip()
        # 1차: 전체 텍스트 직접 파싱 시도 (텍스트 내 {color:...} 등 중괄호 오탐 방지)
        for candidate in (text_clean, re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', text_clean)):
            try:
                return json.loads(candidate, strict=False)
            except Exception:
                pass
        # 2차: 앞뒤 설명문 제거 후 JSON Object / Array 추출
        obj_match = re.search(r'\{.*\}', text_clean, re.DOTALL)
        arr_match = re.search(r'\[.*\]', text_clean, re.DOTALL)
        extracted = None
        if obj_match and (not arr_match or obj_match.start() <= arr_match.start()):
            extracted = obj_match.group(0)
        elif arr_match:
            extracted = arr_match.group(0)
        if extracted:
            try:
                return json.loads(extracted, strict=False)
            except Exception:
                cleaned = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', extracted)
                return json.loads(cleaned, strict=False)
        return json.loads(text_clean, strict=False)

    def _attempt_gemini():
        res = _invoke_gemini_generate_content(
            client=client,
            model_name=model_name,
            prompt=prompt,
            is_paid=is_paid,
            cancel_checker=cancel_checker,
        )

        raw_text = res.text.strip() if res.text else ""
        parsed = _parse_gemini_json_response(raw_text)

        # 1) ID 매핑 JSON Object 응답 처리 (인덱스 밀림 원천 차단)
        if isinstance(parsed, dict):
                translated_unique = [None] * len(unique_unresolved)
                missing_indices = []
                for idx_u in range(len(unique_unresolved)):
                    val = parsed.get(str(idx_u))
                    if val is None and idx_u in parsed:
                        val = parsed.get(idx_u)
                    if val is not None and str(val).strip() != "":
                        translated_unique[idx_u] = str(val)
                    else:
                        missing_indices.append(idx_u)

                # 매칭 성공한 항목은 즉시 메모리 캐시에 보존
                with _gemini_cache_lock:
                    if len(_gemini_cache) >= _GEMINI_CACHE_MAX:
                        _gemini_cache.clear()
                    for idx_u, dst_val in enumerate(translated_unique):
                        if dst_val is not None:
                            _gemini_cache[(unique_unresolved[idx_u], target_lang)] = dst_val

                # 일부 누락된 키가 있을 경우 누락된 항목만 별도로 1회 복구 호출
                if missing_indices and len(missing_indices) < len(unique_unresolved):
                    if log_callback:
                        log_callback(f"⚠️ [일부 키 누락] {len(unique_unresolved)}개 중 {len(missing_indices)}개 누락 → 누락분만 재번역합니다.")
                    missing_texts = [unique_unresolved[m_i] for m_i in missing_indices]
                    rescued = translate_gemini_batch(
                        missing_texts, api_key, is_paid=is_paid,
                        log_callback=log_callback, cancel_checker=cancel_checker,
                        reference_map=reference_map, glossary=glossary,
                        ai_model=ai_model, target_lang=target_lang
                    )
                    for m_i, r_val in zip(missing_indices, rescued):
                        translated_unique[m_i] = r_val
                elif missing_indices and len(missing_indices) == len(unique_unresolved):
                    raise json.JSONDecodeError("All keys missing in Gemini JSON object", raw_text, 0)

                merged = list(resolved)
                for original_pos, resolved_idx in enumerate(unresolved_indices):
                    unique_pos = unresolved_to_unique[original_pos]
                    val_out = translated_unique[unique_pos]
                    merged[resolved_idx] = val_out if val_out is not None else unique_unresolved[unique_pos]
                return merged

        # 2) 하위 호환: 모델이 JSON Array로 응답한 경우에도 안전하게 처리
        if isinstance(parsed, list) and len(parsed) == len(unique_unresolved):
            with _gemini_cache_lock:
                if len(_gemini_cache) >= _GEMINI_CACHE_MAX:
                    _gemini_cache.clear()
                for src, dst in zip(unique_unresolved, parsed):
                    _gemini_cache[(src, target_lang)] = str(dst)

            merged = list(resolved)
            for original_pos, resolved_idx in enumerate(unresolved_indices):
                unique_pos = unresolved_to_unique[original_pos]
                merged[resolved_idx] = parsed[unique_pos]
            return merged

        # 대용량 배치에서 배열 길이가 어긋난 경우 절반씩 분할 재시도하여 번역 유실 방지
        if len(unique_unresolved) >= 4:
            if log_callback:
                got_len = len(parsed) if isinstance(parsed, list) else 0
                log_callback(f"⚠️ [배치 개수 불일치] 요청 {len(unique_unresolved)}개 / 응답 {got_len}개 → 절반으로 분할하여 안전하게 재시도합니다.")
            mid = len(unique_unresolved) // 2
            left_res = translate_gemini_batch(
                unique_unresolved[:mid], api_key, is_paid=is_paid,
                log_callback=log_callback, cancel_checker=cancel_checker,
                reference_map=reference_map, glossary=glossary,
                ai_model=ai_model, target_lang=target_lang
            )
            right_res = translate_gemini_batch(
                unique_unresolved[mid:], api_key, is_paid=is_paid,
                log_callback=log_callback, cancel_checker=cancel_checker,
                reference_map=reference_map, glossary=glossary,
                ai_model=ai_model, target_lang=target_lang
            )
            combined_unique = list(left_res) + list(right_res)
            merged = list(resolved)
            for original_pos, resolved_idx in enumerate(unresolved_indices):
                unique_pos = unresolved_to_unique[original_pos]
                merged[resolved_idx] = combined_unique[unique_pos]
            return merged
        return [item if item is not None else text for item, text in zip(resolved, text_list)]

    fallback_result = [item if item is not None else text for item, text in zip(resolved, text_list)]
    return _execute_batch_with_retry(
        "Gemini",
        _attempt_gemini,
        fallback_result,
        is_paid=is_paid,
        model_name=model_name,
        log_callback=log_callback,
        cancel_checker=cancel_checker,
        rate_limit_wait_fn=lambda att: (15 * att if is_paid else 65 * att),
        quota_exceeded_msg="Gemini API 한도(RPM/RPD) 초과로 더 이상 진행할 수 없습니다. 작업을 중단합니다.",
        auth_error_msg="Gemini API 키가 유효하지 않습니다. 올바른 API 키인지 확인해주세요.",
    )


def _resolve_batch_cache(text_list, reference_map=None, target_lang="한국어 (Korean)"):
    """배치 번역 전 용어집/메모리/인메모리 캐시를 조회하고 고유 미번역 항목을 추출합니다."""
    resolved = []
    unresolved_indices = []
    unresolved_raw = []

    for text in text_list:
        cached = get_reference_translation(text, reference_map)
        if cached is not None:
            resolved.append(cached)
            continue
        mem_cached = translation_memory.get_cached_translation(text, target_lang)
        if mem_cached is not None:
            resolved.append(mem_cached)
            continue
        cache_key = (text, target_lang)
        with _gemini_cache_lock:
            cached_local = _gemini_cache.get(cache_key)
        if cached_local is not None:
            resolved.append(cached_local)
        else:
            resolved.append(None)
            unresolved_indices.append(len(resolved) - 1)
            unresolved_raw.append(text)

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

    return resolved, unresolved_indices, unique_unresolved, unresolved_to_unique


def _build_chat_batch_prompt(unique_unresolved, target_lang, glossary=None, use_id_mapping=True):
    """Claude / DeepSeek / Local AI 배치 호출용 시스템 프롬프트와 입력 JSON 문자열을 생성합니다."""
    target_prompt = LANG_CODES.get(target_lang, ("KO", "ko", "natural Korean"))[2]
    if use_id_mapping:
        input_json = json.dumps({str(i): t for i, t in enumerate(unique_unresolved)}, ensure_ascii=False)
        system_prompt = (
            f"Translate every string value in the input JSON object from English to {target_prompt} for Minecraft quests.\n"
            "Rules:\n"
            "- Return ONLY a valid JSON object with the exact same numeric string keys (\"0\", \"1\", ...) (no explanation, no markdown).\n"
            "- Do NOT skip, merge, or renumber any keys.\n"
            "- Preserve formatting/placeholders exactly (&a, §c, {color:...}, %s, {0}, <...>).\n"
            "- NEVER use square brackets [] around translated words.\n"
            "- Preserve abbreviations like 'Lv.', 'HP', 'MP', 'ATK', 'x2'.\n"
        )
    else:
        input_json = json.dumps(unique_unresolved, ensure_ascii=False)
        system_prompt = (
            f"Translate the input JSON array from English to {target_prompt} for Minecraft quests.\n"
            "Rules:\n"
            "- Return ONLY a valid JSON array matching the exact order and length of the input.\n"
            "- Output MUST start with '[' and end with ']'.\n"
            "- Preserve formatting/placeholders exactly (&a, §c, {color:...}, %s, {0}, <...>).\n"
            "- NEVER use square brackets [] around translated words.\n"
            "- Preserve abbreviations like 'Lv.', 'HP', 'MP', 'ATK', 'x2'.\n"
        )
    if glossary:
        clean_glossary = [f"'{k}' as '{v.split('#')[0].strip()}'" for k, v in glossary.items()]
        system_prompt += f"- Glossary (Strictly apply): {', '.join(clean_glossary)}\n"
    return system_prompt, input_json


def _merge_batch_json_response(raw_text, text_list, resolved, unresolved_indices, unique_unresolved, unresolved_to_unique, target_lang, log_callback=None, retry_fn=None):
    """JSON Object(ID 매핑) 또는 JSON Array 응답을 파싱하여 캐시에 저장하고 최종 결과 리스트를 반환합니다."""
    text_clean = (raw_text or "").strip()
    if text_clean.startswith("```"):
        text_clean = re.sub(r"^```[a-zA-Z]*\s*", "", text_clean)
        text_clean = re.sub(r"\s*```$", "", text_clean).strip()

    parsed = None
    for cand in (text_clean, re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', text_clean)):
        try:
            parsed = json.loads(cand, strict=False)
            break
        except Exception:
            pass
    if parsed is None:
        obj_m = re.search(r'\{.*\}', text_clean, re.DOTALL)
        arr_m = re.search(r'\[.*\]', text_clean, re.DOTALL)
        extracted = obj_m.group(0) if (obj_m and (not arr_m or obj_m.start() <= arr_m.start())) else (arr_m.group(0) if arr_m else None)
        if extracted:
            parsed = json.loads(re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', extracted), strict=False)

    if isinstance(parsed, dict):
        translated_unique = [None] * len(unique_unresolved)
        missing_indices = []
        for idx_u in range(len(unique_unresolved)):
            val = parsed.get(str(idx_u))
            if val is None and idx_u in parsed:
                val = parsed.get(idx_u)
            if val is not None and str(val).strip() != "":
                translated_unique[idx_u] = str(val)
            else:
                missing_indices.append(idx_u)

        with _gemini_cache_lock:
            if len(_gemini_cache) >= _GEMINI_CACHE_MAX:
                _gemini_cache.clear()
            for idx_u, dst_val in enumerate(translated_unique):
                if dst_val is not None:
                    _gemini_cache[(unique_unresolved[idx_u], target_lang)] = dst_val

        if missing_indices and len(missing_indices) < len(unique_unresolved) and retry_fn is not None:
            if log_callback:
                log_callback(f"⚠️ [일부 키 누락] {len(unique_unresolved)}개 중 {len(missing_indices)}개 누락 → 누락분만 재번역합니다.")
            rescued = retry_fn([unique_unresolved[m_i] for m_i in missing_indices])
            for m_i, r_val in zip(missing_indices, rescued):
                translated_unique[m_i] = r_val

        merged = list(resolved)
        for original_pos, resolved_idx in enumerate(unresolved_indices):
            unique_pos = unresolved_to_unique[original_pos]
            val_out = translated_unique[unique_pos]
            merged[resolved_idx] = val_out if val_out is not None else unique_unresolved[unique_pos]
        return merged

    if isinstance(parsed, list) and len(parsed) == len(unique_unresolved):
        with _gemini_cache_lock:
            if len(_gemini_cache) >= _GEMINI_CACHE_MAX:
                _gemini_cache.clear()
            for src, dst in zip(unique_unresolved, parsed):
                _gemini_cache[(src, target_lang)] = str(dst)

        merged = list(resolved)
        for original_pos, resolved_idx in enumerate(unresolved_indices):
            unique_pos = unresolved_to_unique[original_pos]
            merged[resolved_idx] = parsed[unique_pos]
        return merged

    if log_callback:
        log_callback("⚠️ [응답 파싱 실패] AI 응답이 올바른 JSON 형식이 아니어 원문으로 대체됩니다.")
    return [item if item is not None else text for item, text in zip(resolved, text_list)]


def translate_openai_batch(text_list, api_key, reference_map=None, glossary=None, ai_model=None, target_lang="한국어 (Korean)", log_callback=None, cancel_checker=None):
    """OpenAI (ChatGPT) 전용 ID 매핑 JSON Object 배치 번역 엔진.
    단건 호출 대비 고정 프롬프트·용어집 중복 과금을 수십 분의 1로 줄이고 커넥션 풀을 재사용합니다."""
    if not text_list:
        return []
    if MOCK_MODE:
        if log_callback:
            log_callback(f"🧪 [Mock Mode] {len(text_list)}개 텍스트를 모의 번역합니다.")
        time.sleep(0.1)
        return _mock_translate_batch(text_list)

    resolved, unresolved_indices, unique_unresolved, unresolved_to_unique = _resolve_batch_cache(
        text_list, reference_map=reference_map, target_lang=target_lang
    )
    if not unique_unresolved:
        return resolved

    model_name = ai_model if ai_model else "gpt-6-luna"
    system_prompt, input_json = _build_chat_batch_prompt(unique_unresolved, target_lang, glossary=glossary, use_id_mapping=True)

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key or ''}",
    }
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Input JSON: {input_json}"},
        ],
        "response_format": {"type": "json_object"},
    }
    if any(model_name.startswith(p) for p in ("o1", "o3", "o4", "gpt-5", "gpt-6")):
        payload["reasoning_effort"] = "low"
    else:
        payload["temperature"] = 0.2

    sess = _get_http_session()

    def _attempt_openai():
        translation_memory.record_api_call(1)
        res = sess.post(url, headers=headers, json=payload, timeout=180)
        _check_http_billing_error(res, "OpenAI")
        if res.status_code == 429:
            raise QuotaExceededError("OpenAI API 할당량이 초과되었거나 요청이 너무 빠릅니다.")
        if res.status_code == 401:
            raise Exception("OpenAI API 키가 유효하지 않습니다.")
        if res.status_code in (500, 502, 503, 504):
            raise Exception(f"HTTP {res.status_code} unavailable")
        res.raise_for_status()
        res_json = res.json()
        try:
            u = res_json.get("usage") or {}
            translation_memory.record_token_usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0), model_name=model_name, is_paid=True)
        except Exception:
            pass
        raw_text = res_json["choices"][0]["message"]["content"].strip()

        return _merge_batch_json_response(
            raw_text, text_list, resolved, unresolved_indices, unique_unresolved, unresolved_to_unique,
            target_lang, log_callback=log_callback,
            retry_fn=lambda missing: translate_openai_batch(
                missing, api_key, reference_map=reference_map, glossary=glossary,
                ai_model=model_name, target_lang=target_lang,
                log_callback=log_callback, cancel_checker=cancel_checker
            )
        )

    fallback_result = [item if item is not None else text for item, text in zip(resolved, text_list)]
    return _execute_batch_with_retry(
        "OpenAI",
        _attempt_openai,
        fallback_result,
        is_paid=True,
        model_name=model_name,
        log_callback=log_callback,
        cancel_checker=cancel_checker,
        rate_limit_wait_fn=lambda att: 6.0 * att,
        quota_exceeded_msg="OpenAI API 할당량이 초과되었거나 요청이 너무 빠릅니다.",
        auth_error_msg="OpenAI API 키가 유효하지 않습니다.",
    )


def translate_claude(text_list, api_key, ai_model=None, log_callback=None, cancel_checker=None, reference_map=None, glossary=None, target_lang="한국어 (Korean)"):
    """Anthropic (Claude) 전용 배치 번역 엔진.
    Prompt Caching(ephemeral), 429/529 과부하 백오프 재시도, 커넥션 풀링을 적용합니다."""
    if not text_list:
        return []
    if MOCK_MODE:
        if log_callback:
            log_callback(f"🧪 [Mock Mode] {len(text_list)}개 텍스트를 모의 번역합니다.")
        time.sleep(0.1)
        return _mock_translate_batch(text_list)

    resolved, unresolved_indices, unique_unresolved, unresolved_to_unique = _resolve_batch_cache(
        text_list, reference_map=reference_map, target_lang=target_lang
    )
    if not unique_unresolved:
        return resolved

    model_name = ai_model or "claude-haiku-5-5"
    system_prompt, input_json = _build_chat_batch_prompt(unique_unresolved, target_lang, glossary=glossary, use_id_mapping=True)

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key or "",
        "anthropic-version": "2023-06-01",
    }
    payload = {
        "model": model_name,
        "max_tokens": 8192,
        "system": [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": f"Input JSON: {input_json}"}],
        "temperature": 0.1,
    }

    sess = _get_http_session()

    def _attempt_claude():
        # Tier 1(50 RPM)만 보수적 리미터 적용, Tier 2+(1,000+ RPM)는 대기 없이 고속 처리
        if _paid_api_tier <= 1:
            _claude_rate_limiter.acquire_slot(cancel_checker)
        translation_memory.record_api_call(1)
        response = sess.post(url, headers=headers, json=payload, timeout=180)
        _check_http_billing_error(response, "Claude")
        if response.status_code == 429:
            raise QuotaExceededError("Anthropic (Claude) API 요청 한도(429)에 도달했습니다.")
        if response.status_code == 401:
            raise Exception("Anthropic (Claude) API 키가 유효하지 않습니다.")
        if response.status_code in (500, 502, 503, 504, 529):
            raise Exception(f"HTTP {response.status_code} overloaded")
        response.raise_for_status()
        data = response.json()
        try:
            u = data.get("usage") or {}
            translation_memory.record_token_usage(u.get("input_tokens", 0), u.get("output_tokens", 0), model_name=model_name, is_paid=True)
        except Exception:
            pass
        raw_text = data["content"][0]["text"].strip()

        return _merge_batch_json_response(
            raw_text, text_list, resolved, unresolved_indices, unique_unresolved, unresolved_to_unique,
            target_lang, log_callback=log_callback,
            retry_fn=lambda missing: translate_claude(
                missing, api_key, ai_model=model_name, log_callback=log_callback,
                cancel_checker=cancel_checker, reference_map=reference_map,
                glossary=glossary, target_lang=target_lang
            )
        )

    fallback_result = [item if item is not None else text for item, text in zip(resolved, text_list)]
    return _execute_batch_with_retry(
        "Claude",
        _attempt_claude,
        fallback_result,
        is_paid=True,
        model_name=model_name,
        log_callback=log_callback,
        cancel_checker=cancel_checker,
        rate_limit_wait_fn=lambda att: 8.0 * att,
        quota_exceeded_msg="Anthropic (Claude) API 요청 한도(429)에 도달했습니다.",
        auth_error_msg="Anthropic (Claude) API 키가 유효하지 않습니다.",
    )


def translate_deepseek(text_list, api_key, ai_model=None, log_callback=None, cancel_checker=None, reference_map=None, glossary=None, target_lang="한국어 (Korean)"):
    """DeepSeek 전용 배치 번역 엔진.
    고정 시스템 프롬프트(Prefix Cache 할인), 429/503 백오프 재시도, 커넥션 풀링을 적용합니다."""
    if not text_list:
        return []
    if MOCK_MODE:
        if log_callback:
            log_callback(f"🧪 [Mock Mode] {len(text_list)}개 텍스트를 모의 번역합니다.")
        time.sleep(0.1)
        return _mock_translate_batch(text_list)

    resolved, unresolved_indices, unique_unresolved, unresolved_to_unique = _resolve_batch_cache(
        text_list, reference_map=reference_map, target_lang=target_lang
    )
    if not unique_unresolved:
        return resolved

    model_name = ai_model or "deepseek-flash"
    system_prompt, input_json = _build_chat_batch_prompt(unique_unresolved, target_lang, glossary=glossary, use_id_mapping=True)

    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key or ''}",
    }
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Input JSON: {input_json}"},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
    }

    sess = _get_http_session()

    def _attempt_deepseek():
        translation_memory.record_api_call(1)
        response = sess.post(url, headers=headers, json=payload, timeout=180)
        _check_http_billing_error(response, "DeepSeek")
        if response.status_code == 429:
            raise QuotaExceededError("DeepSeek API 요청 한도(429)에 도달했습니다.")
        if response.status_code == 401:
            raise Exception("DeepSeek API 키가 유효하지 않습니다.")
        if response.status_code in (500, 502, 503, 504):
            raise Exception(f"HTTP {response.status_code} unavailable")
        response.raise_for_status()
        data = response.json()
        try:
            u = data.get("usage") or {}
            translation_memory.record_token_usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0), model_name=model_name, is_paid=True)
        except Exception:
            pass
        raw_text = data["choices"][0]["message"]["content"].strip()

        return _merge_batch_json_response(
            raw_text, text_list, resolved, unresolved_indices, unique_unresolved, unresolved_to_unique,
            target_lang, log_callback=log_callback,
            retry_fn=lambda missing: translate_deepseek(
                missing, api_key, ai_model=model_name, log_callback=log_callback,
                cancel_checker=cancel_checker, reference_map=reference_map,
                glossary=glossary, target_lang=target_lang
            )
        )

    fallback_result = [item if item is not None else text for item, text in zip(resolved, text_list)]
    return _execute_batch_with_retry(
        "DeepSeek",
        _attempt_deepseek,
        fallback_result,
        is_paid=True,
        model_name=model_name,
        log_callback=log_callback,
        cancel_checker=cancel_checker,
        rate_limit_wait_fn=lambda att: 6.0 * att,
        quota_exceeded_msg="DeepSeek API 요청 한도(429)에 도달했습니다.",
        auth_error_msg="DeepSeek API 키가 유효하지 않습니다.",
    )


def translate_local_ai(text_list, base_url, model_name, api_key=None, log_callback=None, cancel_checker=None, reference_map=None, glossary=None, target_lang="한국어 (Korean)"):
    """로컬 LLM(Ollama / LM Studio 등) 및 커스텀 OpenAI 호환 서버 전용 번역 엔진."""
    if not text_list:
        return []
    if MOCK_MODE:
        if log_callback:
            log_callback(f"🧪 [Mock Mode] {len(text_list)}개 텍스트를 모의 번역합니다.")
        time.sleep(0.1)
        return _mock_translate_batch(text_list)

    resolved, unresolved_indices, unique_unresolved, unresolved_to_unique = _resolve_batch_cache(
        text_list, reference_map=reference_map, target_lang=target_lang
    )
    if not unique_unresolved:
        return resolved

    system_prompt, input_json = _build_chat_batch_prompt(unique_unresolved, target_lang, glossary=glossary, use_id_mapping=False)
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Input JSON: {input_json}"},
        ],
        "temperature": 0.1,
    }

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    if not base_url or not str(base_url).strip():
        raise Exception("로컬 AI 서버 URL이 설정되지 않았습니다. 설정에서 로컬 AI 주소를 확인해주세요.")
    url = str(base_url).strip().rstrip("/")
    if "169.254." in url:
        if log_callback:
            log_callback("⚠️ [보안 차단] 클라우드 메타데이터 IP(169.254.x.x)는 SSRF 방지를 위해 사용할 수 없습니다.")
        return [item if item is not None else text for item, text in zip(resolved, text_list)]

    if not url.endswith("/chat/completions"):
        url += "/chat/completions"

    try:
        if cancel_checker and cancel_checker():
            raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")

        translation_memory.record_api_call(1)
        response = requests.post(url, headers=headers, json=payload, timeout=300)
        if response.status_code == 402 or (response.status_code in (400, 429) and _is_billing_depleted_text(response.text)):
            try:
                import session_logger
                session_logger.log_event("FAIL", "API_BILLING_402", f"Custom/LocalAI({model_name}) HTTP {response.status_code}")
            except Exception:
                pass
            if log_callback:
                log_callback("❌ [Custom API 결제 잔액 부족(402)] 크레딧(잔액)이 모두 소진되었습니다. 충전 후 다시 시도해주세요.")
            raise QuotaExceededError("Custom API 유료 결제 크레딧(잔액)이 모두 소진되었습니다. 결제 페이지에서 크레딧을 충전한 뒤 다시 시도해주세요.")
        response.raise_for_status()
        data = response.json()
        try:
            u = data.get("usage") or {}
            translation_memory.record_token_usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0), model_name=model_name, is_paid=False)
        except Exception:
            pass
        raw_text = data["choices"][0]["message"]["content"].strip()

        return _merge_batch_json_response(
            raw_text, text_list, resolved, unresolved_indices, unique_unresolved, unresolved_to_unique,
            target_lang, log_callback=log_callback
        )
    except (TranslationCancelledError, QuotaExceededError):
        raise
    except Exception as e:
        if log_callback:
            log_callback(f"⚠️ [통신/파싱 에러] 로컬 AI 호출 실패: {str(e)}")
        return [item if item is not None else text for item, text in zip(resolved, text_list)]


ENGINES = {
    "Google AI (Gemini)": "gemini_batch",
    "OpenAI (ChatGPT)": "openai",
    "Anthropic (Claude)": "claude",
    "DeepSeek (딥시크)": "deepseek",
    "Custom API (Local AI 등)": "local_ai",
    "🧪 테스트 모드 (Mock - API 미호출)": "mock",
}


def get_available_engines():
    """샌드박스 디버그 모드 여부에 따라 UI에 노출할 번역 엔진 목록을 반환합니다.
    디버그 모드가 켜져 있을 때만 '🧪 테스트 모드 (Mock)'를 노출합니다."""
    try:
        import temp_session_logger
        debug_on = temp_session_logger.is_debug_mode()
    except Exception:
        debug_on = False

    engines = {
        "Google AI (Gemini)": "gemini_batch",
        "OpenAI (ChatGPT)": "openai",
        "Anthropic (Claude)": "claude",
        "DeepSeek (딥시크)": "deepseek",
        "Custom API (Local AI 등)": "local_ai",
    }
    if debug_on:
        engines["🧪 테스트 모드 (Mock - API 미호출)"] = "mock"
    return engines


def fetch_local_ai_models(base_url: str, api_key: str = None, timeout: float = 3.0) -> list:
    """로컬 LLM (LM Studio, Ollama, vLLM, Jan 등) 또는 OpenAI 호환 서버에서 모델 목록을 자동으로 감지합니다."""
    if not base_url or not str(base_url).strip():
        return []
    url = str(base_url).strip().rstrip("/")
    if "169.254." in url:
        return []

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # 시도할 엔드포인트 후보군
    candidate_urls = []
    if url.endswith("/v1"):
        candidate_urls.append(f"{url}/models")
        root_url = url[:-3].rstrip("/")
        candidate_urls.append(f"{root_url}/api/tags")
    else:
        candidate_urls.append(f"{url}/v1/models")
        candidate_urls.append(f"{url}/models")
        candidate_urls.append(f"{url}/api/tags")

    found_models = []
    for cand_url in candidate_urls:
        try:
            resp = requests.get(cand_url, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                # 1. OpenAI 호환 포맷: {"data": [{"id": "..."}, ...]}
                if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                    for item in data["data"]:
                        if isinstance(item, dict) and "id" in item:
                            found_models.append(str(item["id"]))
                # 2. Ollama 네이티브 포맷: {"models": [{"name": "..."}, ...]}
                elif isinstance(data, dict) and "models" in data and isinstance(data["models"], list):
                    for item in data["models"]:
                        if isinstance(item, dict) and "name" in item:
                            found_models.append(str(item["name"]))
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, str):
                            found_models.append(item)
                        elif isinstance(item, dict) and "id" in item:
                            found_models.append(str(item["id"]))

                if found_models:
                    # 중복 제거 및 정렬
                    seen = set()
                    unique_models = []
                    for m in found_models:
                        if m not in seen:
                            seen.add(m)
                            unique_models.append(m)
                    return unique_models
        except Exception:
            continue

    return []


def auto_extract_glossary(text_samples, engine_key, api_key, ai_model=None, target_lang="한국어 (Korean)", custom_url=None, is_paid=False, cancel_checker=None):
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
            client = _create_gemini_client(api_key)
            model_name = ai_model if ai_model else 'gemini-3.5-flash-lite'
            res = _invoke_gemini_generate_content(
                client=client,
                model_name=model_name,
                prompt=prompt,
                is_paid=is_paid,
                cancel_checker=cancel_checker,
            )
            raw_text = res.text.strip() if res.text else ""
        elif engine_key == "claude":
            model_name = ai_model if ai_model else "claude-haiku-5-5"
            headers = {
                "Content-Type": "application/json",
                "x-api-key": api_key or "",
                "anthropic-version": "2023-06-01",
            }
            payload = {"model": model_name, "max_tokens": 2048, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3}
            translation_memory.record_api_call(1)
            res = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=30)
            res.raise_for_status()
            raw_text = res.json()["content"][0]["text"].strip()
        elif engine_key in ("openai", "deepseek", "local_ai"):
            if engine_key == "local_ai":
                if not custom_url or not str(custom_url).strip():
                    return {}
                url = str(custom_url).strip().rstrip("/")
            elif engine_key == "deepseek":
                url = "https://api.deepseek.com/v1"
            else:
                url = "https://api.openai.com/v1"
            if "169.254." in url:
                import logging
                logging.warning("SSRF blocked: 169.254.x.x is not allowed.")
                return {}
            if not url.endswith("/chat/completions"):
                url += "/chat/completions"
            
            default_m = "gpt-6-luna" if engine_key == "openai" else ("deepseek-flash" if engine_key == "deepseek" else "llama")
            model_name = ai_model if ai_model else default_m
            headers = {"Content-Type": "application/json"}
            if api_key: headers["Authorization"] = f"Bearer {api_key}"
            payload = {"model": model_name, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3}
            translation_memory.record_api_call(1)
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
    except TranslationCancelledError:
        raise
    except Exception as e:
        import logging
        logging.warning(f"Auto glossary extraction failed: {e}")
        
    return extracted


# ============================================================
# 1차 대분류 클래스: EngineConfig + TranslationEngine
# ============================================================
@dataclass
class EngineConfig:
    """번역 엔진 호출에 필요한 설정(엔진·키·유무료·모델·언어·로컬URL)을 하나로 묶은 불변/표준 설정 객체."""
    engine_key: str = "gemini_batch"
    api_key: str = ""
    is_paid: bool = False
    ai_model: Optional[str] = None
    target_lang: str = "한국어 (Korean)"
    custom_url: Optional[str] = None


class BatchApiClient:
    """공용 HTTP/Gemini 클라이언트 풀, 통합 재시도 실행기, 5개 엔진 배치 호출 디스패치를 전담합니다."""

    def __init__(self, rate_limiter: Optional[GeminiRateLimiter] = None):
        self.rate_limiter = rate_limiter or _default_rate_limiter

    def get_http_session(self) -> requests.Session:
        return _get_http_session()

    def get_gemini_client(self, api_key: str):
        return _create_gemini_client(api_key)

    def invoke_gemini(self, client, model_name: str, prompt: str, is_paid: bool = False, cancel_checker=None):
        return _invoke_gemini_generate_content(
            client, model_name, prompt, is_paid=is_paid, cancel_checker=cancel_checker
        )

    def execute_with_retry(self, engine_label: str, attempt_fn, fallback_result, **kwargs):
        return _execute_batch_with_retry(engine_label, attempt_fn, fallback_result, **kwargs)

    def translate_batch(
        self,
        text_list,
        config: EngineConfig,
        *,
        log_callback=None,
        cancel_checker=None,
        reference_map=None,
        glossary=None,
    ):
        if not text_list:
            return []
        if MOCK_MODE or config.engine_key == "mock":
            return _mock_translate_batch(text_list)

        if config.engine_key == "local_ai":
            return translate_local_ai(
                text_list, config.custom_url, config.ai_model,
                api_key=config.api_key, log_callback=log_callback,
                cancel_checker=cancel_checker, reference_map=reference_map,
                glossary=glossary, target_lang=config.target_lang,
            )
        if config.engine_key == "deepseek":
            return translate_deepseek(
                text_list, config.api_key, ai_model=config.ai_model,
                log_callback=log_callback, cancel_checker=cancel_checker,
                reference_map=reference_map, glossary=glossary,
                target_lang=config.target_lang,
            )
        if config.engine_key == "claude":
            return translate_claude(
                text_list, config.api_key, ai_model=config.ai_model,
                log_callback=log_callback, cancel_checker=cancel_checker,
                reference_map=reference_map, glossary=glossary,
                target_lang=config.target_lang,
            )
        if config.engine_key == "openai":
            return translate_openai_batch(
                text_list, config.api_key, reference_map=reference_map,
                glossary=glossary, ai_model=config.ai_model,
                target_lang=config.target_lang, log_callback=log_callback,
                cancel_checker=cancel_checker,
            )
        return translate_gemini_batch(
            text_list, config.api_key, is_paid=config.is_paid,
            log_callback=log_callback, cancel_checker=cancel_checker,
            reference_map=reference_map, glossary=glossary,
            ai_model=config.ai_model, target_lang=config.target_lang,
        )

    def fetch_local_models(self, base_url: str, api_key: str = None, timeout: float = 3.0) -> list:
        return fetch_local_ai_models(base_url, api_key=api_key, timeout=timeout)


class TranslationEngine:
    """모든 번역 엔진(Gemini/OpenAI/Claude/DeepSeek/Local AI)의 배치 번역·용어 추출·RPM 관리를 통합 제공하는 단일 진입점."""

    def __init__(
        self,
        rate_limiter: Optional[GeminiRateLimiter] = None,
        api_client: Optional[BatchApiClient] = None,
    ):
        self.rate_limiter = rate_limiter or _default_rate_limiter
        self.api_client = api_client or BatchApiClient(self.rate_limiter)

    def get_free_rpm_profile(self, model_name: str):
        return self.rate_limiter.get_profile(model_name)

    def notify_backoff(self, wait_seconds: float):
        self.rate_limiter.notify_backoff(wait_seconds)

    def translate_batch(
        self,
        text_list,
        config: EngineConfig,
        *,
        log_callback=None,
        cancel_checker=None,
        reference_map=None,
        glossary=None,
    ):
        return self.api_client.translate_batch(
            text_list,
            config,
            log_callback=log_callback,
            cancel_checker=cancel_checker,
            reference_map=reference_map,
            glossary=glossary,
        )

    def extract_glossary(self, text_samples, config: EngineConfig, cancel_checker=None):
        return auto_extract_glossary(
            text_samples,
            config.engine_key,
            config.api_key,
            ai_model=config.ai_model,
            target_lang=config.target_lang,
            custom_url=config.custom_url,
            is_paid=config.is_paid,
            cancel_checker=cancel_checker,
        )

    def fetch_local_models(self, base_url: str, api_key: str = None, timeout: float = 3.0) -> list:
        return self.api_client.fetch_local_models(base_url, api_key=api_key, timeout=timeout)


default_engine = TranslationEngine()
