"""
번역 실행 (단일 파일, ZIP, 모드팩) 관련 메서드 믹스인.
"""
import os
import json
import shutil
import threading
import tempfile
import zipfile
import time
import translation_memory
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import customtkinter as ctk
    import tkinter as tk
    from tkinter import filedialog, messagebox
except Exception:
    ctk = None
    tk = None
    filedialog = None
    messagebox = None



from translation_engines import ENGINES, QuotaExceededError, TranslationCancelledError, translate_gemini_batch, translate_local_ai
from file_processors import (
    collect_json_targets, extract_snbt_targets,
    process_hqm_with_progress, process_json_safely,
    process_snbt_with_progress, rebuild_snbt,
)
from review_checks import (
    analyze_hqm_bytes, analyze_json_data, analyze_snbt_texts, render_review_report,
)
from constants import FONT_NAME, TARGET_EXTENSIONS, SCAN_IGNORE_DIRS, has_non_latin

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


class TranslationMixin:
    # ====================================================================
    # 파일 드롭
    # ====================================================================

    def handle_file_drop(self, event):
        engine_key, api_key, is_paid, ai_model, target_lang, custom_url = self.validate_inputs()
        self.app_state.cancel_requested = False
        if not engine_key:
            return
        dropped_path = event.data.strip('{}').strip('"')
        if not os.path.exists(dropped_path):
            return
        if dropped_path.lower().endswith(('.snbt', '.json', '.hqm')):
            threading.Thread(target=self._process_single_file,
                             args=(dropped_path, engine_key, api_key, is_paid, ai_model, target_lang, custom_url), daemon=True).start()
        elif dropped_path.lower().endswith('.zip'):
            threading.Thread(target=self._process_zip_file,
                             args=(dropped_path, engine_key, api_key, is_paid, ai_model, target_lang, None, custom_url), daemon=True).start()
        else:
            messagebox.showwarning("지원하지 않는 파일", ".snbt, .json, .hqm 또는 .zip 파일만 지원합니다.")

    # ====================================================================
    # 모드팩 자동 번역
    # ====================================================================

    def _create_temp_zip_from_modpack(self, modpack_dir):
        safe_name = os.path.basename(os.path.normpath(modpack_dir)) or "modpack"
        temp_zip_path = os.path.join(tempfile.gettempdir(), f"QuestTranslator_{safe_name}.zip")
        if os.path.exists(temp_zip_path):
            os.remove(temp_zip_path)
            
        added_count = 0
        with zipfile.ZipFile(temp_zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files_list in os.walk(modpack_dir):
                dirs[:] = [d for d in dirs if d.lower() not in SCAN_IGNORE_DIRS]
                
                for filename in files_list:
                    if not (filename.lower().endswith(TARGET_EXTENSIONS) or filename.lower().endswith('.lang')):
                        continue
                        
                    full_path = os.path.join(root, filename)
                    rel_path = os.path.relpath(full_path, modpack_dir)
                    norm_path = rel_path.lower().replace('\\', '/')
                    
                    # 언어 파일들 (translation_core.py에서 내용 기반으로 안전하게 퀘스트 관련 텍스트만 깐깐하게 필터링하므로 모두 포함해도 안전함)
                    is_quest_lang = (filename.lower() in ["en_us.json", "en_us.lang"] and 
                                     ("lang" in norm_path or "resources" in norm_path))
                    
                    if not is_quest_lang:
                        # 나머지 퀘스트 파일들은 무조건 config/ 하위에 있어야 함
                        if not (norm_path.startswith("config/ftbquests/") or 
                                norm_path.startswith("config/betterquesting/") or 
                                norm_path.startswith("config/hqm/") or 
                                norm_path.startswith("config/heracles/")):
                            continue

                        if norm_path.startswith("config/ftbquests/"):
                            if "/reward_tables/" in norm_path:
                                continue
                            if filename.lower() in ["data.snbt", "chapter_groups.snbt"]:
                                continue
                                
                        if norm_path.startswith("config/betterquesting/"):
                            is_valid = (filename.lower() in ["defaultquests.json", "questdatabase.json", "questlines.json"] or 
                                        "/chapter" in norm_path or 
                                        "/questline" in norm_path or
                                        "/quest" in norm_path)
                            if not is_valid:
                                continue
                                
                        if norm_path.startswith("config/hqm/"):
                            is_valid = (filename.lower() in ["quests.hqm", "defaultquests.json"] or 
                                        "/chapter/" in norm_path or 
                                        "/chapters/" in norm_path or
                                        "/quests/" in norm_path)
                            if not is_valid:
                                continue
                            
                    zf.write(full_path, rel_path)
                    added_count += 1
        return temp_zip_path, added_count

    def run_selected_modpack(self):
        engine_key, api_key, is_paid, ai_model, target_lang, custom_url = self.validate_inputs()
        if not engine_key:
            return

        modpack_dir = self.selected_modpack_path
        if not modpack_dir:
            messagebox.showwarning("경고", "먼저 인스턴스 경로를 선택하고 모드팩을 탐지해주세요.")
            return

        def run_instance_translation():
            temp_zip_path = None
            try:
                temp_zip_path, file_count = self._create_temp_zip_from_modpack(modpack_dir)
                if file_count > 0:
                    self.log(f"🚀 선택 모드팩 퀘스트 자동 번역 시작: {os.path.basename(modpack_dir)} ({file_count} files)")
                    self._process_zip_file(temp_zip_path, engine_key, api_key, is_paid, ai_model=ai_model, target_lang=target_lang, modpack_path=modpack_dir, custom_url=custom_url, toggle_ui=False)
                else:
                    self.log(f"⚠️ 선택한 모드팩에서 퀘스트 번역 대상 파일을 찾지 못했습니다.")


                        
            except Exception as exc:
                self.log(f"❌ 인스턴스 번역 중 오류: {exc}")
                self.show_messagebox("error", "오류", f"인스턴스 번역 중 오류가 발생했습니다:\n{exc}")
            finally:
                if temp_zip_path and os.path.exists(temp_zip_path):
                    try:
                        os.remove(temp_zip_path)
                    except Exception:
                        pass
                self.toggle_buttons(True)
                self.update_progress(1.0)
                self.set_status("대기 중")
                
        threading.Thread(target=run_instance_translation, daemon=True).start()

    def _translate_lang_files(self, engine_key, api_key, is_paid, ai_model, target_lang, modpack_dir, custom_url=None):
        try:
                self.log("🗂️ 전체 모드(.lang) 번역 및 리소스팩 생성을 시작합니다...")
                import mod_jar_extractor
                import file_processors
                import re
                
                mods_dir = os.path.join(modpack_dir, "mods")
                if not os.path.isdir(mods_dir):
                    self.log("⚠️ mods 폴더를 찾을 수 없어 언어 파일을 번역할 수 없습니다.")
                    return
                    
                langs_map = mod_jar_extractor.extract_lang_files_from_jars(mods_dir, log_callback=self.log)
                if not langs_map:
                    self.log("⚠️ 번역할 .lang 파일을 찾지 못했습니다.")
                    return
                
                self.log(f"✅ 총 {len(langs_map)}개의 모드에서 .lang 파일을 추출했습니다.")
                
                all_targets = [] # (jar_name, zip_path, line_idx, key, value, is_short_item)
                
                def is_book_or_desc_key(key):
                    k = key.lower()
                    keywords = ['book', 'manual', 'guide', 'lexicon', 'tome', 'entry', 'page', 'lore', 'desc', 'tooltip', 'info', 'text']
                    return any(kw in k for kw in keywords)

                def should_append_english(key, text):
                    words = text.split()
                    if len(words) >= 5 or any(p in text for p in ['.', '!', '?']):
                        return False
                    if is_book_or_desc_key(key):
                        return False
                    return True

                parsed_langs_map = {}
                for jar_name, zip_dict in langs_map.items():
                    parsed_langs_map[jar_name] = {}
                    for zip_path, content in zip_dict.items():
                        if zip_path.lower().endswith('.json') or zip_path.lower().endswith('.json5'):
                            try:
                                import json
                                json_data = json.loads(content)
                                parsed_lines = {}
                                for k, v in json_data.items():
                                    if isinstance(v, str) and v.strip():
                                        is_short = should_append_english(k, v)
                                        all_targets.append((jar_name, zip_path, k, k, v, is_short))
                                        parsed_lines[k] = v
                                parsed_langs_map[jar_name][zip_path] = ('json', parsed_lines, json_data)
                            except Exception as e:
                                self.log(f"⚠️ {jar_name}의 {zip_path} 파싱 실패: {e}")
                        else:
                            lines = content.splitlines()
                            parsed_lines = []
                            for idx, line in enumerate(lines):
                                if '=' in line and not line.strip().startswith('#'):
                                    k, v = line.split('=', 1)
                                    k = k.strip()
                                    v = v.strip()
                                    if v:
                                        is_short = should_append_english(k, v)
                                        all_targets.append((jar_name, zip_path, idx, k, v, is_short))
                                    parsed_lines.append([k, v]) # [0] key, [1] value
                                else:
                                    parsed_lines.append(line) # 그냥 텍스트
                            parsed_langs_map[jar_name][zip_path] = ('lang', parsed_lines, None)
                
                # 중복 제거 (Batch 번역 최적화)
                unique_texts = list(dict.fromkeys([t[4] for t in all_targets]))
                self.log(f"🧠 총 {len(all_targets)}개의 텍스트 중 중복을 제거한 {len(unique_texts)}개의 고유 문장을 번역합니다.")
                
                # 용어 자동 추출: 실제로 새로 번역할 텍스트가 충분히 많을 때만 실행 (캐시 히트 시 스킵하여 API 비용 절약)
                uncached_for_glossary = [t for t in unique_texts if translation_memory.get_cached_translation(t, target_lang) is None]
                try:
                    import random
                    if len(uncached_for_glossary) >= 50:
                        samples = uncached_for_glossary.copy()
                        random.shuffle(samples)
                        sample_subset = samples[:100]
                        self.log("🧠 [AI 자동 추출] 번역 시작 전 .lang 파일의 핵심 단어를 추출하여 단어장을 진화시킵니다...")
                        from translation_engines import auto_extract_glossary
                        extracted_glossary = auto_extract_glossary(
                            sample_subset, engine_key, api_key, ai_model, target_lang, custom_url=None
                        )
                        if extracted_glossary:
                            commented_glossary = {k: f"{v} # [Auto-Extracted]" for k, v in extracted_glossary.items()}
                            added_keys = list(commented_glossary.keys())[:5]
                            self.log(f"✨ 진화 완료! 새로 추가된 단어: {added_keys} 등 총 {len(commented_glossary)}개")
                            self.app_state.glossary.update(commented_glossary)
                            if target_lang not in self.app_state.glossaries_by_lang:
                                self.app_state.glossaries_by_lang[target_lang] = {}
                            self.app_state.glossaries_by_lang[target_lang].update(commented_glossary)
                            if hasattr(self, "save_user_settings"):
                                self.save_user_settings()
                    elif len(uncached_for_glossary) > 0:
                        self.log(f"💾 새로 번역할 텍스트가 {len(uncached_for_glossary)}개로 적어 용어 추출을 건너뜁니다.")
                    else:
                        self.log("💾 모든 텍스트가 캐시에 있어 용어 추출이 필요 없습니다.")
                except Exception as e:
                    self.log(f"⚠️ 용어 추출 중 오류가 발생했으나 번역은 계속 진행합니다: {e}")

                def check_cancel():
                    return self.app_state.cancel_requested
                
                def local_log(msg):
                    self.log(msg)
                    
                def local_progress(current, total):
                    self.update_progress(current / max(1, total))

                glossary_map = self.app_state.glossaries_by_lang.get(target_lang, {})
                translated_unique = file_processors._run_batch_jobs(
                    unique_texts,
                    lambda x: x,
                    engine_key, api_key, is_paid,
                    local_log, check_cancel, local_progress,
                    reference_map=glossary_map, glossary=glossary_map, ai_model=ai_model, target_lang=target_lang, log_prefix="모드 텍스트 번역 중"
                )
                
                translation_dict = dict(zip(unique_texts, translated_unique))
                
                self.log("📦 번역된 텍스트를 .lang 구조에 맞게 조립합니다...")
                import re
                def validate_format_specifiers(orig, trans):
                    if not orig or not trans: return False
                    # Minecraft uses Java String.format, e.g., %s, %d, %1$s, %.2f, %%
                    pattern = re.compile(r'%(\d+\$)?[-+#0 ]*\d*(?:\.\d+)?[a-zA-Z%]')
                    orig_matches = sorted(pattern.findall(orig))
                    trans_matches = sorted(pattern.findall(trans))
                    return orig_matches == trans_matches

                # 재조립
                for (jar_name, zip_path, idx, k, v, is_short) in all_targets:
                    trans_v = translation_dict.get(v, v)
                    
                    # 🚀 크래시 방지 로직: 원본에 %s 같은 포맷 문자가 있는데 번역본에서 누락되었거나 변형되었다면 원본으로 복구
                    if trans_v != v and not validate_format_specifiers(v, trans_v):
                        trans_v = v

                    if trans_v and is_short and str(trans_v).strip() != str(v).strip():
                        # 아이템 이름이면 영어 병기 (한국어 이름 (English Name))
                        # 단, 원본에 %s 등 포맷 지정자가 있으면 병기하면 안됨 (개수가 2배로 불어나서 크래시)
                        has_fmt = re.search(r'%(\d+\$)?[a-zA-Z]', v)
                        if has_fmt:
                            final_v = trans_v
                        else:
                            final_v = f"{trans_v} ({v})"
                    else:
                        final_v = trans_v if trans_v else v
                    
                    if parsed_langs_map[jar_name][zip_path][0] == 'lang':
                        parsed_langs_map[jar_name][zip_path][1][idx][1] = final_v
                    else:
                        parsed_langs_map[jar_name][zip_path][1][idx] = final_v

                # 다시 문자열로 합치기
                final_langs_map = {}
                for jar_name, zip_dict in parsed_langs_map.items():
                    final_langs_map[jar_name] = {}
                    for zip_path, (file_type, lines_or_dict, orig_json) in zip_dict.items():
                        if file_type == 'lang':
                            out_lines = []
                            for line in lines_or_dict:
                                if isinstance(line, list):
                                    out_lines.append(f"{line[0]}={line[1]}")
                                else:
                                    out_lines.append(line)
                            final_langs_map[jar_name][zip_path] = "\n".join(out_lines)
                        else:
                            import json
                            for k, v in lines_or_dict.items():
                                orig_json[k] = v
                            final_langs_map[jar_name][zip_path] = json.dumps(orig_json, ensure_ascii=False, indent=2)
                
                self.log("✨ 전체 모드(.lang) 번역 처리가 완료되었습니다!")
                return final_langs_map

        except Exception as exc:
            if "사용자에 의해 번역이 취소되었습니다" in str(exc) or getattr(self.app_state, 'cancel_requested', False):
                self.log("⚠️ 사용자가 언어 파일 번역을 취소했습니다.")
            else:
                self.log(f"❌ 언어 파일 번역 중 오류: {exc}")
                self.show_messagebox("error", "오류", f"언어 파일 번역 중 오류가 발생했습니다:\n{exc}")

    def _translate_patchouli_books(self, engine_key, api_key, is_paid, ai_model, target_lang, modpack_dir, custom_url=None):
        try:
                self.log("📚 가이드북(Patchouli) 전용 번역 및 리소스팩 생성을 시작합니다...")
                import mod_jar_extractor
                import patchouli_processor
                
                mods_dir = os.path.join(modpack_dir, "mods")
                if not os.path.isdir(mods_dir):
                    self.log("⚠️ mods 폴더를 찾을 수 없어 가이드북을 번역할 수 없습니다.")
                    return {}
                    
                books_map = mod_jar_extractor.find_patchouli_books_in_jars(mods_dir, log_callback=self.log)
                if not books_map:
                    self.log("⚠️ 번역할 가이드북을 찾지 못했습니다.")
                    return {}
                
                total_pages = sum(len(pages) for pages in books_map.values())
                
                partial_pack_path = os.path.join(modpack_dir, "QuestTranslatorPro_Patchouli_Pack_Partial.zip")
                partial_books_map = {}
                import tkinter.messagebox as mb
                if os.path.exists(partial_pack_path):
                    if mb.askyesno("이어하기", "이전에 중단된 부분 번역 리소스팩이 발견되었습니다.\n이어서 번역하시겠습니까? (이전에 번역된 페이지는 API를 소모하지 않고 건너뜁니다)"):
                        self.log("🔄 부분 번역 데이터에서 진행 상황을 복구합니다...")
                        import zipfile
                        import json
                        try:
                            with zipfile.ZipFile(partial_pack_path, 'r') as zf:
                                for name in zf.namelist():
                                    if name.endswith('.json'):
                                        try:
                                            partial_books_map[name] = json.loads(zf.read(name).decode('utf-8'))
                                        except Exception:
                                            pass
                            self.log(f"✅ 총 {len(partial_books_map)}개의 페이지 복구 완료!")
                        except Exception as e:
                            self.log(f"⚠️ 부분 번역 파일 복구 실패: {e}")
                
                self.log(f"✅ 총 {total_pages}개의 가이드북 페이지 번역을 진행합니다.")
                
                translated_books_map = {}
                all_targets = []  # (jar_name, zip_path, json_data, node, k, protected_text, mapping)
                
                def check_cancel():
                    return self.app_state.cancel_requested
                    
                processed_pages = 0
                for jar_name, pages in books_map.items():
                    if check_cancel(): break
                    translated_books_map[jar_name] = {}
                    for zip_path, json_data in pages.items():
                        if check_cancel(): break
                        
                        if zip_path in partial_books_map:
                            translated_books_map[jar_name][zip_path] = partial_books_map[zip_path]
                            processed_pages += 1
                            continue
                        
                        targets = []
                        patchouli_processor.collect_patchouli_targets(json_data, targets)
                        
                        if not targets:
                            translated_books_map[jar_name][zip_path] = json_data
                            processed_pages += 1
                            continue
                            
                        # json_data는 in-place로 수정될 것이므로 미리 저장
                        translated_books_map[jar_name][zip_path] = json_data
                        
                        for node, k, v in targets:
                            protected_text, mapping = patchouli_processor.protect_patchouli_formatting(v)
                            all_targets.append((node, k, protected_text, mapping))
                
                if not check_cancel() and all_targets:
                    self.log(f"📝 가이드북 전체에서 번역할 텍스트 노드 {len(all_targets)}개를 수집했습니다.")
                    original_texts = [t[2] for t in all_targets]
                    
                    # 중복 텍스트 제거 (API 비용/RPD 절감)
                    unique_texts = list(dict.fromkeys(original_texts))
                    self.log(f"✂️ 중복 제거 후 실제 번역할 고유 텍스트는 {len(unique_texts)}개 입니다. 일괄 번역 시작!")
                    
                    # 용어 자동 추출: 실제로 새로 번역할 텍스트가 충분히 많을 때만 실행
                    uncached_patchouli = [t for t in unique_texts if translation_memory.get_cached_translation(t, target_lang) is None]
                    try:
                        import random
                        if len(uncached_patchouli) >= 50:
                            samples = uncached_patchouli.copy()
                            random.shuffle(samples)
                            sample_subset = samples[:100]
                            self.log("🧠 [AI 자동 추출] 번역 시작 전 가이드북의 핵심 단어를 추출하여 단어장을 진화시킵니다...")
                            from translation_engines import auto_extract_glossary
                            extracted_glossary = auto_extract_glossary(
                                sample_subset, engine_key, api_key, ai_model, target_lang, custom_url=None
                            )
                            if extracted_glossary:
                                commented_glossary = {k: f"{v} # [Auto-Extracted]" for k, v in extracted_glossary.items()}
                                added_keys = list(commented_glossary.keys())[:5]
                                self.log(f"✨ 진화 완료! 새로 추가된 단어: {added_keys} 등 총 {len(commented_glossary)}개")
                                self.app_state.glossary.update(commented_glossary)
                                if target_lang not in self.app_state.glossaries_by_lang:
                                    self.app_state.glossaries_by_lang[target_lang] = {}
                                self.app_state.glossaries_by_lang[target_lang].update(commented_glossary)
                                if hasattr(self, "save_user_settings"):
                                    self.save_user_settings()
                        elif len(uncached_patchouli) > 0:
                            self.log(f"💾 새로 번역할 텍스트가 {len(uncached_patchouli)}개로 적어 용어 추출을 건너뜁니다.")
                        else:
                            self.log("💾 모든 가이드북 텍스트가 캐시에 있어 용어 추출이 필요 없습니다.")
                    except Exception as e:
                        self.log(f"⚠️ 용어 추출 중 오류가 발생했으나 번역은 계속 진행합니다: {e}")
                    
                    import file_processors
                    from translation_engines import translate_deepl, translate_openai, translate_google
                    
                    def prog_cb(c, t):
                        self.update_progress(c / t if t > 0 else 1)
                        self.set_status(f"⏳ 가이드북 텍스트 일괄 번역 중... [{c}/{t}]")
                    
                    glossary_map = self.app_state.glossaries_by_lang.get(target_lang, {})
                    if engine_key in ("gemini_batch", "local_ai"):
                        translated_unique = file_processors._run_batch_jobs(
                            unique_texts, lambda x: x, engine_key, api_key, is_paid,
                            log_callback=lambda m: None, cancel_checker=check_cancel, progress_callback=prog_cb,
                            reference_map=glossary_map, glossary=glossary_map,
                            ai_model=ai_model, target_lang=target_lang, log_prefix="가이드북 번역", custom_url=custom_url
                        )
                    else:
                        translated_unique = []
                        for idx, t in enumerate(unique_texts, 1):
                            if check_cancel(): break
                            if engine_key == "deepl":
                                trans = translate_deepl(t, api_key, reference_map=glossary_map, target_lang=target_lang)
                            elif engine_key == "openai":
                                trans = translate_openai(t, api_key, reference_map=glossary_map, glossary=glossary_map, ai_model=ai_model, target_lang=target_lang)
                            else:
                                trans = translate_google(t, api_key, reference_map=glossary_map, target_lang=target_lang)
                            translated_unique.append(trans)
                            if idx % 5 == 0:
                                prog_cb(idx, len(unique_texts))
                                
                    # 고유 텍스트 번역 결과를 다시 원래 리스트 길이로 매핑
                    translation_dict = {u: t for u, t in zip(unique_texts, translated_unique)}
                    translated_texts = [translation_dict.get(orig, orig) for orig in original_texts]
                                
                    for i, (node, k, protected_text, mapping) in enumerate(all_targets):
                        if i < len(translated_texts) and translated_texts[i]:
                            t_text = translated_texts[i]
                            t_text = patchouli_processor.restore_patchouli_formatting(t_text, mapping)
                            node[k] = t_text
                
                if not check_cancel():
                    self.log(f"🎉 가이드북 번역 데이터 생성 완료!")
                    return translated_books_map
                else:
                    self.log("⚠️ 번역이 취소되었습니다.")
                    return translated_books_map
        except Exception as exc:
            if "취소" in str(exc) or getattr(self.app_state, 'cancel_requested', False):
                self.log("⚠️ 번역이 취소되었습니다.")
                return locals().get('translated_books_map', {})
            else:
                self.log(f"❌ 가이드북 번역 중 오류: {exc}")
                self.show_messagebox("error", "오류", f"가이드북 번역 중 오류가 발생했습니다:\n{exc}")
                return {}

    def _translate_custom_books(self, engine_key, api_key, is_paid, ai_model, target_lang, modpack_dir, custom_url=None):
        try:
            import mod_jar_extractor
            import custom_book_processor
            import file_processors
            from translation_engines import translate_deepl, translate_openai, translate_google
            
            mods_dir = os.path.join(modpack_dir, "mods")
            if not os.path.isdir(mods_dir):
                return {}
                
            books_map = mod_jar_extractor.find_custom_guidebooks_in_jars(mods_dir, log_callback=self.log)
            if not books_map or not any(books_map.values()):
                self.log("⚠️ 번역할 커스텀 가이드북을 찾지 못했습니다.")
                return {}
                
            def check_cancel():
                return getattr(self.app_state, 'cancel_requested', False)
                
            final_custom_map = {"mcjty": {}, "forestry": {}, "markdown": {}, "eu2": {}, "pi_xml": {}}
            
            def do_translation(texts, log_prefix):
                if not texts: return []
                self.log(f"🔎 {log_prefix}: 고유 문장 {len(texts)}개 번역 시작...")
                def prog_cb(c, t):
                    self.update_progress(c / t if t > 0 else 1)
                    self.set_status(f"💬 {log_prefix} 매뉴얼 번역 중... [{c}/{t}]")
                glossary_map = self.app_state.glossaries_by_lang.get(target_lang, {})
                
                if engine_key in ("gemini_batch", "local_ai"):
                    translated = file_processors._run_batch_jobs(
                        texts, lambda x: x, engine_key, api_key, is_paid,
                        log_callback=lambda m: None, cancel_checker=check_cancel, progress_callback=prog_cb,
                        reference_map=glossary_map, glossary=glossary_map,
                        ai_model=ai_model, target_lang=target_lang, log_prefix=log_prefix, custom_url=custom_url
                    )
                else:
                    translated = []
                    for idx, t in enumerate(texts, 1):
                        if check_cancel(): break
                        if engine_key == "deepl":
                            trans = translate_deepl(t, api_key, reference_map=glossary_map, target_lang=target_lang)
                        elif engine_key == "openai":
                            trans = translate_openai(t, api_key, reference_map=glossary_map, glossary=glossary_map, ai_model=ai_model, target_lang=target_lang)
                        else:
                            trans = translate_google(t, api_key, reference_map=glossary_map, target_lang=target_lang)
                        translated.append(trans)
                        if idx % 5 == 0: prog_cb(idx, len(texts))
                return translated

            # --- 1. McJty (XNet, RFTools, etc) ---
            mcjty_map = books_map.get("mcjty", {})
            if mcjty_map:
                self.log("🔎 McJty 매뉴얼 텍스트 추출 중...")
                unique_texts, parsed_map = custom_book_processor.extract_mcjty_texts(mcjty_map)
                
                if unique_texts:
                    translated_unique = do_translation(unique_texts, "McJty")
                    if check_cancel(): return {}
                    
                    trans_dict = {orig: trans for orig, trans in zip(unique_texts, translated_unique)}
                    final_custom_map["mcjty"] = custom_book_processor.assemble_mcjty_books(parsed_map, trans_dict)
                    self.log("✅ McJty 번역 재조립 완료!")
                    
            # --- 2. Forestry ---
            forestry_map = books_map.get("forestry", {})
            if forestry_map:
                self.log("📖 Forestry 매뉴얼 텍스트 추출 중...")
                unique_texts = custom_book_processor.extract_forestry_texts(forestry_map)
                
                if unique_texts:
                    translated_unique = do_translation(unique_texts, "Forestry")
                    if check_cancel(): return {}
                    
                    trans_dict = {orig: trans for orig, trans in zip(unique_texts, translated_unique)}
                    final_custom_map["forestry"] = custom_book_processor.assemble_forestry_books(forestry_map, trans_dict)
                    self.log("✅ Forestry 번역 및 조립 완료!")
            

            # --- 3. Markdown (OpenComputers, BuildCraft) ---
            markdown_map = books_map.get("markdown", {})
            if markdown_map:
                self.log("📖 마크다운 매뉴얼(OpenComputers 등) 텍스트 추출 중...")
                unique_texts = custom_book_processor.extract_markdown_texts(markdown_map)
                
                if unique_texts:
                    translated_unique = do_translation(unique_texts, "마크다운")
                    if check_cancel(): return {}
                    
                    trans_dict = {orig: trans for orig, trans in zip(unique_texts, translated_unique)}
                    final_custom_map["markdown"] = custom_book_processor.assemble_markdown_books(markdown_map, trans_dict)
                    self.log("✅ 마크다운 번역 재조립 완료!")

            # --- 4. Extra Utilities 2 (en_us.json) ---
            eu2_map = books_map.get("eu2", {})
            if eu2_map:
                self.log("🔎 Extra Utilities 2 매뉴얼 텍스트 추출 중...")
                unique_texts = custom_book_processor.extract_eu2_texts(eu2_map)
                
                if unique_texts:
                    translated_unique = do_translation(unique_texts, "EU2")
                    if check_cancel(): return {}
                    
                    trans_dict = {orig: trans for orig, trans in zip(unique_texts, translated_unique)}
                    final_custom_map["eu2"] = custom_book_processor.assemble_eu2_books(eu2_map, trans_dict)
                    self.log("✅ Extra Utilities 2 번역 재조립 완료!")

            # --- 5. Project Intelligence (Draconic Evolution XML) ---
            pi_xml_map = books_map.get("pi_xml", {})
            if pi_xml_map:
                self.log("🔎 Project Intelligence XML 텍스트 추출 중...")
                unique_texts = custom_book_processor.extract_pi_xml_texts(pi_xml_map)
                
                if unique_texts:
                    translated_unique = do_translation(unique_texts, "ProjectIntel")
                    if check_cancel(): return {}
                    
                    trans_dict = {orig: trans for orig, trans in zip(unique_texts, translated_unique)}
                    final_custom_map["pi_xml"] = custom_book_processor.assemble_pi_xml_books(pi_xml_map, trans_dict)
                    self.log("✅ Project Intelligence 번역 재조립 완료!")

            return final_custom_map
            
        except Exception as exc:
            if "취소" in str(exc) or getattr(self.app_state, 'cancel_requested', False):
                self.log("⚠️ 번역이 취소되었습니다.")
                return {}
            else:
                self.log(f"❌ 커스텀 가이드북 번역 중 오류: {exc}")
                self.show_messagebox("error", "오류", f"커스텀 가이드북 번역 중 오류가 발생했습니다:\n{exc}")
                return {}

    # ====================================================================
    # 단일 파일 번역
    # ====================================================================

    def run_single_file(self):
        engine_key, api_key, is_paid, ai_model, target_lang, custom_url = self.validate_inputs()
        if not engine_key:
            return
        file_path = filedialog.askopenfilename(
            title="번역할 파일 선택",
            filetypes=[("Quest Files", "*.snbt *.json *.hqm"), ("All Files", "*.*")]
        )
        if not file_path:
            return
        threading.Thread(target=self._process_single_file,
                         args=(file_path, engine_key, api_key, is_paid, ai_model, target_lang), daemon=True).start()

    def _process_single_file(self, file_path, engine_key, api_key, is_paid, ai_model=None, target_lang="한국어 (Korean)", custom_url=None):
        self.app_state.cancel_requested = False
        self.after(0, lambda: getattr(self, "show_translate_screen")(force=True))
        self.toggle_buttons(False)
        self.update_progress(0)

        file_name = os.path.basename(file_path)
        mode_str = "[유료/초고속]" if is_paid else "[무료/안전대기]"
        engine_label = next((name for name, key in ENGINES.items() if key == engine_key), engine_key)
        self.log(f"\n🎯 파일 작업 시작: '{file_name}'")
        self.log(f"🧩 선택 옵션: 엔진={engine_label} / 모드={mode_str}")

        def progress_cb(current, total):
            prog = current / total if total > 0 else 1
            self.update_progress(prog)
            self.set_status(f"⏳ 번역 진행 중... [{current}/{total}] ({int(prog * 100)}%)")

        try:
            translated_content = None
            json_data = None
            source_content = None
            source_json_data = None

            if file_path.lower().endswith('.snbt'):
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                source_content = content
                translated_content = process_snbt_with_progress(
                    content, engine_key, api_key, is_paid, progress_cb, self.route_log, self.is_cancelled, glossary=getattr(self, 'glossary', {}), ai_model=ai_model, target_lang=target_lang, custom_url=custom_url)
            elif file_path.lower().endswith('.json'):
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    try:
                        json_data = json.load(f)
                    except json.JSONDecodeError as e:
                        raise Exception(f"'{file_name}' 파일이 올바른 JSON 형식이 아닙니다 ({e.lineno}:{e.colno}: {e.msg})")
                source_json_data = json.loads(json.dumps(json_data, ensure_ascii=False))
                process_json_safely(json_data, engine_key, api_key, is_paid, progress_cb, self.route_log, self.is_cancelled, glossary=getattr(self, 'glossary', {}), ai_model=ai_model, target_lang=target_lang, custom_url=custom_url)
            elif file_path.lower().endswith('.hqm'):
                with open(file_path, 'rb') as f:
                    content = f.read()
                source_content = content
                try:
                    translated_content = process_hqm_with_progress(
                        content, engine_key, api_key, is_paid, progress_cb, self.route_log, self.is_cancelled, glossary=getattr(self, 'glossary', {}), ai_model=ai_model, target_lang=target_lang, custom_url=custom_url)
                except ValueError as exc:
                    self.log(f"⚠️ HQM 처리 경고: {exc}")
                    translated_content = content

            self.update_progress(1.0)
            self.log("\n✅ 모든 텍스트 번역 완료! 저장할 위치를 선택해주세요.")
            save_dir = self._pick_dir_main("번역된 파일을 저장할 폴더 선택")

            if save_dir:
                out_path = os.path.join(save_dir, file_name)
                review_reports = []

                if file_path.lower().endswith('.snbt') and translated_content is not None:
                    with open(out_path, 'w', encoding='utf-8', newline='\n') as f:
                        f.write(translated_content)
                    if source_content is not None:
                        review_reports.append((file_name, analyze_snbt_texts(source_content, translated_content)))
                elif file_path.lower().endswith('.hqm') and translated_content is not None:
                    with open(out_path, 'wb') as f:
                        f.write(translated_content)
                    if source_content is not None:
                        review_reports.append((file_name, analyze_hqm_bytes(source_content, translated_content)))
                elif file_path.lower().endswith('.json') and json_data is not None:
                    with open(out_path, 'w', encoding='utf-8') as f:
                        json.dump(json_data, f, ensure_ascii=False, indent=2)
                    if source_json_data is not None:
                        review_reports.append((file_name, analyze_json_data(source_json_data, json_data)))

                if review_reports:
                    report_text = render_review_report("번역 검수 리포트", review_reports)
                    self.show_review_report(report_text)
                    self.log("🧪 검수 리포트가 결과창으로 표시되었습니다.")

                self.log(f"💾 저장 성공: {out_path}")
                self.show_messagebox("info", "완료", f"성공적으로 번역되어 저장되었습니다!\n\n저장 위치:\n{out_path}")
            else:
                self.log("⚠️ 저장 폴더 선택이 취소되었습니다.")

        except TranslationCancelledError as e:
            self.log(f"\n🛑 {str(e)}")
            self.show_messagebox("warning", "취소됨", "사용자에 의해 번역 작업이 중단되었습니다.")
        except QuotaExceededError as e:
            self.log(f"\n🛑 [중단] {str(e)}")
            self.show_messagebox("error", "한도 초과", str(e))
        except Exception as e:
            self.log(f"\n❌ 오류 발생: {str(e)}")
            self.show_messagebox("error", "오류", f"오류가 발생했습니다:\n{str(e)}")
        finally:
            self.toggle_buttons(True)

    # ====================================================================
    # ZIP 번역
    # ====================================================================

    def run_zip_file(self):
        engine_key, api_key, is_paid, ai_model, target_lang, custom_url = self.validate_inputs()
        if not engine_key:
            return
        zip_path = filedialog.askopenfilename(
            title="번역할 ZIP 선택",
            filetypes=[("ZIP Files", "*.zip"), ("All Files", "*.*")]
        )
        if not zip_path:
            return
        threading.Thread(target=self._process_zip_file,
                         args=(zip_path, engine_key, api_key, is_paid, ai_model, target_lang), daemon=True).start()

    def _translate_jobs_parallel(self, jobs, api_key, is_paid, ai_model=None, target_lang="한국어 (Korean)", on_job_completed=None):
        batch_size = 500 if is_paid else 150
        
        all_targets = []
        for job in jobs:
            job["tasks_total"] = len(job["targets"])
            job["tasks_done"] = 0
            for item in job["targets"]:
                all_targets.append((job, item))
                
        total_items = len(all_targets)
        completed_items = 0
        lock = threading.Lock()
        
        chunks = []
        for i in range(0, total_items, batch_size):
            chunks.append(all_targets[i:i + batch_size])

        def run_chunk(chunk):
            if self.is_cancelled():
                raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")
                
            orig_texts = [item[2].replace('\\"', '"') if job["kind"] == "snbt" else item[2] for job, item in chunk]
            translated_texts = translate_gemini_batch(orig_texts, api_key, is_paid, self.route_log, self.is_cancelled, ai_model=ai_model, target_lang=target_lang)
            
            jobs_to_check = []
            for (job, item), orig_text, trans in zip(chunk, orig_texts, translated_texts):
                # 캐시에 번역 결과 저장
                if trans:
                    translation_memory.add_to_memory(orig_text, trans, target_lang)
                    
                if job["kind"] == "snbt":
                    line_idx, prefix, _, suffix = item
                    job["translated_map"][line_idx] = f'{prefix}"{str(trans).replace(chr(34), chr(92)+chr(34))}"{suffix}'
                elif job["kind"] == "lang":
                    line_idx, prefix, _, suffix = item
                    job["translated_map"][line_idx] = f'{prefix}{trans}{suffix}'
                else:
                    parent_node, key, _ = item
                    parent_node[key] = trans
                
                with lock:
                    job["tasks_done"] += 1
                    if job["tasks_done"] == job["tasks_total"]:
                        jobs_to_check.append(job)
                        
            for job in jobs_to_check:
                if on_job_completed:
                    on_job_completed(job)
            
            return len(chunk)

        max_w = min(8, len(chunks)) if is_paid else 1
        max_w = max_w or 1
        executor = ThreadPoolExecutor(max_workers=max_w)
        try:
            futures = [executor.submit(run_chunk, c) for c in chunks]
            for future in as_completed(futures):
                n = future.result()
                with lock:
                    completed_items += n
                self.set_status(f"⏳ Gemini API 묶음 번역 진행 중... [{completed_items}/{total_items}]")
                self.update_progress(completed_items / total_items if total_items else 1)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
            translation_memory.save_memory()

    def _translate_jobs_sequential(self, jobs, engine_key, api_key, is_paid, ai_model=None, target_lang="한국어 (Korean)", on_job_completed=None, custom_url=None):
        total_files = len(jobs)
        try:
            for idx, job in enumerate(jobs, 1):
                if self.is_cancelled():
                    raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")
                self.set_status(f"📂 [{idx}/{total_files}] 파일 처리 중...")

                def progress_cb(current, total, _idx=idx):
                    base = (_idx - 1) / total_files
                    inner = (current / total) / total_files if total > 0 else 0
                    self.update_progress(base + inner)

                if job["kind"] == "snbt":
                    job["final_text"] = process_snbt_with_progress(
                        "\n".join(job["lines"]), engine_key, api_key, is_paid,
                        progress_cb, self.route_log, self.is_cancelled, verbose=False, reference_map=None, glossary=getattr(self, 'glossary', {}), ai_model=ai_model, target_lang=target_lang, custom_url=custom_url)
                elif job["kind"] == "lang":
                    from translation_engines import translate_with_builtin_fallback
                    def get_translator():
                        if engine_key == "openai":
                            from translation_engines import _translate_openai_request
                            return lambda v, k: _translate_openai_request(v, k, getattr(self, 'glossary', {}), ai_model, target_lang)
                        elif engine_key == "claude":
                            from translation_engines import _translate_claude_request
                            return lambda v, k: _translate_claude_request(v, k, getattr(self, 'glossary', {}), ai_model, target_lang)
                        elif engine_key == "deepl":
                            from translation_engines import translate_deepl
                            return lambda v, k: translate_deepl(v, k)
                        elif engine_key == "local_ai":
                            from translation_engines import translate_local_ai
                            return lambda v, k: translate_local_ai([v], custom_url, ai_model, target_lang=target_lang)[0]
                        else:
                            return lambda v, k: v
                            
                    trans_func = get_translator()
                    total_targets = len(job["targets"])
                    for i, (line_idx, prefix, orig_text, suffix) in enumerate(job["targets"]):
                        if self.is_cancelled():
                            raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")
                        trans = translate_with_builtin_fallback(orig_text, api_key, None, trans_func, target_lang)
                        job["translated_map"][line_idx] = f'{prefix}{trans}{suffix}'
                        progress_cb(i + 1, total_targets, idx)
                else:
                    process_json_safely(
                        job["data"], engine_key, api_key, is_paid,
                        progress_cb, self.route_log, self.is_cancelled, verbose=False, reference_map=None, glossary=getattr(self, 'glossary', {}), ai_model=ai_model, target_lang=target_lang, custom_url=custom_url)
                
                if on_job_completed:
                    on_job_completed(job)
        finally:
            translation_memory.save_memory()

    # ====================================================================
    # 스레드 안전 다이얼로그 & 백업 헬퍼
    # ====================================================================

    def _ask_main(self, title, message):
        """Worker-thread-safe yes/no dialog. Blocks until the user answers."""
        result = [None]
        ev = threading.Event()
        def _run():
            result[0] = messagebox.askyesno(title, message)
            ev.set()
        self.after(0, _run)
        ev.wait()
        return result[0]

    def _ask_resume_backup_list(self, backups):
        """Worker-thread-safe dialog displaying a list of available backups to resume from.
        Returns the chosen index, or -1 if the user chose to start new, or None if cancelled."""
        result = [None]
        ev = threading.Event()
        def _run():
            dialog = tk.Toplevel(self)
            dialog.title("번역 재개 기록 선택")
            dialog.geometry("500x380")
            dialog.transient(self)
            dialog.grab_set()

            # Center window
            dialog.update_idletasks()
            x = self.winfo_x() + (self.winfo_width() - 500) // 2
            y = self.winfo_y() + (self.winfo_height() - 380) // 2
            dialog.geometry(f"+{x}+{y}")

            label = ctk.CTkLabel(
                dialog,
                text="이전에 중단된 번역 기록이 여러 개 발견되었습니다.\n이어서 번역할 기록을 선택해주세요:",
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                text_color="#fdba74"
            )
            label.pack(pady=12, padx=15)

            list_frame = ctk.CTkFrame(dialog, fg_color="#101015", corner_radius=8)
            list_frame.pack(fill="both", expand=True, padx=15, pady=(0, 15))

            canvas = tk.Canvas(list_frame, bg="#101015", highlightthickness=0)
            scrollbar = tk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
            scrollable_frame = tk.Frame(canvas, bg="#101015")

            scrollable_frame.bind(
                "<Configure>",
                lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
            )
            canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
            canvas.configure(yscrollcommand=scrollbar.set)

            canvas.pack(side="left", fill="both", expand=True, padx=5, pady=5)
            scrollbar.pack(side="right", fill="y")

            selected_idx = tk.IntVar(value=0)

            # Draw choices
            for idx, item in enumerate(backups):
                frm = tk.Frame(scrollable_frame, bg="#101015", pady=4)
                frm.pack(fill="x", anchor="w", padx=5)

                rbtn = tk.Radiobutton(
                    frm,
                    text=f"{item['name']} ({item['count']}개 파일 완료)",
                    variable=selected_idx,
                    value=idx,
                    bg="#101015",
                    fg="#f5f5f5",
                    selectcolor="#1c1917",
                    activebackground="#101015",
                    activeforeground="#ea580c",
                    font=(FONT_NAME, 10)
                )
                rbtn.pack(side="left", anchor="w")

                lbl_path = tk.Label(
                    scrollable_frame,
                    text=f"  ↳ 경로: {item['path']}",
                    font=(FONT_NAME, 8),
                    fg="#71717a",
                    bg="#101015"
                )
                lbl_path.pack(fill="x", anchor="w", padx=(25, 5))

            btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
            btn_row.pack(fill="x", pady=(0, 15), padx=15)

            def on_confirm():
                result[0] = selected_idx.get()
                dialog.destroy()
                ev.set()

            def on_start_new():
                result[0] = -1
                dialog.destroy()
                ev.set()

            def on_cancel():
                result[0] = None
                dialog.destroy()
                ev.set()

            dialog.protocol("WM_DELETE_WINDOW", on_cancel)

            ctk.CTkButton(
                btn_row, text="선택한 번역 재개", fg_color="#ea580c", hover_color="#c2410c",
                font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
                command=on_confirm
            ).pack(side="left", fill="x", expand=True, padx=(0, 6))

            ctk.CTkButton(
                btn_row, text="새로 시작", fg_color="#27272a", hover_color="#3f3f46",
                font=ctk.CTkFont(family=FONT_NAME, size=11),
                command=on_start_new
            ).pack(side="left", fill="x", expand=True, padx=(0, 6))

            ctk.CTkButton(
                btn_row, text="취소", fg_color="#3f3f46", hover_color="#52525b",
                font=ctk.CTkFont(family=FONT_NAME, size=11),
                command=on_cancel
            ).pack(side="right", width=80)

        self.after(0, _run)
        ev.wait()
        return result[0]

    def _pick_dir_main(self, title):
        """Worker-thread-safe directory picker. Blocks until answered."""
        result = [None]
        ev = threading.Event()
        def _run():
            result[0] = filedialog.askdirectory(title=title)
            ev.set()
        self.after(0, _run)
        ev.wait()
        return result[0]

    def _offer_partial_backup(self, out_dir, partial_name, error_msg=None):
        """After cancel/error, ask user if they want to backup and preserve records.
        Yes -> Save backup zip and keep out_dir (can resume later).
        No -> Clean up/delete the out_dir (do not save, clean up state)."""
        if not os.path.exists(out_dir):
            if error_msg: self.show_messagebox("warning", "중단됨", error_msg)
            return


        if self._ask_main(
            '번역 작업 중단',
            '번역 작업을 중단했습니다.\n지금까지 진행된 번역 상태를 저장하시겠습니까?\n\n'
            '예: 내부적으로 기록을 저장하여 다음 실행 시 이어서 번역 가능 (추천)\n'
            '아니요: 지금까지 진행된 번역을 모두 삭제하고 취소'
        ):
            self.log("💾 번역 진행 상황이 성공적으로 저장되었습니다. 다음에 다시 실행하면 이어서 번역할 수 있습니다.")
        else:
            self.log("🗑️ 진행 중이던 번역 작업 파일과 임시 기록을 삭제합니다.")
            try:
                shutil.rmtree(out_dir, ignore_errors=True)
            except Exception as exc:
                self.log(f"⚠️ 임시 폴더 삭제 중 경고: {exc}")

    def do_backup_on_close(self):
        """Called synchronously from on_close during active translation.
        Returns False if the user chose to cancel the window close."""
        out_dir = getattr(self, '_translation_out_dir', None)
        if not out_dir or not os.path.exists(out_dir):
            return True
        has_data = any(
            fname != PROGRESS_FILE
            for root, _, files in os.walk(out_dir)
            for fname in files
        )
        if not has_data:
            try:
                shutil.rmtree(out_dir, ignore_errors=True)
            except Exception:
                pass
            return True

        answer = messagebox.askyesnocancel(
            '번역 진행 중',
            '번역이 진행 중입니다. 앱을 종료하면 현재 작업이 중단됩니다.\n\n'
            '예: 현재까지 진행된 번역 상황을 저장하고 종료 (다음에 이어서 가능)\n'
            '아니요: 진행 상황을 모두 삭제하고 앱 종료\n'
            '취소: 앱 종료를 취소하고 계속 번역 진행'
        )
        if answer is None:  # 취소 — 종료하지 않음
            return False
        if answer:  # 예 — 기록 보존
            pass
        else:  # 아니요 — 진행중인 것 삭제 후 종료
            try:
                shutil.rmtree(out_dir, ignore_errors=True)
            except Exception:
                pass
        self.app_state.cancel_requested = True
        return True

    def _generate_zip_review_report(self, raw_dir, out_dir, report_title):
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
                    self.log(f"⚠️ 검수 스킵 [{rel_p}]: {review_exc}")

        if review_items:
            report_text = render_review_report(report_title, review_items)
            self.show_review_report(report_text)
            self.log("🧪 검수 리포트가 결과창으로 표시되었습니다.")

    def _process_zip_file(self, zip_path, engine_key, api_key, is_paid, ai_model=None, target_lang="한국어 (Korean)", modpack_path=None, custom_url=None, toggle_ui=True):
        self.app_state.cancel_requested = False
        self.after(0, lambda: getattr(self, "show_translate_screen")(force=True))
        self.toggle_buttons(False)
        self.update_progress(0)

        from translation_core import TranslationUIContext, run_zip_translation_logic
        from tkinter import filedialog
        
        class AppTranslationContext(TranslationUIContext):
            def __init__(self, app):
                self.app = app
                
            def log(self, message):
                self.app.log(message)
                
            def set_status(self, text):
                self.app.set_status(text)
                
            def update_progress(self, current, total=None):
                self.app.update_progress(current, total)
                
            def is_cancelled(self):
                return self.app.is_cancelled()
                
            def show_messagebox(self, type_, title, message):
                self.app.show_messagebox(type_, title, message)
                
            def show_review_report(self, report_text):
                self.app.show_review_report(report_text)
                
            def ask_resume(self, candidates, default_out_dir):
                if not candidates:
                    return default_out_dir, set(), False
                    
                if len(candidates) == 1:
                    single = candidates[0]
                    if self.app._ask_main('번역 재개', f"이전에 중단된 번역 기록이 발견되었습니다.\n'{single['name']}' ({single['count']}개 완료)에서 이어서 번역하시겠습니까?\n\n아니요: 처음부터 새로 시작합니다."):
                        self.log(f"♻️ 이전 번역 이어서 시작: {single['name']} (완료된 파일 {len(single['completed'])}개 건너뜀)")
                        return single["path"], single["completed"], True
                    return default_out_dir, set(), False
                else:
                    choice = self.app._ask_resume_backup_list(candidates)
                    if choice is None:
                        self.log("⚠️ 번역 재개 선택이 취소되었습니다. 작업을 종료합니다.")
                        return None, set(), False
                    elif choice == -1:
                        self.log("♻️ 새 번역으로 처음부터 시작합니다.")
                        return default_out_dir, set(), False
                    else:
                        chosen = candidates[choice]
                        self.log(f"♻️ 이전 번역 이어서 시작: {chosen['name']} (완료된 파일 {len(chosen['completed'])}개 건너뜀)")
                        return chosen["path"], chosen["completed"], True

            def ask_apply_mode(self):
                result = [None]
                ev = threading.Event()
                def _run():
                    from tkinter import messagebox
                    result[0] = messagebox.askyesnocancel(
                        "번역 적용 방식 선택", 
                        "모드팩 번역이 완료되었습니다!\n\n'예': 원본 모드팩에 번역본을 덮어쓰기 (즉시 적용)\n'아니요': ZIP 파일로 압축하여 저장 (백업)\n'취소': 아무 작업도 하지 않고 종료"
                    )
                    ev.set()
                self.app.after(0, _run)
                ev.wait()
                return result[0]

            def ask_save_dir(self):
                return self.app._pick_dir_main(title="번역된 ZIP 저장 폴더 선택")
                
            def offer_partial_backup(self, out_dir, backup_name, error_msg=None):
                self.app._offer_partial_backup(out_dir, backup_name, error_msg)
                
            def on_translation_success(self, modpack_path):
                if not self.is_cancelled():
                    self.app.app_state.translated_history[modpack_path] = time.strftime("%Y-%m-%d %H:%M:%S")
                    self.app.save_user_settings()
                    if hasattr(self.app, "scan_modpacks_from_entry"):
                        self.app.after(0, self.app.scan_modpacks_from_entry)

        context = AppTranslationContext(self)
        try:
            # We already loaded glossary/reference_map before this thread started, or we load it here.
            # In the original, it loaded it here or we can just load it here.
            reference_map = self.app_state.glossaries_by_lang.get(target_lang, {})
            glossary = self.app_state.glossaries_by_lang.get(target_lang, {})
            

            run_zip_translation_logic(
                context, zip_path, engine_key, api_key, is_paid, ai_model, target_lang, modpack_path,
                apply_mode=getattr(self, "apply_mode", False) if modpack_path else False,
                reference_map=reference_map, glossary=glossary, custom_url=custom_url
            )
        finally:
            self._translation_out_dir = None
            if toggle_ui:
                self.toggle_buttons(True)


    def run_all_modpack_translations(self):
        engine_key, api_key, is_paid, ai_model, target_lang, custom_url = self.validate_inputs()
        if not engine_key:
            return

        modpack_dir = self.selected_modpack_path
        if not modpack_dir:
            self.show_messagebox("warning", "경고", "먼저 인스턴스 경로를 선택하고 모드팩을 탐지해주세요.")
            return

        def run_translation_task():
            self.app_state.cancel_requested = False
            self.after(0, lambda: getattr(self, "show_translate_screen")(force=True))
            self.toggle_buttons(False)
            
            # 1. Patchouli Books
            self.update_progress(0.0)
            self.log("\n==========================================")
            self.log("📚 [1/3단계] 가이드북(Patchouli) 전용 번역 시작...")
            patchouli_map = self._translate_patchouli_books(engine_key, api_key, is_paid, ai_model, target_lang, modpack_dir, custom_url)
            
            if getattr(self.app_state, 'cancel_requested', False):
                self.log("⚠️ 사용자가 번역을 취소했습니다.")
            else:
                # 1.5 Custom Books
                self.update_progress(0.0)
                self.log("\n==========================================")
                self.log("📖 [2/3단계] 커스텀 가이드북(XNet 등) 번역 시작...")
                custom_map = self._translate_custom_books(engine_key, api_key, is_paid, ai_model, target_lang, modpack_dir, custom_url)
            
            if getattr(self.app_state, 'cancel_requested', False):
                self.log("⚠️ 사용자가 번역을 취소했습니다.")
            else:
                # 2. Lang Files
                self.update_progress(0.0)
                self.log("\n==========================================")
                self.log("🗂️ [3/3단계] 전체 모드(.lang) 텍스트 번역 시작...")
                lang_map = self._translate_lang_files(engine_key, api_key, is_paid, ai_model, target_lang, modpack_dir, custom_url)

            # 3. Create Combined Pack (Even if cancelled, save what we have)
            if 'lang_map' not in locals(): lang_map = {}
            if 'custom_map' not in locals(): custom_map = {}
            if patchouli_map or lang_map or custom_map:
                import mod_jar_extractor
                import os
                import tkinter.messagebox as mb
                output_zip = os.path.join(modpack_dir, "QuestTranslatorPro_Pack.zip")
                mod_jar_extractor.create_combined_resource_pack(lang_map, patchouli_map, output_zip, modpack_dir=modpack_dir, custom_map=custom_map)
                self.log(f"🎉 통합 리소스팩 생성 완료!\n경로: {output_zip}")
                
                msg = "모드팩 전체 번역이 완료되었습니다!\n마인크래프트 리소스팩 설정에서 'QuestTranslatorPro_Pack.zip' 하나만 적용해주세요."
                if not custom_map.get("pi_xml"):
                    mods_dir = os.path.join(modpack_dir, "mods")
                    if os.path.isdir(mods_dir) and any('draconic' in f.lower() or 'projectintelligence' in f.lower() for f in os.listdir(mods_dir)):
                        msg += "\n\n⚠️ 주의: 드라코닉 에볼루션 등(Project Intelligence)은 매뉴얼을 게임 내에서 실시간 다운로드합니다.\n게임을 켜서 인게임 태블릿을 한 번 연 뒤, 툴을 다시 돌려주셔야 해당 매뉴얼 한글화가 적용됩니다!"
                
                mb.showinfo("번역 완료", msg)
            
            self.toggle_buttons(True)
            self.update_progress(1.0)
            self.set_status("대기 중")

        import threading
        threading.Thread(target=run_translation_task, daemon=True).start()
