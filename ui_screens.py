"""
UI 화면 전환, 레이아웃, 로그, 입력 검증 관련 메서드 믹스인.
QuestTranslatorApp이 이 클래스를 상속해서 사용합니다.
"""
import threading
import os
import time

try:
    import customtkinter as ctk
    import tkinter as tk
    from tkinter import messagebox
except Exception:
    ctk = None
    tk = None
    messagebox = None

from translation_engines import ENGINES
from constants import FONT_NAME, MODELS_GEMINI_FREE, MODELS_GEMINI_PAID, MODELS_OPENAI


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
        if saved_root and os.path.isdir(saved_root) and not self.scan_thread_active and not getattr(self, "_suppress_auto_scan", False):
            self.scan_modpacks_from_entry(show_screen=False)

    def show_quick_translate_screen(self):
        self.phase_label.configure(text="STEP 1-A/2 · 파일/ZIP 번역")
        self.home_screen.grid_remove()
        self.select_screen.grid_remove()
        self.translate_screen.grid_remove()
        self.quick_translate_screen.grid()
        self._apply_responsive_layout()

    def show_translate_screen(self):
        if not self.selected_modpack_path:
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
        bar.set(0.18)

        self.after(120, lambda: (self._update_startup_status("설정을 불러오는 중..."), bar.set(0.45)))
        self.after(300, lambda: (self._update_startup_status("인스턴스를 확인하는 중..."), bar.set(0.72)))
        self.after(560, lambda: (self._update_startup_status("모드팩을 정리하는 중..."), bar.set(0.9)))
        self.after(820, self._finish_startup_loading)

    def _update_startup_status(self, message):
        if hasattr(self, "startup_status_label") and self.startup_status_label is not None:
            self.startup_status_label.configure(text=message)

    def _finish_startup_loading(self):
        self._fade_out_startup_overlay()

    def _fade_out_startup_overlay(self, step=0):
        if step >= 4:
            if hasattr(self, "startup_overlay") and self.startup_overlay is not None:
                self.startup_overlay.destroy()
                self.startup_overlay = None
            self.attributes("-alpha", 1.0)
            self.load_user_settings()
            self.show_home_screen()
            self.log("[안내] 원하는 작업을 선택한 뒤 이어서 진행하세요.")
            self._apply_responsive_layout()
            return

        alpha = 1.0 - (step + 1) * 0.06
        self.attributes("-alpha", max(0.2, alpha))
        self.after(40, lambda: self._fade_out_startup_overlay(step + 1))

    # ====================================================================
    # 반응형 레이아웃
    # ====================================================================

    def _on_window_resize(self, _event=None):
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(100, self._apply_responsive_layout)

    def _apply_responsive_layout(self):
        width = max(self.winfo_width(), 1)
        self._arrange_path_buttons(width)
        self._arrange_quick_buttons(width)

        card_columns = self._get_card_columns()
        if card_columns != self._last_card_columns:
            self._last_card_columns = card_columns
            if self.modpack_entries:
                self.render_modpack_cards(self.modpack_entries)

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
        func = {"info": messagebox.showinfo, "warning": messagebox.showwarning, "error": messagebox.showerror}[kind]
        if threading.current_thread() is not threading.main_thread():
            self.after(0, lambda: func(title, message))
        else:
            func(title, message)

    # ====================================================================
    # 설정 & 입력 검증
    # ====================================================================

    def on_plan_change(self, choice=None):
        if choice is None:
            choice = self.plan_combo.get()
        engine = self.engine_combo.get()
        if "Gemini" in engine:
            if "무료" in choice:
                self.model_combo.configure(values=MODELS_GEMINI_FREE)
                if self.model_combo.get() not in MODELS_GEMINI_FREE:
                    self.model_combo.set(MODELS_GEMINI_FREE[0])
            else:
                self.model_combo.configure(values=MODELS_GEMINI_PAID)
                if self.model_combo.get() not in MODELS_GEMINI_PAID:
                    self.model_combo.set(MODELS_GEMINI_PAID[0])

    def on_engine_change(self, choice):
        if choice == "Google Translate":
            self.api_entry.configure(state="disabled")
            self.show_btn.configure(state="disabled")
            self.plan_combo.configure(state="disabled")
            self.model_combo.configure(state="disabled")
            self.log("💡 Google Translate는 API 키 없이 무료 사용 가능합니다.")
        else:
            self.api_entry.configure(state="normal")
            self.show_btn.configure(state="normal")
            if "Gemini" in choice:
                self.plan_combo.configure(state="normal")
                self.model_combo.configure(state="normal")
                self.on_plan_change(self.plan_combo.get())
                self.log("💡 Gemini API 키를 입력하고 계정 상태(유료/무료)를 지정해주세요.")
            elif "OpenAI" in choice:
                self.plan_combo.configure(state="disabled")
                self.model_combo.configure(state="normal", values=MODELS_OPENAI)
                if self.model_combo.get() not in MODELS_OPENAI:
                    self.model_combo.set(MODELS_OPENAI[0])
                self.log("💡 OpenAI API 키를 입력해주세요. 요금제는 계정 설정에 따릅니다.")
            else:
                self.plan_combo.configure(state="disabled")
                self.model_combo.configure(state="disabled")
                self.log("💡 DeepL API 키를 입력해주세요.")

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

        textbox = ctk.CTkTextbox(editor, font=ctk.CTkFont(family=FONT_NAME, size=12))
        textbox.pack(fill="both", expand=True, padx=12, pady=8)

        # Load existing
        current_text = ""
        for k, v in getattr(self, 'glossary', {}).items():
            current_text += f"{k}={v}\n"
        textbox.insert("1.0", current_text)

        def save_and_close():
            content = textbox.get("1.0", "end").strip()
            new_glossary = {}
            for line in content.split('\n'):
                line = line.strip()
                if not line or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                new_glossary[k.strip()] = v.strip()
            self.glossary = new_glossary
            if hasattr(self, "glossaries_by_lang"):
                lang = self.target_lang_combo.get()
                self.glossaries_by_lang[lang] = new_glossary
            self.save_user_settings()
            self.log(f"✅ 용어집 저장 완료! (총 {len(self.glossary)}개 단어)")
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
        btn_state = "normal" if state else "disabled"
        cancel_state = "disabled" if state else "normal"

        for attr in ("btn_single", "btn_zip", "btn_pick_instance_root", "btn_auto_detect_root",
                     "btn_rescan_modpacks", "btn_open_translate_options", "btn_translate_selected_modpack",
                     "btn_back_to_select", "btn_back_from_quick"):
            if hasattr(self, attr):
                getattr(self, attr).configure(state=btn_state)

        if hasattr(self, "btn_go_translate"):
            if state and not self.selected_modpack_path:
                self.btn_go_translate.configure(state="disabled")
            else:
                self.btn_go_translate.configure(state=btn_state)

        if hasattr(self, "btn_cancel"):
            self.btn_cancel.configure(state=cancel_state)

    def validate_inputs(self):
        engine_name = self.engine_combo.get()
        engine_key = ENGINES.get(engine_name)
        api_key = self.api_entry.get().strip()

        if engine_key != "google" and not api_key:
            messagebox.showwarning("경고", f"{engine_name} 사용을 위해 API 키를 입력해주세요.")
            return None, None, False, None, None

        self.save_user_settings()
        is_paid = "유료" in self.plan_combo.get()
        ai_model = self.model_combo.get()
        target_lang = self.target_lang_combo.get()
        return engine_key, api_key, is_paid, ai_model, target_lang

    def request_cancel(self):
        self.cancel_requested = True
        self.log("🛑 사용자가 번역 취소를 요청했습니다. 작업을 중단합니다...")
        self.btn_cancel.configure(state="disabled")

    def is_cancelled(self):
        return self.cancel_requested
