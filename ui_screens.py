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
    from tkinter import messagebox, ttk
except Exception:
    ctk = None
    tk = None
    messagebox = None
    ttk = None

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
        editor.geometry("1040x820")
        editor.minsize(920, 700)
        editor.grab_set()

        # ================================================================
        # 1. 상단 통합 검색 바
        # ================================================================
        search_card = ctk.CTkFrame(editor, fg_color="#18181d", corner_radius=10, border_width=1, border_color="#2a2a33")
        search_card.pack(fill="x", padx=14, pady=(12, 6))

        search_row = ctk.CTkFrame(search_card, fg_color="transparent")
        search_row.pack(fill="x", padx=12, pady=10)

        query_var = ctk.StringVar()
        entry_search = ctk.CTkEntry(
            search_row, textvariable=query_var,
            placeholder_text="검색어 (영어 원문 또는 한글 번역문 입력...)",
            font=ctk.CTkFont(family=FONT_NAME, size=12), width=260
        )
        entry_search.pack(side="left", padx=(0, 6))

        field_var = ctk.StringVar(value="통합 (원문+번역)")
        field_combo = ctk.CTkComboBox(
            search_row, variable=field_var,
            values=["통합 (원문+번역)", "영어 원문(EN)만", "한글 번역(KO)만"],
            font=ctk.CTkFont(family=FONT_NAME, size=11), width=125
        )
        field_combo.pack(side="left", padx=(0, 6))

        cat_var = ctk.StringVar(value="all")
        cat_combo = ctk.CTkComboBox(
            search_row, variable=cat_var,
            values=["all", "items", "general", "books"],
            font=ctk.CTkFont(family=FONT_NAME, size=11), width=80
        )
        cat_combo.pack(side="left", padx=(0, 6))

        limit_var = ctk.StringVar(value="1,000개")
        limit_combo = ctk.CTkComboBox(
            search_row, variable=limit_var,
            values=["500개", "1,000개", "3,000개", "5,000개", "제한 없음(전체)"],
            font=ctk.CTkFont(family=FONT_NAME, size=11), width=110
        )
        limit_combo.pack(side="left", padx=(0, 8))

        btn_search = ctk.CTkButton(
            search_row, text="🔍 검색", width=75, height=28,
            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
            command=lambda: do_search()
        )
        btn_search.pack(side="left", padx=(0, 8))

        btn_guide = ctk.CTkButton(
            search_row, text="💡 인게임 반영 가이드", width=145, height=28,
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
            fg_color="#854d0e", hover_color="#a16207",
            command=lambda: open_guide_modal()
        )
        btn_guide.pack(side="right", padx=(6, 0))

        btn_add_data = ctk.CTkButton(
            search_row, text="➕ 새 번역 추가", width=115, height=28,
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
            fg_color="#0284c7", hover_color="#0369a1",
            command=lambda: open_add_dialog()
        )
        btn_add_data.pack(side="right")

        # ================================================================
        # 2. 수정 & 치환 작업 패널 (검색창 바로 밑에 밀착 배치!)
        # ================================================================
        action_container = ctk.CTkFrame(editor, fg_color="#18181d", corner_radius=10, border_width=1, border_color="#2a2a33")
        action_container.pack(fill="x", padx=14, pady=(0, 6))

        # 2-A. [패널: 오역 데이터 직접 수정] (기본 패널)
        panel_edit = ctk.CTkFrame(action_container, fg_color="transparent")
        panel_edit.pack(fill="x", padx=12, pady=10)

        # 원문 표시 행
        row_orig = ctk.CTkFrame(panel_edit, fg_color="transparent")
        row_orig.pack(fill="x", pady=(0, 6))

        ctk.CTkLabel(
            row_orig, text="원문:",
            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"), text_color="#cbd5e1"
        ).pack(side="left", padx=(0, 8))

        lbl_original = ctk.CTkLabel(
            row_orig, text="(아래 목록에서 수정할 항목을 클릭하세요)", anchor="w",
            font=ctk.CTkFont(family=FONT_NAME, size=12),
            fg_color="#111113", corner_radius=6, height=28
        )
        lbl_original.pack(side="left", fill="x", expand=True)

        # 번역 입력 및 저장 버튼 행
        row_trans = ctk.CTkFrame(panel_edit, fg_color="transparent")
        row_trans.pack(fill="x", pady=(0, 6))

        ctk.CTkLabel(
            row_trans, text="번역:",
            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"), text_color="#cbd5e1"
        ).pack(side="left", padx=(0, 8))

        entry_translated = ctk.CTkEntry(
            row_trans, font=ctk.CTkFont(family=FONT_NAME, size=12),
            fg_color="#111113", border_color="#52525b"
        )
        entry_translated.pack(side="left", fill="x", expand=True, padx=(0, 10))

        btn_save = ctk.CTkButton(
            row_trans, text="💾 저장 및 즉시 적용", width=140, height=30,
            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
            fg_color="#15803d", hover_color="#166534",
            command=lambda: do_save()
        )
        btn_save.pack(side="right")

        chk_glossary_var = ctk.BooleanVar(value=True)
        chk_glossary = ctk.CTkCheckBox(
            panel_edit, text="📖 용어집(Glossary)에도 고정 등록하여 향후 AI 오역 재발 방지",
            variable=chk_glossary_var,
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
            text_color="#38bdf8", checkmark_color="#ffffff", fg_color="#0284c7"
        )
        chk_glossary.pack(anchor="w", padx=(44, 0))

        # 2-B. [패널: 동일 단어 일괄 치환] (스위칭 패널)
        panel_replace = ctk.CTkFrame(action_container, fg_color="transparent")

        replace_row = ctk.CTkFrame(panel_replace, fg_color="transparent")
        replace_row.pack(fill="x", padx=4, pady=8)

        ctk.CTkLabel(
            replace_row, text="찾을 단어:",
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"), text_color="#cbd5e1"
        ).pack(side="left", padx=(0, 4))

        find_var = ctk.StringVar()
        entry_find = ctk.CTkEntry(
            replace_row, textvariable=find_var, placeholder_text="예: 팃커스, 휭커스 (다중 가능)",
            font=ctk.CTkFont(family=FONT_NAME, size=11), width=230
        )
        entry_find.pack(side="left", padx=(0, 8))

        ctk.CTkLabel(
            replace_row, text="➡️ 바꿀 단어:",
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"), text_color="#cbd5e1"
        ).pack(side="left", padx=(0, 4))

        replace_var = ctk.StringVar()
        entry_replace = ctk.CTkEntry(
            replace_row, textvariable=replace_var, placeholder_text="예: 팅커스 (표준어)",
            font=ctk.CTkFont(family=FONT_NAME, size=11), width=200
        )
        entry_replace.pack(side="left", padx=(0, 10))

        btn_batch_replace = ctk.CTkButton(
            replace_row, text="⚡ 선택 항목 일괄 치환", height=30,
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
            fg_color="#ea580c", hover_color="#c2410c",
            command=lambda: do_batch_replace()
        )
        btn_batch_replace.pack(side="right")

        # ================================================================
        # 3. 툴바 (선택 제어 & 모드 전환 버튼)
        # ================================================================
        toolbar = ctk.CTkFrame(editor, fg_color="transparent")
        toolbar.pack(fill="x", padx=14, pady=(0, 6))

        btn_select_all = ctk.CTkButton(
            toolbar, text="☑ 전체 선택", width=75, height=26,
            font=ctk.CTkFont(family=FONT_NAME, size=11),
            fg_color="#334155", hover_color="#475569",
            command=lambda: set_all_selection(True)
        )
        btn_select_all.pack(side="left", padx=(0, 6))

        btn_deselect_all = ctk.CTkButton(
            toolbar, text="선택 해제", width=65, height=26,
            font=ctk.CTkFont(family=FONT_NAME, size=11),
            fg_color="#27272a", hover_color="#3f3f46",
            command=lambda: set_all_selection(False)
        )
        btn_deselect_all.pack(side="left", padx=(0, 10))

        lbl_selected_count = ctk.CTkLabel(
            toolbar, text="선택: 0개 / 검색: 0개",
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
            text_color="#38bdf8"
        )
        lbl_selected_count.pack(side="left")

        # 우측: 모드 스위치 버튼 2개
        mode_box = ctk.CTkFrame(toolbar, fg_color="#18181d", corner_radius=8, border_width=1, border_color="#2a2a33")
        mode_box.pack(side="right")

        btn_mode_edit = ctk.CTkButton(
            mode_box, text="🛠️ 번역 직접 수정", width=125, height=26,
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
            fg_color="#0284c7", hover_color="#0369a1",
            command=lambda: switch_mode("edit")
        )
        btn_mode_edit.pack(side="left", padx=2, pady=2)

        btn_mode_replace = ctk.CTkButton(
            mode_box, text="⚡ 동일 단어 일괄 치환", width=140, height=26,
            font=ctk.CTkFont(family=FONT_NAME, size=11),
            fg_color="transparent", text_color="#94a3b8", hover_color="#27272a",
            command=lambda: switch_mode("replace")
        )
        btn_mode_replace.pack(side="left", padx=2, pady=2)

        def switch_mode(mode):
            if mode == "edit":
                btn_mode_edit.configure(fg_color="#0284c7", text_color="#ffffff")
                btn_mode_replace.configure(fg_color="transparent", text_color="#94a3b8")
                panel_replace.pack_forget()
                panel_edit.pack(fill="x", padx=12, pady=10)
            else:
                btn_mode_replace.configure(fg_color="#ea580c", text_color="#ffffff")
                btn_mode_edit.configure(fg_color="transparent", text_color="#94a3b8")
                panel_edit.pack_forget()
                panel_replace.pack(fill="x", padx=12, pady=10)

        # ================================================================
        # 4. 중앙 및 하단 결과 테이블 리스트 (Treeview - 시원하게 펼쳐짐)
        # ================================================================
        table_container = ctk.CTkFrame(editor, fg_color="#141418", corner_radius=10, border_width=1, border_color="#27272a")
        table_container.pack(fill="both", expand=True, padx=14, pady=(0, 12))

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(
            "DarkTree.Treeview",
            background="#141418",
            foreground="#f1f5f9",
            fieldbackground="#141418",
            borderwidth=0,
            rowheight=28,
            font=(FONT_NAME, 10)
        )
        style.map(
            "DarkTree.Treeview",
            background=[("selected", "#0284c7")],
            foreground=[("selected", "#ffffff")]
        )
        style.configure(
            "DarkTree.Treeview.Heading",
            background="#202026",
            foreground="#94a3b8",
            relief="flat",
            borderwidth=0,
            font=(FONT_NAME, 10, "bold")
        )
        style.map(
            "DarkTree.Treeview.Heading",
            background=[("active", "#2a2a32")]
        )

        cols = ("check", "category", "src", "tgt")
        tree = ttk.Treeview(table_container, columns=cols, show="headings", style="DarkTree.Treeview", selectmode="browse")

        tree.heading("check", text="선택")
        tree.column("check", width=46, minwidth=46, anchor="center", stretch=False)
        tree.heading("category", text="분류")
        tree.column("category", width=75, minwidth=65, anchor="center", stretch=False)
        tree.heading("src", text="영어 원문 (EN)")
        tree.column("src", width=420, minwidth=200, anchor="w")
        tree.heading("tgt", text="한글 번역 (KO)")
        tree.column("tgt", width=420, minwidth=200, anchor="w")

        scrollbar = ctk.CTkScrollbar(table_container, orientation="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y", padx=(0, 2), pady=2)
        tree.pack(side="left", fill="both", expand=True, padx=2, pady=2)

        # ================================================================
        # 5. 데이터 로직 & 비즈니스 기능
        # ================================================================
        selected_item = {"category": None, "src": None, "tgt": None}
        current_items = []
        checked_iids = set()

        def update_selected_count():
            cnt = len(checked_iids)
            total_cnt = len(current_items)
            lbl_selected_count.configure(text=f"선택: {cnt}개 / 검색: {total_cnt}개")

        def set_all_selection(select_all=True):
            checked_iids.clear()
            chk_char = "☑" if select_all else "☐"
            for i, item in enumerate(current_items):
                iid = str(i)
                if select_all:
                    checked_iids.add(iid)
                tree.item(iid, values=(chk_char, f"[{item['category']}]", item["src"], item["tgt"]))
            update_selected_count()

        def on_item_click(item):
            selected_item["category"] = item["category"]
            selected_item["src"] = item["src"]
            selected_item["tgt"] = item["tgt"]
            lbl_original.configure(text=item["src"])
            entry_translated.delete(0, 'end')
            entry_translated.insert(0, item["tgt"])

        def toggle_check(iid):
            if not iid:
                return
            idx = int(iid)
            if 0 <= idx < len(current_items):
                if iid in checked_iids:
                    checked_iids.remove(iid)
                    chk_char = "☐"
                else:
                    checked_iids.add(iid)
                    chk_char = "☑"
                item = current_items[idx]
                tree.item(iid, values=(chk_char, f"[{item['category']}]", item["src"], item["tgt"]))
                update_selected_count()

        def on_tree_click(event):
            iid = tree.identify_row(event.y)
            col = tree.identify_column(event.x)
            if not iid:
                return
            idx = int(iid)
            if 0 <= idx < len(current_items):
                item = current_items[idx]
                on_item_click(item)
            if col == "#1":
                toggle_check(iid)

        def on_space_key(event):
            sel = tree.selection()
            if sel:
                toggle_check(sel[0])

        tree.bind("<ButtonRelease-1>", on_tree_click)
        tree.bind("<space>", on_space_key)

        def open_guide_modal():
            guide_win = ctk.CTkToplevel(editor)
            guide_win.title("💡 인게임 반영 가이드 & 주의사항")
            guide_win.geometry("660x680")
            guide_win.minsize(560, 520)
            guide_win.grab_set()

            scroller = ctk.CTkScrollableFrame(guide_win, fg_color="#121216", corner_radius=8)
            scroller.pack(fill="both", expand=True, padx=16, pady=16)

            # 1. ⚠️ 최상단 강조 경고 배너
            warn_frame = ctk.CTkFrame(scroller, fg_color="#271c06", border_width=2, border_color="#f59e0b", corner_radius=8)
            warn_frame.pack(fill="x", pady=(0, 14))

            ctk.CTkLabel(
                warn_frame,
                text="⚠️ 이 세션 이전에 번역한 파일들은 수정한 내역이 변경되지 않습니다!",
                font=ctk.CTkFont(family=FONT_NAME, size=13, weight="bold"),
                text_color="#fde68a"
            ).pack(anchor="w", padx=14, pady=(12, 4))

            ctk.CTkLabel(
                warn_frame,
                text="오역 수정 이후에 다시 번역을 돌려 파일을 교체하시는 것을 추천드립니다.",
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                text_color="#fef3c7"
            ).pack(anchor="w", padx=14, pady=(0, 12))

            # 2. 기본 적용 규칙 안내
            rule_frame = ctk.CTkFrame(scroller, fg_color="#1a1a22", corner_radius=8)
            rule_frame.pack(fill="x", pady=(0, 14))

            ctk.CTkLabel(
                rule_frame, text="🎮 기본 반영 안내",
                font=ctk.CTkFont(family=FONT_NAME, size=13, weight="bold"),
                text_color="#38bdf8"
            ).pack(anchor="w", padx=14, pady=(10, 4))

            ctk.CTkLabel(
                rule_frame,
                text="• 마인크래프트를 끈 상태에서 수정 후 게임 실행 시 ➡️ [100% 자동 적용]\n• 마인크래프트가 켜져 있는 상태 ➡️ 아래 상황별 새로고침 명령어를 사용하세요.",
                font=ctk.CTkFont(family=FONT_NAME, size=11), justify="left", text_color="#cbd5e1"
            ).pack(anchor="w", padx=14, pady=(0, 10))

            # 3. 상황별 인게임 새로고침 방법 (4개 독립 숨김 박스)
            ctk.CTkLabel(
                scroller, text="📋 상황별 인게임 새로고침 방법 (클릭하여 펼치기)",
                font=ctk.CTkFont(family=FONT_NAME, size=13, weight="bold"),
                text_color="#f1f5f9"
            ).pack(anchor="w", pady=(4, 8))

            cases = [
                (
                    "1. 퀘스트 모드 계열 (FTB Quests, Better Questing, HQM 등)",
                    "• FTB Quests (1.12 ~ 1.20+): /ftbquests reload\n"
                    "• Better Questing (1.7.10 ~ 1.12.2): /bq_admin default load\n"
                    "• Hardcore Questing Mode (HQM): /hqm reload\n"
                    "• Custom NPCs (퀘스트/대화): 대화창 닫고 다시 열기 또는 /reload"
                ),
                (
                    "2. 아이템명 / 블록명 / 모드 언어 파일 (리소스팩)",
                    "• 모든 모드 언어 파일 (.lang, ko_kr.json): [ F3 + T ] (단축키)\n"
                    "  ※ 치트 권한 불필요! 인게임에서 리소스팩 전체를 즉시 다시 읽어 들입니다.\n"
                    "• KubeJS 아이템/툴팁 스크립트: /kubejs reload client_scripts"
                ),
                (
                    "3. 가이드북 및 레시피 모드 (Patchouli, 데이터팩)",
                    "• 패출리 책 (Patchouli Books): /patchouli reload 또는 [ F3 + T ]\n"
                    "• 데이터팩 / 레시피: /reload"
                ),
                (
                    "4. 치트 권한(OP)이 없는 경우 (싱글 LAN 열기, 멀티 등)",
                    "• 싱글플레이: ESC ➡️ 'LAN 서버 열기' ➡️ [치트 허용: ON] 후 명령어 입력\n"
                    "• 멀티플레이 / 치트 불가 시: 월드 재접속(서버 재접속) 또는 마인크래프트 재실행"
                ),
            ]

            for idx, (title, content) in enumerate(cases):
                acc_container = ctk.CTkFrame(scroller, fg_color="#181820", corner_radius=6, border_width=1, border_color="#2b2b36")
                acc_container.pack(fill="x", pady=4)

                body_frame = ctk.CTkFrame(acc_container, fg_color="#121218", corner_radius=4)
                ctk.CTkLabel(
                    body_frame, text=content,
                    font=ctk.CTkFont(family="Consolas" if os.name == 'nt' else FONT_NAME, size=11),
                    justify="left", text_color="#93c5fd"
                ).pack(anchor="w", padx=12, pady=10)

                def make_toggle(b_frame, btn_ref, item_title):
                    is_open = [False]
                    def _toggle():
                        is_open[0] = not is_open[0]
                        if is_open[0]:
                            b_frame.pack(fill="x", padx=6, pady=(0, 6))
                            btn_ref.configure(text=f"▼  {item_title}", text_color="#67e8f9")
                        else:
                            b_frame.pack_forget()
                            btn_ref.configure(text=f"▶  {item_title}", text_color="#e2e8f0")
                    return _toggle

                btn_toggle = ctk.CTkButton(
                    acc_container, text=f"▶  {title}", anchor="w",
                    font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
                    fg_color="transparent", hover_color="#242430", text_color="#e2e8f0",
                    height=32
                )
                btn_toggle.pack(fill="x", padx=4, pady=2)
                toggle_cmd = make_toggle(body_frame, btn_toggle, title)
                btn_toggle.configure(command=toggle_cmd)

            # 4. 작업 주의사항 프레임
            caution_frame = ctk.CTkFrame(scroller, fg_color="#1a1a22", corner_radius=8)
            caution_frame.pack(fill="x", pady=(14, 4))

            ctk.CTkLabel(
                caution_frame, text="💡 작업 주의사항",
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                text_color="#a3e635"
            ).pack(anchor="w", padx=14, pady=(10, 4))

            ctk.CTkLabel(
                caution_frame,
                text="• 단어 일괄 치환 시 너무 짧은 1~2글자 일반 단어(조사 등) 치환에 주의하세요.\n"
                     "• 공백(' ') 치환 시 해당 단어가 문장에서 완전히 삭제됩니다.\n"
                     "• 잘못 수정했더라도 디스코드 알림이나 [되돌리기]를 통해 언제든 복구할 수 있습니다.",
                font=ctk.CTkFont(family=FONT_NAME, size=11), justify="left", text_color="#cbd5e1"
            ).pack(anchor="w", padx=14, pady=(0, 10))

            btn_close = ctk.CTkButton(
                guide_win, text="닫기", height=32,
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                fg_color="#334155", hover_color="#475569",
                command=guide_win.destroy
            )
            btn_close.pack(pady=(0, 12))

        def apply_hotpatch(old_tgt, new_tgt):
            import temp_session_logger
            if temp_session_logger.is_debug_mode():
                self.log(f"🧪 [테스트 모드] 모드팩 파일 수정을 건너뜁니다. ({old_tgt} ➡️ {new_tgt})")
                return

            mp_dir = getattr(self.app_state, 'modpack_dir', None) or getattr(self, 'selected_modpack_path', None)
            if not mp_dir or not os.path.isdir(mp_dir):
                return

            try:
                import translation_editor
                res = translation_editor.patch_modpack_files(mp_dir, old_tgt, new_tgt)
                mp_name = res.get("modpack_name") or os.path.basename(mp_dir.rstrip(os.sep))
                total = res.get("total", 0)
                quests = res.get("quests", 0)
                lang_res = res.get("lang_resources", 0)
                books = res.get("books", 0)

                if total > 0:
                    self.log(
                        f"⚡ [모드팩 파일 즉시 패치] '{mp_name}' 파일 총 {total}개 수정 완료 "
                        f"(퀘스트: {quests}개, 리소스팩/아이템: {lang_res}개, 가이드북: {books}개)"
                    )
                else:
                    self.log(f"ℹ️ [번역 메모리 갱신] 캐시 및 클라우드 DB에 반영되었습니다. ('{mp_name}' 모드팩 내 일치 항목 0개)")
            except Exception as e:
                self.log(f"⚠️ 모드팩 파일 즉시 패치 중 오류 발생: {e}")

        def apply_batch_hotpatch(batch_changes):
            import temp_session_logger
            if temp_session_logger.is_debug_mode():
                self.log(f"🧪 [테스트 모드] 모드팩 파일 일괄 수정을 건너뜁니다. ({len(batch_changes)}건)")
                return

            mp_dir = getattr(self.app_state, 'modpack_dir', None) or getattr(self, 'selected_modpack_path', None)
            if not mp_dir or not os.path.isdir(mp_dir):
                return

            try:
                import translation_editor
                res = translation_editor.patch_modpack_batch_files(mp_dir, batch_changes)
                mp_name = res.get("modpack_name") or os.path.basename(mp_dir.rstrip(os.sep))
                total = res.get("total", 0)
                quests = res.get("quests", 0)
                lang_res = res.get("lang_resources", 0)
                books = res.get("books", 0)

                if total > 0:
                    self.log(
                        f"⚡ [모드팩 파일 즉시 패치] '{mp_name}' 파일 총 {total}개 일괄 치환 완료 "
                        f"(퀘스트: {quests}개, 리소스팩/아이템: {lang_res}개, 가이드북: {books}개)"
                    )
                else:
                    self.log(f"ℹ️ [번역 메모리 갱신] 캐시 및 클라우드 DB에 반영되었습니다. ('{mp_name}' 모드팩 내 일치 항목 0개)")
            except Exception as e:
                self.log(f"⚠️ 모드팩 파일 즉시 일괄 패치 중 오류 발생: {e}")

        def register_glossary_rule(src, tgt):
            if not chk_glossary_var.get() or not src or not tgt:
                return
            import temp_session_logger
            if temp_session_logger.is_debug_mode():
                self.log(f"🧪 [테스트 모드] 용어집 설정 저장을 건너뜁니다. ({src} ➡️ {tgt})")
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
            tree.delete(*tree.get_children())
            current_items.clear()
            checked_iids.clear()
            selected_item["category"] = None
            selected_item["src"] = None
            selected_item["tgt"] = None
            lbl_original.configure(text="(아래 목록에서 수정할 항목을 클릭하세요)")
            entry_translated.delete(0, 'end')

            q = query_var.get().strip()
            if len(q) < 1:
                update_selected_count()
                return

            f_val = field_var.get()
            s_field = "src" if "EN" in f_val else ("tgt" if "KO" in f_val else "all")

            l_str = limit_var.get()
            if "500" in l_str:
                lim = 500
            elif "3,000" in l_str:
                lim = 3000
            elif "5,000" in l_str:
                lim = 5000
            elif "전체" in l_str:
                lim = 100000
            else:
                lim = 1000

            results = translation_memory.search_memory(q, category=cat_var.get(), limit=lim, search_field=s_field)
            current_items.extend(results)

            for i, r in enumerate(results):
                iid = str(i)
                tree.insert("", "end", iid=iid, values=("☐", f"[{r['category']}]", r["src"], r["tgt"]))
            update_selected_count()

        entry_search.bind("<Return>", do_search)

        def do_save():
            if not selected_item["src"]:
                self.show_messagebox("warning", "안내", "수정할 항목을 아래 목록에서 먼저 선택해 주세요.")
                return
            new_val = entry_translated.get().strip()
            if not new_val:
                self.show_messagebox("warning", "경고", "번역문을 입력해 주세요.")
                return
            old_val = selected_item["tgt"]
            src_val = selected_item["src"]

            # 오역률 산출 (단순 수치 참고용)
            err_rate, val_rate, verdict, det = translation_memory.calculate_translation_error_rate(
                src_val, new_val, reference_hint=old_val
            )
            self.log(f"🔍 [오역률 검증] 원문 '{src_val}' ➡️ 수정 '{new_val}' | 오역률: {err_rate:.1f}%")

            # 통일된 수정 확인 알림창
            confirm_msg = (
                f"정말 이렇게 수정하시겠습니까?\n\n"
                f"• 영어 원문: {src_val}\n"
                f"• 원래 번역: {old_val}\n"
                f"• 바뀐 번역: {new_val}\n\n"
                f"(오역률 감지: {err_rate:.1f}%)"
            )
            if not messagebox.askyesno("번역 수정 확인", confirm_msg, parent=editor):
                return

            # 캐시 업데이트 및 Cloudflare 커밋 큐 자동 등록
            translation_memory.update_memory_entry(selected_item["category"], src_val, new_val)
            apply_hotpatch(old_val, new_val)
            register_glossary_rule(src_val, new_val)

            # 디스코드 노란색 임베드 + 초록색 롤백 버튼 감사 로그 전송
            translation_memory.dispatch_audit_report(
                selected_item["category"], src_val, old_val, new_val,
                error_rate=err_rate, validity=val_rate, verdict=verdict, details=det
            )
            selected_item["tgt"] = new_val
            self.show_messagebox("info", "성공", f"수정사항이 저장되었습니다! (오역률: {err_rate:.1f}%)")
            do_search()

        def do_batch_replace():
            selected_indices = sorted([int(iid) for iid in checked_iids if iid.isdigit() and int(iid) < len(current_items)])
            if not selected_indices:
                self.show_messagebox("warning", "경고", "치환할 항목을 하나 이상 선택(체크)해주세요.")
                return

            find_term = find_var.get().strip()
            if not find_term:
                self.show_messagebox("warning", "경고", "찾을 단어를 입력해주세요.")
                return

            # 바꿀 단어가 비어있거나 공백(space)이면 감지한 단어를 아예 삭제/제거
            raw_repl = replace_var.get()
            is_deletion = (not raw_repl) or (raw_repl.strip() == "")
            repl_term = "" if is_deletion else raw_repl.strip()
            repl_label = "[단어 삭제]" if is_deletion else f"'{repl_term}'"

            # 오역률 산출
            target_basis = query_var.get().strip() or find_term or "단어"
            err_rate, val_rate, verdict, det = translation_memory.calculate_translation_error_rate(
                target_basis, repl_term or find_term, reference_hint=find_term
            )

            # 통일된 일괄 치환/삭제 확인 팝업창
            confirm_msg = (
                f"정말 이렇게 수정하시겠습니까?\n\n"
                f"• 선택된 항목: 총 {len(selected_indices)}개\n"
                f"• 찾은 단어: '{find_term}'\n"
                f"• 바뀐 번역: {repl_label}\n\n"
                f"(오역률 감지: {err_rate:.1f}%)"
            )
            if not messagebox.askyesno("일괄 치환 확인", confirm_msg, parent=editor):
                return

            updated_count = 0
            selected_items = [current_items[i] for i in selected_indices]
            find_terms = [t.strip() for t in re.split(r'[,|/]', find_term) if t.strip()] if find_term else []
            batch_changes = []

            for item in selected_items:
                old_tgt = item["tgt"]
                new_tgt = old_tgt
                if find_terms:
                    for ft in find_terms:
                        pattern = re.compile(re.escape(ft), re.IGNORECASE)
                        if pattern.search(new_tgt):
                            new_tgt = pattern.sub(repl_term, new_tgt)
                        elif pattern.search(item["src"]):
                            new_tgt = new_tgt.replace(ft, repl_term)
                else:
                    new_tgt = repl_term

                # 공백 치환(삭제) 시 남은 연속 공백 및 양끝 공백 정리
                if is_deletion:
                    new_tgt = re.sub(r'\s+', ' ', new_tgt).strip()

                if new_tgt != old_tgt:
                    batch_changes.append({
                        "category": item["category"], "src": item["src"],
                        "old_tgt": old_tgt, "new_tgt": new_tgt
                    })
                    item["tgt"] = new_tgt
                    with translation_memory._lock:
                        cache_dict = (
                            translation_memory._items_cache if item["category"] == "items" else
                            (translation_memory._books_cache if item["category"] == "books" else translation_memory._global_cache)
                        )
                        translation_memory._store(cache_dict, item["src"], new_tgt, "한국어(Korean)")
                    updated_count += 1

            if updated_count > 0:
                translation_memory.save_memory(flush_now=True)
                apply_batch_hotpatch(batch_changes)
                action_text = f"단어 삭제: '{find_term}' 제거" if is_deletion else f"일괄 치환: '{find_term}' ➡️ '{repl_term}'"
                translation_memory.dispatch_batch_audit_report(
                    batch_changes, error_rate=err_rate, validity=val_rate, verdict=verdict,
                    details=f"{action_text} (총 {updated_count}건)"
                )

            q_term = query_var.get().strip()
            if q_term and not is_deletion:
                register_glossary_rule(q_term, repl_term)

            self.show_messagebox("info", "완료", f"총 {updated_count}개 항목이 {repl_label}(으)로 일괄 수정되었습니다.")
            do_search()

            q_term = query_var.get().strip()
            if q_term and repl_term:
                register_glossary_rule(q_term, repl_term)

            self.show_messagebox("info", "완료", f"총 {updated_count}개 항목이 '{repl_term}'(으)로 일괄 수정되었습니다.")
            do_search()

        # ================================================================
        # 새 번역 데이터 추가 다이얼로그
        # ================================================================
        def open_add_dialog():
            dlg = ctk.CTkToplevel(editor)
            dlg.title("➕ 새 번역 데이터 추가")
            dlg.geometry("520x370")
            dlg.minsize(480, 320)
            dlg.grab_set()

            ctk.CTkLabel(
                dlg, text="캐시 테이블에 새 번역을 직접 등록합니다.",
                font=ctk.CTkFont(family=FONT_NAME, size=13, weight="bold"), text_color="#38bdf8"
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

                err_rate, val_rate, verdict, det = translation_memory.calculate_translation_error_rate(src, tgt)
                self.log(f"🔍 [신규 등록 오역률 검증] 원문 '{src}' ➡️ 번역 '{tgt}' | 오역률: {err_rate:.1f}% ({verdict})")

                translation_memory.update_memory_entry(cat, src, tgt)
                apply_hotpatch(src, tgt)
                if add_glossary_var.get():
                    register_glossary_rule(src, tgt)
                translation_memory.dispatch_audit_report(
                    cat, src, "", tgt,
                    error_rate=err_rate, validity=val_rate, verdict=verdict, details=det
                )

                dlg.destroy()
                self.show_messagebox("info", "등록 완료", f"'{src}' ➡️ '{tgt}' 데이터가 성공적으로 등록되었습니다!")
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
