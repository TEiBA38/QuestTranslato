"""
UI 화면 전환, 레이아웃, 로그, 입력 검증 관련 메서드 믹스인.
QuestTranslatorApp이 이 클래스를 상속해서 사용합니다.
"""
import threading
import os
import time
import re
import translation_memory
import translation_editor
import logging

try:
    import customtkinter as ctk
    import tkinter as tk
    from tkinter import messagebox, ttk
except Exception:
    ctk = None
    tk = None
    messagebox = None
    ttk = None

from translation_engines import ENGINES
from constants import (
    FONT_NAME,
    MODELS_GEMINI_FREE,
    MODELS_GEMINI_PAID,
    MODELS_OPENAI,
    MODELS_CLAUDE,
    MODELS_DEEPSEEK,
    normalize_model_name,
    has_hangul,
)


class UIScreensMixin:
    # ====================================================================
    # 화면 전환
    # ====================================================================

    def show_home_screen(self):
        self.phase_label.configure(text="START · 작업 선택")
        self.select_screen.grid_remove()
        self.quick_translate_screen.grid_remove()
        self.translate_screen.grid_remove()
        self.home_screen.grid()

    def show_select_screen(self):
        self.phase_label.configure(text="STEP 1/2 · 모드팩 선택")
        self.home_screen.grid_remove()
        self.quick_translate_screen.grid_remove()
        self.translate_screen.grid_remove()
        self.select_screen.grid()
        self._apply_responsive_layout()

        saved_root = self.instance_path_entry.get().strip()
        if saved_root and os.path.isdir(saved_root) and not self.app_state.scan_thread_active and not getattr(self, "_suppress_auto_scan", False):
            self.scan_modpacks_from_entry(show_screen=False)

    def show_quick_translate_screen(self):
        self.phase_label.configure(text="STEP 1-A/2 · 파일/ZIP 번역")
        self.home_screen.grid_remove()
        self.select_screen.grid_remove()
        self.translate_screen.grid_remove()
        self.quick_translate_screen.grid()
        self._apply_responsive_layout()

    def show_translate_screen(self, force=False):
        if not force and not self.selected_modpack_path:
            messagebox.showwarning("경고", "먼저 모드팩을 선택해주세요.")
            return
        self.phase_label.configure(text="STEP 2/2 · 번역 설정 및 진행")
        self.home_screen.grid_remove()
        self.select_screen.grid_remove()
        self.quick_translate_screen.grid_remove()
        self.translate_screen.grid()
        self._apply_responsive_layout()

    # 하위 호환 별칭
    def show_launcher_setup_screen(self):
        self.show_select_screen()

    def show_launcher_modpack_screen(self):
        self.show_translate_screen()

    # ====================================================================
    # 스플래시 & 시작 로딩
    # ====================================================================

    def _show_startup_loading(self):
        self.startup_overlay = ctk.CTkFrame(self, fg_color="#07070a", corner_radius=0)
        self.startup_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)

        panel = ctk.CTkFrame(self.startup_overlay, fg_color="#16161a", corner_radius=18, border_width=1, border_color="#2a2a30")
        panel.place(relx=0.5, rely=0.5, anchor="center")

        logo_canvas = tk.Canvas(panel, width=82, height=82, bg="#16161a", highlightthickness=0)
        logo_canvas.pack(pady=(24, 10))
        logo_canvas.create_oval(6, 6, 76, 76, outline="#fb923c", width=2, fill="#0f0f12")
        logo_canvas.create_text(41, 41, text="Q", font=(FONT_NAME, 26, "bold"), fill="#f5f5f5")
        logo_canvas.create_oval(48, 18, 60, 30, fill="#f97316", outline="")

        ctk.CTkLabel(panel, text="Quest Translator Pro",
                     font=ctk.CTkFont(family=FONT_NAME, size=24, weight="bold"),
                     text_color="#f5f5f5").pack(padx=28, pady=(2, 4))

        ctk.CTkLabel(panel, text="Minecraft 모드팩 번역을 더 부드럽게",
                     font=ctk.CTkFont(family=FONT_NAME, size=12),
                     text_color="#fb923c").pack(padx=28, pady=(0, 8))

        self.startup_status_label = ctk.CTkLabel(
            panel, text="초기 환경을 준비하고 있습니다...",
            font=ctk.CTkFont(family=FONT_NAME, size=12), text_color="#cbd5e1")
        self.startup_status_label.pack(padx=28, pady=(0, 12))

        bar = ctk.CTkProgressBar(panel, width=260, fg_color="#27272a", progress_color="#ea580c")
        bar.pack(padx=28, pady=(0, 20))
        # 백그라운드 클라우드 마스터 메모리 초고속 사전 로드 시작
        threading.Thread(target=translation_memory.load_memory, daemon=True).start()

        # 인위적 지연 및 DWM 윈도우 투명도 프리징을 제거하고 150ms 후 즉시 메인 화면 진입
        bar.set(0.9)
        self.after(150, self._finish_startup_loading)

    def _update_startup_status(self, message):
        if hasattr(self, "startup_status_label") and self.startup_status_label is not None:
            self.startup_status_label.configure(text=message)

    def _finish_startup_loading(self):
        if hasattr(self, "startup_overlay") and self.startup_overlay is not None:
            self.startup_overlay.destroy()
            self.startup_overlay = None
        self.load_user_settings()
        self.show_home_screen()
        self.log("[안내] 원하는 작업을 선택한 뒤 이어서 진행하세요.")
        self._apply_responsive_layout()

    # ====================================================================
    # 반응형 레이아웃
    # ====================================================================

    def _on_window_resize(self, event=None):
        if event is not None and getattr(event, "widget", None) is not self:
            return
        new_width = max(self.winfo_width(), 1)
        if hasattr(self, '_last_resize_width') and abs(new_width - self._last_resize_width) < 15:
            return
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        # Windows 창 최대화 애니메이션(약 200ms) 완료 후 단 한 번만 실행되도록 220ms 디바운스 적용
        self._resize_job = self.after(220, self._apply_responsive_layout)

    def _apply_responsive_layout(self):
        width = max(self.winfo_width(), 1)
        self._last_resize_width = width
        self._arrange_path_buttons(width)
        self._arrange_quick_buttons(width)

        card_columns = self._get_card_columns()
        if card_columns != self._last_card_columns or getattr(self, "_needs_card_relayout", False):
            self._last_card_columns = card_columns
            self._needs_card_relayout = False

            # 모드팩 선택 화면이 활성화(화면에 표시) 상태일 때만 재배치 실행
            is_select_screen_visible = False
            try:
                is_select_screen_visible = self.select_screen.winfo_ismapped()
            except Exception:
                pass

            if is_select_screen_visible and self.modpack_entries:
                # 🌟 초고속 재배치: 위젯 파괴/재생성 없이 grid 위치만 즉시 변경 (0ms 렌더링)
                relayout_fn = getattr(self, "relayout_modpack_cards", None)
                if not relayout_fn or not relayout_fn(card_columns):
                    self.render_modpack_cards(self.modpack_entries)
            else:
                # 선택 화면이 보이지 않는 상태(홈 화면/번역 화면)라면 플래그만 세팅하고 리렌더링 스킵!
                self._needs_card_relayout = True

    def _arrange_path_buttons(self, width):
        if not hasattr(self, "path_action_buttons"):
            return
        for button in self.path_action_buttons:
            button.pack_forget()

        if width < 980:
            for button in self.path_action_buttons:
                button.configure(width=0)
                button.pack(fill="x", pady=3)
        else:
            self.btn_pick_instance_root.configure(width=110)
            self.btn_auto_detect_root.configure(width=110)
            self.btn_rescan_modpacks.configure(width=110)
            self.btn_open_translate_options.configure(width=170)
            self.btn_pick_instance_root.pack(side="left")
            self.btn_auto_detect_root.pack(side="left", padx=(6, 0))
            self.btn_rescan_modpacks.pack(side="left", padx=(6, 0))
            self.btn_open_translate_options.pack(side="right")

    def _arrange_quick_buttons(self, width):
        if not hasattr(self, "quick_buttons"):
            return
        self.btn_single.pack_forget()
        self.btn_zip.pack_forget()

        if width < 860:
            self.btn_single.pack(fill="x", pady=(0, 6))
            self.btn_zip.pack(fill="x")
        else:
            self.btn_single.pack(side="left", fill="x", expand=True, padx=(0, 5))
            self.btn_zip.pack(side="right", fill="x", expand=True, padx=(5, 0))

    # ====================================================================
    # 로그 & 상태 표시
    # ====================================================================

    def log(self, message):
        if not message:
            return
        if not message.startswith("⏳"):
            logging.info(message)

        if not hasattr(self, '_log_queue'):
            self._log_queue = []
            self._log_flush_job = None
            self._log_queue_lock = threading.Lock()

        with self._log_queue_lock:
            self._log_queue.append(message)
            if self._log_flush_job is None:
                if threading.current_thread() is threading.main_thread():
                    self._log_flush_job = self.after(60, self._flush_log_queue)
                else:
                    self.after(0, self._schedule_log_flush)

    def _schedule_log_flush(self):
        if hasattr(self, '_log_flush_job') and self._log_flush_job is None:
            self._log_flush_job = self.after(60, self._flush_log_queue)

    def _flush_log_queue(self):
        self._log_flush_job = None
        if not hasattr(self, '_log_queue'):
            return
        with self._log_queue_lock:
            if not self._log_queue:
                return
            batch = list(self._log_queue)
            self._log_queue.clear()

        if not hasattr(self, 'log_textbox') or self.log_textbox is None:
            return

        try:
            self.log_textbox.configure(state="normal")
            self.log_textbox.insert("end", "\n".join(batch) + "\n")
            # 3500줄 초과 시 메모리 누수 방지 자동 가지치기
            line_count = int(float(self.log_textbox.index("end-1c")))
            if line_count > 3500:
                self.log_textbox.delete("1.0", f"{line_count - 3000}.0")
            self.log_textbox.see("end")
            self.log_textbox.configure(state="disabled")
        except Exception:
            pass

    def set_status(self, text):
        if threading.current_thread() is not threading.main_thread():
            self.after(0, lambda: self.set_status(text))
            return
        self._last_status_base = text
        eta = getattr(self, "_current_eta", "")
        if eta and ("번역" in text or "처리" in text or "진행" in text):
            self.status_label.configure(text=f"{text} (남은 시간: 약 {eta})")
        else:
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
        
        self._target_progress = val
        if val == 0.0 or val >= 1.0:
            self._current_eta = ""
            
        if val == 0.0:
            self._current_progress = 0.0
            if getattr(self, "progress", None) and self.progress.winfo_exists():
                self.progress.set(0.0)
            self._progress_start_time = time.time()
            if not getattr(self, "_animating_progress", False):
                self._animating_progress = True
                self._animate_progress()
        
        elif val > 0.0 and val < 1.0 and hasattr(self, '_progress_start_time'):
            elapsed = time.time() - self._progress_start_time
            if elapsed > 3.0 and val > 0.001:
                total_est = elapsed / val
                remaining = max(total_est - elapsed, 0)
                m, s = divmod(int(remaining), 60)
                h, m = divmod(m, 60)
                time_str = f"{h}시간 {m}분" if h > 0 else f"{m}분 {s}초"
                
                self._current_eta = time_str
                
                if getattr(self, "status_label", None) and self.status_label.winfo_exists():
                    base_text = getattr(self, "_last_status_base", self.status_label.cget("text").split(" (남은 시간:")[0])
                    self.status_label.configure(text=f"{base_text} (남은 시간: 약 {time_str})")

        self.update_idletasks()

    def _animate_progress(self):
        if not getattr(self, "_animating_progress", False):
            return
        if not getattr(self, "progress", None) or not self.progress.winfo_exists():
            self._animating_progress = False
            return
            
        target = getattr(self, "_target_progress", 0.0)
        current = getattr(self, "_current_progress", 0.0)
        
        if target >= 1.0:
            self.progress.set(1.0)
            self._animating_progress = False
            return
            
        fake_target = min(target + 0.03, 0.99)
        if current < fake_target:
            step = 0.0003
            if current < target:
                step = max(step, (target - current) * 0.1)
                
            current = min(current + step, fake_target)
            self._current_progress = current
            self.progress.set(current)
            
        self.after(50, self._animate_progress)

    def show_messagebox(self, kind, title, message):
        def _show():
            try:
                import platform
                if platform.system() == "Windows":
                    import winsound
                    flags = {"info": winsound.MB_OK, "warning": winsound.MB_ICONEXCLAMATION, "error": winsound.MB_ICONHAND}
                    winsound.MessageBeep(flags.get(kind, winsound.MB_OK))
            except:
                pass
            
            import os
            func = {"info": messagebox.showinfo, "warning": messagebox.showwarning, "error": messagebox.showerror}[kind]
            
            if kind == "info" and ("위치:" in message or "경로:" in message):
                path_str = ""
                if "경로:" in message:
                    path_str = message.split("경로:")[-1].strip()
                elif "위치:" in message:
                    path_str = message.split("위치:")[-1].strip()
                    
                if path_str and os.path.exists(path_str):
                    folder_path = path_str if os.path.isdir(path_str) else os.path.dirname(path_str)
                    if messagebox.askyesno(title, message + "\n\n📂 결과 폴더를 여시겠습니까?"):
                        try:
                            os.startfile(folder_path)
                        except:
                            pass
                    return
            
            func(title, message)

        if threading.current_thread() is not threading.main_thread():
            self.after(0, _show)
        else:
            _show()

    def show_review_report(self, report_text):
        if threading.current_thread() is not threading.main_thread():
            self.after(0, lambda: self.show_review_report(report_text))
            return

        dialog = ctk.CTkToplevel(self)
        dialog.title("🧪 번역 검수 리포트")
        dialog.geometry("620x520")
        dialog.minsize(500, 400)
        dialog.transient(self)
        dialog.grab_set()

        dialog.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - 620) // 2
        y = self.winfo_y() + (self.winfo_height() - 520) // 2
        dialog.geometry(f"+{x}+{y}")

        header = ctk.CTkFrame(dialog, fg_color="#18181d", corner_radius=12)
        header.pack(fill="x", padx=12, pady=(12, 6))

        ctk.CTkLabel(header, text="🧪 번역 검수 리포트",
                     font=ctk.CTkFont(family=FONT_NAME, size=16, weight="bold"),
                     text_color="#f8fafc").pack(anchor="w", padx=12, pady=(10, 2))
        ctk.CTkLabel(header, text="번역 품질을 자동으로 분석한 결과입니다.",
                     font=ctk.CTkFont(family=FONT_NAME, size=11),
                     text_color="#a1a1aa").pack(anchor="w", padx=12, pady=(0, 10))

        textbox = ctk.CTkTextbox(dialog, font=ctk.CTkFont(family="Consolas", size=11),
                                 fg_color="#0f0f14", text_color="#e4e4e7",
                                 corner_radius=10, border_width=1, border_color="#27272a")
        textbox.pack(fill="both", expand=True, padx=12, pady=6)
        textbox.insert("1.0", report_text)
        textbox.configure(state="disabled")

        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(fill="x", padx=12, pady=(4, 12))

        ctk.CTkButton(btn_frame, text="닫기", command=dialog.destroy,
                      fg_color="#3f3f46", hover_color="#52525b",
                      font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                      width=100).pack(side="right")

    # ====================================================================
    # 설정 & 입력 검증
    # ====================================================================

    def on_plan_change(self, choice=None):
        if choice is None:
            choice = self.plan_combo.get()
        engine = self.engine_combo.get()
        if "Gemini" in engine:
            gemini_plans = [
                "무료 계정 (안전대기 / 10 RPM 속도제한)",
                "유료 계정 (초고속·고품질 번역 / 제한없음)",
            ]
            if choice in gemini_plans:
                self._last_gemini_plan = choice
            current_raw = normalize_model_name(self.model_combo.get())
            if choice == "유료 계정 (초고속·고품질 번역 / 제한없음)":
                self.model_combo.configure(values=MODELS_GEMINI_PAID)
                matched = next((m for m in MODELS_GEMINI_PAID if normalize_model_name(m) == current_raw), None)
                self.model_combo.set(matched or MODELS_GEMINI_PAID[0])
            else:
                self.model_combo.configure(values=MODELS_GEMINI_FREE)
                matched = next((m for m in MODELS_GEMINI_FREE if normalize_model_name(m) == current_raw), None)
                self.model_combo.set(matched or MODELS_GEMINI_FREE[0])

    def on_engine_change(self, choice):
        if "Custom API" in choice or "Local AI" in choice:
            self.standard_api_frame.pack_forget()
            self.local_api_frame.pack(fill="x", before=self.btn_translate_selected_modpack)
            self.log("💡 커스텀 호환 API 주소와 모델명(필요시 API 키 포함)을 입력해주세요. '🔍 모델 감지' 버튼으로 실행 중인 모델을 자동 검색할 수 있습니다.")
            return

        self.local_api_frame.pack_forget()
        self.standard_api_frame.pack(fill="x", before=self.btn_translate_selected_modpack)

        if "테스트 모드" in choice or "Mock" in choice:
            self.api_entry.configure(state="disabled")
            self.show_btn.configure(state="disabled")
            self.plan_combo.configure(state="disabled")
            self.model_combo.configure(state="disabled")
            self.log("🧪 테스트 모드입니다. API 키가 필요 없습니다.")
        else:
            self.api_entry.configure(state="normal")
            self.show_btn.configure(state="normal")
            gemini_plans = [
                "무료 계정 (안전대기 / 10 RPM 속도제한)",
                "유료 계정 (초고속·고품질 번역 / 제한없음)",
            ]
            current_plan = self.plan_combo.get()
            if current_plan in gemini_plans:
                self._last_gemini_plan = current_plan

            current_raw = normalize_model_name(self.model_combo.get())
            if "Gemini" in choice:
                self.plan_combo.configure(state="normal", values=gemini_plans)
                if self.plan_combo.get() not in gemini_plans:
                    restored_plan = getattr(self, "_last_gemini_plan", gemini_plans[0])
                    if restored_plan not in gemini_plans:
                        restored_plan = gemini_plans[0]
                    self.plan_combo.set(restored_plan)
                self.model_combo.configure(state="normal")
                self.on_plan_change(self.plan_combo.get())
                self.log("💡 Google AI (Gemini) API 키를 입력하고 계정 상태(무료/유료)를 선택해주세요.")
            elif "OpenAI" in choice:
                self.plan_combo.set("유료 전용 (선불 크레딧 충전 필수)")
                self.plan_combo.configure(state="disabled")
                self.model_combo.configure(state="normal", values=MODELS_OPENAI)
                matched = next((m for m in MODELS_OPENAI if normalize_model_name(m) == current_raw), None)
                self.model_combo.set(matched or MODELS_OPENAI[0])
                self.log("💡 OpenAI (ChatGPT) API 키와 모델을 선택해주세요. (선불 충전 계정 필요)")
            elif "Claude" in choice:
                self.plan_combo.set("유료 전용 (선불 크레딧 충전 필수)")
                self.plan_combo.configure(state="disabled")
                self.model_combo.configure(state="normal", values=MODELS_CLAUDE)
                matched = next((m for m in MODELS_CLAUDE if normalize_model_name(m) == current_raw), None)
                self.model_combo.set(matched or MODELS_CLAUDE[0])
                self.log("💡 Anthropic (Claude) API 키와 모델을 선택해주세요. (선불 충전 계정 필요)")
            elif "DeepSeek" in choice:
                self.plan_combo.set("유료 전용 (선불 크레딧 충전 필수)")
                self.plan_combo.configure(state="disabled")
                self.model_combo.configure(state="normal", values=MODELS_DEEPSEEK)
                matched = next((m for m in MODELS_DEEPSEEK if normalize_model_name(m) == current_raw), None)
                self.model_combo.set(matched or MODELS_DEEPSEEK[0])
                self.log("💡 DeepSeek (딥시크) API 키와 모델을 선택해주세요. (선불 충전 계정 필요)")

    def on_target_lang_change(self, choice):
        if hasattr(self, "_sync_glossary_to_current_lang"):
            self._sync_glossary_to_current_lang()
        self.log(f"🌍 타겟 언어가 '{choice}'(으)로 변경되었습니다. (해당 언어 용어집 적용)")

    def open_glossary_editor(self):
        editor = ctk.CTkToplevel(self)
        editor.title("용어집 편집 (Glossary)")
        editor.geometry("460x550")
        editor.minsize(400, 450)
        editor.grab_set()

        ctk.CTkLabel(editor, text="고정할 단어를 '원문=번역문' 형태로 한 줄씩 입력하세요.\n예: Creeper=크리퍼", 
                     font=ctk.CTkFont(family=FONT_NAME, size=12), text_color="#d4d4d8", justify="left"
                     ).pack(padx=12, pady=(12, 4), anchor="w")

        count_label = ctk.CTkLabel(editor, text="현재 등록된 단어: 0개", font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"), text_color="#fb923c")
        count_label.pack(padx=12, pady=(0, 4), anchor="w")

        textbox = ctk.CTkTextbox(editor, font=ctk.CTkFont(family=FONT_NAME, size=12))
        textbox.pack(fill="both", expand=True, padx=12, pady=8)

        def update_count(*args):
            content = textbox.get("1.0", "end").strip()
            count = len([line for line in content.split('\n') if line.strip() and '=' in line])
            count_label.configure(text=f"현재 등록된 단어: {count}개")
            
        textbox.bind("<KeyRelease>", update_count)

        # Load existing
        current_text = ""
        for k, v in getattr(self.app_state, 'glossary', {}).items():
            current_text += f"{k}={v}\n"
        textbox.insert("1.0", current_text)
        update_count()

        def save_and_close():
            content = textbox.get("1.0", "end").strip()
            new_glossary = {}
            for line in content.split('\n'):
                line = line.strip()
                if not line or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                new_glossary[k.strip()] = v.strip()
            self.app_state.glossary = new_glossary
            if hasattr(self.app_state, "glossaries_by_lang"):
                lang = self.target_lang_combo.get()
                self.app_state.glossaries_by_lang[lang] = new_glossary
            self.save_user_settings()
            self.log(f"✅ 용어집 저장 완료! (총 {len(self.app_state.glossary)}개 단어)")
            editor.destroy()

        def import_txt():
            from tkinter import filedialog
            path = filedialog.askopenfilename(title="용어집 텍스트 파일 불러오기", filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")])
            if not path: return
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    content = f.read()
                # Append to current textbox text
                textbox.insert("end", "\n" + content.strip())
                self.log(f"📥 용어집 파일을 불러왔습니다: {path}")
            except Exception as e:
                if messagebox: messagebox.showerror("오류", f"파일을 읽는 중 오류가 발생했습니다: {e}")

        def export_txt():
            from tkinter import filedialog
            path = filedialog.asksaveasfilename(title="용어집 텍스트 파일 내보내기", defaultextension=".txt", filetypes=[("Text Files", "*.txt")])
            if not path: return
            try:
                content = textbox.get("1.0", "end").strip()
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(content)
                self.log(f"📤 용어집을 텍스트 파일로 저장했습니다: {path}")
            except Exception as e:
                if messagebox: messagebox.showerror("오류", f"파일을 저장하는 중 오류가 발생했습니다: {e}")

        btn_frame = ctk.CTkFrame(editor, fg_color="transparent")
        btn_frame.pack(fill="x", padx=12, pady=(0, 8))

        btn_import = ctk.CTkButton(btn_frame, text="📥 텍스트 파일 불러오기", command=import_txt,
                                   fg_color="#3f3f46", hover_color="#52525b", font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"), width=150)
        btn_import.pack(side="left", padx=(0, 4))

        btn_export = ctk.CTkButton(btn_frame, text="📤 텍스트 파일 내보내기", command=export_txt,
                                   fg_color="#3f3f46", hover_color="#52525b", font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"), width=150)
        btn_export.pack(side="right", padx=(4, 0))

        btn_save = ctk.CTkButton(editor, text="저장 및 닫기", command=save_and_close,
                                 fg_color="#16a34a", hover_color="#15803d", font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"))
        btn_save.pack(fill="x", padx=12, pady=(0, 12))

    def toggle_api_visibility(self):
        if self.api_entry.cget("show") == "*":
            self.api_entry.configure(show="")
            self.show_btn.configure(text="숨기기")
        else:
            self.api_entry.configure(show="*")
            self.show_btn.configure(text="보기")

    def toggle_buttons(self, state):
        def _toggle():
            btn_state = "normal" if state else "disabled"
            cancel_state = "disabled" if state else "normal"

            for attr in (
                "btn_single", "btn_zip", "btn_pick_instance_root", "btn_auto_detect_root",
                "btn_rescan_modpacks", "btn_open_translate_options",
                "btn_translate_selected_modpack", "btn_translate_all_guidebooks",
                "chk_auto_apply_rp", "btn_edit_glossary",
                "btn_back_to_select", "btn_back_from_select", "btn_back_from_quick"
            ):
                if hasattr(self, attr):
                    getattr(self, attr).configure(state=btn_state)

            if hasattr(self, "btn_go_translate"):
                if state and getattr(self, "selected_modpack_path", None) is None:
                    self.btn_go_translate.configure(state="disabled")
                else:
                    self.btn_go_translate.configure(state=btn_state)

            if hasattr(self, "btn_cancel"):
                self.btn_cancel.configure(state=cancel_state)

            if state and hasattr(self, "_schedule_feedback_poll"):
                self._schedule_feedback_poll(1000)
        
        self.after(0, _toggle)

    def validate_inputs(self):
        engine_name = self.engine_combo.get()
        engine_key = ENGINES.get(engine_name)

        self.save_user_settings()

        # Mock Mode 자동 활성화/비활성화
        import translation_engines
        translation_engines.MOCK_MODE = (engine_key == "mock")
        if engine_key == "mock":
            self.log("🧪 [테스트 모드] API 호출 없이 모의 번역을 수행합니다.")
            target_lang = self.target_lang_combo.get()
            return "gemini_batch", "MOCK_KEY", False, None, target_lang, None

        if engine_key == "local_ai":
            local_url = self.local_url_entry.get().strip()
            local_model = self.local_model_entry.get().strip()
            local_api_key = self.local_api_key_entry.get().strip()
            if not local_url or not local_model:
                messagebox.showwarning("경고", "커스텀 API 주소와 모델명을 모두 입력해주세요.")
                return None, None, False, None, None, None
            target_lang = self.target_lang_combo.get()
            return engine_key, local_api_key, False, local_model, target_lang, local_url

        api_key = self.api_entry.get().strip()

        if not api_key:
            messagebox.showwarning("경고", f"{engine_name} 사용을 위해 API 키를 입력해주세요.")
            return None, None, False, None, None, None

        if engine_key == "gemini_batch":
            gemini_paid_label = "유료 계정 (초고속·고품질 번역 / 제한없음)"
            gemini_free_label = "무료 계정 (안전대기 / 10 RPM 속도제한)"
            current_plan = self.plan_combo.get()
            if current_plan not in (gemini_free_label, gemini_paid_label):
                self.plan_combo.set(gemini_free_label)
                current_plan = gemini_free_label
            is_paid = (current_plan == gemini_paid_label)
        else:
            is_paid = True
        ai_model = normalize_model_name(self.model_combo.get())
        target_lang = self.target_lang_combo.get()
        return engine_key, api_key, is_paid, ai_model, target_lang, None

    def request_cancel(self):
        self.app_state.cancel_requested = True
        self.log("🛑 사용자가 번역 취소를 요청했습니다. 지금까지 번역된 내용을 로컬 캐시에 저장 중...")
        self.btn_cancel.configure(state="disabled")

        def _bg_save():
            try:
                translation_memory.save_memory(flush_now=False)
                self.log("💾 [로컬 캐시 보존] 지금까지 번역된 내용이 로컬 캐시에 안전하게 영구 저장되었습니다!")
            except Exception:
                pass
        threading.Thread(target=_bg_save, daemon=True).start()

    def is_cancelled(self):
        return self.app_state.cancel_requested

    def open_memory_editor(self):
        from memory_editor_window import MemoryEditorWindow
        return MemoryEditorWindow(self)

    def open_ai_model_info_modal(self):
        """AI 모델별 무료/유료 구분, 회사별 스펙, 1,000개 항목당 예상 비용 및 RPD 가이드 모달"""
        info_win = ctk.CTkToplevel(self)
        info_win.title("ℹ️ AI 모델 정보 (무료 vs 유료 · 회사별 요금 · 1,000개 항목당 예상 비용)")
        info_win.geometry("820x720")
        info_win.minsize(700, 540)
        info_win.transient(self)
        info_win.grab_set()

        info_win.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - 820) // 2
        y = self.winfo_y() + (self.winfo_height() - 720) // 2
        info_win.geometry(f"+{x}+{y}")

        # 상단 핵심 안내 배너
        banner_frame = ctk.CTkFrame(
            info_win, fg_color="#1e293b", border_color="#38bdf8", border_width=2, corner_radius=10
        )
        banner_frame.pack(fill="x", padx=16, pady=(14, 8))

        ctk.CTkLabel(
            banner_frame,
            text=(
                "💡 [핵심 요약: 무료 vs 유료 API]\n"
                "• 카드 등록이나 선결제 없이 쓸 수 있는 진정한 무료 AI API는 사실상 Google Gemini뿐입니다!\n"
                "• OpenAI(GPT), DeepSeek, Claude는 모두 최소 $2~$5 이상의 선불 크레딧 충전(유료 결제)이 필수입니다.\n"
                "• 아래 '신규 항목 1,000개당 예상 비용'은 진행 로그의 번역 개수(예: /4,847) 기준, 단어+문장 혼합 평균 길이(입력 2.5만~4만 / 출력 3.5만~5.5만 토큰) 추정치입니다."
            ),
            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
            text_color="#e0f2fe", justify="left"
        ).pack(padx=14, pady=10, anchor="w")

        scroll = ctk.CTkScrollableFrame(info_win, fg_color="#0f0f14", corner_radius=10)
        scroll.pack(fill="both", expand=True, padx=16, pady=6)

        def create_accordion(parent, title_str, body_str, default_open=False, active_color="#1d4ed8"):
            box_wrapper = ctk.CTkFrame(parent, fg_color="#18181b", border_color="#27272a", border_width=1, corner_radius=8)
            box_wrapper.pack(fill="x", padx=8, pady=4)

            content_frame = ctk.CTkFrame(box_wrapper, fg_color="#111115", corner_radius=6)
            lbl_body = ctk.CTkLabel(
                content_frame, text=body_str,
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                text_color="#d4d4d8", justify="left", wraplength=720
            )
            lbl_body.pack(padx=14, pady=12, anchor="w")

            state = {"open": False}
            btn = ctk.CTkButton(
                box_wrapper, text=f"▶  {title_str}",
                font=ctk.CTkFont(family=FONT_NAME, size=13, weight="bold"),
                fg_color="#27272a", hover_color="#3f3f46", text_color="#f4f4f5",
                anchor="w", height=36, corner_radius=6
            )

            def _toggle():
                if state["open"]:
                    cf_hide = content_frame
                    cf_hide.pack_forget()
                    btn.configure(text=f"▶  {title_str}", fg_color="#27272a")
                    state["open"] = False
                else:
                    content_frame.pack(fill="x", padx=8, pady=(0, 8))
                    btn.configure(text=f"▼  {title_str}", fg_color=active_color)
                    state["open"] = True

            btn.configure(command=_toggle)
            btn.pack(fill="x", padx=6, pady=5)
            if default_open:
                _toggle()

        # ====================================================================
        # [요약] 한눈에 보는 목적별 추천 모델 (가성비 / 성능 / 고품질)
        # ====================================================================
        quick_pick_body = (
            "  💰 [가성비 최강 — 대형 모드팩 5,000~20,000개 대량 번역]\n"
            "     • 무료 1순위: gemini-3.5-flash-lite (0원 · 500 RPD / 15 RPM, 소진 시 3.1-flash-lite 백업)\n"
            "     • 유료 1순위: claude-haiku-5-5 / gpt-6-luna (1,000개당 30~45원 · 초저가 고속)\n"
            "     • 유료 최저가(비피크): deepseek-flash (1,000개당 30~55원, 저녁~오전/주말 15~28원)\n\n"
            "  ⚡ [성능 · 밸런스 — 품질과 비용의 균형]\n"
            "     • 저비용 고성능: deepseek-v4-pro (1,000개당 120~190원)\n"
            "     • Gemini 유료 주력: gemini-3.8-flash (1,000개당 210~330원)\n"
            "     • 중급 올라운더: gpt-6.1-terra (1,000개당 280~440원) / gemini-4-hydrogen (무료 5 RPM · 유료 420~660원)\n\n"
            "  👑 [고품질 정밀 — 스토리 퀘스트 · 가이드북 · 오역 AI 재번역]\n"
            "     • 스토리/어투 1순위: claude-sonnet-5-5 (1,000개당 530~840원)\n"
            "     • 서식/지시 정밀 1순위: gpt-6.1-sol (1,000개당 560~880원)\n"
            "     • Gemini 최상위: gemini-4-argon (1,000개당 프로모션 560~880원 / 12월 정가 1,120~1,760원)\n\n"
            "  💡 추천 조합: 대량 번역은 무료 flash-lite 또는 유료 haiku-5-5/gpt-6-luna로 빠르게 밀고,\n"
            "     어색한 핵심 문장만 오역 수정기 'AI 재번역'에서 sonnet-5-5 / gpt-6.1-sol로 다듬는 것이 가장 효율적입니다."
        )
        create_accordion(scroll, "⭐ 한눈에 보는 목적별 추천 모델 (가성비 · 성능 · 고품질)", quick_pick_body, default_open=True, active_color="#b45309")

        # ====================================================================
        # [대분류 1] 무료 AI API
        # ====================================================================
        ctk.CTkLabel(
            scroll, text="🆓 1. 무료 AI API (선결제/카드 등록 불필요 — Google Gemini 독점)",
            font=ctk.CTkFont(family=FONT_NAME, size=14, weight="bold"),
            text_color="#4ade80"
        ).pack(anchor="w", padx=10, pady=(10, 4))

        free_gemini_body = (
            "• Google AI Studio에서 API 키만 발급받으면 결제 수단 등록 없이 즉시 무료로 작동합니다.\n"
            "• 무료 할당량(RPD)은 'API 키' 단위가 아닌 'Google Cloud 프로젝트' 단위로 적용되며 매일 태평양 자정(한국 오후 4~5시)에 초기화됩니다.\n\n"
            "  1) gemini-3.5-flash-lite (★무료 대량 번역 1순위 추천 / 기본값)\n"
            "     - 일일 무료 한도: 500 RPD | 분당 15 RPM (자동 10~11 RPM 속도 조절) | 분당 250,000 TPM\n"
            "     - 예상 비용: 0원 (완전 무료) | 210점 배치 기준 하루 최대 약 3만~10만 항목 무료 번역 가능\n\n"
            "  2) gemini-3.1-flash-lite\n"
            "     - 일일 무료 한도: 500 RPD | 분당 15 RPM (자동 10~11 RPM 속도 조절) | 분당 250,000 TPM\n"
            "     - 예상 비용: 0원 | 3.5-flash-lite 일일 500 RPD 소진 시 스위칭하기 좋은 백업 모델\n\n"
            "  3) gemini-3.8-flash (⚠️ 무료 사용 시 주의)\n"
            "     - 일일 무료 한도: 하루 20 RPD / 분당 5 RPM 제한 (자동 4.5 RPM 이하 보호)\n"
            "     - 예상 비용: 0원 | 성능은 최상급이나 무료 계정은 소량 번역에만 사용하세요.\n\n"
            "  4) gemini-4-hydrogen (최신 Gemini 4 세대)\n"
            "     - 무료 분당 5 RPM 제한 (자동 4.5 RPM 이하 보호)\n"
            "     - 예상 비용: 0원 (무료 티어) | 최신 세대 고성능 모델"
        )
        create_accordion(scroll, "🟢 Google AI (Gemini) 무료 티어 (모델별 일일 RPD / RPM 한도)", free_gemini_body, default_open=True, active_color="#15803d")

        # ====================================================================
        # [대분류 2] 유료 AI API (회사별 구분 & 신규 항목 1,000개당 예상 비용)
        # ====================================================================
        ctk.CTkLabel(
            scroll, text="💳 2. 유료 AI API (선불 충전/결제 등록 필수 — 회사별 요금 & 신규 1,000개 항목당 예상 비용)",
            font=ctk.CTkFont(family=FONT_NAME, size=14, weight="bold"),
            text_color="#38bdf8"
        ).pack(anchor="w", padx=10, pady=(14, 4))

        paid_companies = [
            (
                "🏢 [Google AI (Gemini) 유료] (결제 카드 연동 후 사용한 만큼 후불 결제)",
                "• 결제 수단 연동 즉시 무료 RPD/RPM 제한이 해제되며, 150점 가중치 병렬 배치(10스레드) + 커넥션 풀 재사용으로 초고속 번역이 가능합니다.\n\n"
                "  • gemini-3.1-flash-lite : 입력 $0.20 / 출력 $1.50 (1M 토큰당)\n"
                "    👉 신규 1,000개 항목당 예상 비용: 약 $0.06 ~ $0.09 (약 80원 ~ 130원)\n\n"
                "  • gemini-3.5-flash-lite : 입력 $0.30 / 출력 $2.50 (1M 토큰당)\n"
                "    👉 신규 1,000개 항목당 예상 비용: 약 $0.10 ~ $0.15 (약 135원 ~ 210원)\n\n"
                "  • gemini-3.6-flash : 입력 ~$0.50 / 출력 ~$3.00 (1M 토큰당)\n"
                "    👉 신규 1,000개 항목당 예상 비용: 약 $0.12 ~ $0.19 (약 165원 ~ 260원)\n\n"
                "  • gemini-3.8-flash (★유료 주력 워크호스) : 입력 $0.75 / 출력 $3.75 (1M 토큰당)\n"
                "    👉 신규 1,000개 항목당 예상 비용: 약 $0.15 ~ $0.24 (약 210원 ~ 330원)\n\n"
                "  • gemini-4-hydrogen (최신 Gemini 4 세대 고성능) : 입력 $1.50 (캐시 $0.075) / 출력 $7.50 (1M 토큰당)\n"
                "    👉 신규 1,000개 항목당 예상 비용: 약 $0.30 ~ $0.47 (약 420원 ~ 660원)\n\n"
                "  • gemini-4-argon (★유료 전용 최상위 플래그십) : 입력 $2.00 / 출력 $10.00 (12/1 정가 $4.00 / $20.00)\n"
                "    👉 신규 1,000개 항목당 예상 비용: 약 $0.40 ~ $0.63 (약 560원 ~ 880원 / 정가 시 1,120원 ~ 1,760원)"
            ),
            (
                "🏢 [OpenAI (ChatGPT)] (최소 $5 선불 크레딧 충전 필수)",
                "• OpenAI는 $0 미결제 상태에서는 3 RPM 제한으로 번역이 불가능하며, 최소 $5 선불 충전(Tier 1: 500 RPM)이 필수입니다.\n"
                "• ID 매핑 JSON 배치(최대 150점) + 10스레드 병렬 처리가 적용되어 기존 단건 호출 대비 입력 토큰 비용이 크게 절감됩니다.\n\n"
                "  • gpt-6-luna (👑유료 가성비 전체 1위 / 기본값) : 입력 $0.10 (캐시 $0.01) / 출력 $0.50\n"
                "    👉 신규 1,000개 항목당 예상 비용: 약 $0.02 ~ $0.03 (약 30원 ~ 45원) — 가장 저렴하고 빠른 최신 모델!\n\n"
                "  • gpt-4o-mini (구버전 호환용) : 입력 $0.15 / 출력 $0.60\n"
                "    👉 신규 1,000개 항목당 예상 비용: 약 $0.025 ~ $0.04 (약 35원 ~ 55원)\n\n"
                "  • gpt-4.1-mini : 입력 $0.40 / 출력 $1.60\n"
                "    👉 신규 1,000개 항목당 예상 비용: 약 $0.07 ~ $0.10 (약 95원 ~ 145원)\n\n"
                "  • gpt-5-mini : 입력 $0.25 / 출력 $2.00\n"
                "    👉 신규 1,000개 항목당 예상 비용: 약 $0.08 ~ $0.12 (약 110원 ~ 170원)\n\n"
                "  • gpt-6.1-terra (중급 밸런스) : 입력 $1.00 / 출력 $5.00\n"
                "    👉 신규 1,000개 항목당 예상 비용: 약 $0.20 ~ $0.32 (약 280원 ~ 440원)\n\n"
                "  • gpt-4.1 : 입력 $2.00 / 출력 $8.00\n"
                "    👉 신규 1,000개 항목당 예상 비용: 약 $0.33 ~ $0.52 (약 460원 ~ 730원)\n\n"
                "  • gpt-5 : 입력 $1.25 / 출력 $10.00\n"
                "    👉 신규 1,000개 항목당 예상 비용: 약 $0.38 ~ $0.60 (약 530원 ~ 840원)\n\n"
                "  • gpt-6.1-sol (★고성능 정밀 번역) : 입력 $2.00 / 출력 $10.00\n"
                "    👉 신규 1,000개 항목당 예상 비용: 약 $0.40 ~ $0.63 (약 560원 ~ 880원)\n\n"
                "  • gpt-6-astra (최상위 초고가 플래그십) : 입력 $10.00 / 출력 $50.00\n"
                "    👉 신규 1,000개 항목당 예상 비용: 약 $2.00 ~ $3.15 (약 2,800원 ~ 4,410원)"
            ),
            (
                "🏢 [Anthropic (Claude)] (최소 $5 선불 크레딧 충전 필수)",
                "• 번역 엔진에서 'Anthropic (Claude)' 선택 후 공식 API 키만 입력하면 바로 작동합니다.\n"
                "• 한국어 어투와 판타지/스토리 문맥이 가장 매끄러우며, Prompt Caching + 150점 배치 최적화로 반복 입력 비용이 절감됩니다.\n\n"
                "  • claude-haiku-5-5 (👑한국어 자연스러움 가성비 1위 / 기본값) : 입력 $0.10 (캐시 $0.01) / 출력 $0.50\n"
                "    👉 신규 1,000개 항목당 예상 비용: 약 $0.02 ~ $0.03 (약 30원 ~ 45원)\n\n"
                "  • claude-sonnet-5-5 (★고품질 번역의 정석) : 입력 $2.00 (캐시 $0.10) / 출력 $10.00\n"
                "    👉 신규 1,000개 항목당 예상 비용: 약 $0.38 ~ $0.60 (약 530원 ~ 840원)\n\n"
                "  • claude-opus-5-5 : 입력 $4.00 / 출력 $20.00\n"
                "    👉 신규 1,000개 항목당 예상 비용: 약 $0.80 ~ $1.26 (약 1,120원 ~ 1,760원)\n\n"
                "  • claude-fable-5-2 : 입력 $10.00 / 출력 $50.00\n"
                "    👉 신규 1,000개 항목당 예상 비용: 약 $2.00 ~ $3.15 (약 2,800원 ~ 4,410원)"
            ),
            (
                "🏢 [DeepSeek (딥시크)] (최소 $2 선불 크레딧 충전 필수)",
                "• 번역 엔진에서 'DeepSeek (딥시크)' 선택 후 API 키만 입력하면 바로 작동합니다.\n"
                "• 150점 ID 매핑 배치 + 자동 Prefix Cache 할인 + 비피크 시간대(한국 시간 오후 7시 ~ 익일 오전 10시 및 주말) 50% 할인이 적용됩니다!\n\n"
                "  • deepseek-flash (V4.1, 👑초저가 가성비 추천 / 기본값) : 입력 $0.15 (캐시 $0.003) / 출력 $0.60\n"
                "    👉 신규 1,000개 항목당 예상 비용: 약 $0.02 ~ $0.04 (약 30원 ~ 55원, 비피크 할인 시 약 15원 ~ 28원!)\n\n"
                "  • deepseek-v4-pro (고성능 추론 모델) : 입력 $0.66 / 출력 $1.98\n"
                "    👉 신규 1,000개 항목당 예상 비용: 약 $0.09 ~ $0.14 (약 120원 ~ 190원)"
            ),
        ]

        for comp_title, comp_body in paid_companies:
            create_accordion(scroll, comp_title, comp_body, default_open=False, active_color="#1d4ed8")

        btn_close = ctk.CTkButton(
            info_win, text="닫기", width=120, height=34,
            font=ctk.CTkFont(family=FONT_NAME, size=13, weight="bold"),
            fg_color="#3f3f46", hover_color="#52525b",
            command=info_win.destroy
        )
        btn_close.pack(pady=(6, 14))
