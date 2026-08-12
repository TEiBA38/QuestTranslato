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

FONT_NAME = "Malgun Gothic"


from translation_engines import ENGINES, QuotaExceededError, TranslationCancelledError, translate_gemini_batch
from file_processors import (
    collect_json_targets, extract_snbt_targets,
    process_hqm_with_progress, process_json_safely,
    process_snbt_with_progress, rebuild_snbt,
)
from review_checks import (
    analyze_hqm_bytes, analyze_json_data, analyze_snbt_texts, render_review_report,
)
from constants import FONT_NAME, TARGET_EXTENSIONS, SCAN_IGNORE_DIRS

PROGRESS_FILE = "_progress.json"


def _has_non_latin(text):
    """Return True if text contains non-Latin characters (Hangul, CJK, Cyrillic, etc.)
    indicating it may already be translated."""
    for ch in str(text):
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:  # Hangul
            return True
        if 0x3040 <= code <= 0x30FF:  # Hiragana / Katakana
            return True
        if 0x4E00 <= code <= 0x9FFF:  # CJK Unified
            return True
        if 0x0400 <= code <= 0x04FF:  # Cyrillic
            return True
    return False


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
        engine_key, api_key, is_paid, ai_model, target_lang = self.validate_inputs()
        self.app_state.cancel_requested = False
        if not engine_key:
            return
        dropped_path = event.data.strip('{}').strip('"')
        if not os.path.exists(dropped_path):
            return
        if dropped_path.lower().endswith(('.snbt', '.json', '.hqm')):
            threading.Thread(target=self._process_single_file,
                             args=(dropped_path, engine_key, api_key, is_paid, ai_model, target_lang), daemon=True).start()
        elif dropped_path.lower().endswith('.zip'):
            threading.Thread(target=self._process_zip_file,
                             args=(dropped_path, engine_key, api_key, is_paid, ai_model, target_lang), daemon=True).start()
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
                    
                    # 특수 예외: 외부 퀘스트 lang 파일 (예: kubejs/assets/ftbquests/lang/en_us.json, resources/betterquesting/lang/en_us.lang)
                    is_quest_lang = (filename.lower() in ["en_us.json", "en_us.lang"] and 
                                     any(q in norm_path for q in ["ftbquests", "betterquesting", "hqm", "heracles"]))
                    
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
                            is_valid = (filename.lower() == "defaultquests.json" or 
                                        "/chapter/" in norm_path or 
                                        "/chapters/" in norm_path)
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
        engine_key, api_key, is_paid, ai_model, target_lang = self.validate_inputs()
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
                if file_count == 0:
                    self.show_messagebox("warning", "대상 없음", "선택한 모드팩에서 번역 대상 파일을 찾지 못했습니다.")
                    return
                self.log(f"🚀 선택 모드팩 자동 번역 시작: {os.path.basename(modpack_dir)} ({file_count} files)")
                self._process_zip_file(temp_zip_path, engine_key, api_key, is_paid, ai_model=ai_model, target_lang=target_lang, modpack_path=modpack_dir)
            except Exception as exc:
                self.log(f"❌ 인스턴스 번역 준비 중 오류: {exc}")
                self.show_messagebox("error", "오류", f"인스턴스 번역 준비 중 오류가 발생했습니다:\n{exc}")
            finally:
                if temp_zip_path and os.path.exists(temp_zip_path):
                    try:
                        os.remove(temp_zip_path)
                    except Exception:
                        pass

        threading.Thread(target=run_instance_translation, daemon=True).start()

    # ====================================================================
    # 단일 파일 번역
    # ====================================================================

    def run_single_file(self):
        engine_key, api_key, is_paid, ai_model, target_lang = self.validate_inputs()
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

    def _process_single_file(self, file_path, engine_key, api_key, is_paid, ai_model=None, target_lang="한국어 (Korean)"):
        self.app_state.cancel_requested = False
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
                    content, engine_key, api_key, is_paid, progress_cb, self.route_log, self.is_cancelled, glossary=getattr(self, 'glossary', {}), ai_model=ai_model, target_lang=target_lang)
            elif file_path.lower().endswith('.json'):
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    try:
                        json_data = json.load(f)
                    except json.JSONDecodeError as e:
                        raise Exception(f"'{file_name}' 파일이 올바른 JSON 형식이 아닙니다 ({e.lineno}:{e.colno}: {e.msg})")
                source_json_data = json.loads(json.dumps(json_data, ensure_ascii=False))
                process_json_safely(json_data, engine_key, api_key, is_paid, progress_cb, self.route_log, self.is_cancelled, glossary=getattr(self, 'glossary', {}), ai_model=ai_model, target_lang=target_lang)
            elif file_path.lower().endswith('.hqm'):
                with open(file_path, 'rb') as f:
                    content = f.read()
                source_content = content
                try:
                    translated_content = process_hqm_with_progress(
                        content, engine_key, api_key, is_paid, progress_cb, self.route_log, self.is_cancelled, glossary=getattr(self, 'glossary', {}), ai_model=ai_model, target_lang=target_lang)
                except ValueError as exc:
                    self.log(f"⚠️ HQM 처리 경고: {exc}")
                    translated_content = content

            self.update_progress(1.0)
            self.log("\n✅ 모든 텍스트 번역 완료! 저장할 위치를 선택해주세요.")
            save_dir = filedialog.askdirectory(title="번역된 파일을 저장할 폴더 선택")

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
        engine_key, api_key, is_paid, ai_model, target_lang = self.validate_inputs()
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
        batch_size = 50
        tasks = []
        for job in jobs:
            job["tasks_total"] = (len(job["targets"]) + batch_size - 1) // batch_size
            job["tasks_done"] = 0
            for i in range(0, len(job["targets"]), batch_size):
                tasks.append((job, job["targets"][i:i + batch_size]))
        total_items = sum(len(job["targets"]) for job in jobs)
        completed_items = 0
        lock = threading.Lock()

        def run_task(task):
            job, chunk = task
            if self.is_cancelled():
                raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")
            texts = [item[2].replace('\\"', '"') if job["kind"] == "snbt" else item[2] for item in chunk]
            translated_texts = translate_gemini_batch(texts, api_key, is_paid, self.route_log, self.is_cancelled, ai_model=ai_model, target_lang=target_lang)
            if job["kind"] == "snbt":
                for (line_idx, prefix, _, suffix), trans in zip(chunk, translated_texts):
                    job["translated_map"][line_idx] = f'{prefix}"{str(trans).replace(chr(34), chr(92)+chr(34))}"{suffix}'
            else:
                for (parent_node, key, _), trans in zip(chunk, translated_texts):
                    parent_node[key] = trans
            return job, len(chunk)

        executor = ThreadPoolExecutor(max_workers=min(8, len(tasks)) or 1)
        try:
            futures = [executor.submit(run_task, t) for t in tasks]
            for future in as_completed(futures):
                job, n = future.result()
                with lock:
                    completed_items += n
                    job["tasks_done"] += 1
                    if job["tasks_done"] >= job.get("tasks_total", 1):
                        if on_job_completed:
                            on_job_completed(job)
                self.set_status(f"⏳ Gemini API 번역 진행 중... [{completed_items}/{total_items}]")
                self.update_progress(completed_items / total_items if total_items else 1)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _translate_jobs_sequential(self, jobs, engine_key, api_key, is_paid, ai_model=None, target_lang="한국어 (Korean)", on_job_completed=None):
        total_files = len(jobs)
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
                    progress_cb, self.route_log, self.is_cancelled, verbose=False, reference_map=None, glossary=getattr(self, 'glossary', {}), ai_model=ai_model, target_lang=target_lang)
            else:
                process_json_safely(
                    job["data"], engine_key, api_key, is_paid,
                    progress_cb, self.route_log, self.is_cancelled, verbose=False, reference_map=None, glossary=getattr(self, 'glossary', {}), ai_model=ai_model, target_lang=target_lang)
            
            if on_job_completed:
                on_job_completed(job)

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

    def _offer_partial_backup(self, out_dir, partial_name):
        """After cancel/error, ask user if they want to backup and preserve records.
        Yes -> Save backup zip and keep out_dir (can resume later).
        No -> Clean up/delete the out_dir (do not save, clean up state)."""
        if not os.path.exists(out_dir):
            return
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

    def _process_zip_file(self, zip_path, engine_key, api_key, is_paid, ai_model=None, target_lang="한국어 (Korean)", modpack_path=None):
        self.app_state.cancel_requested = False
        self.toggle_buttons(False)
        self.update_progress(0)

        base_zip_name = os.path.basename(zip_path)
        origin_dir = os.path.dirname(zip_path)
        raw_dir = os.path.join(origin_dir, "_temp_raw")
        
        # --- Multi-backup Detection ---
        # Look for folders containing PROGRESS_FILE in the same directory or temp directory
        out_dir = os.path.join(origin_dir, "_temp_out")
        completed_set = set()
        resuming = False
        
        # Find potential resume candidate folders
        candidates = []
        # Case A: Default out_dir
        if os.path.isfile(os.path.join(out_dir, PROGRESS_FILE)):
            comp = _load_progress(out_dir)
            if comp:
                candidates.append({
                    "path": out_dir,
                    "name": "기본 임시 번역 폴더 (기본 기록)",
                    "count": len(comp),
                    "completed": comp
                })
        
        # Case B: Other subfolders inside the original zip directory with valid progress files
        if os.path.isdir(origin_dir):
            for entry in os.scandir(origin_dir):
                if entry.is_dir() and entry.name.startswith("_temp_out_") and entry.path != out_dir:
                    prog_path = os.path.join(entry.path, PROGRESS_FILE)
                    if os.path.isfile(prog_path):
                        comp = _load_progress(entry.path)
                        if comp:
                            candidates.append({
                                "path": entry.path,
                                "name": f"임시 백업 폴더 ({entry.name})",
                                "count": len(comp),
                                "completed": comp
                            })

        if candidates:
            if len(candidates) == 1:
                # Only 1 candidate, ask single yes/no dialog
                single = candidates[0]
                if self._ask_main(
                    '번역 재개',
                    f"이전에 중단된 번역 기록이 발견되었습니다.\n'{single['name']}' ({single['count']}개 완료)에서 이어서 번역하시겠습니까?\n\n아니요: 처음부터 새로 시작합니다."
                ):
                    out_dir = single["path"]
                    completed_set = single["completed"]
                    resuming = True
                    self.log(f"♻️ 이전 번역 이어서 시작: {single['name']} (완료된 파일 {len(completed_set)}개 건너뜀)")
            else:
                # Multiple candidates, show the list dialog
                choice = self._ask_resume_backup_list(candidates)
                if choice is None:
                    # Cancelled
                    self.log("⚠️ 번역 재개 선택이 취소되었습니다. 작업을 종료합니다.")
                    self.toggle_buttons(True)
                    return
                elif choice == -1:
                    # Start New
                    resuming = False
                    self.log("♻️ 새 번역으로 처음부터 시작합니다.")
                else:
                    # Chosen index
                    chosen = candidates[choice]
                    out_dir = chosen["path"]
                    completed_set = chosen["completed"]
                    resuming = True
                    self.log(f"♻️ 이전 번역 이어서 시작: {chosen['name']} (완료된 파일 {len(completed_set)}개 건너뜀)")

        self._translation_out_dir = out_dir

        try:
            # raw_dir 항상 새로 추출 (원본 보장), out_dir은 재개 시 보존
            if os.path.exists(raw_dir):
                shutil.rmtree(raw_dir)
            if not resuming and os.path.exists(out_dir):
                shutil.rmtree(out_dir)


            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(raw_dir)

            target_files = [
                os.path.join(root, f)
                for root, _, files in os.walk(raw_dir)
                for f in files if f.lower().endswith(('.snbt', '.json', '.lang', '.hqm'))
            ]
            total_files = len(target_files)
            if total_files == 0:
                self.log("⚠️ 번역할 대상 파일을 찾을 수 없습니다.")
                return

            if engine_key == "gemini_batch":
                mode_str = "[유료/초고속]" if is_paid else "[무료/안전대기]"
            else:
                mode_str = "[초고속]"
            engine_label = next((name for name, key in ENGINES.items() if key == engine_key), engine_key)
            self.log(f"\n🎯 총 {total_files}개 파일 압축 번역 시작... {mode_str}")
            self.log(f"🧩 선택 옵션: 엔진={engine_label} / 모드={mode_str}")

            jobs = []
            skipped_no_targets = 0
            skipped_bad_json = 0
            skipped_translated = 0
            skipped_resume = 0

            for idx, file_path in enumerate(target_files, 1):
                rel_path = os.path.relpath(file_path, raw_dir)
                target_path = os.path.join(out_dir, rel_path)
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                filename = os.path.basename(file_path)

                # 재개: 이미 완료된 파일은 건너뜀
                if rel_path in completed_set:
                    skipped_resume += 1
                    continue

                if file_path.lower().endswith('.snbt'):
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                    if _has_non_latin(content):
                        skipped_translated += 1
                        shutil.copy2(file_path, target_path)
                        completed_set.add(rel_path)
                        _save_progress(out_dir, completed_set)
                        continue
                    lines, targets = extract_snbt_targets(content)
                    if not targets:
                        skipped_no_targets += 1
                        shutil.copy2(file_path, target_path)
                        completed_set.add(rel_path)
                        _save_progress(out_dir, completed_set)
                        continue
                    jobs.append({"kind": "snbt", "target_path": target_path, "rel_path": rel_path,
                                 "lines": lines, "targets": targets, "translated_map": {}})

                elif file_path.lower().endswith('.hqm'):
                    with open(file_path, 'rb') as f:
                        hqm_content = f.read()
                    if _has_non_latin(hqm_content.decode('utf-8', errors='ignore')):
                        skipped_translated += 1
                        shutil.copy2(file_path, target_path)
                        completed_set.add(rel_path)
                        _save_progress(out_dir, completed_set)
                        continue
                    self.set_status(f"🔄 [{idx}/{total_files}] [{filename}] HQM 바이너리 번역 중...")

                    def hqm_progress_cb(current, total, _idx=idx, _fn=filename):
                        base = (_idx - 1) / total_files
                        inner = (current / total) / total_files if total > 0 else 0
                        self.update_progress(base + inner)
                        self.set_status(f"⏳ [{_idx}/{total_files}] [{_fn}] HQM 번역 중... [{current}/{total}]")

                    try:
                        translated_hqm = process_hqm_with_progress(
                            hqm_content, engine_key, api_key, is_paid,
                            progress_callback=hqm_progress_cb,
                            log_callback=self.route_log, cancel_checker=self.is_cancelled, glossary=getattr(self, 'glossary', {}), ai_model=ai_model, target_lang=target_lang)
                    except Exception as exc:
                        self.log(f"⚠️ [{filename}] HQM 처리 경고: {exc}")
                        translated_hqm = hqm_content
                    with open(target_path, 'wb') as f:
                        f.write(translated_hqm)
                    self.log(f"✅ [{filename}] HQM 번역 완료")
                    completed_set.add(rel_path)
                    _save_progress(out_dir, completed_set)
                    continue

                elif file_path.lower().endswith(('.json', '.lang')):
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        try:
                            data = json.load(f)
                        except json.JSONDecodeError:
                            skipped_bad_json += 1
                            shutil.copy2(file_path, target_path)
                            completed_set.add(rel_path)
                            _save_progress(out_dir, completed_set)
                            continue
                    if _has_non_latin(json.dumps(data, ensure_ascii=False)):
                        skipped_translated += 1
                        with open(target_path, 'w', encoding='utf-8') as f:
                            json.dump(data, f, ensure_ascii=False, indent=2)
                        completed_set.add(rel_path)
                        _save_progress(out_dir, completed_set)
                        continue
                    node_targets = []
                    collect_json_targets(data, node_targets)
                    if not node_targets:
                        skipped_no_targets += 1
                        with open(target_path, 'w', encoding='utf-8') as f:
                            json.dump(data, f, ensure_ascii=False, indent=2)
                        completed_set.add(rel_path)
                        _save_progress(out_dir, completed_set)
                        continue
                    jobs.append({"kind": "json", "target_path": target_path, "rel_path": rel_path,
                                 "data": data, "targets": node_targets})

            if jobs:
                def on_job_completed(job):
                    if job["kind"] == "snbt":
                        text = job.get("final_text") or rebuild_snbt(job["lines"], job["translated_map"])
                        with open(job["target_path"], 'w', encoding='utf-8', newline='\n') as f:
                            f.write(text)
                    else:
                        with open(job["target_path"], 'w', encoding='utf-8') as f:
                            json.dump(job["data"], f, ensure_ascii=False, indent=2)
                    completed_set.add(job["rel_path"])
                    _save_progress(out_dir, completed_set)

                if engine_key == "gemini_batch" and is_paid:
                    self._translate_jobs_parallel(jobs, api_key, is_paid, ai_model=ai_model, target_lang=target_lang, on_job_completed=on_job_completed)
                else:
                    self._translate_jobs_sequential(jobs, engine_key, api_key, is_paid, ai_model=ai_model, target_lang=target_lang, on_job_completed=on_job_completed)

            skip_parts = []
            if skipped_resume:
                skip_parts.append(f"재개 스킵 {skipped_resume}개")
            if skipped_translated:
                skip_parts.append(f"이미 번역됨 스킵 {skipped_translated}개")
            if skipped_no_targets:
                skip_parts.append(f"번역 대상 없음 {skipped_no_targets}개")
            if skipped_bad_json:
                skip_parts.append(f"JSON 오류 {skipped_bad_json}개")
            if skip_parts:
                self.log(f"ℹ️ 스캔 요약: {', '.join(skip_parts)} 건너뜀")

            self.update_progress(1.0)
            
            if modpack_path:
                self.log("\n🎉 모든 파일 번역 완료! 저장 및 적용 방식을 선택해주세요.")
                apply_mode = messagebox.askyesnocancel(
                    "번역 적용 방식 선택", 
                    "모드팩 번역이 완료되었습니다!\n\n'예': 원본 모드팩에 번역본을 덮어쓰기 (즉시 적용)\n'아니요': ZIP 파일로 압축하여 저장 (백업)\n'취소': 아무 작업도 하지 않고 종료"
                )
            else:
                self.log("\n🎉 모든 파일 번역 완료! 저장할 위치를 선택해주세요.")
                apply_mode = False

            if apply_mode is None:
                self.log(f"⚠️ 저장 및 적용이 취소되었습니다. 임시 결과 폴더에 파일이 남아있습니다:\n{out_dir}")
                return
            elif apply_mode is True:
                self.log(f"\n📦 원본 모드팩에 즉시 덮어쓰기 시작: {modpack_path}")
                shutil.copytree(out_dir, modpack_path, dirs_exist_ok=True)
                self.log(f"💾 덮어쓰기 완료: {modpack_path}")
                
                self.app_state.translated_history[modpack_path] = time.strftime("%Y-%m-%d %H:%M:%S")
                self.save_user_settings()
                if hasattr(self, "scan_modpacks_from_entry"):
                    self.after(0, self.scan_modpacks_from_entry) # Refresh UI

                # 검수 리포트 생성
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
                    report_text = render_review_report("덮어쓰기 완료: 번역 검수 리포트", review_items)
                    self.show_review_report(report_text)
                    self.log("🧪 검수 리포트가 결과창으로 표시되었습니다.")

                self.show_messagebox("info", "적용 완료", f"모드팩에 번역본이 성공적으로 덮어쓰기 되었습니다!\n\n적용 경로:\n{modpack_path}")
                shutil.rmtree(out_dir, ignore_errors=True)
                return

            # ZIP으로 저장하는 로직 (apply_mode == False)
            save_dir = filedialog.askdirectory(title="번역된 ZIP 저장 폴더 선택")

            if save_dir:
                out_zip_path = os.path.join(save_dir, base_zip_name)
                with zipfile.ZipFile(out_zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_out:
                    for root, _, files_list in os.walk(out_dir):
                        for f in files_list:
                            if f == PROGRESS_FILE:
                                continue
                            full_p = os.path.join(root, f)
                            zip_out.write(full_p, os.path.relpath(full_p, out_dir))

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
                    report_text = render_review_report("ZIP 번역 검수 리포트", review_items)
                    self.show_review_report(report_text)
                    self.log("🧪 검수 리포트가 결과창으로 표시되었습니다.")

                self.log(f"💾 압축 저장 완료: {out_zip_path}")
                
                if modpack_path and apply_mode is False:
                    if not self.is_cancelled():
                        self.app_state.translated_history[modpack_path] = time.strftime("%Y-%m-%d %H:%M:%S")
                        self.save_user_settings()
                        if hasattr(self, "scan_modpacks_from_entry"):
                            self.after(0, self.scan_modpacks_from_entry) # Refresh UI

                self.show_messagebox("info", "완료", f"모든 작업이 완료되었습니다!\n\n저장 위치:\n{out_zip_path}")
                shutil.rmtree(out_dir, ignore_errors=True)
            else:
                self.log(f"⚠️ 저장 폴더 선택이 취소되었습니다. 번역 결과는 여기 남아있습니다:\n{out_dir}")

        except TranslationCancelledError as e:
            self.log(f"\n🛑 {str(e)}")
            self._offer_partial_backup(out_dir, os.path.splitext(base_zip_name)[0] + '_partial.zip')
        except QuotaExceededError as e:
            self.log(f"\n🛑 [중단] {str(e)}")
            self._offer_partial_backup(out_dir, os.path.splitext(base_zip_name)[0] + '_partial.zip')
        except Exception as e:
            self.log(f"\n❌ 오류 발생: {str(e)}")
            self._offer_partial_backup(out_dir, os.path.splitext(base_zip_name)[0] + '_partial.zip')
        finally:
            shutil.rmtree(raw_dir, ignore_errors=True)
            self._translation_out_dir = None
            self.toggle_buttons(True)

