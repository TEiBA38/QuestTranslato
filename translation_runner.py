"""
번역 실행 (단일 파일, ZIP, 모드팩) 관련 메서드 믹스인.
"""
import os
import json
import shutil
import threading
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from tkinter import filedialog, messagebox
except Exception:
    filedialog = None
    messagebox = None

from translation_engines import ENGINES, QuotaExceededError, TranslationCancelledError, translate_gemini_batch
from file_processors import (
    collect_json_targets, extract_snbt_targets,
    process_hqm_with_progress, process_json_safely,
    process_snbt_with_progress, rebuild_snbt,
)
from review_checks import (
    analyze_hqm_bytes, analyze_json_data, analyze_snbt_texts, render_review_report,
)

TARGET_EXTENSIONS = ('.snbt', '.json', '.lang', '.hqm')
SCAN_IGNORE_DIRS = {
    '.git', '.venv', '__pycache__',
    'logs', 'saves', 'resourcepacks', 'shaderpacks',
    'screenshots', 'crash-reports', 'backups',
}


class TranslationMixin:
    # ====================================================================
    # 파일 드롭
    # ====================================================================

    def handle_file_drop(self, event):
        engine_key, api_key, is_paid = self.validate_inputs()
        if not engine_key:
            return
        dropped_path = event.data.strip('{}').strip('"')
        if not os.path.exists(dropped_path):
            return
        if dropped_path.lower().endswith(('.snbt', '.json', '.hqm')):
            threading.Thread(target=self._process_single_file,
                             args=(dropped_path, engine_key, api_key, is_paid), daemon=True).start()
        elif dropped_path.lower().endswith('.zip'):
            threading.Thread(target=self._process_zip_file,
                             args=(dropped_path, engine_key, api_key, is_paid), daemon=True).start()
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
                    if not filename.lower().endswith(TARGET_EXTENSIONS):
                        continue
                    full_path = os.path.join(root, filename)
                    zf.write(full_path, os.path.relpath(full_path, modpack_dir))
                    added_count += 1
        return temp_zip_path, added_count

    def run_selected_modpack(self):
        engine_key, api_key, is_paid = self.validate_inputs()
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
                self._process_zip_file(temp_zip_path, engine_key, api_key, is_paid)
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
        engine_key, api_key, is_paid = self.validate_inputs()
        if not engine_key:
            return
        file_path = filedialog.askopenfilename(
            title="번역할 파일 선택",
            filetypes=[("Quest Files", "*.snbt *.json *.hqm"), ("All Files", "*.*")]
        )
        if not file_path:
            return
        threading.Thread(target=self._process_single_file,
                         args=(file_path, engine_key, api_key, is_paid), daemon=True).start()

    def _process_single_file(self, file_path, engine_key, api_key, is_paid):
        self.cancel_requested = False
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
                    content, engine_key, api_key, is_paid, progress_cb, self.route_log, self.is_cancelled)
            elif file_path.lower().endswith('.json'):
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    try:
                        json_data = json.load(f)
                    except json.JSONDecodeError as e:
                        raise Exception(f"'{file_name}' 파일이 올바른 JSON 형식이 아닙니다 ({e.lineno}:{e.colno}: {e.msg})")
                source_json_data = json.loads(json.dumps(json_data, ensure_ascii=False))
                process_json_safely(json_data, engine_key, api_key, is_paid, progress_cb, self.route_log, self.is_cancelled)
            elif file_path.lower().endswith('.hqm'):
                with open(file_path, 'rb') as f:
                    content = f.read()
                source_content = content
                try:
                    translated_content = process_hqm_with_progress(
                        content, engine_key, api_key, is_paid, progress_cb, self.route_log, self.is_cancelled)
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
                    report_path = os.path.join(save_dir, f"{os.path.splitext(file_name)[0]}_review.txt")
                    with open(report_path, 'w', encoding='utf-8', newline='\n') as rf:
                        rf.write(report_text)
                    self.log(f"🧪 검수 리포트 저장: {report_path}")

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
        engine_key, api_key, is_paid = self.validate_inputs()
        if not engine_key:
            return
        zip_path = filedialog.askopenfilename(
            title="번역할 ZIP 선택",
            filetypes=[("ZIP Files", "*.zip"), ("All Files", "*.*")]
        )
        if not zip_path:
            return
        threading.Thread(target=self._process_zip_file,
                         args=(zip_path, engine_key, api_key, is_paid), daemon=True).start()

    def _translate_jobs_parallel(self, jobs, api_key, is_paid):
        batch_size = 50
        tasks = [(job, job["targets"][i:i + batch_size])
                 for job in jobs
                 for i in range(0, len(job["targets"]), batch_size)]
        total_items = sum(len(job["targets"]) for job in jobs)
        completed_items = 0
        lock = threading.Lock()

        def run_task(task):
            job, chunk = task
            if self.is_cancelled():
                raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")
            texts = [item[2].replace('\\"', '"') if job["kind"] == "snbt" else item[2] for item in chunk]
            translated_texts = translate_gemini_batch(texts, api_key, is_paid, self.route_log, self.is_cancelled)
            if job["kind"] == "snbt":
                for (line_idx, prefix, _, suffix), trans in zip(chunk, translated_texts):
                    job["translated_map"][line_idx] = f'{prefix}"{str(trans).replace(chr(34), chr(92)+chr(34))}"{suffix}'
            else:
                for (parent_node, key, _), trans in zip(chunk, translated_texts):
                    parent_node[key] = trans
            return len(chunk)

        executor = ThreadPoolExecutor(max_workers=min(8, len(tasks)) or 1)
        try:
            futures = [executor.submit(run_task, t) for t in tasks]
            for future in as_completed(futures):
                n = future.result()
                with lock:
                    completed_items += n
                self.set_status(f"⏳ Gemini API 번역 진행 중... [{completed_items}/{total_items}]")
                self.update_progress(completed_items / total_items if total_items else 1)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _translate_jobs_sequential(self, jobs, engine_key, api_key, is_paid):
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
                    progress_cb, self.route_log, self.is_cancelled, verbose=False, reference_map=None)
            else:
                process_json_safely(
                    job["data"], engine_key, api_key, is_paid,
                    progress_cb, self.route_log, self.is_cancelled, verbose=False, reference_map=None)

    def _process_zip_file(self, zip_path, engine_key, api_key, is_paid):
        self.cancel_requested = False
        self.toggle_buttons(False)
        self.update_progress(0)

        base_zip_name = os.path.basename(zip_path)
        origin_dir = os.path.dirname(zip_path)
        raw_dir = os.path.join(origin_dir, "_temp_raw")
        out_dir = os.path.join(origin_dir, "_temp_out")

        try:
            for d in [raw_dir, out_dir]:
                if os.path.exists(d):
                    shutil.rmtree(d)

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

            mode_str = "[유료/초고속]" if is_paid else "[무료/안전대기]"
            engine_label = next((name for name, key in ENGINES.items() if key == engine_key), engine_key)
            self.log(f"\n🎯 총 {total_files}개 파일 압축 번역 시작... {mode_str}")
            self.log(f"🧩 선택 옵션: 엔진={engine_label} / 모드={mode_str}")

            jobs = []
            skipped_no_targets = 0
            skipped_bad_json = 0

            for idx, file_path in enumerate(target_files, 1):
                rel_path = os.path.relpath(file_path, raw_dir)
                target_path = os.path.join(out_dir, rel_path)
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                filename = os.path.basename(file_path)

                if file_path.lower().endswith('.snbt'):
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                    lines, targets = extract_snbt_targets(content)
                    if not targets:
                        skipped_no_targets += 1
                        shutil.copy2(file_path, target_path)
                        continue
                    jobs.append({"kind": "snbt", "target_path": target_path,
                                 "lines": lines, "targets": targets, "translated_map": {}})

                elif file_path.lower().endswith('.hqm'):
                    with open(file_path, 'rb') as f:
                        hqm_content = f.read()
                    self.set_status(f"🔄 [{idx}/{total_files}] [{filename}] HQM 바이너리 번역 중...")

                    def hqm_progress_cb(current, total, _idx=idx, _fn=filename):
                        base = (_idx - 1) / total_files
                        inner = (current / total) / total_files if total > 0 else 0
                        prog = base + inner
                        self.update_progress(prog)
                        self.set_status(f"⏳ [{_idx}/{total_files}] [{_fn}] HQM 번역 중... [{current}/{total}]")

                    try:
                        translated_hqm = process_hqm_with_progress(
                            hqm_content, engine_key, api_key, is_paid,
                            progress_callback=hqm_progress_cb,
                            log_callback=self.route_log, cancel_checker=self.is_cancelled)
                    except Exception as exc:
                        self.log(f"⚠️ [{filename}] HQM 처리 경고: {exc}")
                        translated_hqm = hqm_content
                    with open(target_path, 'wb') as f:
                        f.write(translated_hqm)
                    self.log(f"✅ [{filename}] HQM 번역 완료")
                    continue

                elif file_path.lower().endswith(('.json', '.lang')):
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        try:
                            data = json.load(f)
                        except json.JSONDecodeError as e:
                            skipped_bad_json += 1
                            shutil.copy2(file_path, target_path)
                            continue
                    node_targets = []
                    collect_json_targets(data, node_targets)
                    if not node_targets:
                        skipped_no_targets += 1
                        with open(target_path, 'w', encoding='utf-8') as f:
                            json.dump(data, f, ensure_ascii=False, indent=2)
                        continue
                    jobs.append({"kind": "json", "target_path": target_path,
                                 "data": data, "targets": node_targets})

            if jobs:
                if engine_key == "gemini_batch" and is_paid:
                    self._translate_jobs_parallel(jobs, api_key, is_paid)
                else:
                    self._translate_jobs_sequential(jobs, engine_key, api_key, is_paid)

            for job in jobs:
                if job["kind"] == "snbt":
                    text = job.get("final_text") or rebuild_snbt(job["lines"], job["translated_map"])
                    with open(job["target_path"], 'w', encoding='utf-8', newline='\n') as f:
                        f.write(text)
                else:
                    with open(job["target_path"], 'w', encoding='utf-8') as f:
                        json.dump(job["data"], f, ensure_ascii=False, indent=2)

            if skipped_no_targets or skipped_bad_json:
                self.log(f"ℹ️ 스캔 요약: 번역 대상 없음 {skipped_no_targets}개, JSON 형식 오류 {skipped_bad_json}개 건너뜀")

            self.update_progress(1.0)
            self.log("\n🎉 모든 파일 번역 완료! 저장할 위치를 선택해주세요.")
            save_dir = filedialog.askdirectory(title="번역된 ZIP 저장 폴더 선택")

            if save_dir:
                out_zip_path = os.path.join(save_dir, base_zip_name)
                with zipfile.ZipFile(out_zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_out:
                    for root, _, files_list in os.walk(out_dir):
                        for f in files_list:
                            full_p = os.path.join(root, f)
                            zip_out.write(full_p, os.path.relpath(full_p, out_dir))

                review_items = []
                for root, _, files_list in os.walk(raw_dir):
                    for f in files_list:
                        rel_path = os.path.relpath(os.path.join(root, f), raw_dir)
                        src_path = os.path.join(raw_dir, rel_path)
                        dst_path = os.path.join(out_dir, rel_path)
                        if not os.path.exists(dst_path):
                            continue
                        try:
                            if f.lower().endswith('.snbt'):
                                src = open(src_path, 'r', encoding='utf-8', errors='ignore').read()
                                dst = open(dst_path, 'r', encoding='utf-8', errors='ignore').read()
                                review_items.append((rel_path, analyze_snbt_texts(src, dst)))
                            elif f.lower().endswith(('.json', '.lang')):
                                src = json.load(open(src_path, encoding='utf-8', errors='ignore'))
                                dst = json.load(open(dst_path, encoding='utf-8', errors='ignore'))
                                review_items.append((rel_path, analyze_json_data(src, dst)))
                            elif f.lower().endswith('.hqm'):
                                src = open(src_path, 'rb').read()
                                dst = open(dst_path, 'rb').read()
                                review_items.append((rel_path, analyze_hqm_bytes(src, dst)))
                        except Exception as review_exc:
                            self.log(f"⚠️ 검수 스킵 [{rel_path}]: {review_exc}")

                if review_items:
                    report_text = render_review_report("ZIP 번역 검수 리포트", review_items)
                    report_path = os.path.join(save_dir, f"{os.path.splitext(base_zip_name)[0]}_review.txt")
                    with open(report_path, 'w', encoding='utf-8', newline='\n') as rf:
                        rf.write(report_text)
                    self.log(f"🧪 검수 리포트 저장: {report_path}")

                self.log(f"💾 압축 저장 완료: {out_zip_path}")
                self.show_messagebox("info", "완료", f"모든 작업이 완료되었습니다!\n\n저장 위치:\n{out_zip_path}")
                shutil.rmtree(out_dir, ignore_errors=True)
            else:
                self.log(f"⚠️ 저장 폴더 선택이 취소되었습니다. 번역 결과는 여기 남아있습니다:\n{out_dir}")

        except TranslationCancelledError as e:
            self.log(f"\n🛑 {str(e)}")
            self._notify_partial_result(out_dir, "warning", "취소됨", "사용자에 의해 번역 작업이 중단되었습니다.")
        except QuotaExceededError as e:
            self.log(f"\n🛑 [중단] {str(e)}")
            self._notify_partial_result(out_dir, "error", "한도 초과", str(e))
        except Exception as e:
            self.log(f"\n❌ 오류 발생: {str(e)}")
            self._notify_partial_result(out_dir, "error", "오류", f"오류가 발생했습니다:\n{str(e)}")
        finally:
            shutil.rmtree(raw_dir, ignore_errors=True)
            self.toggle_buttons(True)

    def _notify_partial_result(self, out_dir, kind, title, message):
        if os.path.exists(out_dir) and any(os.scandir(out_dir)):
            self.log(f"💾 지금까지 번역된 파일은 여기 남아있습니다:\n{out_dir}")
            message = f"{message}\n\n그때까지 번역된 파일은 아래 폴더에 남아있습니다:\n{out_dir}"
        self.show_messagebox(kind, title, message)
