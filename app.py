import os
import zipfile
import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import customtkinter as ctk
    import tkinter as tk
    from tkinter import filedialog, messagebox
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:  # pragma: no cover - headless/test environments
    ctk = None
    tk = None
    filedialog = None
    messagebox = None
    DND_FILES = ()
    TkinterDnD = None

from translation_engines import (
    ENGINES,
    QuotaExceededError,
    TranslationCancelledError,
    translate_gemini_batch,
)
from file_processors import (
    collect_json_targets,
    extract_snbt_targets,
    process_hqm_with_progress,
    process_json_safely,
    process_snbt_with_progress,
    rebuild_snbt,
)
from review_checks import (
    analyze_hqm_bytes,
    analyze_json_data,
    analyze_snbt_texts,
    render_review_report,
)

# ==============================================================================
# 📊 아키텍처 문서 (Mermaid) - 코드 이해를 돕기 위한 다이어그램 주석입니다.
#    https://mermaid.live 에 아래 코드를 붙여넣으면 그림으로 확인할 수 있습니다.
# ==============================================================================
"""
[클래스 다이어그램]

classDiagram
    class QuestTranslatorApp {
        -cancel_requested bool
        +log(message)
        +update_progress(val)
        +toggle_buttons(state)
        +validate_inputs()
        +handle_file_drop(event)
        +run_single_file()
        +run_zip_file()
        +on_engine_change(choice)
        +toggle_api_visibility()
        +request_cancel()
        +is_cancelled()
    }
    class TranslationEngine {
        +translate_deepl(text, api_key)
        +translate_google(text, api_key)
        +translate_openai(text, api_key)
        +translate_gemini_batch(text_list, api_key)
    }
    class QuestFileProcessor {
        +process_snbt_with_progress(content)
        +process_json_safely(node)
        +collect_json_targets(node, list)
    }
    class QuotaExceededError
    class TranslationCancelledError
    Exception <|-- QuotaExceededError
    Exception <|-- TranslationCancelledError
    QuestTranslatorApp ..> QuestFileProcessor : uses
    QuestTranslatorApp ..> TranslationEngine : uses
    QuestFileProcessor ..> TranslationEngine : uses
    QuestFileProcessor ..> QuotaExceededError : raises
    QuestFileProcessor ..> TranslationCancelledError : raises

[유스케이스 다이어그램]

flowchart LR
    User(("사용자"))
    UC1("번역 엔진 및 API 키 설정")
    UC2("단일 파일 선택 번역")
    UC3("ZIP 파일 선택 번역")
    UC4("드래그 앤 드롭 번역")
    UC5("번역 진행 취소")
    UC6("번역 결과 저장")
    User --> UC1
    User --> UC2
    User --> UC3
    User --> UC4
    User --> UC5
    User --> UC6
    UC2 -.->|include| UC6
    UC3 -.->|include| UC6
    UC4 -.->|include| UC6
"""

# ==============================================================================
# 🎨 UI 및 테마 설정
# ==============================================================================
if ctk is not None:
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")
FONT_NAME = "Malgun Gothic"

# ==============================================================================
# 🚨 사용자 정의 예외 클래스
# ==============================================================================

# Core translation/parsing logic has been split into translation_engines.py and file_processors.py.


# ==============================================================================
# 🖥️ [3] GUI 구현 (CustomTkinter)
# ==============================================================================

if ctk is not None and TkinterDnD is not None:
    class QuestTranslatorApp(ctk.CTk, TkinterDnD.DnDWrapper):
        def __init__(self):
            if ctk is None or tk is None or TkinterDnD is None:
                raise RuntimeError("GUI 라이브러리가 설치되지 않아 앱을 초기화할 수 없습니다.")
            super().__init__()
            self.TkdndVersion = TkinterDnD._require(self)
            self._setup_ui()

        def _setup_ui(self):
            self.title("Quest Translator Pro (Batch Supported)")
            self.geometry("640x800")
            self.resizable(False, False)

            self.cancel_requested = False

            ctk.CTkLabel(
                self, text="⚡ 마인크래프트 퀘스트 자동 번역기", 
                font=ctk.CTkFont(family=FONT_NAME, size=20, weight="bold")
            ).pack(padx=20, pady=(15, 5))

            self.config_frame = ctk.CTkFrame(self)
            self.config_frame.pack(fill="x", padx=20, pady=5)

            ctk.CTkLabel(
                self.config_frame, text="🌐 번역 엔진 선택", 
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold")
            ).pack(anchor="w", padx=15, pady=(8, 2))

            self.engine_combo = ctk.CTkComboBox(
                self.config_frame, 
                values=list(ENGINES.keys()),
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                command=self.on_engine_change
            )
            self.engine_combo.pack(fill="x", padx=15, pady=(0, 6))
            self.engine_combo.set("Gemini Lite (배치 번역)")

            ctk.CTkLabel(
                self.config_frame, text="💳 Gemini 계정 상태 (요금제)", 
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold")
            ).pack(anchor="w", padx=15, pady=(2, 2))

            self.plan_combo = ctk.CTkComboBox(
                self.config_frame,
                values=["유료 계정 (Pay-as-you-go / 초고속 / 제한없음)", "무료 계정 (안전대기 / 10 RPM 속도제한)"],
                font=ctk.CTkFont(family=FONT_NAME, size=12)
            )
            self.plan_combo.pack(fill="x", padx=15, pady=(0, 8))
            self.plan_combo.set("유료 계정 (Pay-as-you-go / 초고속 / 제한없음)")

            ctk.CTkLabel(
                self.config_frame, text="🔑 API 키 설정", 
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold")
            ).pack(anchor="w", padx=15, pady=(2, 2))

            self.api_sub_frame = ctk.CTkFrame(self.config_frame, fg_color="transparent")
            self.api_sub_frame.pack(fill="x", padx=15, pady=(0, 8))

            self.api_entry = ctk.CTkEntry(
                self.api_sub_frame, show="*", placeholder_text="API 키를 입력하세요",
                font=ctk.CTkFont(family=FONT_NAME, size=12)
            )
            self.api_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))

            self.show_btn = ctk.CTkButton(
                self.api_sub_frame, text="보기", width=60, 
                font=ctk.CTkFont(family=FONT_NAME, size=12), command=self.toggle_api_visibility
            )
            self.show_btn.pack(side="right")



            self.drop_frame = ctk.CTkFrame(self, fg_color="#2b2b2b", border_color="#1f538d", border_width=2)
            self.drop_frame.pack(fill="x", padx=20, pady=10)

            ctk.CTkLabel(
                self.drop_frame, 
                text="📂 파일(.snbt, .json, .hqm) 또는 ZIP을 여기에 끌어다 놓으세요!",
                font=ctk.CTkFont(family=FONT_NAME, size=13, weight="bold"),
                text_color="#3B82F6", pady=18
            ).pack()

            self.drop_frame.drop_target_register(DND_FILES)
            self.drop_frame.dnd_bind('<<Drop>>', self.handle_file_drop)

            self.mode_frame = ctk.CTkFrame(self)
            self.mode_frame.pack(fill="x", padx=20, pady=5)

            self.btn_sub_frame = ctk.CTkFrame(self.mode_frame, fg_color="transparent")
            self.btn_sub_frame.pack(fill="x", padx=15, pady=10)

            self.btn_single = ctk.CTkButton(
                self.btn_sub_frame, text="📄 파일 선택 번역\n(.snbt / .json / .hqm)", 
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"), height=45,
                fg_color="#1f538d", hover_color="#14375e", command=self.run_single_file
            )
            self.btn_single.pack(side="left", fill="x", expand=True, padx=(0, 5))

            self.btn_zip = ctk.CTkButton(
                self.btn_sub_frame, text="📦 ZIP 선택 번역\n(.zip 파일)", 
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"), height=45,
                fg_color="#2e7d32", hover_color="#1b5e20", command=self.run_zip_file
            )
            self.btn_zip.pack(side="right", fill="x", expand=True, padx=(5, 0))

            self.log_frame = ctk.CTkFrame(self)
            self.log_frame.pack(fill="both", expand=True, padx=20, pady=(5, 15))

            self.progress_sub_frame = ctk.CTkFrame(self.log_frame, fg_color="transparent")
            self.progress_sub_frame.pack(fill="x", padx=15, pady=(10, 5))

            self.progress = ctk.CTkProgressBar(self.progress_sub_frame)
            self.progress.pack(side="left", fill="x", expand=True, padx=(0, 10))
            self.progress.set(0)

            self.btn_cancel = ctk.CTkButton(
                self.progress_sub_frame, text="🛑 취소", width=70, height=28,
                fg_color="#c62828", hover_color="#8e0000",
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                command=self.request_cancel, state="disabled"
            )
            self.btn_cancel.pack(side="right")

            self.status_label = ctk.CTkLabel(
                self.log_frame, text="", anchor="w",
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                text_color="#8fd6b8"
            )
            self.status_label.pack(fill="x", padx=15, pady=(0, 5))

            log_text_frame = tk.Frame(self.log_frame, bg="#2b2b2b")
            log_text_frame.pack(fill="both", expand=True, padx=15, pady=(0, 10))

            log_scrollbar = tk.Scrollbar(log_text_frame)
            log_scrollbar.pack(side="right", fill="y")

            self.log_textbox = tk.Text(
                log_text_frame,
                font=(FONT_NAME, 12),
                bg="#2b2b2b", fg="#d7dbe0",
                insertbackground="#d7dbe0",
                selectbackground="#2f5d8c", selectforeground="#ffffff",
                relief="flat", borderwidth=0, highlightthickness=0,
                wrap="word", yscrollcommand=log_scrollbar.set,
            )
            self.log_textbox.pack(side="left", fill="both", expand=True)
            log_scrollbar.config(command=self.log_textbox.yview)
            self.log_textbox.configure(state="disabled")

            # ... 앞부분 코드 생략 (def _asetup_ui 내부 마지막 부분) ...

            self.log("💡 [안내] FTB 및 HQM 퀘스트 언어 파일(.snbt / .json / .hqm) 완벽 지원이 적용되었습니다.")
            

        # ---------------------------------------------------------
        # 🚨 수정됨: 아래의 모든 메서드들을 `else:` 블록 밖으로 꺼내고, 
        # QuestTranslatorApp 클래스 안에 포함되도록 들여쓰기(Space 8칸)를 맞춥니다.
        # ---------------------------------------------------------

        def request_cancel(self):
            self.cancel_requested = True
            self.log("🛑 사용자가 번역 취소를 요청했습니다. 작업을 중단합니다...")
            self.btn_cancel.configure(state="disabled")

        def is_cancelled(self):
            return self.cancel_requested

        def on_engine_change(self, choice):
            if choice == "Google Translate":
                self.api_entry.configure(state="disabled")
                self.show_btn.configure(state="disabled")
                self.plan_combo.configure(state="disabled")
                self.log("💡 Google Translate는 API 키 없이 무료 사용 가능합니다.")
            else:
                self.api_entry.configure(state="normal")
                self.show_btn.configure(state="normal")
                if "Gemini" in choice:
                    self.plan_combo.configure(state="normal")
                    self.log("💡 Gemini API 키를 입력하고 계정 상태(유료/무료)를 지정해주세요.")
                else:
                    self.plan_combo.configure(state="disabled")

        def toggle_api_visibility(self):
            if self.api_entry.cget("show") == "*":
                self.api_entry.configure(show="")
                self.show_btn.configure(text="숨기기")
            else:
                self.api_entry.configure(show="*")
                self.show_btn.configure(text="보기")

        def log(self, message):
            if threading.current_thread() is not threading.main_thread():
                self.after(0, lambda: self.log(message))
                return

            self.log_textbox.configure(state="normal")
            self.log_textbox.insert("end", message + "\n")
            self.log_textbox.see("end")
            self.log_textbox.configure(state="disabled")
            self.update_idletasks()

        def set_status(self, text):
            if threading.current_thread() is not threading.main_thread():
                self.after(0, lambda: self.set_status(text))
                return
            self.status_label.configure(text=text)

        def route_log(self, message):
            if message.strip().startswith("⏳"):
                self.set_status(message.strip())
            else:
                self.log(message)

        def update_progress(self, val):
            if threading.current_thread() is not threading.main_thread():
                self.after(0, lambda: self.update_progress(val))
                return

            self.progress.set(val)
            self.update_idletasks()

        def show_messagebox(self, kind, title, message):
            func = {"info": messagebox.showinfo, "warning": messagebox.showwarning, "error": messagebox.showerror}[kind]
            if threading.current_thread() is not threading.main_thread():
                self.after(0, lambda: func(title, message))
            else:
                func(title, message)

        def toggle_buttons(self, state):
            btn_state = "normal" if state else "disabled"
            cancel_state = "disabled" if state else "normal"

            self.btn_single.configure(state=btn_state)
            self.btn_zip.configure(state=btn_state)
            if hasattr(self, 'btn_cancel'):
                self.btn_cancel.configure(state=cancel_state)

        def validate_inputs(self):
            engine_name = self.engine_combo.get()
            engine_key = ENGINES.get(engine_name)
            api_key = self.api_entry.get().strip()

            if engine_key != "google" and not api_key:
                messagebox.showwarning("경고", f"{engine_name} 사용을 위해 API 키를 입력해주세요.")
                return None, None, False

            is_paid = "유료" in self.plan_combo.get()
            return engine_key, api_key, is_paid

        def handle_file_drop(self, event):
            engine_key, api_key, is_paid = self.validate_inputs()
            if not engine_key: return

            dropped_path = event.data.strip('{}').strip('"')
            if not os.path.exists(dropped_path): return

            if dropped_path.lower().endswith(('.snbt', '.json', '.hqm')):
                threading.Thread(target=self._process_single_file, args=(dropped_path, engine_key, api_key, is_paid), daemon=True).start()
            elif dropped_path.lower().endswith('.zip'):
                threading.Thread(target=self._process_zip_file, args=(dropped_path, engine_key, api_key, is_paid), daemon=True).start()
            else:
                messagebox.showwarning("지원하지 않는 파일", ".snbt, .json, .hqm 또는 .zip 파일만 지원합니다.")

        def run_single_file(self):
            engine_key, api_key, is_paid = self.validate_inputs()
            if not engine_key: return

            file_path = filedialog.askopenfilename(
                title="번역할 파일 선택",
                filetypes=[("Quest Files", "*.snbt *.json *.hqm"), ("All Files", "*.*")]
            )
            if not file_path: return

            threading.Thread(target=self._process_single_file, args=(file_path, engine_key, api_key, is_paid), daemon=True).start()

        def _process_single_file(self, file_path, engine_key, api_key, is_paid):
            self.cancel_requested = False
            self.toggle_buttons(False)
            self.update_progress(0)
            
            file_name = os.path.basename(file_path)
            mode_str = "[유료/초고속]" if is_paid else "[무료/안전대기]"
            engine_label = next((name for name, key in ENGINES.items() if key == engine_key), engine_key)
            self.log(f"\n🎯 파일 작업 시작: '{file_name}'")
            self.log(f"🧩 선택 옵션: 엔진={engine_label} / 모드={mode_str}")

            def single_file_progress_callback(current, total):
                prog = current / total if total > 0 else 1
                self.update_progress(prog)
                self.set_status(f"⏳ 번역 진행 중... [{current}/{total}] ({int(prog * 100)}%)")

            try:
                translated_content = None
                json_data = None
                source_content = None
                source_json_data = None
                reference_map = None

                if file_path.lower().endswith('.snbt'):
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                    source_content = content
                    translated_content = process_snbt_with_progress(
                        content, engine_key, api_key, is_paid, single_file_progress_callback, self.route_log, self.is_cancelled, reference_map=reference_map
                    )
                elif file_path.lower().endswith('.json'):
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        try:
                            json_data = json.load(f)
                        except json.JSONDecodeError as e:
                            raise Exception(
                                f"'{file_name}' 파일이 올바른 JSON/HQM 형식이 아닙니다 "
                                f"({e.lineno}번째 줄, {e.colno}번째 열 부근을 확인해주세요: {e.msg})"
                            )
                    source_json_data = json.loads(json.dumps(json_data, ensure_ascii=False))
                    process_json_safely(
                        json_data, engine_key, api_key, is_paid, single_file_progress_callback, self.route_log, self.is_cancelled, reference_map=reference_map
                    )
                elif file_path.lower().endswith('.hqm'):
                    with open(file_path, 'rb') as f:
                        content = f.read()
                    source_content = content
                    try:
                        translated_content = process_hqm_with_progress(
                            content, engine_key, api_key, is_paid, single_file_progress_callback, self.route_log, self.is_cancelled, reference_map=reference_map
                        )
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
                            review_reports.append((
                                file_name,
                                analyze_snbt_texts(source_content, translated_content)
                            ))
                    elif file_path.lower().endswith('.hqm') and translated_content is not None:
                        with open(out_path, 'wb') as f:
                            f.write(translated_content)
                        if source_content is not None:
                            review_reports.append((
                                file_name,
                                analyze_hqm_bytes(source_content, translated_content)
                            ))
                    elif file_path.lower().endswith('.json') and json_data is not None:
                        with open(out_path, 'w', encoding='utf-8') as f:
                            json.dump(json_data, f, ensure_ascii=False, indent=2)
                        if source_json_data is not None:
                            review_reports.append((
                                file_name,
                                analyze_json_data(source_json_data, json_data)
                            ))

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

        def run_zip_file(self):
            engine_key, api_key, is_paid = self.validate_inputs()
            if not engine_key: return

            zip_path = filedialog.askopenfilename(
                title="번역할 ZIP 선택",
                filetypes=[("ZIP Files", "*.zip"), ("All Files", "*.*")]
            )
            if not zip_path: return

            threading.Thread(target=self._process_zip_file, args=(zip_path, engine_key, api_key, is_paid), daemon=True).start()

        def _translate_jobs_parallel(self, jobs, api_key, is_paid):
            batch_size = 50
            tasks = []
            for job in jobs:
                targets = job["targets"]
                for i in range(0, len(targets), batch_size):
                    tasks.append((job, targets[i:i + batch_size]))

            total_items = sum(len(job["targets"]) for job in jobs)
            completed_items = 0
            lock = threading.Lock()

            def run_task(task):
                job, chunk = task
                if self.is_cancelled():
                    raise TranslationCancelledError("사용자에 의해 번역이 취소되었습니다.")

                if job["kind"] == "snbt":
                    texts = [item[2].replace('\\"', '"') for item in chunk]
                else:
                    texts = [item[2] for item in chunk]

                translated_texts = translate_gemini_batch(texts, api_key, is_paid, self.route_log, self.is_cancelled)

                if job["kind"] == "snbt":
                    for (line_idx, prefix, _, suffix), trans_text in zip(chunk, translated_texts):
                        final_text = str(trans_text).replace('"', '\\"')
                        job["translated_map"][line_idx] = f'{prefix}"{final_text}"{suffix}'
                else:
                    for (parent_node, key, _), trans_text in zip(chunk, translated_texts):
                        parent_node[key] = trans_text

                return len(chunk)

            max_workers = min(8, len(tasks)) or 1
            executor = ThreadPoolExecutor(max_workers=max_workers)
            try:
                futures = [executor.submit(run_task, t) for t in tasks]
                for future in as_completed(futures):
                    n = future.result() 
                    with lock:
                        completed_items += n
                        current = completed_items
                    self.set_status(f"⏳ Gemini API 번역 진행 중... [{current}/{total_items}]")
                    self.update_progress(current / total_items if total_items else 1)
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
                    content = "\n".join(job["lines"])
                    job["final_text"] = process_snbt_with_progress(
                        content, engine_key, api_key, is_paid, progress_cb, self.route_log, self.is_cancelled,
                        verbose=False, reference_map=None
                    )
                else:
                    process_json_safely(
                        job["data"], engine_key, api_key, is_paid, progress_cb, self.route_log, self.is_cancelled,
                        verbose=False, reference_map=None
                    )

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

                target_files = []
                for root, _, files_list in os.walk(raw_dir):
                    for f in files_list:
                        if f.lower().endswith(('.snbt', '.json', '.lang', '.hqm')):
                            target_files.append(os.path.join(root, f))

                total_files = len(target_files)
                if total_files == 0:
                    self.log("⚠️ 번역할 대상 파일(.snbt, .json, .hqm)을 찾을 수 없습니다.")
                    return

                mode_str = "[유료/초고속]" if is_paid else "[무료/안전대기]"
                engine_label = next((name for name, key in ENGINES.items() if key == engine_key), engine_key)
                self.log(f"\n🎯 총 {total_files}개 파일 압축 번역 시작... {mode_str}")
                self.log(f"🧩 선택 옵션: 엔진={engine_label} / 모드={mode_str}")

                jobs = []
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
                            self.log(f"⚠️ [{filename}] 번역할 텍스트 대상을 찾지 못했습니다.")
                            shutil.copy2(file_path, target_path)
                            continue
                        jobs.append({
                            "kind": "snbt", "target_path": target_path,
                            "lines": lines, "targets": targets, "translated_map": {},
                        })

                    elif file_path.lower().endswith('.hqm'):
                        with open(file_path, 'rb') as f:
                            hqm_content = f.read()
                        self.set_status(f"🔄 [{idx}/{total_files}] [{filename}] HQM 바이너리 번역 중...")

                        def hqm_progress_cb(current, total, _idx=idx):
                            base = (_idx - 1) / total_files
                            inner = (current / total) / total_files if total > 0 else 0
                            prog = base + inner
                            self.update_progress(prog)
                            self.set_status(f"⏳ [{_idx}/{total_files}] [{filename}] HQM 번역 진행 중... [{current}/{total}] ({int(prog * 100)}%)")

                        try:
                            translated_hqm = process_hqm_with_progress(
                                hqm_content, engine_key, api_key, is_paid,
                                progress_callback=hqm_progress_cb,
                                log_callback=self.route_log, cancel_checker=self.is_cancelled
                            )
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
                                self.log(
                                    f"⚠️ [{filename}] 올바른 JSON 형식이 아니라 건너뜁니다 "
                                    f"({e.lineno}번째 줄, {e.colno}번째 열 부근: {e.msg})"
                                )
                                shutil.copy2(file_path, target_path)
                                continue
                        node_targets = []
                        collect_json_targets(data, node_targets)
                        if not node_targets:
                            self.log(f"⚠️ [{filename}] 번역할 텍스트 대상을 찾지 못했습니다.")
                            with open(target_path, 'w', encoding='utf-8') as f:
                                json.dump(data, f, ensure_ascii=False, indent=2)
                            continue
                        jobs.append({
                            "kind": "json", "target_path": target_path,
                            "data": data, "targets": node_targets,
                        })

                if jobs:
                    if engine_key == "gemini_batch" and is_paid:
                        self._translate_jobs_parallel(jobs, api_key, is_paid)
                    else:
                        self._translate_jobs_sequential(jobs, engine_key, api_key, is_paid)

                for job in jobs:
                    if job["kind"] == "snbt":
                        final_text = job.get("final_text")
                        text = final_text if final_text is not None else rebuild_snbt(job["lines"], job["translated_map"])
                        with open(job["target_path"], 'w', encoding='utf-8', newline='\n') as f:
                            f.write(text)
                    else:
                        with open(job["target_path"], 'w', encoding='utf-8') as f:
                            json.dump(job["data"], f, ensure_ascii=False, indent=2)

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
                                    with open(src_path, 'r', encoding='utf-8', errors='ignore') as sf:
                                        src_text = sf.read()
                                    with open(dst_path, 'r', encoding='utf-8', errors='ignore') as df:
                                        dst_text = df.read()
                                    review_items.append((rel_path, analyze_snbt_texts(src_text, dst_text)))
                                elif f.lower().endswith(('.json', '.lang')):
                                    with open(src_path, 'r', encoding='utf-8', errors='ignore') as sf:
                                        src_json = json.load(sf)
                                    with open(dst_path, 'r', encoding='utf-8', errors='ignore') as df:
                                        dst_json = json.load(df)
                                    review_items.append((rel_path, analyze_json_data(src_json, dst_json)))
                                elif f.lower().endswith('.hqm'):
                                    with open(src_path, 'rb') as sf:
                                        src_bin = sf.read()
                                    with open(dst_path, 'rb') as df:
                                        dst_bin = df.read()
                                    review_items.append((rel_path, analyze_hqm_bytes(src_bin, dst_bin)))
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

# ---------------------------------------------------------
# 🚨 예외 처리를 위한 else 구문은 모든 메서드 바깥(맨 하단)에 위치해야 합니다.
# ---------------------------------------------------------
else:
    class QuestTranslatorApp(object):
        def __init__(self):
            raise RuntimeError("GUI 라이브러리가 설치되지 않아 앱을 초기화할 수 없습니다.")

# ==============================================================================
# 🚀 메인 프로그램 진입점
# ==============================================================================
if __name__ == "__main__":
    app = QuestTranslatorApp()
    app.mainloop()