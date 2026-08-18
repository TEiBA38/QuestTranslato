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

def _run_batch_jobs(items, text_extractor, engine_key, api_key, is_paid, log_callback, cancel_checker, progress_callback, reference_map, glossary=None, ai_model=None, target_lang="한국어 (Korean)", log_prefix="API 번역 진행 중", custom_url=None):
    total = len(items)
    translated_results = [None] * total

    # 1단계: 글로벌 번역 메모리 조회
    uncached_indices = []
    uncached_texts = []
    
    for i, item in enumerate(items):
        original_text = text_extractor(item)
        cached_val = translation_memory.get_cached_translation(original_text, target_lang)
        if cached_val is not None:
            translated_results[i] = cached_val
        else:
            uncached_indices.append(i)
            uncached_texts.append(original_text)
            
    if not uncached_texts:
        return translated_results
        
    if log_callback and len(uncached_texts) < total:
        log_callback(f"💾 이전에 번역된 {total - len(uncached_texts)}개의 텍스트를 캐시에서 불러왔습니다.")

    total_uncached = len(uncached_texts)

    if is_paid:
        batch_size = 50
        chunks = [(i, uncached_indices[i:i + batch_size], uncached_texts[i:i + batch_size]) for i in range(0, total_uncached, batch_size)]
        completed_items = 0

        def process_chunk(start_idx, indices_chunk, texts_chunk):
            if cancel_checker and cancel_checker():
                raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")
            res = translate_gemini_batch(texts_chunk, api_key, is_paid, log_callback, cancel_checker, reference_map=reference_map, glossary=glossary, ai_model=ai_model, target_lang=target_lang)
            return indices_chunk, texts_chunk, res

        with ThreadPoolExecutor(max_workers=min(8, len(chunks) or 1)) as executor:
            futures = [executor.submit(process_chunk, idx, indices_chunk, texts_chunk) for idx, indices_chunk, texts_chunk in chunks]
            for future in as_completed(futures):
                if cancel_checker and cancel_checker():
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")
                indices_chunk, texts_chunk, res_texts = future.result()
                
                for idx_in_global, orig, res in zip(indices_chunk, texts_chunk, res_texts):
                    translated_results[idx_in_global] = res
                    translation_memory.add_to_memory(orig, res, target_lang)
                
                translation_memory.save_memory()
                completed_items += len(indices_chunk)
                if progress_callback:
                    progress_callback(completed_items, total_uncached)
                if log_callback:
                    log_callback(f"⏳ {log_prefix} (병렬) [{completed_items}/{total_uncached}]")
    else:
        # 무료 제미나이 또는 Local AI: 
        # TPM 여유가 매우 크므로 한 번에 더 많은 문장(40개)을 묶어서 보냅니다 (약 3배 속도 향상!)
        batch_size = 40 if engine_key in ("gemini_batch", "local_ai") else 15
        sleep_sec = 4.5
        for i in range(0, total_uncached, batch_size):
            if cancel_checker and cancel_checker():
                raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")

            indices_chunk = uncached_indices[i:i + batch_size]
            texts_chunk = uncached_texts[i:i + batch_size]
            
            current_count = min(i + len(texts_chunk), total_uncached)
            if log_callback:
                log_callback(f"⏳ {log_prefix}... [{current_count}/{total_uncached}]")

            if engine_key == "local_ai":
                res_texts = translate_local_ai(texts_chunk, custom_url, ai_model, api_key=api_key, log_callback=log_callback, cancel_checker=cancel_checker, reference_map=reference_map, glossary=glossary, target_lang=target_lang)
            else:
                res_texts = translate_gemini_batch(texts_chunk, api_key, is_paid, log_callback, cancel_checker, reference_map=reference_map, glossary=glossary, ai_model=ai_model, target_lang=target_lang)
            
            for idx_in_global, orig, res in zip(indices_chunk, texts_chunk, res_texts):
                translated_results[idx_in_global] = res
                translation_memory.add_to_memory(orig, res, target_lang)

            translation_memory.save_memory()

            if progress_callback:
                progress_callback(current_count, total_uncached)

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
    array_text_pattern = re.compile(r'^(\s*)"((?:[^"\\]|\\.)*)"(.*)$')
    title_pattern = re.compile(r'^(.*?title:\s*)"((?:[^"\\]|\\.)+)"(.*)$')
    subtitle_pattern = re.compile(r'^(.*?subtitle:\s*)"((?:[^"\\]|\\.)+)"(.*)$')
    single_line_array_pattern = re.compile(r'^(.*?\[\s*)"((?:[^"\\]|\\.)*)"(.*)$')

    for idx, line in enumerate(lines):
        stripped = line.strip()

        if stripped.startswith(ignore_keys):
            continue

        kv_match = kv_pattern.search(line)
        if kv_match:
            prefix, orig_text, suffix = kv_match.groups()
            if orig_text.strip() and not is_code_or_id(orig_text):
                targets.append((idx, prefix, orig_text, suffix))
            continue

        if stripped.startswith(("text: [", "description: [", "hover: [")):
            in_text_block = True
            match = single_line_array_pattern.search(line)
            if match:
                prefix, orig_text, suffix = match.groups()
                if orig_text.strip() and not is_code_or_id(orig_text):
                    targets.append((idx, prefix, orig_text, suffix))

            if stripped.endswith("]"):
                in_text_block = False
            continue

        if in_text_block:
            match = array_text_pattern.search(line)
            if match:
                prefix, orig_text, suffix = match.groups()
                if orig_text.strip() and not is_code_or_id(orig_text):
                    targets.append((idx, prefix, orig_text, suffix))

            if "]" in stripped:
                in_text_block = False
            continue

        title_match = title_pattern.search(line)
        subtitle_match = subtitle_pattern.search(line)

        if title_match:
            prefix, orig_text, suffix = title_match.groups()
            if not is_code_or_id(orig_text):
                targets.append((idx, prefix, orig_text, suffix))
        elif subtitle_match:
            prefix, orig_text, suffix = subtitle_match.groups()
            if not is_code_or_id(orig_text):
                targets.append((idx, prefix, orig_text, suffix))

    return lines, targets


def rebuild_snbt(lines, translated_map):
    new_lines = [translated_map.get(idx, line) for idx, line in enumerate(lines)]
    return "\n".join(new_lines)


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

    if engine_key in ("gemini_batch", "local_ai"):
        log_pref = "초고속 SNBT 번역" if is_paid else "SNBT 번역"
        if engine_key == "local_ai": log_pref = "Local AI SNBT 번역"
        translated_texts = _run_batch_jobs(
            targets,
            lambda item: item[2].replace('\\"', '"'),
            engine_key, api_key, is_paid, log_callback, cancel_checker, progress_callback, reference_map, glossary, ai_model, target_lang,
            log_prefix=log_pref, custom_url=custom_url
        )
        for (line_idx, prefix, _, suffix), trans_text in zip(targets, translated_texts):
            final_text = str(trans_text).replace('"', '\\"')
            translated_map[line_idx] = f'{prefix}"{final_text}"{suffix}'
    else:
        for count, (line_idx, prefix, orig_text, suffix) in enumerate(targets, 1):
            if cancel_checker and cancel_checker():
                raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")

            raw_orig = orig_text.replace('\\"', '"')

            if engine_key == "deepl":
                trans = translate_deepl(raw_orig, api_key, reference_map=reference_map, target_lang=target_lang)
            elif engine_key == "openai":
                trans = translate_openai(raw_orig, api_key, reference_map=reference_map, glossary=glossary, ai_model=ai_model, target_lang=target_lang)
            else:
                trans = translate_google(raw_orig, api_key, reference_map=reference_map, target_lang=target_lang)

            final_text = str(trans if trans else raw_orig).replace('"', '\\"')
            translated_map[line_idx] = f'{prefix}"{final_text}"{suffix}'

            if progress_callback and count % 5 == 0:
                progress_callback(count, len(targets))
                if log_callback:
                    log_callback(f"⏳ 번역 진행 중... [{count}/{len(targets)}]")

        if progress_callback:
            progress_callback(len(targets), len(targets))

    return rebuild_snbt(lines, translated_map)


def collect_json_targets(node, target_list):
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str) and v.strip() and not is_code_or_id(v):
                if k.startswith("ftbquests.") or k in ["name:8", "desc:8", "name", "title", "subtitle", "description", "text", "details"]:
                    target_list.append((node, k, v))
                elif len(v) > 3 and " " in v:
                    target_list.append((node, k, v))
            elif isinstance(v, (dict, list)):
                collect_json_targets(v, target_list)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                collect_json_targets(item, target_list)


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

    if engine_key in ("gemini_batch", "local_ai"):
        log_pref = "초고속 다국어 번역" if is_paid else "다국어 번역"
        if engine_key == "local_ai": log_pref = "Local AI 다국어 번역"
        translated_texts = _run_batch_jobs(
            targets,
            lambda item: item[2],
            engine_key, api_key, is_paid, log_callback, cancel_checker, progress_callback, reference_map, glossary, ai_model, target_lang,
            log_prefix=log_pref, custom_url=custom_url
        )
        for (parent_node, key, _), trans_text in zip(targets, translated_texts):
            parent_node[key] = trans_text
    else:
        for count, (parent_node, key, orig_text) in enumerate(targets, 1):
            if cancel_checker and cancel_checker():
                raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")

            if engine_key == "deepl":
                trans = translate_deepl(orig_text, api_key, reference_map=reference_map, target_lang=target_lang)
            elif engine_key == "openai":
                trans = translate_openai(orig_text, api_key, reference_map=reference_map, glossary=glossary, ai_model=ai_model, target_lang=target_lang)
            else:
                trans = translate_google(orig_text, api_key, reference_map=reference_map, target_lang=target_lang)
            if trans:
                parent_node[key] = trans

            if progress_callback and count % 5 == 0:
                progress_callback(count, len(targets))
                if log_callback:
                    log_callback(f"⏳ 번역 진행 중... [{count}/{len(targets)}]")

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
