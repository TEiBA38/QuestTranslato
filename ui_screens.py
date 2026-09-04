"""
UI 화면 전환, 레이아웃, 로그, 입력 검증 관련 메서드 믹스인.
QuestTranslatorApp이 이 클래스를 상속해서 사용합니다.
"""
import threading
import os
import time
import re
import translation_memory
import logging

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
        # 백그라운드 클라우드 마스터 메모리 초고속 사전 로드 시작
        threading.Thread(target=translation_memory.load_memory, daemon=True).start()

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

        if message and not message.startswith("⏳"):
            logging.info(message)
        if not hasattr(self, 'log_textbox') or self.log_textbox is None:
            return
        try:
            self.log_textbox.configure(state="normal")
            self.log_textbox.insert("end", message + "\n")
            self.log_textbox.see("end")
            self.log_textbox.configure(state="disabled")
            self.update_idletasks()
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

            for attr in (
                "btn_single", "btn_zip", "btn_pick_instance_root", "btn_auto_detect_root",
                "btn_rescan_modpacks", "btn_open_translate_options",
                "btn_translate_selected_modpack", "btn_translate_all_guidebooks", "btn_edit_glossary",
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
        self.log("🛑 사용자가 번역 취소를 요청했습니다. 지금까지 번역된 내용을 로컬 캐시에 저장 중...")
        try:
            translation_memory.save_memory()
            self.log("💾 [로컬 캐시 보존] 지금까지 번역된 내용이 로컬 캐시에 안전하게 영구 저장되었습니다!")
        except Exception as e:
            pass
        self.btn_cancel.configure(state="disabled")

    def is_cancelled(self):
        return self.app_state.cancel_requested

    def open_memory_editor(self):
        editor = ctk.CTkToplevel(self)
        editor.title("🔍 인게임 오역 수정기 (Memory Editor)")
        editor.geometry("960x780")
        editor.minsize(860, 680)
        editor.grab_set()

        # ----------------------------------------------------------------
        # 1. 상단 검색 및 데이터 추가 바
        # ----------------------------------------------------------------
        search_frame = ctk.CTkFrame(editor, fg_color="transparent")
        search_frame.pack(fill="x", padx=14, pady=(12, 6))

        query_var = ctk.StringVar()
        entry_search = ctk.CTkEntry(
            search_frame, textvariable=query_var,
            placeholder_text="검색어 (영어 원문 또는 한글 번역문 입력...)",
            font=ctk.CTkFont(family=FONT_NAME, size=12), width=270
        )
        entry_search.pack(side="left", padx=(0, 6))

        field_var = ctk.StringVar(value="통합 (원문+번역)")
        field_combo = ctk.CTkComboBox(
            search_frame, variable=field_var,
            values=["통합 (원문+번역)", "영어 원문(EN)만", "한글 번역(KO)만"],
            font=ctk.CTkFont(family=FONT_NAME, size=12), width=130
        )
        field_combo.pack(side="left", padx=(0, 6))

        cat_var = ctk.StringVar(value="all")
        cat_combo = ctk.CTkComboBox(
            search_frame, variable=cat_var,
            values=["all", "items", "general", "books"],
            font=ctk.CTkFont(family=FONT_NAME, size=12), width=85
        )
        cat_combo.pack(side="left", padx=(0, 6))

        btn_search = ctk.CTkButton(
            search_frame, text="🔍 검색", width=75,
            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
            command=lambda: do_search()
        )
        btn_search.pack(side="left", padx=(0, 10))

        # [➕ 새 번역 데이터 추가] 버튼
        btn_add_data = ctk.CTkButton(
            search_frame, text="➕ 새 번역 데이터 추가", width=160,
            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
            fg_color="#0284c7", hover_color="#0369a1",
            command=lambda: open_add_dialog()
        )
        btn_add_data.pack(side="right")

        # ----------------------------------------------------------------
        # 2. 일괄 작업 바 (선택 항목 치환 / 삭제)
        # ----------------------------------------------------------------
        batch_frame = ctk.CTkFrame(editor, fg_color="#18181d", corner_radius=10, border_width=1, border_color="#2a2a33")
        batch_frame.pack(fill="x", padx=14, pady=(0, 8))

        batch_top_row = ctk.CTkFrame(batch_frame, fg_color="transparent")
        batch_top_row.pack(fill="x", padx=10, pady=(8, 4))

        btn_select_all = ctk.CTkButton(
            batch_top_row, text="☑ 전체 선택", width=85, height=26,
            font=ctk.CTkFont(family=FONT_NAME, size=11),
            fg_color="#334155", hover_color="#475569",
            command=lambda: set_all_selection(True)
        )
        btn_select_all.pack(side="left", padx=(0, 6))

        btn_deselect_all = ctk.CTkButton(
            batch_top_row, text="선택 해제", width=75, height=26,
            font=ctk.CTkFont(family=FONT_NAME, size=11),
            fg_color="#27272a", hover_color="#3f3f46",
            command=lambda: set_all_selection(False)
        )
        btn_deselect_all.pack(side="left", padx=(0, 12))

        lbl_selected_count = ctk.CTkLabel(
            batch_top_row, text="선택: 0개 / 검색: 0개",
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
            text_color="#38bdf8"
        )
        lbl_selected_count.pack(side="left")

        batch_replace_row = ctk.CTkFrame(batch_frame, fg_color="transparent")
        batch_replace_row.pack(fill="x", padx=10, pady=(0, 8))

        ctk.CTkLabel(
            batch_replace_row, text="찾을 단어:",
            font=ctk.CTkFont(family=FONT_NAME, size=11), text_color="#cbd5e1"
        ).pack(side="left", padx=(0, 4))

        find_var = ctk.StringVar()
        entry_find = ctk.CTkEntry(
            batch_replace_row, textvariable=find_var, placeholder_text="예: 팃커스, 휭커스, 틴커즈, 커스 (다중 가능)",
            font=ctk.CTkFont(family=FONT_NAME, size=11), width=230
        )
        entry_find.pack(side="left", padx=(0, 8))

        ctk.CTkLabel(
            batch_replace_row, text="➡️ 바꿀 단어:",
            font=ctk.CTkFont(family=FONT_NAME, size=11), text_color="#cbd5e1"
        ).pack(side="left", padx=(0, 4))

        replace_var = ctk.StringVar()
        entry_replace = ctk.CTkEntry(
            batch_replace_row, textvariable=replace_var, placeholder_text="예: 팅커스 (0% 표준어)",
            font=ctk.CTkFont(family=FONT_NAME, size=11), width=160
        )
        entry_replace.pack(side="left", padx=(0, 10))

        btn_batch_replace = ctk.CTkButton(
            batch_replace_row, text="⚡ 선택 항목 일괄 치환", height=28,
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
            fg_color="#ea580c", hover_color="#c2410c",
            command=lambda: do_batch_replace()
        )
        btn_batch_replace.pack(side="left", padx=(0, 6))

        btn_batch_delete = ctk.CTkButton(
            batch_replace_row, text="🗑️ 선택 항목 일괄 삭제", height=28,
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
            fg_color="#b91c1c", hover_color="#991b1b",
            command=lambda: do_batch_delete()
        )
        btn_batch_delete.pack(side="right")

        # ----------------------------------------------------------------
        # 3. 결과 리스트 
        # ----------------------------------------------------------------
        list_frame = ctk.CTkScrollableFrame(editor, height=320, fg_color="#111115", corner_radius=10)
        list_frame.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        # ----------------------------------------------------------------
        # 4. 하단 개별 수정 바
        # ----------------------------------------------------------------
        edit_frame = ctk.CTkFrame(editor, fg_color="#18181d", corner_radius=10, border_width=1, border_color="#2a2a33")
        edit_frame.pack(fill="x", padx=14, pady=(0, 12))

        ctk.CTkLabel(
            edit_frame, text="원문:",
            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
            text_color="#cbd5e1"
        ).grid(row=0, column=0, padx=10, pady=(10, 4), sticky="e")

        lbl_original = ctk.CTkLabel(
            edit_frame, text="(목록에서 항목을 선택하세요)", anchor="w",
            font=ctk.CTkFont(family=FONT_NAME, size=12),
            fg_color="#111113", corner_radius=6, height=28
        )
        lbl_original.grid(row=0, column=1, padx=(0, 10), pady=(10, 4), sticky="we")

        ctk.CTkLabel(
            edit_frame, text="번역:",
            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
            text_color="#cbd5e1"
        ).grid(row=1, column=0, padx=10, pady=4, sticky="e")

        entry_translated = ctk.CTkEntry(
            edit_frame, font=ctk.CTkFont(family=FONT_NAME, size=12),
            fg_color="#111113", border_color="#52525b"
        )
        entry_translated.grid(row=1, column=1, padx=(0, 10), pady=4, sticky="we")
        edit_frame.grid_columnconfigure(1, weight=1)

        # 용어집 고정 등록 체크박스 (오역 재발 방지)
        chk_glossary_var = ctk.BooleanVar(value=True)
        chk_glossary = ctk.CTkCheckBox(
            edit_frame, text="📖 용어집(Glossary)에도 고정 등록하여 향후 AI 오역 재발 방지",
            variable=chk_glossary_var,
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
            text_color="#38bdf8",
            checkmark_color="#ffffff",
            fg_color="#0284c7"
        )
        chk_glossary.grid(row=2, column=1, padx=(0, 10), pady=(2, 6), sticky="w")

        action_btn_row = ctk.CTkFrame(edit_frame, fg_color="transparent")
        action_btn_row.grid(row=3, column=1, padx=(0, 10), pady=(4, 10), sticky="we")

        btn_save = ctk.CTkButton(
            action_btn_row, text="💾 저장 및 즉시 적용", height=32,
            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
            fg_color="#15803d", hover_color="#166534",
            command=lambda: do_save()
        )
        btn_save.pack(side="left", padx=(0, 8))

        btn_delete_single = ctk.CTkButton(
            action_btn_row, text="🗑️ 이 항목 삭제", height=32,
            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
            fg_color="#b91c1c", hover_color="#991b1b",
            command=lambda: do_delete_single()
        )
        btn_delete_single.pack(side="left")

        # ----------------------------------------------------------------
        # 로직 구현 함수들
        # ----------------------------------------------------------------
        selected_item = {"category": None, "src": None, "tgt": None}
        current_items = []
        check_vars = []

        def update_selected_count():
            cnt = sum(1 for v in check_vars if v.get())
            lbl_selected_count.configure(text=f"선택: {cnt}개 / 검색: {len(current_items)}개")

        def set_all_selection(select_all=True):
            for v in check_vars:
                v.set(select_all)
            update_selected_count()

        def on_item_click(item):
            selected_item["category"] = item["category"]
            selected_item["src"] = item["src"]
            selected_item["tgt"] = item["tgt"]
            lbl_original.configure(text=item["src"])
            entry_translated.delete(0, 'end')
            entry_translated.insert(0, item["tgt"])

        def apply_hotpatch(old_tgt, new_tgt):
            if hasattr(self.app_state, 'modpack_dir') and self.app_state.modpack_dir:
                import os, glob
                import mod_jar_extractor
                mp_dir = self.app_state.modpack_dir
                mp_name = os.path.basename(mp_dir.rstrip(os.sep))
                candidates = glob.glob(os.path.join(mp_dir, f"{mp_name}[*].zip"))
                candidates.extend(glob.glob(os.path.join(mp_dir, "resourcepacks", f"{mp_name}[*].zip")))
                legacy = os.path.join(mp_dir, "QuestTranslatorPro_Pack.zip")
                if os.path.exists(legacy):
                    candidates.append(legacy)

                for pz in set(candidates):
                    try:
                        mod_jar_extractor.hotpatch_resource_pack(pz, old_tgt, new_tgt)
                        self.log(f"✅ 리소스팩({os.path.basename(pz)}) 핫패치 완료! ({old_tgt} ➡️ {new_tgt})")
                    except Exception as e:
                        self.log(f"⚠️ 리소스팩 핫패치 실패 ({os.path.basename(pz)}): {e}")

        def register_glossary_rule(src, tgt):
            if not chk_glossary_var.get() or not src or not tgt:
                return
            clean_src = src.strip()
            clean_tgt = tgt.strip()
            if hasattr(self, 'app_state') and self.app_state:
                target_lang = self.target_lang_combo.get() if hasattr(self, 'target_lang_combo') else "한국어 (Korean)"
                self.app_state.glossary[clean_src] = clean_tgt
                if target_lang not in self.app_state.glossaries_by_lang:
                    self.app_state.glossaries_by_lang[target_lang] = {}
                self.app_state.glossaries_by_lang[target_lang][clean_src] = clean_tgt
                self.save_user_settings()
                self.log(f"📖 [용어집 등록] '{clean_src}' = '{clean_tgt}' 규칙 저장 (향후 AI 오역 재발 방지)")

        def do_search(*args):
            for widget in list_frame.winfo_children():
                widget.destroy()
            current_items.clear()
            check_vars.clear()

            q = query_var.get().strip()
            if len(q) < 2:
                update_selected_count()
                return

            f_val = field_var.get()
            s_field = "src" if "EN" in f_val else ("tgt" if "KO" in f_val else "all")
            results = translation_memory.search_memory(q, category=cat_var.get(), limit=300, search_field=s_field)

            for i, r in enumerate(results):
                current_items.append(r)
                var = ctk.BooleanVar(value=False)
                check_vars.append(var)

                row = ctk.CTkFrame(list_frame, fg_color="#18181d")
                row.pack(fill="x", pady=2, padx=4)

                chk = ctk.CTkCheckBox(row, text="", variable=var, width=24, command=update_selected_count)
                chk.pack(side="left", padx=(6, 2))

                cat_badge = ctk.CTkLabel(row, text=f"[{r['category']}]", width=60, text_color="#38bdf8")
                cat_badge.pack(side="left", padx=4)

                text_label = ctk.CTkLabel(row, text=f"{r['src'][:45]}... ➡️ {r['tgt'][:45]}...", anchor="w")
                text_label.pack(side="left", fill="x", expand=True, padx=4)

                btn = ctk.CTkButton(row, text="선택", width=55, height=26, command=lambda item=r: on_item_click(item))
                btn.pack(side="right", padx=6)

            update_selected_count()

        entry_search.bind("<Return>", do_search)

        def do_save():
            if not selected_item["src"]:
                return
            new_val = entry_translated.get().strip()
            if not new_val:
                return
            old_val = selected_item["tgt"]
            src_val = selected_item["src"]

            # 오역률 및 분탕 방지 검증 (Two-Pillar Validation)
            err_rate, val_rate, verdict, det = translation_memory.calculate_translation_error_rate(
                src_val, new_val, reference_hint=old_val
            )
            self.log(f"🔍 [오역률 검증] 원문 '{src_val}' ➡️ 수정 '{new_val}' | 오역률: {err_rate:.1f}% (연관도: {val_rate:.1f}%) | 판정: {verdict.upper()} ({det})")

            if verdict == "hard_block":
                # 기존에 이미 철, 아이언 등 명확한 번역 데이터가 존재하는 단어는 무관 단어로 변조 원천 차단 (우회 불가)
                messagebox.showerror(
                    "수정 불가 (기등록 데이터 오염 차단)",
                    f"'{src_val}'(은)는 이미 표준 번역 또는 기존 데이터가 확립된 단어입니다.\n\n"
                    f"• 분석 판정: 🚨 오역률 {err_rate:.1f}%\n"
                    f"• 상세 사유: {det}\n\n"
                    f"⚠️ 기존 정상 단어를 무관한 단어로 변조하는 행위는 캐시 및 클라우드 오염 방지를 위해 절대 허용되지 않습니다.",
                    parent=editor
                )
                return
            elif verdict == "block":
                # 캐시나 사전에 없던 '완전히 새로운 신규 단어'일 때만 확인 팝업 허용
                force_apply = messagebox.askyesno(
                    "신규 번역 등록 확인",
                    f"입력하신 번역문은 기존 데이터가 없는 신규 단어이며, 발음상 유사도가 낮습니다.\n\n"
                    f"• 분석 판정: ⚠️ 오역률 {err_rate:.1f}% (연관도: {val_rate:.1f}%)\n"
                    f"• 상세 사유: {det}\n\n"
                    f"완전히 새로운 고유 의역/번역으로 신규 등록하시겠습니까?",
                    parent=editor
                )
                if not force_apply:
                    return
            elif verdict == "warning":
                confirm = messagebox.askyesno(
                    "오역률 주의 확인",
                    f"입력하신 번역문의 오역률이 다소 높게 감지되었습니다.\n\n"
                    f"• 분석 판정: ⚠️ 오역률 {err_rate:.1f}% (연관도: {val_rate:.1f}%)\n"
                    f"• 상세 사유: {det}\n\n"
                    f"정말로 이 번역문으로 저장하시겠습니까?",
                    parent=editor
                )
                if not confirm:
                    return

            translation_memory.update_memory_entry(selected_item["category"], src_val, new_val)
            apply_hotpatch(old_val, new_val)
            register_glossary_rule(src_val, new_val)
            selected_item["tgt"] = new_val
            self.show_messagebox("info", "성공", f"수정사항이 저장되었습니다. (오역률: {err_rate:.1f}%)")
            do_search()

        def do_delete_single():
            if not selected_item["src"]:
                return
            translation_memory.delete_memory_entry(selected_item["category"], selected_item["src"])
            self.show_messagebox("info", "성공", "항목이 삭제되었습니다.")
            do_search()

        def do_batch_replace():
            selected_indices = [i for i, v in enumerate(check_vars) if v.get()]
            if not selected_indices:
                self.show_messagebox("warning", "경고", "치환할 항목을 하나 이상 선택(체크)해주세요.")
                return

            find_term = find_var.get().strip()
            repl_term = replace_var.get().strip()
            if not repl_term:
                self.show_messagebox("warning", "경고", "바꿀 단어를 입력해주세요.")
                return

            # 일괄 치환 전 핵심 단어 오역률 검증
            target_basis = query_var.get().strip() or find_term or "단어"
            err_rate, val_rate, verdict, det = translation_memory.calculate_translation_error_rate(
                target_basis, repl_term, reference_hint=find_term
            )
            self.log(f"🔍 [일괄 치환 오역률 검증] 기준 '{target_basis}' ➡️ 치환 '{repl_term}' | 오역률: {err_rate:.1f}% (연관도: {val_rate:.1f}%) | 판정: {verdict.upper()} ({det})")

            if verdict == "hard_block":
                messagebox.showerror(
                    "일괄 치환 불가 (기등록 데이터 오염 차단)",
                    f"기준 단어 '{target_basis}'(은)는 이미 확립된 데이터가 존재하는 단어입니다.\n\n"
                    f"• 분석 판정: 🚨 오역률 {err_rate:.1f}%\n"
                    f"• 상세 사유: {det}\n\n"
                    f"⚠️ 기존 정상 단어를 무관한 단어로 일괄 변조하는 행위는 절대 허용되지 않습니다.",
                    parent=editor
                )
                return
            elif verdict == "block":
                force_apply = messagebox.askyesno(
                    "신규 단어 일괄 치환 확인",
                    f"치환할 단어 '{repl_term}'(은)는 기존 데이터가 없는 신규 단어입니다.\n\n"
                    f"• 분석 판정: ⚠️ 오역률 {err_rate:.1f}%\n"
                    f"• 상세 사유: {det}\n\n"
                    f"신규 번역 단어로 일괄 치환하시겠습니까?",
                    parent=editor
                )
                if not force_apply:
                    return
            elif verdict == "warning":
                confirm = messagebox.askyesno(
                    "일괄 치환 주의 확인",
                    f"치환할 단어의 오역률이 다소 높게 감지되었습니다.\n\n"
                    f"• 분석 판정: ⚠️ 오역률 {err_rate:.1f}% (연관도: {val_rate:.1f}%)\n"
                    f"• 상세 사유: {det}\n\n"
                    f"선택된 항목들을 정말로 일괄 치환하시겠습니까?",
                    parent=editor
                )
                if not confirm:
                    return

            updated_count = 0
            selected_items = [current_items[i] for i in selected_indices]
            find_terms = [t.strip() for t in re.split(r'[,|/]', find_term) if t.strip()] if find_term else []

            for item in selected_items:
                old_tgt = item["tgt"]
                new_tgt = old_tgt
                if find_terms:
                    replaced_any = False
                    for ft in find_terms:
                        if ft in new_tgt:
                            new_tgt = new_tgt.replace(ft, repl_term)
                            replaced_any = True
                    if not replaced_any:
                        continue
                else:
                    new_tgt = repl_term

                if new_tgt != old_tgt:
                    translation_memory.update_memory_entry(item["category"], item["src"], new_tgt)
                    apply_hotpatch(old_tgt, new_tgt)
                    updated_count += 1

            # 용어집 등록 (향후 오역 재발 방지)
            q_term = query_var.get().strip()
            if q_term and repl_term:
                register_glossary_rule(q_term, repl_term)

            # 수정 후 "오역 데이터 일괄 삭제 예/아니요" 팝업
            unselected_items = [item for i, item in enumerate(current_items) if i not in selected_indices]
            unselected_count = len(unselected_items)

            if unselected_count > 0:
                ask_del = messagebox.askyesno(
                    "오역 데이터 일괄 삭제",
                    f"총 {updated_count}개 항목이 '{repl_term}'(으)로 일괄 수정되었습니다! 🎉\n\n"
                    f"검색 결과 중 체크하지 않은 나머지 {unselected_count}개의 이전 오역 항목들을\n"
                    f"캐시 및 클라우드 DB에서 일괄 삭제하시겠습니까?",
                    parent=editor
                )
                if ask_del:
                    for item in unselected_items:
                        translation_memory.delete_memory_entry(item["category"], item["src"])
                    self.log(f"🗑️ 이전 오역 데이터 {unselected_count}개가 캐시에서 일괄 삭제되었습니다.")
                    self.show_messagebox("info", "완료", f"수정 {updated_count}건 반영 및 나머지 오역 {unselected_count}건 삭제가 완료되었습니다.")
                else:
                    self.show_messagebox("info", "완료", f"총 {updated_count}개 항목이 일괄 수정되었습니다.")
            else:
                self.show_messagebox("info", "완료", f"총 {updated_count}개 항목이 일괄 수정되었습니다.")

            do_search()

        def do_batch_delete():
            selected_indices = [i for i, v in enumerate(check_vars) if v.get()]
            if not selected_indices:
                self.show_messagebox("warning", "경고", "삭제할 항목을 선택해주세요.")
                return

            count = len(selected_indices)
            confirm = messagebox.askyesno(
                "일괄 삭제 확인",
                f"선택한 {count}개 오역 데이터를 캐시 및 클라우드 DB에서 영구 삭제하시겠습니까?\n\n"
                f"삭제된 데이터는 다음 번역 시 AI가 올바른 규칙으로 새로 번역하게 됩니다.",
                parent=editor
            )
            if not confirm:
                return

            for i in selected_indices:
                item = current_items[i]
                translation_memory.delete_memory_entry(item["category"], item["src"])

            self.show_messagebox("info", "삭제 완료", f"선택한 {count}개 오역 데이터가 삭제되었습니다.")
            do_search()

        def open_add_dialog():
            dlg = ctk.CTkToplevel(editor)
            dlg.title("➕ 새 번역 데이터 추가")
            dlg.geometry("520x370")
            dlg.minsize(480, 320)
            dlg.grab_set()

            ctk.CTkLabel(
                dlg, text="캐시 테이블에 새 번역을 직접 등록합니다.",
                font=ctk.CTkFont(family=FONT_NAME, size=13, weight="bold"),
                text_color="#38bdf8"
            ).pack(padx=16, pady=(16, 8), anchor="w")

            form_frame = ctk.CTkFrame(dlg, fg_color="transparent")
            form_frame.pack(fill="both", expand=True, padx=16, pady=4)

            ctk.CTkLabel(form_frame, text="카테고리:").grid(row=0, column=0, sticky="w", pady=4)
            add_cat_var = ctk.StringVar(value="items")
            add_cat_combo = ctk.CTkComboBox(form_frame, variable=add_cat_var, values=["items", "general", "books"], width=120)
            add_cat_combo.grid(row=0, column=1, sticky="w", pady=4)

            ctk.CTkLabel(form_frame, text="영어 원문:").grid(row=1, column=0, sticky="w", pady=4)
            add_src_entry = ctk.CTkEntry(form_frame, placeholder_text="예: Tinkers' Construct", width=340)
            add_src_entry.grid(row=1, column=1, sticky="we", pady=4)
            curr_q = query_var.get().strip()
            if curr_q:
                add_src_entry.insert(0, curr_q)

            ctk.CTkLabel(form_frame, text="한글 번역문:").grid(row=2, column=0, sticky="w", pady=4)
            add_tgt_entry = ctk.CTkEntry(form_frame, placeholder_text="예: 팅커스 컨스트럭트", width=340)
            add_tgt_entry.grid(row=2, column=1, sticky="we", pady=4)

            add_glossary_var = ctk.BooleanVar(value=True)
            ctk.CTkCheckBox(
                form_frame, text="📖 용어집(Glossary)에도 고정 등록 (향후 오역 재발 방지)",
                variable=add_glossary_var, text_color="#38bdf8", fg_color="#0284c7"
            ).grid(row=3, column=1, sticky="w", pady=(8, 12))

            form_frame.grid_columnconfigure(1, weight=1)

            def do_add_save():
                src = add_src_entry.get().strip()
                tgt = add_tgt_entry.get().strip()
                cat = add_cat_var.get()
                if not src or not tgt:
                    messagebox.showwarning("경고", "원문과 번역문을 모두 입력해주세요.", parent=dlg)
                    return

                # 신규 등록 전 오역률 검증
                err_rate, val_rate, verdict, det = translation_memory.calculate_translation_error_rate(src, tgt)
                self.log(f"🔍 [신규 등록 오역률 검증] 원문 '{src}' ➡️ 번역 '{tgt}' | 오역률: {err_rate:.1f}% (연관도: {val_rate:.1f}%) | 판정: {verdict.upper()} ({det})")

                if verdict == "hard_block":
                    messagebox.showerror(
                        "등록 불가 (기등록 데이터 오염 차단)",
                        f"'{src}'(은)는 이미 표준 번역이 확립된 단어입니다.\n\n"
                        f"• 분석 판정: 🚨 오역률 {err_rate:.1f}%\n"
                        f"• 상세 사유: {det}\n\n"
                        f"⚠️ 기존 정상 단어를 무관한 단어로 변조하여 등록할 수 없습니다.",
                        parent=dlg
                    )
                    return
                elif verdict == "block":
                    force_apply = messagebox.askyesno(
                        "신규 등록 확인",
                        f"입력하신 번역 '{tgt}'은(는) 기존 데이터가 없는 신규 단어입니다.\n\n"
                        f"• 분석 판정: ⚠️ 오역률 {err_rate:.1f}% (연관도: {val_rate:.1f}%)\n"
                        f"• 상세 사유: {det}\n\n"
                        f"신규 단어로 등록하시겠습니까?",
                        parent=dlg
                    )
                    if not force_apply:
                        return
                elif verdict == "warning":
                    confirm = messagebox.askyesno(
                        "신규 등록 주의 확인",
                        f"입력하신 번역의 오역률이 다소 높습니다 (오역률: {err_rate:.1f}%).\n\n"
                        f"정말로 이 데이터를 캐시 테이블에 신규 등록하시겠습니까?",
                        parent=dlg
                    )
                    if not confirm:
                        return

                translation_memory.update_memory_entry(cat, src, tgt)
                apply_hotpatch(src, tgt)
                if add_glossary_var.get():
                    register_glossary_rule(src, tgt)

                dlg.destroy()
                self.show_messagebox("info", "등록 완료", f"'{src}' ➡️ '{tgt}' 데이터가 성공적으로 등록되었습니다! (오역률: {err_rate:.1f}%)")
                query_var.set(src)
                do_search()

            btn_box = ctk.CTkFrame(dlg, fg_color="transparent")
            btn_box.pack(fill="x", padx=16, pady=(0, 16))

            ctk.CTkButton(
                btn_box, text="💾 저장 및 등록",
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                fg_color="#15803d", hover_color="#166534",
                command=do_add_save
            ).pack(side="right", padx=(8, 0))

            ctk.CTkButton(
                btn_box, text="취소", fg_color="#3f3f46", hover_color="#52525b",
                command=dlg.destroy
            ).pack(side="right")
