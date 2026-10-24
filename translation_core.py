import os
import json
import shutil
import zipfile
import tempfile
import time
import re
from constants import has_non_latin
import translation_memory
from translation_engines import ENGINES, QuotaExceededError, TranslationCancelledError, is_code_or_id
from file_processors import (
    extract_snbt_targets,
    rebuild_snbt,
    process_hqm_with_progress,
)
from review_checks import analyze_snbt_texts, analyze_json_data, analyze_hqm_bytes, render_review_report

PROGRESS_FILE = "_progress.json"


def _load_progress(out_dir):
    """Load set of completed relative paths from progress file."""
    try:
        with open(os.path.join(out_dir, PROGRESS_FILE), encoding='utf-8') as f:
            return set(json.load(f).get('completed', []))
    except Exception:
        return set()


def _save_progress(out_dir, completed):
    """Persist completed relative paths to progress file."""
    try:
        with open(os.path.join(out_dir, PROGRESS_FILE), 'w', encoding='utf-8') as f:
            json.dump({'completed': sorted(completed)}, f, ensure_ascii=False)
    except Exception:
        pass


def cleanup_old_temp_files(max_age_seconds=3600):
    """이전 세션의 비정상 종료나 취소로 인해 Temp 폴더에 남은 찌꺼기 임시 파일/폴더를 안전하게 정리합니다."""
    temp_dir = tempfile.gettempdir()
    now = time.time()
    cleaned = 0
    try:
        for name in os.listdir(temp_dir):
            if name.startswith(("_temp_raw_", "_temp_out_", "QuestTranslator_")):
                full_path = os.path.join(temp_dir, name)
                try:
                    mtime = os.path.getmtime(full_path)
                    if now - mtime > max_age_seconds:
                        if os.path.isdir(full_path):
                            shutil.rmtree(full_path, ignore_errors=True)
                            cleaned += 1
                        elif os.path.isfile(full_path):
                            os.remove(full_path)
                            cleaned += 1
                except Exception:
                    pass
    except Exception:
        pass
    return cleaned


class TranslationUIContext:
    """Interface to communicate with the main UI thread during translation."""
    def log(self, message):
        pass

    def set_status(self, text):
        pass

    def update_progress(self, current, total=None):
        pass

    def is_cancelled(self):
        return False

    def check_cancel(self):
        if self.is_cancelled():
            raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")

    def show_messagebox(self, type_, title, message):
        pass

    def show_review_report(self, report_text):
        pass

    def ask_resume(self, candidates):
        """
        Ask user if they want to resume from a backup.
        candidates: List of dicts [{"path": str, "name": str, "count": int, "completed": set}]
        Returns the chosen out_dir and completed_set, or (None, set()) if new/cancelled, or raises Exception if cancelled.
        (Implementation will vary based on how many candidates exist)
        """
        pass

    def ask_save_dir(self):
        """Ask user for a directory to save the ZIP file."""
        pass

    def offer_partial_backup(self, out_dir, backup_name):
        pass

    def on_translation_success(self, modpack_path):
        pass


def _generate_zip_review_report(context, raw_dir, out_dir, report_title):
    review_items = []
    for root, _, files_list in os.walk(raw_dir):
        for f in files_list:
            rel_p = os.path.relpath(os.path.join(root, f), raw_dir)
            src_path = os.path.join(raw_dir, rel_p)
            dst_path = os.path.join(out_dir, rel_p)
            if not os.path.exists(dst_path):
                continue
            try:
                if f.lower().endswith('.snbt'):
                    with open(src_path, 'r', encoding='utf-8', errors='ignore') as fh:
                        src = fh.read()
                    with open(dst_path, 'r', encoding='utf-8', errors='ignore') as fh:
                        dst = fh.read()
                    review_items.append((rel_p, analyze_snbt_texts(src, dst)))
                elif f.lower().endswith(('.json', '.lang')):
                    with open(src_path, encoding='utf-8', errors='ignore') as fh:
                        src = json.load(fh)
                    with open(dst_path, encoding='utf-8', errors='ignore') as fh:
                        dst = json.load(fh)
                    review_items.append((rel_p, analyze_json_data(src, dst)))
                elif f.lower().endswith('.hqm'):
                    with open(src_path, 'rb') as fh:
                        src = fh.read()
                    with open(dst_path, 'rb') as fh:
                        dst = fh.read()
                    review_items.append((rel_p, analyze_hqm_bytes(src, dst)))
            except Exception as review_exc:
                context.log(f"⚠️ 검수 스킵 [{rel_p}]: {review_exc}")

    if review_items:
        report_text = render_review_report(report_title, review_items)
        context.show_review_report(report_text)
        context.log("🧪 검수 리포트가 결과창으로 표시되었습니다.")


def run_zip_translation_logic(context: TranslationUIContext, zip_path, engine_key, api_key, is_paid, ai_model, target_lang, modpack_path, apply_mode, reference_map, glossary, custom_url=None):
    # 모드팩 컨텍스트 설정 (짧은 문장의 모드팩별 메모리 분리)
    translation_memory.set_current_modpack(modpack_path or zip_path)
    
    base_zip_name = os.path.basename(zip_path)
    base_no_ext = os.path.splitext(base_zip_name)[0]
    clean_base_no_ext = re.sub(r'_\d+_\d+$', '', base_no_ext)
    origin_dir = os.path.dirname(zip_path)
    
    # 모드팩(ZIP) 이름별로 고유한 임시 폴더 경로 생성
    raw_dir = os.path.join(origin_dir, f"_temp_raw_{base_no_ext}")
    out_dir = os.path.join(origin_dir, f"_temp_out_{clean_base_no_ext}")
    
    # 1. Resume detection
    candidates = []
    if os.path.isfile(os.path.join(out_dir, PROGRESS_FILE)):
        comp = _load_progress(out_dir)
        if comp:
            candidates.append({"path": out_dir, "name": f"이전 번역 백업 ({clean_base_no_ext})", "count": len(comp), "completed": comp})
    
    # 예전 방식(_temp_out_ 백업)이 남아있을 수 있으므로 호환성을 위해 스캔하되, 현재 이름이 포함된 경우만 추가
    if os.path.isdir(origin_dir):
        for entry in os.scandir(origin_dir):
            if entry.is_dir() and entry.name.startswith(f"_temp_out_{base_no_ext}_") and entry.path != out_dir:
                prog_path = os.path.join(entry.path, PROGRESS_FILE)
                if os.path.isfile(prog_path):
                    comp = _load_progress(entry.path)
                    if comp:
                        candidates.append({"path": entry.path, "name": f"임시 백업 ({entry.name})", "count": len(comp), "completed": comp})

    out_dir, completed_set, resuming = context.ask_resume(candidates, out_dir)
    if out_dir is None:
        return # Cancelled by user

    try:
        if os.path.exists(raw_dir):
            shutil.rmtree(raw_dir)
        if not resuming and os.path.exists(out_dir):
            shutil.rmtree(out_dir)

        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            for member in zip_ref.namelist():
                if member.startswith('/') or member.startswith('\\') or '..' in member:
                    raise ValueError(f"보안 차단: 압축 해제 경로 탐색(Zip Slip) 취약점이 의심되는 파일이 포함되어 있습니다 - {member}")
            zip_ref.extractall(raw_dir)

        target_files = [
            os.path.join(root, f)
            for root, _, files in os.walk(raw_dir)
            for f in files if f.lower().endswith(('.snbt', '.json', '.lang', '.hqm'))
        ]
        total_files = len(target_files)
        if total_files == 0:
            context.log("⚠️ 번역할 대상 파일을 찾을 수 없습니다.")
            return

        mode_str = "[유료/초고속]" if (engine_key == "gemini_batch" and is_paid) else "[무료/안전대기]" if engine_key == "gemini_batch" else "[초고속]"
        engine_label = next((name for name, key in ENGINES.items() if key == engine_key), engine_key)
        context.log(f"\n🎯 총 {total_files}개 파일 압축 번역 시작... {mode_str}")
        context.log(f"🧩 선택 옵션: 엔진={engine_label} / 모드={mode_str}")

        jobs = []
        skipped_no_targets = 0
        skipped_translated = 0
        skipped_resume = 0

        is_korean_target = ("한국어" in target_lang or "Korean" in target_lang)

        def _find_sibling_ko_file(en_file_path, orig_rel_dir, ext):
            if not is_korean_target:
                return None
            candidates_paths = []
            raw_parent = os.path.dirname(en_file_path)
            for ko_name in (f"ko_kr{ext}", f"ko_KR{ext}"):
                candidates_paths.append(os.path.join(raw_parent, ko_name))
                if modpack_path:
                    candidates_paths.append(os.path.join(modpack_path, orig_rel_dir, ko_name) if orig_rel_dir else os.path.join(modpack_path, ko_name))
            for cp in candidates_paths:
                if os.path.isfile(cp):
                    return cp
            return None

        def _is_target_untranslated(raw_text):
            if not raw_text or not str(raw_text).strip():
                return False
            clean_text = re.sub(r'^[&§][0-9a-fk-orA-FK-OR]', '', str(raw_text)).strip()
            if not clean_text:
                return False
            # 1. 한글 또는 목표 언어 비라틴 문자가 이미 포함된 경우 -> 이미 번역됨
            if has_non_latin(clean_text, target_lang=target_lang):
                return False
            words = clean_text.split()
            # 2. 3단어 이상의 긴 문장인데 목표 언어 문자가 없다면 100% 미번역 본문
            if len(words) >= 3:
                return True
            # 3. 1~2단어 짧은 고유명사/약어/티어 명칭:
            from translation_engines import is_code_or_id
            if is_code_or_id(clean_text):
                return False
            # 이미 번역 메모리(캐시)에 등록되어 있는 문장이면 (원문 유지 번역 포함) 번역 완료로 판정
            cached = (translation_memory.get_cached_translation(clean_text, target_lang) or
                      translation_memory.get_cached_item_translation(clean_text, target_lang) or
                      translation_memory.get_cached_book_translation(clean_text, target_lang))
            if cached is not None:
                return False
            return True

        # 클라우드 마스터 번역 메모리를 먼저 동기화 완료한 뒤 내장 ko_kr 패치를 적용하여
        # 백그라운드 클라우드 동기화가 내장 공식 한글패치를 덮어쓰는 현상 방지
        context.set_status("🌐 클라우드 마스터 번역 메모리 확인 중...")
        translation_memory.load_memory()
        translation_memory.wait_for_cloud_sync(timeout=6.0)

        harvested_ko_keys = 0

        for idx, file_path in enumerate(target_files, 1):
            rel_path = os.path.relpath(file_path, raw_dir)
            orig_rel_dir = os.path.dirname(rel_path)
            base_name = os.path.basename(rel_path).lower()

            # 같은 폴더에 en_us가 존재하는 경우, 단독 ko_kr 파일은 en_us 병합 시 함께 처리하므로 중복 덮어쓰기 방지
            if base_name in ('ko_kr.lang', 'ko_kr.json') and ('lang' in rel_path.lower() or 'resources' in rel_path.lower()):
                ext = '.json' if base_name.endswith('.json') else '.lang'
                raw_parent = os.path.dirname(file_path)
                if os.path.isfile(os.path.join(raw_parent, f"en_us{ext}")) or os.path.isfile(os.path.join(raw_parent, f"en_US{ext}")):
                    continue

            # en_us.lang 등의 영어 언어 파일을 ko_kr.lang으로 변환하여 생성
            sibling_ko_path = None
            if base_name in ('en_us.lang', 'en_us.json') and ('lang' in rel_path.lower() or 'resources' in rel_path.lower()):
                new_ext = '.json' if base_name.endswith('.json') else '.lang'
                sibling_ko_path = _find_sibling_ko_file(file_path, orig_rel_dir, new_ext)
                rel_path = os.path.join(orig_rel_dir, f"ko_kr{new_ext}") if orig_rel_dir else f"ko_kr{new_ext}"

            target_path = os.path.join(out_dir, rel_path)
            os.makedirs(os.path.dirname(target_path), exist_ok=True)

            if rel_path in completed_set:
                skipped_resume += 1
                continue

            if file_path.lower().endswith('.snbt'):
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                lines, targets = extract_snbt_targets(content)
                if not targets:
                    skipped_no_targets += 1
                    shutil.copy2(file_path, target_path)
                    completed_set.add(rel_path)
                    _save_progress(out_dir, completed_set)
                    continue
                untranslated_targets = [t for t in targets if _is_target_untranslated(t[2])]
                if not untranslated_targets:
                    skipped_translated += 1
                    shutil.copy2(file_path, target_path)
                    completed_set.add(rel_path)
                    _save_progress(out_dir, completed_set)
                    continue
                jobs.append({"kind": "snbt", "target_path": target_path, "rel_path": rel_path,
                             "lines": lines, "targets": untranslated_targets, "translated_map": {}})
            elif file_path.lower().endswith('.hqm'):
                with open(file_path, 'rb') as f:
                    hqm_content = f.read()
                if has_non_latin(hqm_content.decode('utf-8', errors='ignore')):
                    skipped_translated += 1
                    shutil.copy2(file_path, target_path)
                    completed_set.add(rel_path)
                    _save_progress(out_dir, completed_set)
                    continue
                context.set_status(f"🔄 [{idx}/{total_files}] [{os.path.basename(file_path)}] HQM 바이너리 번역 중...")
                
                def hqm_progress_cb(current, total, _idx=idx, _fn=os.path.basename(file_path)):
                    base = (_idx - 1) / total_files
                    inner = (current / total) / total_files if total > 0 else 0
                    context.update_progress(base + inner)
                    context.set_status(f"⏳ [{_idx}/{total_files}] [{_fn}] HQM 번역 중... [{current}/{total}]")

                try:
                    translated_hqm = process_hqm_with_progress(
                        hqm_content, engine_key, api_key, is_paid,
                        progress_callback=hqm_progress_cb,
                        log_callback=context.log, cancel_checker=context.is_cancelled, glossary=glossary, ai_model=ai_model, target_lang=target_lang, custom_url=custom_url)
                except Exception as exc:
                    context.log(f"⚠️ [{os.path.basename(file_path)}] HQM 처리 경고: {exc}")
                    translated_hqm = hqm_content
                with open(target_path, 'wb') as f:
                    f.write(translated_hqm)
                context.log(f"✅ [{os.path.basename(file_path)}] HQM 번역 완료")
                completed_set.add(rel_path)
                _save_progress(out_dir, completed_set)
                continue
            elif file_path.lower().endswith('.lang'):
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.read().splitlines()

                ko_dict = {}
                if sibling_ko_path:
                    try:
                        with open(sibling_ko_path, 'r', encoding='utf-8', errors='ignore') as kf:
                            for kline in kf.read().splitlines():
                                if '=' in kline and not kline.strip().startswith('#'):
                                    kp = kline.split('=', 1)
                                    if len(kp) == 2 and kp[0].strip() and kp[1].strip():
                                        ko_dict[kp[0].strip()] = kp[1].strip()
                    except Exception:
                        pass

                targets = []
                for i, line in enumerate(lines):
                    if '=' in line and not line.strip().startswith('#'):
                        parts = line.split('=', 1)
                        if len(parts) == 2 and parts[1].strip() and not is_code_or_id(parts[1]):
                            raw_key = parts[0].strip()
                            en_val = parts[1].strip()
                            key_lower = raw_key.lower()

                            # 내장 ko_kr.lang에 이미 번역된 키가 있으면 기존 캐시 유무와 무관하게 무조건 공식 한글패치로 덮어쓰기
                            if raw_key in ko_dict:
                                ko_val = ko_dict[raw_key]
                                if ko_val and ko_val != en_val and translation_memory.is_valid_translation(en_val, ko_val, target_lang):
                                    lines[i] = f"{parts[0]}={ko_val}"
                                    harvested_ko_keys += 1
                                    is_it_key = key_lower.endswith('.name') or key_lower.startswith(('item.', 'block.', 'tile.', 'entity.'))
                                    translation_memory.add_official_ko_to_memory(en_val, ko_val, target_lang, is_item=is_it_key)
                                    continue
                            
                            # 1. 아이템/블록 이름 등은 번역에서 강제 제외 (호환성 보호)
                            if key_lower.endswith('.name') or key_lower.startswith('itemgroup.') or key_lower.startswith('key.'):
                                continue
                                
                            # 2. 퀘스트, 가이드북, 툴팁, 설명문 관련 키워드만 번역 허용 (쓸데없는 모드 번역 방지)
                            valid_keywords = ['quest', 'advancement', 'title', 'desc', 'text', 'book', 'page', 'tooltip', 'info', 'dialog', 'chapter', 'interactions']
                            if not any(kw in key_lower for kw in valid_keywords):
                                continue

                            # 3. 이미 번역되었거나 캐시/코드인 경우 건너뛰기
                            if _is_target_untranslated(parts[1]):
                                targets.append((i, parts[0] + "=", parts[1], ""))
                
                if not targets:
                    skipped_no_targets += 1
                    with open(target_path, 'w', encoding='utf-8', newline='\n') as f:
                        f.write('\n'.join(lines))
                    completed_set.add(rel_path)
                    _save_progress(out_dir, completed_set)
                    continue
                
                jobs.append({"kind": "lang", "target_path": target_path, "rel_path": rel_path,
                             "lines": lines, "targets": targets, "translated_map": {}})
            elif file_path.lower().endswith('.json'):
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    try:
                        data = json.load(f)
                    except json.JSONDecodeError:
                        shutil.copy2(file_path, target_path)
                        completed_set.add(rel_path)
                        _save_progress(out_dir, completed_set)
                        continue

                if sibling_ko_path and isinstance(data, dict):
                    try:
                        with open(sibling_ko_path, 'r', encoding='utf-8', errors='ignore') as kf:
                            ko_data = json.load(kf)
                        if isinstance(ko_data, dict):
                            for k_json, en_val in list(data.items()):
                                if isinstance(en_val, str) and en_val.strip() and k_json in ko_data and isinstance(ko_data[k_json], str):
                                    ko_val = ko_data[k_json].strip()
                                    if ko_val and ko_val != en_val.strip() and translation_memory.is_valid_translation(en_val, ko_val, target_lang):
                                        data[k_json] = ko_val
                                        harvested_ko_keys += 1
                                        is_it_key = k_json.lower().startswith(('item.', 'block.', 'tile.', 'entity.'))
                                        translation_memory.add_official_ko_to_memory(en_val, ko_val, target_lang, is_item=is_it_key)
                    except Exception:
                        pass

                from file_processors import collect_json_targets
                node_targets = []
                collect_json_targets(data, node_targets)
                if not node_targets:
                    skipped_no_targets += 1
                    with open(target_path, 'w', encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    completed_set.add(rel_path)
                    _save_progress(out_dir, completed_set)
                    continue

                # 이미 번역되었거나 캐시/코드인 노드는 번역 대상에서 제외
                untranslated_node_targets = [nt for nt in node_targets if _is_target_untranslated(nt[2])]
                if not untranslated_node_targets:
                    skipped_translated += 1
                    with open(target_path, 'w', encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    completed_set.add(rel_path)
                    _save_progress(out_dir, completed_set)
                    continue

                jobs.append({"kind": "json", "target_path": target_path, "rel_path": rel_path,
                             "data": data, "targets": untranslated_node_targets})

        if harvested_ko_keys > 0:
            context.log(f"🇰🇷 기존 ko_kr 언어 파일에서 {harvested_ko_keys:,}개의 공식 한글 키를 캐시로 가져와 번역 메모리에 저장했습니다.")
            import translation_engines
            if not getattr(translation_engines, "MOCK_MODE", False):
                translation_memory.save_memory()

        context.log(f"🔎 분석 완료: 총 {total_files}개 중 {len(jobs)}개 번역 대기 (건너뜀: 재개 {skipped_resume}개, 완료됨 {skipped_translated}개, 텍스트없음 {skipped_no_targets}개)")

        if not jobs:
            context.log("✅ 새로 번역할 파일이 없습니다. 모든 작업이 완료되었습니다.")
        else:
            try:
                samples = []
                for j in jobs:
                    if j["kind"] == "snbt":
                        for t in j["targets"]:
                            samples.append(t[2])
                    elif j["kind"] == "json":
                        for t in j["targets"]:
                            samples.append(t[2])
                
                context.set_status("🌐 클라우드 마스터 번역 메모리 확인 중...")
                translation_memory.load_memory()
                translation_memory.wait_for_cloud_sync(timeout=6.0)

                import random
                # 캐시에 없는 텍스트만 추출하여 용어 추출 필요 여부 판단
                uncached_samples = [s for s in samples if translation_memory.get_cached_translation(s, target_lang) is None]
                if len(uncached_samples) >= 50:
                    random.shuffle(uncached_samples)
                    sample_subset = uncached_samples[:100]
                    
                    context.log("🧠 [AI 자동 추출] 번역 시작 전 모드팩 핵심 단어를 추출하여 단어장을 진화시킵니다...")
                    from translation_engines import auto_extract_glossary
                    extracted_glossary = auto_extract_glossary(
                        sample_subset, engine_key, api_key, ai_model, target_lang, custom_url
                    )
                    
                    if extracted_glossary:
                        commented_glossary = {k: f"{v} # [Auto-Extracted]" for k, v in extracted_glossary.items()}
                        added_keys = list(commented_glossary.keys())[:5]
                        context.log(f"✨ 진화 완료! 새로 추가된 단어: {added_keys} 등 총 {len(commented_glossary)}개")
                        if isinstance(glossary, dict):
                            glossary.update(commented_glossary)
                        
                        if hasattr(context, 'app'):
                            app = context.app
                            app.app_state.glossary.update(commented_glossary)
                            if target_lang not in app.app_state.glossaries_by_lang:
                                app.app_state.glossaries_by_lang[target_lang] = {}
                            app.app_state.glossaries_by_lang[target_lang].update(commented_glossary)
                            if hasattr(app, "save_user_settings"):
                                app.save_user_settings()
                elif len(uncached_samples) > 0:
                    context.log(f"💾 새로 번역할 텍스트가 {len(uncached_samples)}개로 적어 용어 추출을 건너뜁니다.")
                else:
                    context.log("💾 모든 텍스트가 캐시에 있어 용어 추출이 필요 없습니다.")
            except Exception as e:
                context.log(f"⚠️ 용어 추출 중 오류가 발생했으나 번역은 계속 진행합니다: {e}")

            context.set_status(f"총 {len(jobs)}개 파일 번역 중...")
            
            def on_job_completed(job):
                if job["kind"] == "snbt":
                    text = job.get("final_text") or rebuild_snbt(job["lines"], job["translated_map"])
                    with open(job["target_path"], 'w', encoding='utf-8', newline='\n') as f:
                        f.write(text)
                elif job["kind"] == "lang":
                    lines = job["lines"].copy()
                    for i, trans_text in job["translated_map"].items():
                        lines[i] = trans_text
                    with open(job["target_path"], 'w', encoding='utf-8', newline='\n') as f:
                        f.write('\n'.join(lines))
                else:
                    with open(job["target_path"], 'w', encoding='utf-8') as f:
                        json.dump(job["data"], f, ensure_ascii=False, indent=2)
                completed_set.add(job["rel_path"])
                _save_progress(out_dir, completed_set)

            if getattr(context.app, "_translate_jobs_parallel", None):
                if engine_key == "gemini_batch":
                    context.app._translate_jobs_parallel(jobs, api_key, is_paid, ai_model=ai_model, target_lang=target_lang, on_job_completed=on_job_completed)
                else:
                    context.app._translate_jobs_sequential(jobs, engine_key, api_key, is_paid, ai_model=ai_model, target_lang=target_lang, on_job_completed=on_job_completed, custom_url=custom_url)

        context.log("\n" + translation_memory.get_cache_stats_summary(target_lang))

        if modpack_path:
            context.log("\n🎉 모든 번역 완료! 저장 및 적용 방식을 선택해주세요.")
            apply_mode = context.ask_apply_mode()
        else:
            context.log("\n🎉 모든 번역 완료! 저장할 위치를 선택해주세요.")
            apply_mode = False

        if apply_mode is None:
            context.log("⚠️ 저장 및 적용이 취소되었습니다. 임시 작업 폴더를 정리합니다.")
            shutil.rmtree(out_dir, ignore_errors=True)
            return

        if apply_mode is True:
            import temp_session_logger
            if temp_session_logger.is_debug_mode():
                context.log(f"\n🛡️ [샌드박스 디버그 모드] 원본 모드팩 보호: 원본 덮어쓰기({modpack_path})를 차단했습니다. 결과물은 임시 작업 폴더에만 안전 보관됩니다.")
                _generate_zip_review_report(context, raw_dir, out_dir, "디버그 모드 완료: 번역 검수 리포트")
                context.show_messagebox("info", "디버그 모드 완료", f"샌드박스 디버그 모드이므로 원본 모드팩 파일은 일절 수정되지 않았습니다!\n\n임시 작업 경로:\n{out_dir}")
                return
            context.log(f"\n📦 원본 모드팩에 즉시 덮어쓰기 시작: {modpack_path}")
            # _progress.json 파일은 모드팩에 유출되지 않도록 복사 전 제거
            prog_in_out = os.path.join(out_dir, PROGRESS_FILE)
            if os.path.exists(prog_in_out):
                try:
                    os.remove(prog_in_out)
                except Exception:
                    pass
            # 덮어쓰기 전 원본 퀘스트/언어 파일 안전 백업(.bak) 생성 (최초 1회 보존)
            for root_b, _, files_b in os.walk(out_dir):
                for fb in files_b:
                    if fb == PROGRESS_FILE:
                        continue
                    rel_b = os.path.relpath(os.path.join(root_b, fb), out_dir)
                    orig_target = os.path.join(modpack_path, rel_b)
                    bak_target = orig_target + ".bak"
                    if os.path.isfile(orig_target) and not os.path.exists(bak_target):
                        try:
                            shutil.copy2(orig_target, bak_target)
                        except Exception:
                            pass
            shutil.copytree(out_dir, modpack_path, dirs_exist_ok=True)
            # 모드팩 루트에 예전 버전에서 복사되었던 _progress.json 잔여물이 있다면 자동 제거
            prog_in_modpack = os.path.join(modpack_path, PROGRESS_FILE)
            if os.path.exists(prog_in_modpack):
                try:
                    os.remove(prog_in_modpack)
                except Exception:
                    pass
            context.log(f"💾 덮어쓰기 완료: {modpack_path}")
            
            _generate_zip_review_report(context, raw_dir, out_dir, "덮어쓰기 완료: 번역 검수 리포트")
            context.show_messagebox("info", "적용 완료", f"모드팩에 번역본이 성공적으로 덮어쓰기 되었습니다!\n\n적용 경로:\n{modpack_path}")
            
            context.on_translation_success(modpack_path)
            shutil.rmtree(out_dir, ignore_errors=True)
            return

        # apply_mode is False -> Save as ZIP
        import temp_session_logger
        if temp_session_logger.is_debug_mode():
            save_dir = temp_session_logger.get_temp_dir()
            context.log(f"🛡️ [디버그 모드] 원본 파일 보호: 번역된 ZIP 파일이 임시 폴더에 저장됩니다: {save_dir}")
        else:
            save_dir = context.ask_save_dir()
        if save_dir:
            out_zip_path = os.path.join(save_dir, base_zip_name)
            with zipfile.ZipFile(out_zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_out:
                for root, _, files_list in os.walk(out_dir):
                    for f in files_list:
                        if f == PROGRESS_FILE:
                            continue
                        full_p = os.path.join(root, f)
                        zip_out.write(full_p, os.path.relpath(full_p, out_dir))

            _generate_zip_review_report(context, raw_dir, out_dir, "ZIP 번역 검수 리포트")
            context.log(f"💾 압축 저장 완료: {out_zip_path}")
            
            if modpack_path:
                context.on_translation_success(modpack_path)

            context.show_messagebox("info", "완료", f"모든 작업이 완료되었습니다!\n\n저장 위치:\n{out_zip_path}")
            shutil.rmtree(out_dir, ignore_errors=True)
        else:
            context.log("⚠️ 저장 폴더 선택이 취소되었습니다. 임시 작업 폴더를 정리합니다.")
            shutil.rmtree(out_dir, ignore_errors=True)

    except TranslationCancelledError as e:
        context.log(f"\n🛑 {str(e)}")
        # pyrefly: ignore [unexpected-keyword]
        context.offer_partial_backup(out_dir, os.path.splitext(base_zip_name)[0] + '_partial.zip', error_msg="사용자에 의해 번역 작업이 중단되었습니다.")
    except QuotaExceededError as e:
        context.log(f"\n🛑 [중단] {str(e)}")
        # pyrefly: ignore [unexpected-keyword]
        context.offer_partial_backup(out_dir, os.path.splitext(base_zip_name)[0] + '_partial.zip', error_msg=str(e))
    except Exception as e:
        context.log(f"\n❌ 오류 발생: {str(e)}")
        # pyrefly: ignore [unexpected-keyword]
        context.offer_partial_backup(out_dir, os.path.splitext(base_zip_name)[0] + '_partial.zip', error_msg=f"오류가 발생했습니다:\n{str(e)}")
    finally:
        shutil.rmtree(raw_dir, ignore_errors=True)
