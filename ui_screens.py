"""
UI 화면 전환, 레이아웃, 로그, 입력 검증 관련 메서드 믹스인.
QuestTranslatorApp이 이 클래스를 상속해서 사용합니다.
"""
import threading
import os

try:
    import customtkinter as ctk
    import tkinter as tk
    from tkinter import messagebox
except Exception:
    ctk = None
    tk = None
    messagebox = None

from translation_engines import ENGINES

FONT_NAME = "Malgun Gothic"


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

    # ====================================================================
    # 설정 & 입력 검증
    # ====================================================================

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
            return None, None, False

        self.save_user_settings()
        is_paid = "유료" in self.plan_combo.get()
        return engine_key, api_key, is_paid

    def request_cancel(self):
        self.cancel_requested = True
        self.log("🛑 사용자가 번역 취소를 요청했습니다. 작업을 중단합니다...")
        self.btn_cancel.configure(state="disabled")

    def is_cancelled(self):
        return self.cancel_requested
