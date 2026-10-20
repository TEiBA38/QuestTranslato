import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

from hqm_parser import HQMQuestConverter, filter_text
from translation_engines import (
    TranslationCancelledError,
    is_code_or_id,
    normalize_reference_text,
    translate_deepl,
    translate_google,
    translate_openai,
    translate_gemini_batch,
    translate_local_ai,
)

import translation_memory
import translation_word
import temp_session_logger
from constants import has_non_latin
from translation_word import sanitize_minecraft_formatting


def _build_chunk_glossary(texts_chunk, base_glossary=None):

    """
    번역할 청크(texts_chunk)에 실제로 등장하는 순수 고유명사(Proper Nouns)만 추출하여
    최소한의 토큰으로 동적 용어집(Dynamic Glossary)을 구성합니다.
    """
    dynamic_proper = translation_word.extract_proper_noun_glossary(texts_chunk)
    if not base_glossary and not dynamic_proper:
        return None

    merged = {}
    if isinstance(base_glossary, dict):
        # 기본 용어집에서도 문맥 오염을 유발하는 단일 일반 영단어(동음이의어)는 배제하고,
        # 복합 명사구이거나 고유명사 사전에 부합하는 항목만 선별
        chunk_joined_lower = " ".join(texts_chunk).lower()
        for k, v in base_glossary.items():
            k_str = str(k).strip()
            if not k_str:
                continue
            # 단어 경계(\b) 매칭: 'per'가 'copper' 등에 부분일치하지 않도록 독립 단어 검사
            pattern = r'\b' + re.escape(k_str.lower()) + r'\b'
            if not re.search(pattern, chunk_joined_lower):
                continue
            # 2단어 이상 고유 명칭이거나 일반 어휘가 아닌 경우만 허용
            if " " in k_str or not translation_word.is_base_english_word(k_str):
                merged[k_str] = str(v).split("#")[0].strip()

    # translation_word의 검증된 고유명사 사전이 최우선
    for en_low, ko_val in dynamic_proper.items():
        merged[en_low] = ko_val

    return merged if merged else None


def _run_batch_jobs(items, text_extractor, engine_key, api_key, is_paid, log_callback, cancel_checker, progress_callback, reference_map, glossary=None, ai_model=None, target_lang="한국어 (Korean)", log_prefix="API 번역 진행 중", custom_url=None, is_item_flags=None, is_book_flags=None):
    total = len(items)
    translated_results = [None] * total

    # 0단계: 백그라운드 클라우드 동기화가 진행 중이라면 완료될 때까지 짧게 대기 (다른 PC 간 캐시 누락 방지)
    translation_memory.wait_for_cloud_sync(timeout=4.0)

    # 1단계: 글로벌/모드팩/아이템/가이드북 마스터 번역 메모리 100% 일치 조회
    uncached_indices = []
    uncached_texts = []
    
    for i, item in enumerate(items):
        original_text = text_extractor(item)
        if is_code_or_id(original_text) or has_non_latin(original_text):
            translated_results[i] = original_text
            continue

        is_item = is_item_flags[i] if is_item_flags else False
        is_book = is_book_flags[i] if is_book_flags else False
        
        if is_item:
            cached_val = translation_memory.get_cached_item_translation(original_text, target_lang)
        elif is_book:
            cached_val = translation_memory.get_cached_book_translation(original_text, target_lang)
        else:
            cached_val = translation_memory.get_cached_translation(original_text, target_lang)
            
        if cached_val is not None:
            translated_results[i] = cached_val
        else:
            uncached_indices.append(i)
            uncached_texts.append(original_text)
            
    if not uncached_texts:
        if log_callback and total > 0:
            log_callback(f"✨ [캐시 100% 적중] 전체 {total:,}개 문장 모두가 클라우드/캐시에 이미 번역되어 있어 즉시 복원되었습니다! (0초 소요, 0토큰)")
        if progress_callback:
            progress_callback(total, total)
        return translated_results
        
    if log_callback:
        cached_count = total - len(uncached_texts)
        remaining_count = len(uncached_texts)
        pct = (cached_count / total) * 100
        if cached_count > 0:
            log_callback(f"💾 [캐시 복원] 전체 {total:,}개 중 {cached_count:,}개({pct:.1f}%)를 클라우드/캐시에서 즉시 불러왔습니다! ➡️ 실제 번역 대상: {remaining_count:,}개")
        else:
            log_callback(f"📝 [신규 번역] 캐시된 문장이 없어 전체 {total:,}개 문장을 분석 및 번역합니다.")

    # 2단계: 파일 레벨 고유 텍스트 추출 및 역매핑 (중복 문장 토큰/비용 절감)
    unique_texts = []
    unique_is_item = []
    text_to_unique_idx = {}
    uncached_to_unique_map = []
    
    for idx_in_global, txt in zip(uncached_indices, uncached_texts):
        pos = text_to_unique_idx.get(txt)
        is_item_flag = is_item_flags[idx_in_global] if is_item_flags else False
        if pos is None:
            pos = len(unique_texts)
            text_to_unique_idx[txt] = pos
            unique_texts.append(txt)
            unique_is_item.append(is_item_flag)
        elif is_item_flag:
            unique_is_item[pos] = True
        uncached_to_unique_map.append(pos)

    total_unique = len(unique_texts)
    unique_translated = [None] * total_unique

    # 3단계: [Case A] 명사구 로컬 단어 조립 (API 호출 없이 단어사전만으로 즉시 합성)
    llm_target_entries = []  # [(unique_idx, text), ...]
    assembled_count = 0

    for u_idx, (u_text, u_item_hint) in enumerate(zip(unique_texts, unique_is_item)):
        assembled = translation_word.try_assemble_noun_phrase(
            u_text, target_lang=target_lang, is_item_hint=u_item_hint
        )
        if assembled is not None:
            unique_translated[u_idx] = assembled
            assembled_count += 1
            temp_session_logger.record_case_a(u_text, assembled, "items" if u_item_hint else "general")
        else:
            llm_target_entries.append((u_idx, u_text))

    if temp_session_logger.is_debug_mode() and log_callback:
        log_callback("🛠️ [샌드박스 디버그 모드 ON] 원본 메모리/클라우드가 보호되며 결과는 dist/temp/session_summary.json 에 기록됩니다.")

    if assembled_count > 0 and log_callback:
        log_callback(
            f"⚡ [단어 조립 캐시] {assembled_count:,}개 명사구를 로컬 단어사전 조합으로 즉시 완성했습니다! (API 호출 0, 0토큰)"
        )


    def _flush_completed_to_memory():
        """현재까지 번역 완료된 항목들을 실시간으로 translation_memory에 안전하게 기록 (취소 시 유실 방지)"""
        import translation_engines
        is_mock = getattr(translation_engines, "MOCK_MODE", False)
        for idx_in_global, orig, unique_pos in zip(uncached_indices, uncached_texts, uncached_to_unique_map):
            res = unique_translated[unique_pos]
            if res is not None:
                translated_results[idx_in_global] = res
                if not is_mock:
                    is_item = is_item_flags[idx_in_global] if is_item_flags else False
                    is_book = is_book_flags[idx_in_global] if is_book_flags else False
                    if is_item:
                        translation_memory.add_item_to_memory(orig, res, target_lang)
                    elif is_book:
                        translation_memory.add_book_to_memory(orig, res, target_lang)
                    else:
                        translation_memory.add_to_memory(orig, res, target_lang)

    # 조립 완료된 항목들을 우선 메모리에 즉시 반영
    if assembled_count > 0:
        _flush_completed_to_memory()
        if progress_callback:
            progress_callback(assembled_count, total_unique)

    # 만약 모든 미번역 항목이 로컬 단어 조립만으로 100% 해결되었다면 즉시 종료!
    if not llm_target_entries:
        import translation_engines
        if not getattr(translation_engines, "MOCK_MODE", False):
            translation_memory.save_memory(flush_now=True)
        return translated_results

    total_llm = len(llm_target_entries)

    try:
        if is_paid:
            batch_size = 50
            chunks = [llm_target_entries[i:i + batch_size] for i in range(0, total_llm, batch_size)]
            completed_items = assembled_count

            def process_chunk(entry_chunk):
                if cancel_checker and cancel_checker():
                    raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")
                texts_chunk = [entry[1] for entry in entry_chunk]
                chunk_glossary = _build_chunk_glossary(texts_chunk, glossary)

                if engine_key == "local_ai":
                    res = translate_local_ai(texts_chunk, custom_url, ai_model, api_key=api_key, log_callback=log_callback, cancel_checker=cancel_checker, reference_map=reference_map, glossary=chunk_glossary, target_lang=target_lang)
                elif engine_key == "deepl":
                    res = [translate_deepl(t, api_key, is_pro=is_paid, reference_map=reference_map, target_lang=target_lang) for t in texts_chunk]
                elif engine_key == "openai":
                    res = [translate_openai(t, api_key, reference_map=reference_map, glossary=chunk_glossary, ai_model=ai_model, target_lang=target_lang) for t in texts_chunk]
                elif engine_key == "google":
                    res = [translate_google(t, api_key, reference_map=reference_map, target_lang=target_lang) for t in texts_chunk]
                else:
                    res = translate_gemini_batch(texts_chunk, api_key, is_paid, log_callback, cancel_checker, reference_map=reference_map, glossary=chunk_glossary, ai_model=ai_model, target_lang=target_lang)
                return entry_chunk, res, chunk_glossary

            with ThreadPoolExecutor(max_workers=min(8, len(chunks) or 1)) as executor:
                futures = [executor.submit(process_chunk, chunk) for chunk in chunks]
                for future in as_completed(futures):
                    if cancel_checker and cancel_checker():
                        executor.shutdown(wait=False, cancel_futures=True)
                        _flush_completed_to_memory()
                        translation_memory.save_memory()
                        raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")
                    entry_chunk, res_texts, chunk_glossary = future.result()
                    
                    for (u_idx, orig_txt), res in zip(entry_chunk, res_texts):
                        clean_res = sanitize_minecraft_formatting(orig_txt, translation_word.post_validate_proper_nouns(orig_txt, res, chunk_glossary))
                        unique_translated[u_idx] = clean_res
                        translation_word.learn_from_pair(orig_txt, clean_res, is_official=False)
                        temp_session_logger.record_case_b(orig_txt, clean_res, chunk_glossary, "items" if unique_is_item[u_idx] else "general")
                    
                    _flush_completed_to_memory()
                    completed_items += len(entry_chunk)
                    if progress_callback:
                        progress_callback(completed_items, total_unique)
                    if log_callback:
                        log_callback(f"⏳ {log_prefix} (병렬) [{completed_items}/{total_unique}]")
        else:
            # 무료 제미나이, Local AI, 또는 순차 번역
            batch_size = 40 if engine_key in ("gemini_batch", "local_ai") else 15
            completed_items = assembled_count
            for i in range(0, total_llm, batch_size):
                if cancel_checker and cancel_checker():
                    _flush_completed_to_memory()
                    translation_memory.save_memory()
                    raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")

                entry_chunk = llm_target_entries[i:i + batch_size]
                texts_chunk = [entry[1] for entry in entry_chunk]
                chunk_glossary = _build_chunk_glossary(texts_chunk, glossary)

                current_count = completed_items + len(entry_chunk)
                if log_callback:
                    log_callback(f"⏳ {log_prefix}... [{current_count}/{total_unique}]")

                if engine_key == "local_ai":
                    res_texts = translate_local_ai(texts_chunk, custom_url, ai_model, api_key=api_key, log_callback=log_callback, cancel_checker=cancel_checker, reference_map=reference_map, glossary=chunk_glossary, target_lang=target_lang)
                elif engine_key == "deepl":
                    res_texts = [translate_deepl(t, api_key, is_pro=False, reference_map=reference_map, target_lang=target_lang) for t in texts_chunk]
                elif engine_key == "openai":
                    res_texts = [translate_openai(t, api_key, reference_map=reference_map, glossary=chunk_glossary, ai_model=ai_model, target_lang=target_lang) for t in texts_chunk]
                elif engine_key == "google":
                    res_texts = [translate_google(t, api_key, reference_map=reference_map, target_lang=target_lang) for t in texts_chunk]
                else:
                    res_texts = translate_gemini_batch(texts_chunk, api_key, is_paid, log_callback, cancel_checker, reference_map=reference_map, glossary=chunk_glossary, ai_model=ai_model, target_lang=target_lang)
                
                for (u_idx, orig_txt), res in zip(entry_chunk, res_texts):
                    clean_res = sanitize_minecraft_formatting(orig_txt, translation_word.post_validate_proper_nouns(orig_txt, res, chunk_glossary))
                    unique_translated[u_idx] = clean_res
                    translation_word.learn_from_pair(orig_txt, clean_res, is_official=False)
                    temp_session_logger.record_case_b(orig_txt, clean_res, chunk_glossary, "items" if unique_is_item[u_idx] else "general")

                completed_items = current_count
                _flush_completed_to_memory()
                if progress_callback:
                    progress_callback(completed_items, total_unique)

        # 최종 전파 확인
        _flush_completed_to_memory()

    finally:
        _flush_completed_to_memory()
        import translation_engines
        if not getattr(translation_engines, "MOCK_MODE", False):
            translation_memory.save_memory(flush_now=True)
        if temp_session_logger.is_debug_mode():
            saved_temp = temp_session_logger.save_temp_session()
            if log_callback:
                log_callback(f"📄 [샌드박스 세션 요약 저장] {saved_temp}")
        
    return translated_results




def build_reference_translation_map(source_content, reference_content):
    if not source_content or not reference_content:
        return {}

    source_targets = extract_hqm_targets(source_content)
    reference_targets = extract_hqm_targets(reference_content)

    mapping = {}
    limit = min(len(source_targets), len(reference_targets))
    for idx in range(limit):
        source_text = normalize_reference_text(source_targets[idx].get("text"))
        reference_text = normalize_reference_text(reference_targets[idx].get("text"))
        if not source_text or not reference_text:
            continue
        if is_code_or_id(source_text) or is_code_or_id(reference_text):
            continue
        mapping[source_text] = reference_text
        mapping[source_text.lower()] = reference_text

    return mapping


def extract_snbt_targets(content):
    lines = content.splitlines()
    ignore_keys = ("id:", "icon:", "item:", "dependencies:", "x:", "y:", "shape:", "uid:", "type:", "command:")
    in_text_block = False
    targets = []

    kv_pattern = re.compile(r'^\s*("ftbquests\.[^"]+"\s*:\s*)"((?:[^"\\]|\\.)*)"(.*)$')
    array_start_pattern = re.compile(r'^(.*?\b(text|description|hover|quest_desc|desc|subtitle|chapter_subtitle|quest_subtitle)\s*:\s*\[\s*)(.*)$')
    key_pattern = re.compile(r'^(.*?\b(title|subtitle|description|text|hover|quest_desc|desc|chapter_subtitle|quest_subtitle)\s*:\s*)"((?:[^"\\]|\\.)*)"(.*)$')
    array_text_pattern = re.compile(r'^(\s*)"((?:[^"\\]|\\.)*)"(.*)$')

    for idx, line in enumerate(lines):
        stripped = line.strip()

        if stripped.startswith(ignore_keys):
            continue

        # 1. "ftbquests.xxx": "..."
        kv_match = kv_pattern.search(line)
        if kv_match:
            prefix, orig_text, suffix = kv_match.groups()
            if orig_text.strip() and not is_code_or_id(orig_text):
                targets.append((idx, prefix, orig_text, suffix))
            continue

        # 2. 배열 시작 감지 (description: [, text: [, hover: [ 등 띄어쓰기 유무 무관)
        arr_match = array_start_pattern.search(line)
        if arr_match:
            in_text_block = True
            line_rem = arr_match.group(3)
            # 같은 줄에 따옴표 문자열이 즉시 있는 경우 (예: description: [ "Line 1" ])
            match = re.search(r'^(.*?)"((?:[^"\\]|\\.)*)"(.*)$', line_rem)
            if match:
                inner_pre, orig_text, inner_suf = match.groups()
                full_prefix = arr_match.group(1) + inner_pre
                if orig_text.strip() and not is_code_or_id(orig_text):
                    targets.append((idx, full_prefix, orig_text, inner_suf))
                # 따옴표 밖(suffix)에 닫는 대괄호가 있는지 확인
                if "]" in inner_suf:
                    in_text_block = False
            else:
                if "]" in line_rem:
                    in_text_block = False
            continue

        # 3. 본문 배열 내부 텍스트 라인
        if in_text_block:
            match = array_text_pattern.search(line)
            if match:
                prefix, orig_text, suffix = match.groups()
                if orig_text.strip() and not is_code_or_id(orig_text):
                    targets.append((idx, prefix, orig_text, suffix))
                # ⚠️ 핵심 버그 수정: 본문 안의 [Shift] 같은 대괄호로 인해 배열이 조기 종료되지 않도록 따옴표 밖(suffix)만 검사!
                if "]" in suffix:
                    in_text_block = False
            else:
                # 따옴표가 없는 순수 닫는 괄호 라인 (예: "]" 또는 "],")
                if "]" in stripped:
                    in_text_block = False
            continue

        # 4. 단일 문자열 속성 (title: "...", subtitle: "...", description: "...", text: "...")
        key_match = key_pattern.search(line)
        if key_match:
            prefix, key_name, orig_text, suffix = key_match.groups()
            if orig_text.strip() and not is_code_or_id(orig_text):
                targets.append((idx, prefix, orig_text, suffix))
            continue

    return lines, targets


def rebuild_snbt(lines, translated_map):
    new_lines = [translated_map.get(idx, line) for idx, line in enumerate(lines)]
    return "\n".join(new_lines)


def _clean_snbt_string(text):
    if not isinstance(text, str):
        text = str(text)
    # 줄바꿈 문자를 SNBT 표준 이스케이프(\n)로 변환
    text = text.replace('\r\n', '\\n').replace('\n', '\\n')
    # 기존 이스케이프 중복 방지 후 안전하게 쌍따옴표 이스케이프
    text = text.replace('\\"', '"')
    return text.replace('"', '\\"')


def process_snbt_with_progress(content, engine_key, api_key, is_paid=False, progress_callback=None, log_callback=None, cancel_checker=None, verbose=True, reference_map=None, glossary=None, ai_model=None, target_lang="한국어 (Korean)", custom_url=None):
    if log_callback and verbose:
        log_callback("🔍 .snbt 퀘스트 구조 분석을 시작합니다...")

    lines, targets = extract_snbt_targets(content)

    if not targets:
        if log_callback:
            log_callback("⚠️ 분석 완료: 번역할 텍스트 대상을 찾지 못했습니다.")
        if progress_callback:
            progress_callback(1, 1)
        return content

    if log_callback and verbose:
        log_callback(f"✅ 분석 완료! 총 {len(targets)}개의 번역 대상 문장을 감지했습니다.")
        log_callback("🚀 본격적인 번역 및 텍스트 교체 작업을 시작합니다...")

    translated_map = {}

    log_pref = "초고속 SNBT 번역" if is_paid else "SNBT 번역"
    if engine_key == "local_ai": log_pref = "Local AI SNBT 번역"
    elif engine_key == "deepl": log_pref = "DeepL SNBT 번역"
    elif engine_key == "openai": log_pref = "OpenAI SNBT 번역"
    elif engine_key == "google": log_pref = "Google SNBT 번역"

    translated_texts = _run_batch_jobs(
        targets,
        lambda item: item[2].replace('\\"', '"'),
        engine_key, api_key, is_paid, log_callback, cancel_checker, progress_callback, reference_map, glossary, ai_model, target_lang,
        log_prefix=log_pref, custom_url=custom_url
    )
    for (line_idx, prefix, _, suffix), trans_text in zip(targets, translated_texts):
        final_text = _clean_snbt_string(trans_text)
        translated_map[line_idx] = f'{prefix}"{final_text}"{suffix}'

    if progress_callback:
        progress_callback(len(targets), len(targets))

    return rebuild_snbt(lines, translated_map)


def collect_json_targets(node, target_list, parent_key=""):
    ignore_keys = ("command", "command:8", "cmd", "id", "id:8", "icon", "icon:10", "type", "type:8", "uid", "sound", "sound:8")
    if isinstance(node, dict):
        for k, v in node.items():
            k_clean = str(k).strip().lower()
            if k_clean in ignore_keys or any(k_clean.startswith(ign) for ign in ("command", "icon:", "item:", "tasks:")):
                continue
            if isinstance(v, str) and v.strip() and not is_code_or_id(v):
                if k.startswith("ftbquests.") or k in ["name:8", "desc:8", "name", "title", "subtitle", "description", "text", "details", "quest_desc", "chapter_subtitle"]:
                    target_list.append((node, k, v))
                elif len(v) > 3 and " " in v and not any(k_clean.startswith(ign) for ign in ("id", "type", "tag")):
                    target_list.append((node, k, v))
            elif isinstance(v, (dict, list)):
                collect_json_targets(v, target_list, parent_key=k)
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            if isinstance(item, str) and item.strip() and not is_code_or_id(item):
                # 명시적 설명문/텍스트 부모 키 아래에 있는 문자열 리스트만 수집 (명령어/태그 리스트 배제)
                if parent_key in ["desc:8", "description", "text", "quest_desc", "details", "lines"]:
                    target_list.append((node, idx, item))
            elif isinstance(item, (dict, list)):
                collect_json_targets(item, target_list, parent_key=parent_key)


def process_json_safely(node, engine_key, api_key, is_paid=False, progress_callback=None, log_callback=None, cancel_checker=None, verbose=True, reference_map=None, glossary=None, ai_model=None, target_lang="한국어 (Korean)", custom_url=None):
    if log_callback and verbose:
        log_callback("🔍 JSON/HQM 퀘스트 언어 파일 구조 분석을 시작합니다...")

    if not isinstance(node, (dict, list)):
        return node

    targets = []
    collect_json_targets(node, targets)

    if not targets:
        if log_callback:
            log_callback("⚠️ 분석 완료: 번역할 텍스트 대상을 찾지 못했습니다.")
        if progress_callback:
            progress_callback(1, 1)
        return node

    if log_callback and verbose:
        log_callback(f"✅ 분석 완료! 총 {len(targets)}개의 JSON 노드를 감지했습니다. 번역을 시작합니다...")

    log_pref = "초고속 다국어 번역" if is_paid else "다국어 번역"
    if engine_key == "local_ai": log_pref = "Local AI 다국어 번역"
    elif engine_key == "deepl": log_pref = "DeepL 다국어 번역"
    elif engine_key == "openai": log_pref = "OpenAI 다국어 번역"
    elif engine_key == "google": log_pref = "Google 다국어 번역"

    translated_texts = _run_batch_jobs(
        targets,
        lambda item: item[2],
        engine_key, api_key, is_paid, log_callback, cancel_checker, progress_callback, reference_map, glossary, ai_model, target_lang,
        log_prefix=log_pref, custom_url=custom_url
    )
    for (parent_node, key, _), trans_text in zip(targets, translated_texts):
        parent_node[key] = trans_text

    if progress_callback:
        progress_callback(len(targets), len(targets))


HQM_TEXT_ENCODING = "utf-8"


def _is_hqm_translatable(text):
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped or len(stripped) < 2:
        return False

    for ch in stripped:
        code = ord(ch)
        if code < 32 and ch not in "\n\r\t":
            return False
        if code < 128:
            continue
        if 0xAC00 <= code <= 0xD7A3 or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F:
            continue
        return False

    if is_code_or_id(stripped):
        return False
    if ":" in stripped or re.fullmatch(r"[A-Z0-9_ .#-]+", stripped):
        return False
    if not re.search(r"[A-Za-z가-힣]", stripped):
        return False
    if " " not in stripped and "-" not in stripped and "/" not in stripped and not any("\uac00" <= ch <= "\ud7a3" for ch in stripped):
        return False
    return True


def extract_hqm_targets(content):
    candidates = []
    size = len(content)
    seen_spans = set()

    for start in range(size):
        field_candidates = []

        if start >= 2:
            description_length = int.from_bytes(content[start - 2:start], "little")
            if 3 <= description_length <= size - start:
                field_candidates.append(("description", description_length))
            alt_desc_length = int.from_bytes(content[start - 2:start], "big")
            if 3 <= alt_desc_length <= size - start and alt_desc_length != description_length:
                field_candidates.append(("description", alt_desc_length))

        if start >= 1:
            name_length = content[start - 1] >> 3
            if 3 <= name_length <= 31 and name_length <= size - start:
                field_candidates.append(("name", name_length))
            alt_name_length = content[start - 1] & 0x1F
            if 3 <= alt_name_length <= 31 and alt_name_length <= size - start and alt_name_length != name_length:
                field_candidates.append(("name", alt_name_length))

        for length_kind, length in field_candidates:
            end = start + length
            if end > size or (start, end) in seen_spans:
                continue
            raw = content[start:end]
            if not raw:
                continue
            try:
                text = raw.decode(HQM_TEXT_ENCODING)
            except UnicodeDecodeError:
                try:
                    text = raw.decode("ascii")
                except UnicodeDecodeError:
                    continue

            if not text or text.isdigit():
                continue

            if _is_hqm_translatable(text):
                seen_spans.add((start, end))
                candidates.append({
                    "start": start,
                    "end": end,
                    "length_kind": length_kind,
                    "text": text,
                })

    targets = []
    covered_until = -1
    for candidate in sorted(candidates, key=lambda item: (item["start"], -(item["end"] - item["start"]))):
        if candidate["start"] >= covered_until:
            targets.append(candidate)
            covered_until = candidate["end"]
    return targets


def _translate_hqm_texts(targets, engine_key, api_key, is_paid, progress_callback, log_callback, cancel_checker, reference_map=None, glossary=None, ai_model=None, target_lang="한국어 (Korean)"):
    translated = []
    total = len(targets)
    if total == 0:
        return []

    if engine_key == "gemini_batch":
        res_texts = _run_batch_jobs(
            targets,
            lambda item: item["text"],
            engine_key, api_key, is_paid, log_callback, cancel_checker, progress_callback, reference_map, glossary, ai_model, target_lang,
            log_prefix="초고속 HQM 번역" if is_paid else "HQM 번역"
        )
        translated = [str(res or item["text"]) for item, res in zip(targets, res_texts)]
    else:
        for count, target in enumerate(targets, 1):
            if cancel_checker and cancel_checker():
                raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")

            source = target["text"]
            if engine_key == "deepl":
                result = translate_deepl(source, api_key, reference_map=reference_map, target_lang=target_lang)
            elif engine_key == "openai":
                result = translate_openai(source, api_key, reference_map=reference_map, glossary=glossary, ai_model=ai_model, target_lang=target_lang)
            else:
                result = translate_google(source, api_key, reference_map=reference_map, target_lang=target_lang)

            translated.append(str(result or source))
            if progress_callback:
                progress_callback(count, total)
            if log_callback:
                log_callback(f"⏳ HQM 텍스트 번역 진행 중... [{count}/{total}]")

    return translated


def _fit_utf8_exact_bytes(text, target_len):
    if not text:
        return b" " * target_len

    encoded = text.encode(HQM_TEXT_ENCODING, errors="ignore")
    if len(encoded) == target_len:
        return encoded
    if len(encoded) < target_len:
        return encoded.ljust(target_len, b" ")

    trimmed = text
    while trimmed:
        trimmed = trimmed[:-1]
        candidate = trimmed.encode(HQM_TEXT_ENCODING, errors="ignore")
        if len(candidate) <= target_len:
            return candidate.ljust(target_len, b" ")
    return b" " * target_len


def _translate_hqm_texts_entries(entries, engine_key, api_key, is_paid, progress_callback, log_callback, cancel_checker, reference_map=None, glossary=None, ai_model=None, target_lang="한국어 (Korean)", custom_url=None):
    translated_texts = []
    total = len(entries)
    if total == 0:
        return translated_texts

    if engine_key in ("gemini_batch", "local_ai"):
        log_pref = "초고속 HQM 텍스트 덩어리 번역" if is_paid else "HQM 텍스트 번역"
        if engine_key == "local_ai": log_pref = "Local AI HQM 텍스트 번역"
        res_texts = _run_batch_jobs(
            entries,
            lambda entry: entry.value,
            engine_key, api_key, is_paid, log_callback, cancel_checker, progress_callback, reference_map, glossary, ai_model, target_lang,
            log_prefix=log_pref, custom_url=custom_url
        )
        translated_texts.extend([str(res or entry.value) for entry, res in zip(entries, res_texts)])
        return translated_texts
    else:
        for count, entry in enumerate(entries, 1):
            if cancel_checker and cancel_checker():
                raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")

            source = entry.value
            if engine_key == "deepl":
                result = translate_deepl(source, api_key, reference_map=reference_map, target_lang=target_lang)
            elif engine_key == "openai":
                result = translate_openai(source, api_key, reference_map=reference_map, glossary=glossary, ai_model=ai_model, target_lang=target_lang)
            else:
                result = translate_google(source, api_key, reference_map=reference_map, target_lang=target_lang)

            translated_texts.append(str(result or source))
            if progress_callback:
                progress_callback(count, total)
            if log_callback:
                log_callback(f"⏳ HQM 텍스트 번역 진행 중... [{count}/{total}]")

    return translated_texts


def process_hqm_with_progress(content, engine_key, api_key, is_paid=False, progress_callback=None, log_callback=None, cancel_checker=None, reference_map=None, glossary=None, ai_model=None, target_lang="한국어 (Korean)", custom_url=None):
    if not content:
        raise ValueError("빈 HQM 파일입니다.")

    if HQMQuestConverter is None:
        if log_callback:
            log_callback("⚠️ 로컬 HQM 파서가 없어서 기본 문의 추출 방식을 사용합니다.")
        return _process_hqm_with_heuristic(content, engine_key, api_key, is_paid, progress_callback, log_callback, cancel_checker, reference_map, glossary, ai_model, target_lang, custom_url=custom_url)

    try:
        converter = HQMQuestConverter()
        hqm_file = converter.read(BytesIO(content))
    except Exception as exc:
        if log_callback:
            log_callback(f"⚠️ HQM 파일 파싱 실패: {exc}")
        raise

    modpack_name = "quest"
    entries = [entry for entry in hqm_file.text_entries(modpack_name) if filter_text(entry.value)]
    if not entries:
        if log_callback:
            log_callback("⚠️ HQM에서 번역 가능한 텍스트를 찾지 못했습니다. 파일 포맷이 다르거나 텍스트가 비어 있을 수 있습니다.")
        if progress_callback:
            progress_callback(1, 1)
        return content

    if log_callback:
        log_callback(f"🔎 HQM 퀘스트 텍스트 {len(entries)}개를 감지했습니다.")

    translated_texts = _translate_hqm_texts_entries(
        entries, engine_key, api_key, is_paid, progress_callback, log_callback, cancel_checker, reference_map=reference_map, glossary=glossary, ai_model=ai_model, target_lang=target_lang, custom_url=custom_url
    )

    # Naturalness-first policy: avoid saving hard-truncated Korean text.
    # If translation exceeds HQM byte limit for a field, keep the original source text.
    lang_dict = {}
    overflow_kept_source = 0
    for entry, translated in zip(entries, translated_texts):
        candidate = str(translated or entry.value)
        if len(candidate.encode("utf-8")) > entry.max_bytes:
            lang_dict[entry.key] = entry.value
            overflow_kept_source += 1
        else:
            lang_dict[entry.key] = candidate

    if overflow_kept_source and log_callback:
        log_callback(
            f"⚠️ HQM 길이 제한으로 잘릴 항목 {overflow_kept_source}개는 원문 유지(자연스러움 우선) 처리했습니다."
        )

    warnings = hqm_file.apply_lang_dict(modpack_name, lang_dict)
    if warnings and log_callback:
        for warning in warnings:
            log_callback(f"⚠️ HQM 번역 경고: {warning}")

    if progress_callback:
        progress_callback(1, 1)

    return hqm_file.to_bytes()


def _process_hqm_with_heuristic(content, engine_key, api_key, is_paid, progress_callback=None, log_callback=None, cancel_checker=None, reference_map=None, glossary=None, ai_model=None, target_lang="한국어 (Korean)", custom_url=None):
    targets = extract_hqm_targets(content)
    if not targets:
        if log_callback:
            log_callback("⚠️ HQM에서 번역 가능한 텍스트를 찾지 못했습니다. 파일 포맷이 다르거나 텍스트가 비어 있을 수 있습니다.")
        if progress_callback:
            progress_callback(1, 1)
        return content
    if log_callback:
        log_callback(f"🔎 HQM 바이너리에서 번역할 제목/설명 {len(targets)}개를 찾았습니다.")

    translated_texts = _translate_hqm_texts(
        targets, engine_key, api_key, is_paid, progress_callback, log_callback, cancel_checker, reference_map=reference_map, glossary=glossary, ai_model=ai_model, target_lang=target_lang
    )
    output = bytearray(content)

    for target, translated in reversed(list(zip(targets, translated_texts))):
        target_len = target["end"] - target["start"]
        replacement = _fit_utf8_exact_bytes(translated, target_len)
        output[target["start"]:target["end"]] = replacement

    return bytes(output)
