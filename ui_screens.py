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
        import logging
        if message and not message.startswith("⏳"):
            logging.info(message)
            
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
            if "무료" in choice:
                self.model_combo.configure(values=MODELS_GEMINI_FREE)
                if self.model_combo.get() not in MODELS_GEMINI_FREE:
                    self.model_combo.set(MODELS_GEMINI_FREE[0])
            else:
                self.model_combo.configure(values=MODELS_GEMINI_PAID)
                if self.model_combo.get() not in MODELS_GEMINI_PAID:
                    self.model_combo.set(MODELS_GEMINI_PAID[0])

    def on_engine_change(self, choice):
        if "Custom API" in choice or "Local AI" in choice:
            self.standard_api_frame.pack_forget()
            self.local_api_frame.pack(fill="x", before=self.btn_translate_selected_modpack)
            self.log("💡 커스텀 호환 API 주소와 모델명(필요시 API 키 포함)을 입력해주세요.")
            return

        self.local_api_frame.pack_forget()
        self.standard_api_frame.pack(fill="x", before=self.btn_translate_selected_modpack)

        if choice == "Google Translate":
            self.api_entry.configure(state="disabled")
            self.show_btn.configure(state="disabled")
            self.plan_combo.configure(state="disabled")
            self.model_combo.configure(state="disabled")
            self.log("💡 Google Translate는 API 키 없이 무료 사용 가능합니다.")
        elif "테스트 모드" in choice or "Mock" in choice:
            self.api_entry.configure(state="disabled")
            self.show_btn.configure(state="disabled")
            self.plan_combo.configure(state="disabled")
            self.model_combo.configure(state="disabled")
            self.log("🧪 테스트 모드입니다. API 키가 필요 없습니다.")
        else:
            self.api_entry.configure(state="normal")
            self.show_btn.configure(state="normal")
            if "Gemini" in choice:
                self.plan_combo.configure(state="normal")
                if self.plan_combo.get() not in ["유료 API (기본)", "무료 API (분당 제한)"]:
                    self.plan_combo.set("유료 API (기본)")
                self.model_combo.configure(state="normal")
                self.on_plan_change(self.plan_combo.get())
                self.log("💡 Gemini API 키를 입력하고 계정 상태(유료/무료)를 지정해주세요.")
            elif "OpenAI" in choice:
                self.plan_combo.set("- (해당 없음)")
                self.plan_combo.configure(state="disabled")
                self.model_combo.configure(state="normal", values=MODELS_OPENAI)
                if self.model_combo.get() not in MODELS_OPENAI:
                    self.model_combo.set(MODELS_OPENAI[0])
                self.log("💡 OpenAI API 키를 입력해주세요. 요금제는 계정 설정에 따릅니다.")
            else:
                self.plan_combo.set("- (해당 없음)")
                self.plan_combo.configure(state="disabled")
                self.model_combo.set("- (해당 없음)")
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

        ctk.CTkLabel(editor, text="고정할 단어를 '원문=번역문' 형태로 한 줄씩 입력하세요.\n예: Creeper=크리퍼\n💡 팁: AI가 추가한 단어에는 # [Auto-Extracted]가 표시됩니다.", 
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
            if hasattr(self, "glossaries_by_lang"):
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

            for attr in ("btn_single", "btn_zip", "btn_pick_instance_root", "btn_auto_detect_root",
                         "btn_rescan_modpacks", "btn_open_translate_options", "btn_translate_selected_modpack",
                         "btn_back_to_select", "btn_back_from_quick"):
                if hasattr(self, attr):
                    getattr(self, attr).configure(state=btn_state)

            if hasattr(self, "btn_go_translate"):
                if state and getattr(self, "selected_modpack_path", None) is None:
                    self.btn_go_translate.configure(state="disabled")
                else:
                    self.btn_go_translate.configure(state=btn_state)

            if hasattr(self, "btn_cancel"):
                self.btn_cancel.configure(state=cancel_state)
        
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

        if engine_key != "google" and not api_key:
            messagebox.showwarning("경고", f"{engine_name} 사용을 위해 API 키를 입력해주세요.")
            return None, None, False, None, None, None

        is_paid = "유료" in self.plan_combo.get()
        ai_model = self.model_combo.get()
        target_lang = self.target_lang_combo.get()
        return engine_key, api_key, is_paid, ai_model, target_lang, None

    def request_cancel(self):
        self.app_state.cancel_requested = True
        self.log("🛑 사용자가 번역 취소를 요청했습니다. 작업을 중단합니다...")
        self.btn_cancel.configure(state="disabled")

    def is_cancelled(self):
        return self.app_state.cancel_requested

    def open_memory_editor(self):
        editor = ctk.CTkToplevel(self)
        editor.title("🔍 인게임 오역 수정기 (Memory Editor)")
        editor.geometry("900x700")
        editor.grab_set()

        # 상단 검색 바
        search_frame = ctk.CTkFrame(editor)
        search_frame.pack(fill="x", padx=10, pady=10)

        query_var = ctk.StringVar()
        entry_search = ctk.CTkEntry(search_frame, textvariable=query_var, placeholder_text="검색어 (영어 원문 또는 한글 번역문 입력...)", width=400)
        entry_search.pack(side="left", padx=5)

        cat_var = ctk.StringVar(value="all")
        cat_combo = ctk.CTkComboBox(search_frame, variable=cat_var, values=["all", "items", "general", "books"], width=100)
        cat_combo.pack(side="left", padx=5)

        # 결과 리스트 
        list_frame = ctk.CTkScrollableFrame(editor, width=860, height=400)
        list_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        # 하단 수정 바
        edit_frame = ctk.CTkFrame(editor)
        edit_frame.pack(fill="x", padx=10, pady=10)
        
        ctk.CTkLabel(edit_frame, text="원문:").grid(row=0, column=0, padx=5, pady=5, sticky="e")
        lbl_original = ctk.CTkLabel(edit_frame, text="", width=600, anchor="w", fg_color="gray20", corner_radius=6)
        lbl_original.grid(row=0, column=1, padx=5, pady=5, sticky="we")
        
        ctk.CTkLabel(edit_frame, text="번역:").grid(row=1, column=0, padx=5, pady=5, sticky="e")
        entry_translated = ctk.CTkEntry(edit_frame, width=600)
        entry_translated.grid(row=1, column=1, padx=5, pady=5, sticky="we")

        selected_item = {"category": None, "src": None, "tgt": None}
        
        def on_item_click(item):
            selected_item["category"] = item["category"]
            selected_item["src"] = item["src"]
            selected_item["tgt"] = item["tgt"]
            lbl_original.configure(text=item["src"])
            entry_translated.delete(0, 'end')
            entry_translated.insert(0, item["tgt"])

        def do_search(*args):
            for widget in list_frame.winfo_children():
                widget.destroy()
                
            q = query_var.get().strip()
            if len(q) < 2: return
            
            import translation_memory
            results = translation_memory.search_memory(q, category=cat_var.get(), limit=50)
            
            for i, r in enumerate(results):
                row = ctk.CTkFrame(list_frame)
                row.pack(fill="x", pady=2)
                
                cat_badge = ctk.CTkLabel(row, text=f"[{r['category']}]", width=60, text_color="cyan")
                cat_badge.pack(side="left", padx=5)
                
                text_label = ctk.CTkLabel(row, text=f"{r['src'][:50]}... ➡️ {r['tgt'][:50]}...", anchor="w")
                text_label.pack(side="left", fill="x", expand=True, padx=5)
                
                btn = ctk.CTkButton(row, text="선택", width=60, command=lambda item=r: on_item_click(item))
                btn.pack(side="right", padx=5)

        entry_search.bind("<Return>", do_search)
        btn_search = ctk.CTkButton(search_frame, text="검색", command=do_search, width=80)
        btn_search.pack(side="left", padx=5)

        def do_save():
            if not selected_item["src"]: return
            import translation_memory
            new_val = entry_translated.get().strip()
            if not new_val: return
            translation_memory.update_memory_entry(selected_item["category"], selected_item["src"], new_val)
            
            # 리소스팩 핫패치 (Hot-patch)
            if hasattr(self.app_state, 'modpack_dir') and self.app_state.modpack_dir:
                import os
                import mod_jar_extractor
                pack_zip = os.path.join(self.app_state.modpack_dir, "QuestTranslatorPro_Pack.zip")
                if os.path.exists(pack_zip):
                    try:
                        mod_jar_extractor.hotpatch_resource_pack(pack_zip, selected_item["tgt"], new_val)
                        self.log("✅ 리소스팩(QuestTranslatorPro_Pack.zip) 핫패치 완료! 게임 내에서 F3+T를 누르면 즉시 반영됩니다.")
                    except Exception as e:
                        self.log(f"⚠️ 리소스팩 핫패치 실패: {e}")
            
            selected_item["tgt"] = new_val # 업데이트
            self.show_messagebox("info", "성공", "수정사항이 저장되었습니다.")
            do_search()

        def do_delete():
            if not selected_item["src"]: return
            import translation_memory
            translation_memory.delete_memory_entry(selected_item["category"], selected_item["src"])
            self.show_messagebox("info", "성공", "항목이 삭제되었습니다.")
            do_search()

        btn_save = ctk.CTkButton(edit_frame, text="💾 저장 및 즉시 적용", command=do_save, fg_color="green")
        btn_save.grid(row=2, column=1, sticky="w", padx=5, pady=10)
        
        btn_delete = ctk.CTkButton(edit_frame, text="🗑️ 잘못된 번역 삭제", command=do_delete, fg_color="red")
        btn_delete.grid(row=2, column=1, sticky="e", padx=5, pady=10)
