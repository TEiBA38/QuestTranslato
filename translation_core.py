import os
import json
import shutil
import zipfile
import time

from constants import has_non_latin
from translation_engines import ENGINES
from file_processors import (
    extract_snbt_targets,
    rebuild_snbt,
    _run_batch_jobs,
    process_snbt_with_progress,
    process_json_safely,
    process_hqm_with_progress,
)
from translation_engines import QuotaExceededError, TranslationCancelledError
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


def run_zip_translation_logic(context: TranslationUIContext, zip_path, engine_key, api_key, is_paid, ai_model, target_lang, modpack_path, apply_mode, reference_map, glossary):
    base_zip_name = os.path.basename(zip_path)
    origin_dir = os.path.dirname(zip_path)
    raw_dir = os.path.join(origin_dir, "_temp_raw")
    out_dir = os.path.join(origin_dir, "_temp_out")
    
    # 1. Resume detection
    candidates = []
    if os.path.isfile(os.path.join(out_dir, PROGRESS_FILE)):
        comp = _load_progress(out_dir)
        if comp:
            candidates.append({"path": out_dir, "name": "기본 임시 번역 폴더 (기본 기록)", "count": len(comp), "completed": comp})
    
    if os.path.isdir(origin_dir):
        for entry in os.scandir(origin_dir):
            if entry.is_dir() and entry.name.startswith("_temp_out_") and entry.path != out_dir:
                prog_path = os.path.join(entry.path, PROGRESS_FILE)
                if os.path.isfile(prog_path):
                    comp = _load_progress(entry.path)
                    if comp:
                        candidates.append({"path": entry.path, "name": f"임시 백업 폴더 ({entry.name})", "count": len(comp), "completed": comp})

    out_dir, completed_set, resuming = context.ask_resume(candidates, out_dir)
    if out_dir is None:
        return # Cancelled by user

    try:
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

        for idx, file_path in enumerate(target_files, 1):
            rel_path = os.path.relpath(file_path, raw_dir)
            target_path = os.path.join(out_dir, rel_path)
            os.makedirs(os.path.dirname(target_path), exist_ok=True)

            if rel_path in completed_set:
                skipped_resume += 1
                continue

            if file_path.lower().endswith('.snbt'):
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                if has_non_latin(content):
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
                        log_callback=context.log, cancel_checker=context.is_cancelled, glossary=glossary, ai_model=ai_model, target_lang=target_lang)
                except Exception as exc:
                    context.log(f"⚠️ [{os.path.basename(file_path)}] HQM 처리 경고: {exc}")
                    translated_hqm = hqm_content
                with open(target_path, 'wb') as f:
                    f.write(translated_hqm)
                context.log(f"✅ [{os.path.basename(file_path)}] HQM 번역 완료")
                completed_set.add(rel_path)
                _save_progress(out_dir, completed_set)
                continue
            elif file_path.lower().endswith(('.json', '.lang')):
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    try:
                        data = json.load(f)
                    except json.JSONDecodeError:
                        shutil.copy2(file_path, target_path)
                        completed_set.add(rel_path)
                        _save_progress(out_dir, completed_set)
                        continue
                if has_non_latin(json.dumps(data, ensure_ascii=False)):
                    skipped_translated += 1
                    with open(target_path, 'w', encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    completed_set.add(rel_path)
                    _save_progress(out_dir, completed_set)
                    continue
                
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
                jobs.append({"kind": "json", "target_path": target_path, "rel_path": rel_path,
                             "data": data, "targets": node_targets})

        context.log(f"🔎 분석 완료: 총 {total_files}개 중 {len(jobs)}개 번역 대기 (건너뜀: 재개 {skipped_resume}개, 완료됨 {skipped_translated}개, 텍스트없음 {skipped_no_targets}개)")

        if not jobs:
            context.log("✅ 새로 번역할 파일이 없습니다. 모든 작업이 완료되었습니다.")
        else:
            context.set_status(f"총 {len(jobs)}개 파일 번역 중...")
            
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

            if getattr(context.app, "_translate_jobs_parallel", None):
                if engine_key == "gemini_batch" and is_paid:
                    context.app._translate_jobs_parallel(jobs, api_key, is_paid, ai_model=ai_model, target_lang=target_lang, on_job_completed=on_job_completed)
                else:
                    context.app._translate_jobs_sequential(jobs, engine_key, api_key, is_paid, ai_model=ai_model, target_lang=target_lang, on_job_completed=on_job_completed)

        if apply_mode is True:
            context.log(f"\n📦 원본 모드팩에 즉시 덮어쓰기 시작: {modpack_path}")
            shutil.copytree(out_dir, modpack_path, dirs_exist_ok=True)
            context.log(f"💾 덮어쓰기 완료: {modpack_path}")
            context.on_translation_success(modpack_path)
            
            _generate_zip_review_report(context, raw_dir, out_dir, "덮어쓰기 완료: 번역 검수 리포트")
            context.show_messagebox("info", "적용 완료", f"모드팩에 번역본이 성공적으로 덮어쓰기 되었습니다!\n\n적용 경로:\n{modpack_path}")
            shutil.rmtree(out_dir, ignore_errors=True)
            return

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
            context.log(f"⚠️ 저장 폴더 선택이 취소되었습니다. 번역 결과는 여기 남아있습니다:\n{out_dir}")

    except TranslationCancelledError as e:
        context.log(f"\n🛑 {str(e)}")
        context.offer_partial_backup(out_dir, os.path.splitext(base_zip_name)[0] + '_partial.zip')
    except QuotaExceededError as e:
        context.log(f"\n🛑 [중단] {str(e)}")
        context.offer_partial_backup(out_dir, os.path.splitext(base_zip_name)[0] + '_partial.zip')
    except Exception as e:
        context.log(f"\n❌ 오류 발생: {str(e)}")
        context.offer_partial_backup(out_dir, os.path.splitext(base_zip_name)[0] + '_partial.zip')
    finally:
        shutil.rmtree(raw_dir, ignore_errors=True)
