"""
Quest Translator Pro - 메인 진입점
UI 구성(_setup_ui)과 생명주기 메서드만 담고,
나머지 기능은 Mixin 파일에서 상속받습니다.

파일 구조:
  app.py              - 메인 클래스, UI 구성
  ui_screens.py       - 화면 전환, 레이아웃, 로그, 입력 검증
  modpack_manager.py  - 모드팩 스캔, 카드, 썸네일
  translation_runner.py - 번역 실행 (단일/ZIP/모드팩)
"""

import os
import json
import sys
import threading
import translation_memory
import logging

# PyInstaller noconsole 모드에서 stdout/stderr이 없을 때 발생하는 크래시 방지
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

try:
    import customtkinter as ctk
    import tkinter as tk
    from tkinter import messagebox
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:
    ctk = None
    tk = None
    messagebox = None
    DND_FILES = ()
    TkinterDnD = None

from translation_engines import ENGINES
from ui_screens import UIScreensMixin
from modpack_manager import ModpackMixin
from translation_runner import TranslationMixin
from state_manager import AppState
from constants import FONT_NAME, DEFAULT_GLOSSARY, MODELS_GEMINI_FREE, MODELS_GEMINI_PAID, MODELS_OPENAI, SUPPORTED_LANGUAGES
if ctk is not None:
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")

SETTINGS_FILE_NAME = "settings.json"


if ctk is not None and TkinterDnD is not None:
    class QuestTranslatorApp(UIScreensMixin, ModpackMixin, TranslationMixin, ctk.CTk, TkinterDnD.DnDWrapper):
        def __init__(self):
            super().__init__()
            self._setup_logging()
            self.TkdndVersion = TkinterDnD._require(self)
            self._setup_ui()

        def _setup_logging(self):
            import logging
            import datetime
            appdata_dir = os.getenv("APPDATA") or os.path.expanduser("~")
            log_dir = os.path.join(appdata_dir, "QuestTranslatorPro", "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, f"quest_translator_{datetime.datetime.now().strftime('%Y%m%d')}.log")
            
            logging.basicConfig(
                filename=log_file,
                level=logging.INFO,
                format='%(asctime)s - %(levelname)s - %(message)s',
                encoding='utf-8'
            )
            logging.info("================ QuestTranslatorPro Started ================")



        # ====================================================================
        # UI 구성 (위젯 생성)
        # ====================================================================

        def _setup_ui(self):
            self.title("Quest Translator Pro")
            self.geometry("1120x760")
            self.minsize(760, 560)
            self.resizable(True, True)
            self.configure(fg_color="#0b0b0f")
            
            import sys
            import os
            try:
                base_path = sys._MEIPASS
            except Exception:
                base_path = os.path.abspath(".")
            icon_path = os.path.join(base_path, "icon.ico")
            if os.path.exists(icon_path):
                try:
                    self.iconbitmap(icon_path)
                except Exception:
                    pass

            self.settings_path = self._get_settings_path()
            self.app_state = AppState()
            
            self.protocol("WM_DELETE_WINDOW", self.on_close)

            self.grid_rowconfigure(1, weight=1)
            self.grid_columnconfigure(0, weight=1)

            self._build_header()
            self._build_screens()

            self.detected_modpacks = {}
            self.modpack_entries = []
            self.selected_modpack_path = None
            self.selected_card_widget = None
            self.modpack_cards_by_path = {}
            self.modpack_thumbnail_cache = []
            self.thumbnail_path_cache = {}

            self.home_screen.grid(row=0, column=0, sticky="nsew")
            self.select_screen.grid(row=0, column=0, sticky="nsew")
            self.quick_translate_screen.grid(row=0, column=0, sticky="nsew")
            self.translate_screen.grid(row=0, column=0, sticky="nsew")
            self._resize_job = None
            self._last_card_columns = None
            self.bind("<Configure>", self._on_window_resize)
            self.show_home_screen()

            self._show_startup_loading()
            self.after(100, self._apply_responsive_layout)
            self.after(2000, self._start_background_update_check)

        def _build_header(self):
            self.hero_frame = ctk.CTkFrame(self, fg_color="#121217", corner_radius=18, border_width=1, border_color="#23232b")
            self.hero_frame.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 10))
            self.hero_frame.grid_columnconfigure(1, weight=1)

            title_frame = ctk.CTkFrame(self.hero_frame, fg_color="transparent")
            title_frame.grid(row=0, column=0, sticky="w", padx=16, pady=(12, 0))

            from constants import APP_VERSION, GITHUB_REPO_URL, GITHUB_ICON_B64
            ctk.CTkLabel(title_frame, text="Quest Translator Pro",
                         font=ctk.CTkFont(family=FONT_NAME, size=26, weight="bold"),
                         text_color="#f8fafc").pack(side="left")
            
            ctk.CTkLabel(title_frame, text=APP_VERSION,
                         font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                         text_color="#a1a1aa").pack(side="left", padx=(8, 0), pady=(8, 0))

            self.check_update_btn = ctk.CTkButton(
                title_frame, text="🔄 업데이트 확인", width=95, height=24,
                font=ctk.CTkFont(family=FONT_NAME, size=11),
                fg_color="#27272a", hover_color="#3f3f46", text_color="#d4d4d8",
                command=self._on_manual_check_update
            )
            self.check_update_btn.pack(side="left", padx=(12, 0), pady=(6, 0))

            # 우측 상단 액션 영역 (업데이트 배너 + GitHub 링크 버튼)
            self.header_actions_frame = ctk.CTkFrame(self.hero_frame, fg_color="transparent")
            self.header_actions_frame.grid(row=0, column=1, sticky="e", padx=16, pady=(12, 0))

            # GitHub 바로가기 버튼 (배경 검은색, 공식 GitHub 화이트 로고)
            try:
                import base64, io, webbrowser
                from PIL import Image
                gh_data = base64.b64decode(GITHUB_ICON_B64)
                gh_pil = Image.open(io.BytesIO(gh_data))
                self.github_icon = ctk.CTkImage(light_image=gh_pil, dark_image=gh_pil, size=(18, 18))
            except Exception:
                self.github_icon = None

            import webbrowser
            self.github_btn = ctk.CTkButton(
                self.header_actions_frame,
                text=" GitHub",
                image=self.github_icon,
                compound="left",
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                fg_color="#000000",
                hover_color="#181b20",
                text_color="#ffffff",
                border_width=1,
                border_color="#30363d",
                height=30,
                corner_radius=8,
                cursor="hand2",
                command=lambda: webbrowser.open(GITHUB_REPO_URL)
            )
            self.github_btn.pack(side="right")

            self.update_banner_btn = ctk.CTkButton(
                self.header_actions_frame, text="✨ 새 버전 출시! 클릭하여 업데이트",
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                fg_color="#ea580c", hover_color="#c2410c", text_color="#ffffff",
                height=30, corner_radius=15,
                command=self._on_click_update_banner
            )
            self.pending_update_info = None

            self.phase_label = ctk.CTkLabel(self.hero_frame, text="STEP 1/2 · 모드팩 선택",
                                            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                                            text_color="#fb923c")
            self.phase_label.grid(row=1, column=0, columnspan=2, sticky="w", padx=16, pady=(2, 12))

            self.screen_container = ctk.CTkFrame(self, fg_color="transparent")
            self.screen_container.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 14))
            self.screen_container.grid_rowconfigure(0, weight=1)
            self.screen_container.grid_columnconfigure(0, weight=1)

        def _start_background_update_check(self):
            res = []
            def _worker():
                try:
                    import updater
                    info = updater.check_for_updates(timeout=6.0)
                    res.append(info)
                except Exception as e:
                    import logging
                    logging.debug(f"백그라운드 업데이트 확인 실패: {e}")
            threading.Thread(target=_worker, daemon=True).start()

            def _poll_bg():
                if not res:
                    self.after(200, _poll_bg)
                    return
                info = res[0]
                if info.get("has_update"):
                    self.pending_update_info = info
                    self._show_update_banner()

            self.after(200, _poll_bg)

        def _show_update_banner(self):
            if hasattr(self, "update_banner_btn") and self.pending_update_info:
                latest = self.pending_update_info.get("latest_version", "")
                self.update_banner_btn.configure(text=f"✨ 새 버전({latest}) 출시! 클릭하여 업데이트")
                self.update_banner_btn.pack(side="right", padx=(0, 10))

        def _on_click_update_banner(self):
            if self.pending_update_info:
                import updater
                updater.show_update_dialog(self, self.pending_update_info)

        def _on_manual_check_update(self):
            self.check_update_btn.configure(state="disabled", text="확인 중...")
            res = []
            def _worker():
                import updater
                from constants import APP_VERSION
                try:
                    info = updater.check_for_updates(timeout=6.0)
                except Exception as e:
                    info = {"has_update": False, "latest_version": APP_VERSION, "error": str(e)}
                res.append(info)

            threading.Thread(target=_worker, daemon=True).start()

            def _poll_manual():
                if not res:
                    self.after(100, _poll_manual)
                    return
                
                info = res[0]
                from constants import APP_VERSION
                try:
                    self.check_update_btn.configure(state="normal", text="🔄 업데이트 확인")
                    if info.get("error"):
                        from tkinter import messagebox
                        messagebox.showwarning("업데이트 확인 실패", f"버전 정보를 확인하는 중 오류가 발생했습니다:\n{info['error']}")
                    elif info.get("has_update"):
                        self.pending_update_info = info
                        self._show_update_banner()
                        import updater
                        updater.show_update_dialog(self, info)
                    else:
                        latest_v = info.get("latest_version") or APP_VERSION
                        from tkinter import messagebox
                        messagebox.showinfo(
                            "업데이트 확인",
                            f"현재 최신 버전({APP_VERSION})을 사용하고 있습니다! 🎉\n\n"
                            f"• 내 프로그램 버전: {APP_VERSION}\n"
                            f"• GitHub 최신 릴리즈: {latest_v}\n\n"
                            "새로운 업데이트가 출시되면 다시 알려드립니다."
                        )
                except Exception as err:
                    import logging
                    logging.error(f"Manual update check error: {err}")
                finally:
                    try:
                        self.check_update_btn.configure(state="normal", text="🔄 업데이트 확인")
                    except Exception:
                        pass

            self.after(100, _poll_manual)

        def _build_screens(self):
            self.home_screen = ctk.CTkFrame(self.screen_container, fg_color="transparent")
            self.select_screen = ctk.CTkFrame(self.screen_container, fg_color="#111118", corner_radius=16, border_width=1, border_color="#23232b")
            self.quick_translate_screen = ctk.CTkFrame(self.screen_container, fg_color="#111118", corner_radius=16, border_width=1, border_color="#23232b")
            self.translate_screen = ctk.CTkFrame(self.screen_container, fg_color="#111118", corner_radius=16, border_width=1, border_color="#23232b")

            self.home_screen.grid_rowconfigure(0, weight=1)
            self.home_screen.grid_columnconfigure(0, weight=1)
            self.select_screen.grid_rowconfigure(3, weight=1)
            self.select_screen.grid_columnconfigure(0, weight=1)

            self._build_home_screen()
            self._build_select_screen()
            self._build_quick_translate_screen()
            self._build_translate_screen()

        def _build_home_screen(self):
            panel = ctk.CTkFrame(self.home_screen, fg_color="#12121a", corner_radius=20, border_width=1, border_color="#2a2a33")
            panel.place(relx=0.5, rely=0.5, anchor="center")

            logo_canvas = tk.Canvas(panel, width=96, height=96, bg="#12121a", highlightthickness=0)
            logo_canvas.pack(pady=(24, 10))
            logo_canvas.create_oval(6, 6, 90, 90, outline="#fb923c", width=2, fill="#0f0f12")
            logo_canvas.create_text(48, 48, text="Q", font=(FONT_NAME, 30, "bold"), fill="#f8fafc")
            logo_canvas.create_oval(58, 24, 72, 38, fill="#f97316", outline="")

            ctk.CTkLabel(panel, text="Quest Translator Pro",
                         font=ctk.CTkFont(family=FONT_NAME, size=24, weight="bold"),
                         text_color="#f8fafc").pack(padx=28, pady=(0, 6))
            ctk.CTkLabel(panel, text="원하는 작업을 선택하세요",
                         font=ctk.CTkFont(family=FONT_NAME, size=12),
                         text_color="#fb923c").pack(padx=28, pady=(0, 18))

            button_row = ctk.CTkFrame(panel, fg_color="transparent")
            button_row.pack(fill="x", padx=24, pady=(0, 10))

            ctk.CTkButton(button_row, text="모드팩 리스트 보기", height=40,
                          fg_color="#f97316", hover_color="#fb923c",
                          font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                          command=self.show_select_screen
                          ).pack(side="left", fill="x", expand=True, padx=(0, 6))

            ctk.CTkButton(button_row, text="파일 번역하기", height=40,
                          fg_color="#b45309", hover_color="#d97706",
                          font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                          command=self.show_quick_translate_screen
                          ).pack(side="right", fill="x", expand=True, padx=(6, 0))

            button_row2 = ctk.CTkFrame(panel, fg_color="transparent")
            button_row2.pack(fill="x", padx=24, pady=(0, 20))

            ctk.CTkButton(button_row2, text="🔍 인게임 오역 수정기", height=40,
                          fg_color="#1d4ed8", hover_color="#2563eb",
                          font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                          command=self.open_memory_editor
                          ).pack(side="left", fill="x", expand=True, padx=(0, 6))

            def open_bmac():
                import webbrowser
                webbrowser.open("https://buymeacoffee.com/teiba")

            ctk.CTkButton(button_row2, text="☕ 개발자에게 커피 사주기", height=40,
                          fg_color="#f59e0b", hover_color="#d97706", text_color="#1e1e1e",
                          font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                          command=open_bmac
                          ).pack(side="right", fill="x", expand=True, padx=(6, 0))

        def _build_select_screen(self):
            # 경로 설정 패널
            path_frame = ctk.CTkFrame(self.select_screen, fg_color="#18181d", corner_radius=14, border_width=1, border_color="#2a2a33")
            path_frame.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))

            ctk.CTkLabel(path_frame, text="인스턴스 경로 설정",
                         font=ctk.CTkFont(family=FONT_NAME, size=14, weight="bold"),
                         text_color="#fed7aa").pack(anchor="w", padx=12, pady=(10, 4))

            self.instance_path_entry = ctk.CTkEntry(
                path_frame,
                placeholder_text="예: C:/Users/사용자/AppData/Roaming/PrismLauncher/instances",
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                fg_color="#111113", border_color="#3f3f46")
            self.instance_path_entry.pack(fill="x", padx=12, pady=(0, 8))
            self.instance_path_entry.bind("<FocusOut>", lambda _e: self.save_user_settings())

            path_btn_row = ctk.CTkFrame(path_frame, fg_color="transparent")
            path_btn_row.pack(fill="x", padx=12, pady=(0, 10))

            self.btn_pick_instance_root = ctk.CTkButton(path_btn_row, text="경로 선택", width=110,
                                                        fg_color="#2a2a33", hover_color="#3f3f46",
                                                        font=ctk.CTkFont(family=FONT_NAME, size=12),
                                                        command=self.pick_instance_root)
            self.btn_pick_instance_root.pack(side="left")

            self.btn_auto_detect_root = ctk.CTkButton(path_btn_row, text="자동 탐색", width=110,
                                                      fg_color="#b45309", hover_color="#d97706",
                                                      font=ctk.CTkFont(family=FONT_NAME, size=12),
                                                      command=self.auto_detect_instance_root)
            self.btn_auto_detect_root.pack(side="left", padx=(6, 0))

            self.btn_rescan_modpacks = ctk.CTkButton(path_btn_row, text="모드팩 탐지", width=110,
                                                     fg_color="#f97316", hover_color="#fb923c",
                                                     font=ctk.CTkFont(family=FONT_NAME, size=12),
                                                     command=self.scan_modpacks_from_entry)
            self.btn_rescan_modpacks.pack(side="left", padx=(6, 0))

            self.btn_open_translate_options = ctk.CTkButton(path_btn_row, text="파일/ZIP 번역", width=140,
                                                            fg_color="#27272a", hover_color="#3f3f46",
                                                            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                                                            command=self.show_quick_translate_screen)
            self.btn_open_translate_options.pack(side="right")
            self.path_action_buttons = [self.btn_pick_instance_root, self.btn_auto_detect_root,
                                        self.btn_rescan_modpacks, self.btn_open_translate_options]

            # 뒤로가기 + 타이틀
            select_top = ctk.CTkFrame(self.select_screen, fg_color="transparent")
            select_top.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 4))

            self.btn_back_from_select = ctk.CTkButton(select_top, text="← 홈으로", width=100,
                                                      fg_color="#3f3f46", hover_color="#52525b",
                                                      font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
                                                      command=self.show_home_screen)
            self.btn_back_from_select.pack(side="left")

            ctk.CTkLabel(select_top, text="탐지된 모드팩",
                         font=ctk.CTkFont(family=FONT_NAME, size=13, weight="bold"),
                         text_color="#e4e4e7").pack(side="left", padx=(10, 0))

            # 검색창
            self.modpack_search_entry = ctk.CTkEntry(
                self.select_screen, placeholder_text="모드팩 검색...",
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                fg_color="#27272a", border_color="#3f3f46", text_color="#f5f5f5")
            self.modpack_search_entry.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 6))
            self.modpack_search_entry.bind("<KeyRelease>", self._on_modpack_search_change)

            # 카드 스크롤
            self.cards_scroller = ctk.CTkScrollableFrame(self.select_screen, height=380, fg_color="#0f1116")
            self.cards_scroller.grid(row=3, column=0, sticky="nsew", padx=14, pady=(0, 8))

            # 선택 상태 + 다음 버튼
            footer = ctk.CTkFrame(self.select_screen, fg_color="transparent")
            footer.grid(row=4, column=0, sticky="ew", padx=14, pady=(0, 10))
            footer.grid_columnconfigure(0, weight=1)

            self.selected_modpack_label = ctk.CTkLabel(footer, text="선택된 모드팩: 없음",
                                                       font=ctk.CTkFont(family=FONT_NAME, size=11),
                                                       text_color="#fdba74")
            self.selected_modpack_label.grid(row=0, column=0, sticky="w")

            self.btn_go_translate = ctk.CTkButton(footer, text="다음: 번역 설정  →", width=170, height=34,
                                                  fg_color="#ea580c", hover_color="#c2410c",
                                                  font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                                                  command=self.show_translate_screen, state="disabled")
            self.btn_go_translate.grid(row=0, column=1, sticky="e", padx=(8, 0))

        def _build_quick_translate_screen(self):
            self.quick_translate_screen.grid_rowconfigure(1, weight=1)
            self.quick_translate_screen.grid_columnconfigure(0, weight=1)

            top = ctk.CTkFrame(self.quick_translate_screen, fg_color="transparent")
            top.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))

            self.btn_back_from_quick = ctk.CTkButton(top, text="← 홈으로", width=130,
                                                     fg_color="#3f3f46", hover_color="#52525b",
                                                     font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
                                                     command=self.show_home_screen)
            self.btn_back_from_quick.pack(side="left")

            ctk.CTkLabel(top, text="파일/ZIP 즉시 번역",
                         font=ctk.CTkFont(family=FONT_NAME, size=13, weight="bold"),
                         text_color="#fdba74").pack(side="left", padx=(10, 0))

            mode_frame = ctk.CTkFrame(self.quick_translate_screen, fg_color="#18181d", corner_radius=14, border_width=1, border_color="#2a2a33")
            mode_frame.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 14))

            ctk.CTkLabel(mode_frame, text="번역 방식 선택",
                         font=ctk.CTkFont(family=FONT_NAME, size=14, weight="bold"),
                         text_color="#fed7aa").pack(anchor="w", padx=12, pady=(12, 8))

            btn_row = ctk.CTkFrame(mode_frame, fg_color="transparent")
            btn_row.pack(fill="x", padx=12, pady=(0, 10))

            self.btn_single = ctk.CTkButton(btn_row, text="단일 파일 번역",
                                            font=ctk.CTkFont(family=FONT_NAME, size=13, weight="bold"),
                                            height=40, fg_color="#b45309", hover_color="#d97706",
                                            command=self.run_single_file)
            self.btn_single.pack(side="left", fill="x", expand=True, padx=(0, 5))

            self.btn_zip = ctk.CTkButton(btn_row, text="ZIP 전체 번역",
                                         font=ctk.CTkFont(family=FONT_NAME, size=13, weight="bold"),
                                         height=40, fg_color="#ea580c", hover_color="#c2410c",
                                         command=self.run_zip_file)
            self.btn_zip.pack(side="right", fill="x", expand=True, padx=(5, 0))
            self.quick_buttons = [self.btn_single, self.btn_zip]

            drop_frame = ctk.CTkFrame(mode_frame, fg_color="#101015", border_color="#f97316", border_width=2, corner_radius=12)
            drop_frame.pack(fill="x", padx=12, pady=(0, 8))
            ctk.CTkLabel(drop_frame, text="파일 또는 ZIP을 끌어다 놓아 즉시 번역",
                         font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                         text_color="#fdba74").pack(anchor="w", padx=12, pady=(8, 0))
            ctk.CTkLabel(drop_frame, text="지원 형식: .snbt .json .hqm .zip",
                         font=ctk.CTkFont(family=FONT_NAME, size=11),
                         text_color="#94a3b8").pack(anchor="w", padx=12, pady=(0, 8))
            drop_frame.drop_target_register(DND_FILES)
            drop_frame.dnd_bind('<<Drop>>', self.handle_file_drop)

        def _build_translate_screen(self):
            self.translate_screen.grid_rowconfigure(2, weight=1)
            self.translate_screen.grid_columnconfigure(0, weight=1)

            top = ctk.CTkFrame(self.translate_screen, fg_color="transparent")
            top.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))

            self.btn_back_to_select = ctk.CTkButton(top, text="← 모드팩 선택으로", width=140,
                                                    fg_color="#3f3f46", hover_color="#52525b",
                                                    font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
                                                    command=self.show_select_screen)
            self.btn_back_to_select.pack(side="left")

            self.translate_selected_label = ctk.CTkLabel(top, text="선택 모드팩: 없음",
                                                         font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                                                         text_color="#fdba74")
            self.translate_selected_label.pack(side="left", padx=(12, 0))

            # 번역 설정 패널
            config_frame = ctk.CTkFrame(self.translate_screen, fg_color="#18181d", corner_radius=14, border_width=1, border_color="#2a2a33")
            config_frame.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 10))

            ctk.CTkLabel(config_frame, text="번역 엔진 / API 설정",
                         font=ctk.CTkFont(family=FONT_NAME, size=14, weight="bold"),
                         text_color="#fed7aa").pack(anchor="w", padx=12, pady=(12, 8))

            ctk.CTkLabel(config_frame, text="타겟 언어 선택",
                         font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                         text_color="#cbd5e1").pack(anchor="w", padx=12, pady=(2, 2))

            self.target_lang_combo = ctk.CTkComboBox(config_frame, values=SUPPORTED_LANGUAGES,
                                                font=ctk.CTkFont(family=FONT_NAME, size=12),
                                                command=self.on_target_lang_change,
                                                fg_color="#27272a", button_color="#10b981", button_hover_color="#059669")
            self.target_lang_combo.pack(fill="x", padx=12, pady=(0, 8))
            self.target_lang_combo.set(SUPPORTED_LANGUAGES[0])

            ctk.CTkLabel(config_frame, text="번역 엔진",
                         font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                         text_color="#cbd5e1").pack(anchor="w", padx=12, pady=(2, 2))

            self.engine_combo = ctk.CTkComboBox(config_frame, values=list(ENGINES.keys()),
                                                font=ctk.CTkFont(family=FONT_NAME, size=12),
                                                command=self.on_engine_change,
                                                fg_color="#27272a", button_color="#b45309", button_hover_color="#d97706")
            self.engine_combo.pack(fill="x", padx=12, pady=(0, 8))
            self.engine_combo.set("Gemini Lite (배치 번역)")

            # --- Standard API Frame ---
            self.standard_api_frame = ctk.CTkFrame(config_frame, fg_color="transparent")
            self.standard_api_frame.pack(fill="x")

            ctk.CTkLabel(self.standard_api_frame, text="Gemini 계정 상태",
                         font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                         text_color="#cbd5e1").pack(anchor="w", padx=12, pady=(2, 2))

            self.plan_combo = ctk.CTkComboBox(self.standard_api_frame,
                                              values=["유료 계정 (Pay-as-you-go / 초고속 / 제한없음)",
                                                      "무료 계정 (안전대기 / 10 RPM 속도제한)"],
                                              font=ctk.CTkFont(family=FONT_NAME, size=12),
                                              command=self.on_plan_change,
                                              fg_color="#27272a", button_color="#b45309", button_hover_color="#d97706")
            self.plan_combo.pack(fill="x", padx=12, pady=(0, 8))
            self.plan_combo.set("유료 계정 (Pay-as-you-go / 초고속 / 제한없음)")

            self.model_label = ctk.CTkLabel(self.standard_api_frame, text="AI 모델 선택",
                         font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                         text_color="#cbd5e1")
            self.model_label.pack(anchor="w", padx=12, pady=(2, 2))

            self.model_combo = ctk.CTkComboBox(self.standard_api_frame,
                                              values=MODELS_GEMINI_PAID,
                                              font=ctk.CTkFont(family=FONT_NAME, size=12),
                                              fg_color="#27272a", button_color="#b45309", button_hover_color="#d97706")
            self.model_combo.pack(fill="x", padx=12, pady=(0, 8))
            self.model_combo.set(MODELS_GEMINI_PAID[0])

            ctk.CTkLabel(self.standard_api_frame, text="API 키",
                         font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                         text_color="#cbd5e1").pack(anchor="w", padx=12, pady=(2, 2))

            api_row = ctk.CTkFrame(self.standard_api_frame, fg_color="transparent")
            api_row.pack(fill="x", padx=12, pady=(0, 10))

            self.api_entry = ctk.CTkEntry(api_row, show="*", placeholder_text="API 키를 입력하세요",
                                          font=ctk.CTkFont(family=FONT_NAME, size=12),
                                          fg_color="#111113", border_color="#52525b")
            self.api_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
            self.api_entry.bind("<FocusOut>", lambda _e: self.save_user_settings())

            self.show_btn = ctk.CTkButton(api_row, text="보기", width=70,
                                          font=ctk.CTkFont(family=FONT_NAME, size=12),
                                          fg_color="#3f3f46", hover_color="#52525b",
                                          command=self.toggle_api_visibility)
            self.show_btn.pack(side="right")

            # --- Local API Frame ---
            self.local_api_frame = ctk.CTkFrame(config_frame, fg_color="transparent")
            # Initially not packed!

            # 1. 커스텀 API 주소 (Base URL)
            ctk.CTkLabel(self.local_api_frame, text="커스텀 API 주소 (Base URL)",
                         font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                         text_color="#cbd5e1").pack(anchor="w", padx=12, pady=(2, 2))
            
            self.local_url_entry = ctk.CTkEntry(
                self.local_api_frame,
                placeholder_text="예: http://localhost:1234/v1 (LM Studio) 또는 http://localhost:11434/v1 (Ollama)",
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                fg_color="#111113", border_color="#52525b"
            )
            self.local_url_entry.pack(fill="x", padx=12, pady=(0, 6))
            self.local_url_entry.bind("<FocusOut>", lambda _e: self.save_user_settings())

            # 2. 모델 이름 (필수)
            ctk.CTkLabel(self.local_api_frame, text="모델 이름 (필수)",
                         font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                         text_color="#cbd5e1").pack(anchor="w", padx=12, pady=(2, 2))

            self.local_model_entry = ctk.CTkEntry(
                self.local_api_frame,
                placeholder_text="예: llama3.1, qwen2.5:14b, deepseek-chat (LM Studio/Ollama에 로드된 모델명)",
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                fg_color="#111113", border_color="#52525b"
            )
            self.local_model_entry.pack(fill="x", padx=12, pady=(0, 6))
            self.local_model_entry.bind("<FocusOut>", lambda _e: self.save_user_settings())

            # 3. API 키 (선택사항)
            ctk.CTkLabel(self.local_api_frame, text="API 키 (선택사항, 로컬 AI는 비워두세요)",
                         font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                         text_color="#cbd5e1").pack(anchor="w", padx=12, pady=(2, 2))

            self.local_api_key_entry = ctk.CTkEntry(
                self.local_api_frame, show="*",
                placeholder_text="LM Studio / Ollama는 비워두세요 (클라우드 API만 필요 시 입력)",
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                fg_color="#111113", border_color="#52525b"
            )
            self.local_api_key_entry.pack(fill="x", padx=12, pady=(0, 6))
            self.local_api_key_entry.bind("<FocusOut>", lambda _e: self.save_user_settings())

            # 안내 팁 레이블
            ctk.CTkLabel(
                self.local_api_frame,
                text="💡 LM Studio / Ollama 등 로컬 AI 사용 시 API 키는 입력하지 않아도 됩니다.",
                font=ctk.CTkFont(family=FONT_NAME, size=11),
                text_color="#94a3b8"
            ).pack(anchor="w", padx=12, pady=(0, 4))

            self.btn_translate_selected_modpack = ctk.CTkButton(
                config_frame, text="선택 모드팩 퀘스트 번역",
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                height=36, fg_color="#f97316", hover_color="#fb923c",
                command=self.run_selected_modpack)
            self.btn_translate_selected_modpack.pack(fill="x", padx=12, pady=(12, 4))
            
            self.btn_translate_all_guidebooks = ctk.CTkButton(
                config_frame, text="모드팩 전체 한글화 (가이드북 & 아이템 리소스팩 생성)",
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                height=36, fg_color="#10b981", hover_color="#059669",
                command=self.run_all_modpack_translations)
            self.btn_translate_all_guidebooks.pack(fill="x", padx=12, pady=(0, 6))

            self.btn_edit_glossary = ctk.CTkButton(
                config_frame, text="📖 용어집 편집 (Glossary)",
                font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
                height=30, fg_color="#3f3f46", hover_color="#52525b",
                command=self.open_glossary_editor)
            self.btn_edit_glossary.pack(fill="x", padx=12, pady=(0, 12))

            # 로그 + 진행률 패널
            log_frame = ctk.CTkFrame(self.translate_screen, fg_color="#101015", corner_radius=14, border_width=1, border_color="#2a2a33")
            log_frame.grid(row=2, column=0, sticky="nsew", padx=14, pady=(0, 14))

            prog_row = ctk.CTkFrame(log_frame, fg_color="transparent")
            prog_row.pack(fill="x", padx=12, pady=(12, 4))

            self.progress = ctk.CTkProgressBar(prog_row, fg_color="#27272a", progress_color="#f97316")
            self.progress.pack(side="left", fill="x", expand=True, padx=(0, 10))
            self.progress.set(0)

            self.btn_cancel = ctk.CTkButton(prog_row, text="작업 취소", width=90, height=30,
                                            fg_color="#b91c1c", hover_color="#7f1d1d",
                                            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                                            command=self.request_cancel, state="disabled")
            self.btn_cancel.pack(side="right")

            self.status_label = ctk.CTkLabel(log_frame, text="", anchor="w",
                                             font=ctk.CTkFont(family=FONT_NAME, size=12),
                                             text_color="#fdba74")
            self.status_label.pack(fill="x", padx=12, pady=(0, 4))

            log_text_frame = tk.Frame(log_frame, bg="#101012")
            log_text_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
            log_scrollbar = tk.Scrollbar(log_text_frame)
            log_scrollbar.pack(side="right", fill="y")

            self.log_textbox = tk.Text(log_text_frame, font=(FONT_NAME, 11),
                                       bg="#101012", fg="#e5e7eb",
                                       insertbackground="#e5e7eb", selectbackground="#ea580c",
                                       selectforeground="#ffffff", relief="flat",
                                       borderwidth=0, highlightthickness=0, wrap="word",
                                       yscrollcommand=log_scrollbar.set)
            self.log_textbox.pack(side="left", fill="both", expand=True)
            log_scrollbar.config(command=self.log_textbox.yview)
            self.log_textbox.configure(state="disabled")

        # ====================================================================
        # 생명주기 & 설정 저장
        # ====================================================================

        def _get_settings_path(self):
            appdata_dir = os.getenv("APPDATA") or os.path.expanduser("~")
            return os.path.join(appdata_dir, "QuestTranslatorPro", SETTINGS_FILE_NAME)

        def load_user_settings(self):
            if not os.path.isfile(self.settings_path):
                return
            try:
                with open(self.settings_path, "r", encoding="utf-8") as sf:
                    settings = json.load(sf)
            except Exception:
                return
            saved_root = settings.get("instance_root", "")
            if saved_root:
                self.instance_path_entry.delete(0, "end")
                self.instance_path_entry.insert(0, saved_root)
            saved_api_key = settings.get("api_key", "")
            if saved_api_key:
                self.api_entry.delete(0, "end")
                self.api_entry.insert(0, saved_api_key)
            
            saved_local_url = settings.get("local_url", "")
            if saved_local_url:
                self.local_url_entry.delete(0, "end")
                self.local_url_entry.insert(0, saved_local_url)
                
            saved_local_api_key = settings.get("local_api_key", "")
            if saved_local_api_key:
                self.local_api_key_entry.delete(0, "end")
                self.local_api_key_entry.insert(0, saved_local_api_key)
            
            saved_local_model = settings.get("local_model", "")
            if saved_local_model:
                self.local_model_entry.delete(0, "end")
                self.local_model_entry.insert(0, saved_local_model)
            
            saved_model = settings.get("ai_model", "")
            if saved_model:
                self.model_combo.set(saved_model)
                
            saved_lang = settings.get("target_lang", "")
            if saved_lang:
                self.target_lang_combo.set(saved_lang)

            saved_engine = settings.get("engine_name", "")
            if saved_engine:
                self.engine_combo.set(saved_engine)
                # trigger UI update
                if hasattr(self, "on_engine_change"):
                    self.on_engine_change(saved_engine)

            saved_plan = settings.get("plan_name", "")
            if saved_plan:
                if "Gemini" in saved_engine:
                    self.plan_combo.set(saved_plan)
                    if hasattr(self, "on_plan_change"):
                        self.on_plan_change(saved_plan)
                
            self.app_state.translated_history = settings.get("translated_history", {})
            self.app_state.glossaries_by_lang = settings.get("glossaries_by_lang", {})
            
            # Migrate old single glossary format if present
            if "glossary" in settings and not self.app_state.glossaries_by_lang:
                self.app_state.glossaries_by_lang["한국어 (Korean)"] = settings.get("glossary")

            self._sync_glossary_to_current_lang()

        def _sync_glossary_to_current_lang(self):
            lang = self.target_lang_combo.get()
            self.app_state.sync_glossary(lang)

        def save_user_settings(self):
            try:
                os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
                settings = {
                    "instance_root": self.instance_path_entry.get().strip(),
                    "api_key": self.api_entry.get().strip(),
                    "local_url": self.local_url_entry.get().strip(),
                    "local_api_key": self.local_api_key_entry.get().strip(),
                    "local_model": self.local_model_entry.get().strip(),
                    "ai_model": self.model_combo.get(),
                    "target_lang": self.target_lang_combo.get(),
                    "engine_name": self.engine_combo.get(),
                    "plan_name": self.plan_combo.get() if hasattr(self, 'plan_combo') else "",
                    "translated_history": self.app_state.translated_history,
                    "glossaries_by_lang": self.app_state.glossaries_by_lang
                }
                with open(self.settings_path, "w", encoding="utf-8") as sf:
                    json.dump(settings, sf, ensure_ascii=False, indent=2)
            except Exception as e:
                import logging
                logging.error(f"설정 저장 실패: {e}")

        def on_close(self):
            try:
                self.save_user_settings()
                translation_memory.save_memory()
                if not self.do_backup_on_close():
                    return  # 사용자가 종료를 취소함
            except Exception:
                pass
            finally:
                try:
                    self.destroy()
                except Exception:
                    pass
                import os
                os._exit(0)

else:
    class QuestTranslatorApp:
        def __init__(self):
            raise RuntimeError("GUI 라이브러리가 설치되지 않아 앱을 초기화할 수 없습니다.")

# ==============================================================================
# 🚀 진입점
# ==============================================================================
if __name__ == "__main__":
    app = QuestTranslatorApp()
    app.mainloop()

