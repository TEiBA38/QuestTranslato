import os
import zipfile
import json
import shutil
import threading
import tempfile
import importlib
import hashlib
import urllib.parse
import urllib.request
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

try:
    Image = importlib.import_module("PIL.Image")
    ImageTk = importlib.import_module("PIL.ImageTk")
except Exception:  # pragma: no cover - Pillow optional
    Image = None
    ImageTk = None

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
TARGET_EXTENSIONS = ('.snbt', '.json', '.lang', '.hqm')
SCAN_IGNORE_DIRS = {
    '.git', '.venv', '__pycache__',
    'logs', 'saves', 'resourcepacks', 'shaderpacks',
    'screenshots', 'crash-reports', 'backups',
}
SCAN_EXCLUDE_CANDIDATE_DIRS = {
    'translation_output',
    'install',
}
THUMBNAIL_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".ico")
SETTINGS_FILE_NAME = "settings.json"

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

        # ====================================================================
        # [섹션 1] 기본 설정 & 생명주기
        # ====================================================================
        def _setup_ui(self):
            self.title("Quest Translator Pro")
            self.geometry("1120x760")
            self.minsize(760, 560)
            self.resizable(True, True)
            self.configure(fg_color="#0b0b0f")
            self.settings_path = self._get_settings_path()

            self.cancel_requested = False
            self.scan_thread_active = False
            self.protocol("WM_DELETE_WINDOW", self.on_close)

            self.grid_rowconfigure(1, weight=1)
            self.grid_columnconfigure(0, weight=1)

            self.hero_frame = ctk.CTkFrame(self, fg_color="#121217", corner_radius=18, border_width=1, border_color="#23232b")
            self.hero_frame.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 10))
            self.hero_frame.grid_columnconfigure(0, weight=1)

            self.hero_title_label = ctk.CTkLabel(
                self.hero_frame,
                text="Quest Translator Pro",
                font=ctk.CTkFont(family=FONT_NAME, size=26, weight="bold"),
                text_color="#f8fafc"
            )
            self.hero_title_label.grid(row=0, column=0, sticky="w", padx=16, pady=(12, 0))
            self.phase_label = ctk.CTkLabel(
                self.hero_frame,
                text="STEP 1/2 · 모드팩 선택",
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                text_color="#fb923c"
            )
            self.phase_label.grid(row=1, column=0, sticky="w", padx=16, pady=(2, 12))

            self.screen_container = ctk.CTkFrame(self, fg_color="transparent")
            self.screen_container.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 14))
            self.screen_container.grid_rowconfigure(0, weight=1)
            self.screen_container.grid_columnconfigure(0, weight=1)

            self.home_screen = ctk.CTkFrame(self.screen_container, fg_color="transparent")
            self.select_screen = ctk.CTkFrame(self.screen_container, fg_color="#111118", corner_radius=16, border_width=1, border_color="#23232b")
            self.quick_translate_screen = ctk.CTkFrame(self.screen_container, fg_color="#111118", corner_radius=16, border_width=1, border_color="#23232b")
            self.translate_screen = ctk.CTkFrame(self.screen_container, fg_color="#111118", corner_radius=16, border_width=1, border_color="#23232b")

            self.home_screen.grid_rowconfigure(0, weight=1)
            self.home_screen.grid_columnconfigure(0, weight=1)
            self.select_screen.grid_rowconfigure(3, weight=1)
            self.select_screen.grid_columnconfigure(0, weight=1)

            self.home_panel = ctk.CTkFrame(self.home_screen, fg_color="#12121a", corner_radius=20, border_width=1, border_color="#2a2a33")
            self.home_panel.place(relx=0.5, rely=0.5, anchor="center")

            logo_canvas = tk.Canvas(self.home_panel, width=96, height=96, bg="#12121a", highlightthickness=0)
            logo_canvas.pack(pady=(24, 10))
            logo_canvas.create_oval(6, 6, 90, 90, outline="#fb923c", width=2, fill="#0f0f12")
            logo_canvas.create_text(48, 48, text="Q", font=(FONT_NAME, 30, "bold"), fill="#f8fafc")
            logo_canvas.create_oval(58, 24, 72, 38, fill="#f97316", outline="")

            ctk.CTkLabel(
                self.home_panel,
                text="Quest Translator Pro",
                font=ctk.CTkFont(family=FONT_NAME, size=24, weight="bold"),
                text_color="#f8fafc",
            ).pack(padx=28, pady=(0, 6))

            ctk.CTkLabel(
                self.home_panel,
                text="원하는 작업을 선택하세요",
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                text_color="#fb923c",
            ).pack(padx=28, pady=(0, 18))

            button_row = ctk.CTkFrame(self.home_panel, fg_color="transparent")
            button_row.pack(fill="x", padx=24, pady=(0, 20))

            self.btn_home_modpacks = ctk.CTkButton(
                button_row,
                text="모드팩 리스트 보기",
                height=40,
                fg_color="#f97316",
                hover_color="#fb923c",
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                command=self.show_select_screen,
            )
            self.btn_home_modpacks.pack(side="left", fill="x", expand=True, padx=(0, 6))

            self.btn_home_translate = ctk.CTkButton(
                button_row,
                text="파일 번역하기",
                height=40,
                fg_color="#b45309",
                hover_color="#d97706",
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                command=self.show_quick_translate_screen,
            )
            self.btn_home_translate.pack(side="right", fill="x", expand=True, padx=(6, 0))

            self.path_frame = ctk.CTkFrame(self.select_screen, fg_color="#18181d", corner_radius=14, border_width=1, border_color="#2a2a33")
            self.path_frame.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))

            ctk.CTkLabel(
                self.path_frame,
                text="인스턴스 경로 설정",
                font=ctk.CTkFont(family=FONT_NAME, size=14, weight="bold"),
                text_color="#fed7aa"
            ).pack(anchor="w", padx=12, pady=(10, 4))

            self.instance_path_entry = ctk.CTkEntry(
                self.path_frame,
                placeholder_text="예: C:/Users/사용자/AppData/Roaming/PrismLauncher/instances",
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                fg_color="#111113",
                border_color="#3f3f46"
            )
            self.instance_path_entry.pack(fill="x", padx=12, pady=(0, 8))
            self.instance_path_entry.bind("<FocusOut>", lambda _e: self.save_user_settings())

            path_btn_row = ctk.CTkFrame(self.path_frame, fg_color="transparent")
            path_btn_row.pack(fill="x", padx=12, pady=(0, 10))

            self.btn_pick_instance_root = ctk.CTkButton(
                path_btn_row,
                text="경로 선택",
                width=110,
                fg_color="#2a2a33",
                hover_color="#3f3f46",
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                command=self.pick_instance_root,
            )
            self.btn_pick_instance_root.pack(side="left")

            self.btn_auto_detect_root = ctk.CTkButton(
                path_btn_row,
                text="자동 탐색",
                width=110,
                fg_color="#b45309",
                hover_color="#d97706",
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                command=self.auto_detect_instance_root,
            )
            self.btn_auto_detect_root.pack(side="left", padx=(6, 0))

            self.btn_rescan_modpacks = ctk.CTkButton(
                path_btn_row,
                text="모드팩 탐지",
                width=110,
                fg_color="#f97316",
                hover_color="#fb923c",
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                command=self.scan_modpacks_from_entry,
            )
            self.btn_rescan_modpacks.pack(side="left", padx=(6, 0))

            self.btn_open_translate_options = ctk.CTkButton(
                path_btn_row,
                text="파일/ZIP 번역",
                width=140,
                fg_color="#27272a",
                hover_color="#3f3f46",
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                command=self.show_quick_translate_screen,
            )
            self.btn_open_translate_options.pack(side="right")
            self.path_btn_row = path_btn_row
            self.path_action_buttons = [
                self.btn_pick_instance_root,
                self.btn_auto_detect_root,
                self.btn_rescan_modpacks,
                self.btn_open_translate_options,
            ]

            self.quick_translate_screen.grid_rowconfigure(1, weight=1)
            self.quick_translate_screen.grid_columnconfigure(0, weight=1)

            quick_top = ctk.CTkFrame(self.quick_translate_screen, fg_color="transparent")
            quick_top.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))

            self.btn_back_from_quick = ctk.CTkButton(
                quick_top,
                text="← 모드 선택으로",
                width=130,
                fg_color="#3f3f46",
                hover_color="#52525b",
                font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
                command=self.show_select_screen,
            )
            self.btn_back_from_quick.pack(side="left")

            ctk.CTkLabel(
                quick_top,
                text="파일/ZIP 즉시 번역",
                font=ctk.CTkFont(family=FONT_NAME, size=13, weight="bold"),
                text_color="#fdba74"
            ).pack(side="left", padx=(10, 0))

            self.mode_frame = ctk.CTkFrame(self.quick_translate_screen, fg_color="#18181d", corner_radius=14, border_width=1, border_color="#2a2a33")
            self.mode_frame.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 14))

            ctk.CTkLabel(
                self.mode_frame,
                text="번역 방식 선택",
                font=ctk.CTkFont(family=FONT_NAME, size=14, weight="bold"),
                text_color="#fed7aa"
            ).pack(anchor="w", padx=12, pady=(12, 8))

            self.btn_sub_frame = ctk.CTkFrame(self.mode_frame, fg_color="transparent")
            self.btn_sub_frame.pack(fill="x", padx=12, pady=(0, 10))

            self.btn_single = ctk.CTkButton(
                self.btn_sub_frame,
                text="단일 파일 번역",
                font=ctk.CTkFont(family=FONT_NAME, size=13, weight="bold"),
                height=40,
                fg_color="#b45309",
                hover_color="#d97706",
                command=self.run_single_file
            )
            self.btn_single.pack(side="left", fill="x", expand=True, padx=(0, 5))

            self.btn_zip = ctk.CTkButton(
                self.btn_sub_frame,
                text="ZIP 전체 번역",
                font=ctk.CTkFont(family=FONT_NAME, size=13, weight="bold"),
                height=40,
                fg_color="#ea580c",
                hover_color="#c2410c",
                command=self.run_zip_file
            )
            self.btn_zip.pack(side="right", fill="x", expand=True, padx=(5, 0))
            self.quick_buttons = [self.btn_single, self.btn_zip]

            self.drop_frame = ctk.CTkFrame(self.mode_frame, fg_color="#101015", border_color="#f97316", border_width=2, corner_radius=12)
            self.drop_frame.pack(fill="x", padx=12, pady=(0, 8))
            ctk.CTkLabel(
                self.drop_frame,
                text="파일 또는 ZIP을 끌어다 놓아 즉시 번역",
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                text_color="#fdba74"
            ).pack(anchor="w", padx=12, pady=(8, 0))
            ctk.CTkLabel(
                self.drop_frame,
                text="지원 형식: .snbt .json .hqm .zip",
                font=ctk.CTkFont(family=FONT_NAME, size=11),
                text_color="#94a3b8"
            ).pack(anchor="w", padx=12, pady=(0, 8))
            self.drop_frame.drop_target_register(DND_FILES)
            self.drop_frame.dnd_bind('<<Drop>>', self.handle_file_drop)

            select_top = ctk.CTkFrame(self.select_screen, fg_color="transparent")
            select_top.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))

            self.btn_back_from_select = ctk.CTkButton(
                select_top,
                text="← 홈으로",
                width=100,
                fg_color="#3f3f46",
                hover_color="#52525b",
                font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
                command=self.show_home_screen,
            )
            self.btn_back_from_select.pack(side="left")

            ctk.CTkLabel(
                self.select_screen,
                text="탐지된 모드팩",
                font=ctk.CTkFont(family=FONT_NAME, size=13, weight="bold"),
                text_color="#e4e4e7"
            ).grid(row=1, column=0, sticky="w", padx=16, pady=(0, 4))

            self.modpack_search_entry = ctk.CTkEntry(
                self.select_screen,
                placeholder_text="모드팩 검색...",
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                fg_color="#27272a",
                border_color="#3f3f46",
                text_color="#f5f5f5",
            )
            self.modpack_search_entry.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 6))
            self.modpack_search_entry.bind("<KeyRelease>", self._on_modpack_search_change)
            self.modpack_search_entry.bind("<KeyRelease>", self._on_modpack_search_change)

            self.cards_scroller = ctk.CTkScrollableFrame(self.select_screen, height=380, fg_color="#0f1116")
            self.cards_scroller.grid(row=3, column=0, sticky="nsew", padx=14, pady=(0, 8))

            select_footer = ctk.CTkFrame(self.select_screen, fg_color="transparent")
            select_footer.grid(row=4, column=0, sticky="ew", padx=14, pady=(0, 14))
            select_footer.grid_columnconfigure(0, weight=1)

            self.selected_modpack_label = ctk.CTkLabel(
                select_footer,
                text="선택된 모드팩: 없음",
                font=ctk.CTkFont(family=FONT_NAME, size=11),
                text_color="#fdba74"
            )
            self.selected_modpack_label.grid(row=0, column=0, sticky="w")

            self.btn_go_translate = ctk.CTkButton(
                select_footer,
                text="다음: 번역 설정",
                width=170,
                height=34,
                fg_color="#ea580c",
                hover_color="#c2410c",
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                command=self.show_translate_screen,
                state="disabled",
            )
            self.btn_go_translate.grid(row=0, column=1, sticky="e")

            self.translate_screen.grid_rowconfigure(2, weight=1)
            self.translate_screen.grid_columnconfigure(0, weight=1)

            top_translate = ctk.CTkFrame(self.translate_screen, fg_color="transparent")
            top_translate.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))

            self.btn_back_to_select = ctk.CTkButton(
                top_translate,
                text="← 모드팩 선택으로",
                width=140,
                fg_color="#3f3f46",
                hover_color="#52525b",
                font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
                command=self.show_select_screen,
            )
            self.btn_back_to_select.pack(side="left")

            self.translate_selected_label = ctk.CTkLabel(
                top_translate,
                text="선택 모드팩: 없음",
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                text_color="#fdba74"
            )
            self.translate_selected_label.pack(side="left", padx=(12, 0))

            self.config_frame = ctk.CTkFrame(self.translate_screen, fg_color="#18181d", corner_radius=14, border_width=1, border_color="#2a2a33")
            self.config_frame.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 10))

            ctk.CTkLabel(
                self.config_frame, text="번역 엔진 / API 설정",
                font=ctk.CTkFont(family=FONT_NAME, size=14, weight="bold"),
                text_color="#fed7aa"
            ).pack(anchor="w", padx=12, pady=(12, 8))

            ctk.CTkLabel(
                self.config_frame, text="번역 엔진",
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                text_color="#cbd5e1"
            ).pack(anchor="w", padx=12, pady=(2, 2))

            self.engine_combo = ctk.CTkComboBox(
                self.config_frame,
                values=list(ENGINES.keys()),
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                command=self.on_engine_change,
                fg_color="#27272a",
                button_color="#b45309",
                button_hover_color="#d97706",
            )
            self.engine_combo.pack(fill="x", padx=12, pady=(0, 8))
            self.engine_combo.set("Gemini Lite (배치 번역)")

            ctk.CTkLabel(
                self.config_frame, text="Gemini 계정 상태",
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                text_color="#cbd5e1"
            ).pack(anchor="w", padx=12, pady=(2, 2))

            self.plan_combo = ctk.CTkComboBox(
                self.config_frame,
                values=["유료 계정 (Pay-as-you-go / 초고속 / 제한없음)", "무료 계정 (안전대기 / 10 RPM 속도제한)"],
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                fg_color="#27272a",
                button_color="#b45309",
                button_hover_color="#d97706",
            )
            self.plan_combo.pack(fill="x", padx=12, pady=(0, 8))
            self.plan_combo.set("유료 계정 (Pay-as-you-go / 초고속 / 제한없음)")

            ctk.CTkLabel(
                self.config_frame, text="API 키",
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                text_color="#cbd5e1"
            ).pack(anchor="w", padx=12, pady=(2, 2))

            self.api_sub_frame = ctk.CTkFrame(self.config_frame, fg_color="transparent")
            self.api_sub_frame.pack(fill="x", padx=12, pady=(0, 10))

            self.api_entry = ctk.CTkEntry(
                self.api_sub_frame,
                show="*",
                placeholder_text="API 키를 입력하세요",
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                fg_color="#111113",
                border_color="#52525b"
            )
            self.api_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
            self.api_entry.bind("<FocusOut>", lambda _e: self.save_user_settings())

            self.show_btn = ctk.CTkButton(
                self.api_sub_frame,
                text="보기",
                width=70,
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                fg_color="#3f3f46",
                hover_color="#52525b",
                command=self.toggle_api_visibility
            )
            self.show_btn.pack(side="right")

            action_row = ctk.CTkFrame(self.config_frame, fg_color="transparent")
            action_row.pack(fill="x", padx=12, pady=(4, 12))

            self.btn_translate_selected_modpack = ctk.CTkButton(
                action_row,
                text="선택 모드팩 자동 번역",
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                height=36,
                fg_color="#f97316",
                hover_color="#fb923c",
                command=self.run_selected_modpack,
            )
            self.btn_translate_selected_modpack.pack(fill="x")

            self.log_frame = ctk.CTkFrame(self.translate_screen, fg_color="#101015", corner_radius=14, border_width=1, border_color="#2a2a33")
            self.log_frame.grid(row=2, column=0, sticky="nsew", padx=14, pady=(0, 14))

            self.progress_sub_frame = ctk.CTkFrame(self.log_frame, fg_color="transparent")
            self.progress_sub_frame.pack(fill="x", padx=12, pady=(12, 4))

            self.progress = ctk.CTkProgressBar(self.progress_sub_frame, fg_color="#27272a", progress_color="#f97316")
            self.progress.pack(side="left", fill="x", expand=True, padx=(0, 10))
            self.progress.set(0)

            self.btn_cancel = ctk.CTkButton(
                self.progress_sub_frame,
                text="작업 취소",
                width=90,
                height=30,
                fg_color="#b91c1c",
                hover_color="#7f1d1d",
                font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
                command=self.request_cancel,
                state="disabled"
            )
            self.btn_cancel.pack(side="right")

            self.status_label = ctk.CTkLabel(
                self.log_frame,
                text="",
                anchor="w",
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                text_color="#fdba74"
            )
            self.status_label.pack(fill="x", padx=12, pady=(0, 4))

            log_text_frame = tk.Frame(self.log_frame, bg="#101012")
            log_text_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
            log_scrollbar = tk.Scrollbar(log_text_frame)
            log_scrollbar.pack(side="right", fill="y")

            self.log_textbox = tk.Text(
                log_text_frame,
                font=(FONT_NAME, 11),
                bg="#101012",
                fg="#e5e7eb",
                insertbackground="#e5e7eb",
                selectbackground="#ea580c",
                selectforeground="#ffffff",
                relief="flat",
                borderwidth=0,
                highlightthickness=0,
                wrap="word",
                yscrollcommand=log_scrollbar.set,
            )
            self.log_textbox.pack(side="left", fill="both", expand=True)
            log_scrollbar.config(command=self.log_textbox.yview)
            self.log_textbox.configure(state="disabled")

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

        def _get_settings_path(self):
            appdata_dir = os.getenv("APPDATA") or os.path.expanduser("~")
            settings_dir = os.path.join(appdata_dir, "QuestTranslatorPro")
            return os.path.join(settings_dir, SETTINGS_FILE_NAME)

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

        def save_user_settings(self):
            try:
                os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
                settings = {
                    "instance_root": self.instance_path_entry.get().strip(),
                    "api_key": self.api_entry.get().strip(),
                }
                with open(self.settings_path, "w", encoding="utf-8") as sf:
                    json.dump(settings, sf, ensure_ascii=False, indent=2)
            except Exception:
                pass

        def on_close(self):
            self.save_user_settings()
            self.destroy()

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
            if hasattr(self, 'btn_pick_instance_root'):
                self.btn_pick_instance_root.configure(state=btn_state)
            if hasattr(self, 'btn_auto_detect_root'):
                self.btn_auto_detect_root.configure(state=btn_state)
            if hasattr(self, 'btn_rescan_modpacks'):
                self.btn_rescan_modpacks.configure(state=btn_state)
            if hasattr(self, 'btn_open_translate_options'):
                self.btn_open_translate_options.configure(state=btn_state)
            if hasattr(self, 'btn_translate_selected_modpack'):
                self.btn_translate_selected_modpack.configure(state=btn_state)
            if hasattr(self, 'btn_go_translate'):
                if state and not self.selected_modpack_path:
                    self.btn_go_translate.configure(state="disabled")
                else:
                    self.btn_go_translate.configure(state=btn_state)
            if hasattr(self, 'btn_back_to_select'):
                self.btn_back_to_select.configure(state=btn_state)
            if hasattr(self, 'btn_back_from_quick'):
                self.btn_back_from_quick.configure(state=btn_state)
            if hasattr(self, 'btn_cancel'):
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

        def _count_translatable_files(self, base_dir):
            count = 0
            for _, dirs, files_list in os.walk(base_dir):
                dirs[:] = [d for d in dirs if d.lower() not in SCAN_IGNORE_DIRS]
                for filename in files_list:
                    if filename.lower().endswith(TARGET_EXTENSIONS):
                        count += 1
            return count

        def _scan_modpack_candidates(self, instance_root):
            candidates = []

            for entry in os.scandir(instance_root):
                if not entry.is_dir():
                    continue
                name_lower = entry.name.lower()
                if name_lower in SCAN_IGNORE_DIRS:
                    continue
                if name_lower in SCAN_EXCLUDE_CANDIDATE_DIRS:
                    continue
                file_count = self._count_translatable_files(entry.path)
                if file_count > 0:
                    candidates.append((entry.name, entry.path, file_count))

            candidates.sort(key=lambda item: (-item[2], item[0].lower()))
            return candidates

        def _set_modpack_candidates(self, candidates):
            self.detected_modpacks = {}
            self.modpack_entries = list(candidates)
            self.selected_modpack_path = None
            if not candidates:
                self.modpack_search_entry.delete(0, "end")
                self.detected_modpacks = {}
                self.render_modpack_cards([])
                return

            self.detected_modpacks = {}
            for name, path, file_count in candidates:
                self.detected_modpacks[path] = (name, file_count)

            self.modpack_search_entry.delete(0, "end")
            self.render_modpack_cards(candidates)

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

            ctk.CTkLabel(
                panel,
                text="Quest Translator Pro",
                font=ctk.CTkFont(family=FONT_NAME, size=24, weight="bold"),
                text_color="#f5f5f5",
            ).pack(padx=28, pady=(2, 4))

            ctk.CTkLabel(
                panel,
                text="Minecraft 모드팩 번역을 더 부드럽게",
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                text_color="#fb923c",
            ).pack(padx=28, pady=(0, 8))

            self.startup_status_label = ctk.CTkLabel(
                panel,
                text="초기 환경을 준비하고 있습니다...",
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                text_color="#cbd5e1",
            )
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

        def _get_card_columns(self):
            width = max(self.cards_scroller.winfo_width(), self.winfo_width())
            if width >= 1500:
                return 6
            if width >= 1200:
                return 5
            if width >= 980:
                return 4
            if width >= 760:
                return 3
            return 2

        def show_launcher_setup_screen(self):
            self.show_select_screen()

        def show_launcher_modpack_screen(self):
            self.show_translate_screen()

        def select_modpack_from_path(self, modpack_dir):
            if modpack_dir and modpack_dir in self.detected_modpacks:
                self.selected_modpack_path = modpack_dir
                self.selected_modpack_label.configure(text=f"선택된 모드팩: {os.path.basename(modpack_dir)}")
                self.translate_selected_label.configure(text=f"선택 모드팩: {os.path.basename(modpack_dir)}")
                self.log(f"✅ 모드팩 선택됨: {os.path.basename(modpack_dir)}")
                self.btn_go_translate.configure(state="normal")
            else:
                self.selected_modpack_path = None
                self.selected_modpack_label.configure(text="선택된 모드팩: 없음")
                self.translate_selected_label.configure(text="선택 모드팩: 없음")
                self.btn_go_translate.configure(state="disabled")

        def _on_modpack_search_change(self, event=None):
            search_text = self.modpack_search_entry.get().strip().lower()
            filtered = [
                (name, path, count) for name, path, count in self.modpack_entries
                if search_text in name.lower() or search_text in path.lower()
            ] if search_text else self.modpack_entries
            self.render_modpack_cards(filtered)

        def _get_thumbnail_path(self, modpack_dir):
            cached = self.thumbnail_path_cache.get(modpack_dir)
            if cached:
                if os.path.isfile(cached):
                    return cached
                self.thumbnail_path_cache.pop(modpack_dir, None)

            base_names = ("thumbnail", "icon", "pack", "logo", "profile")
            candidate_dirs = [
                modpack_dir,
                os.path.join(modpack_dir, ".minecraft"),
                os.path.join(modpack_dir, "overrides"),
            ]

            for base_dir in candidate_dirs:
                if not os.path.isdir(base_dir):
                    continue
                for base_name in base_names:
                    for ext in THUMBNAIL_EXTENSIONS:
                        full_path = os.path.join(base_dir, f"{base_name}{ext}")
                        if os.path.isfile(full_path):
                            self.thumbnail_path_cache[modpack_dir] = full_path
                            return full_path

            icon_key = self._read_instance_icon_key(modpack_dir)
            if icon_key:
                icons_dir = os.path.normpath(os.path.join(modpack_dir, "..", "icons"))
                for ext in THUMBNAIL_EXTENSIONS:
                    full_path = os.path.join(icons_dir, f"{icon_key}{ext}")
                    if os.path.isfile(full_path):
                        self.thumbnail_path_cache[modpack_dir] = full_path
                        return full_path

            metadata_thumb = self._get_thumbnail_from_metadata(modpack_dir)
            if metadata_thumb:
                self.thumbnail_path_cache[modpack_dir] = metadata_thumb
                return metadata_thumb

            for base_dir in candidate_dirs:
                if not os.path.isdir(base_dir):
                    continue
                try:
                    entries = sorted(os.scandir(base_dir), key=lambda e: e.name.lower())
                except OSError:
                    continue
                for entry in entries:
                    if entry.is_file() and entry.name.lower().endswith(THUMBNAIL_EXTENSIONS):
                        self.thumbnail_path_cache[modpack_dir] = entry.path
                        return entry.path

            return None

        def _get_thumbnail_from_metadata(self, modpack_dir):
            metadata_files = [
                "minecraftinstance.json",
                "instance.json",
                "manifest.json",
                "launcher_manifest.json",
            ]

            for filename in metadata_files:
                metadata_path = os.path.join(modpack_dir, filename)
                if not os.path.isfile(metadata_path):
                    continue
                try:
                    with open(metadata_path, "r", encoding="utf-8", errors="ignore") as mf:
                        data = json.load(mf)
                except Exception:
                    continue

                candidates = []
                self._collect_thumbnail_candidates(data, candidates)
                for candidate in candidates:
                    resolved = self._resolve_thumbnail_candidate(candidate, modpack_dir)
                    if resolved:
                        return resolved

            return None

        def _collect_thumbnail_candidates(self, value, out):
            if isinstance(value, dict):
                for key, child in value.items():
                    key_lower = str(key).lower()
                    if isinstance(child, str) and any(token in key_lower for token in ("thumbnail", "icon", "image", "logo")):
                        out.append(child)
                    self._collect_thumbnail_candidates(child, out)
                return

            if isinstance(value, list):
                for item in value:
                    self._collect_thumbnail_candidates(item, out)

        def _resolve_thumbnail_candidate(self, candidate, modpack_dir):
            if not candidate:
                return None
            candidate = str(candidate).strip()
            if not candidate:
                return None

            lower = candidate.lower()
            if lower.startswith(("http://", "https://")):
                return self._download_thumbnail_to_cache(candidate)

            if os.path.isabs(candidate) and os.path.isfile(candidate):
                return candidate

            normalized = candidate.replace("\\", os.sep).replace("/", os.sep)
            local_path = os.path.normpath(os.path.join(modpack_dir, normalized))
            if os.path.isfile(local_path):
                return local_path

            if local_path.lower().endswith(THUMBNAIL_EXTENSIONS):
                return None

            for ext in THUMBNAIL_EXTENSIONS:
                maybe_path = local_path + ext
                if os.path.isfile(maybe_path):
                    return maybe_path

            return None

        def _download_thumbnail_to_cache(self, url):
            cache_dir = os.path.join(os.path.dirname(self.settings_path), "thumb_cache")
            os.makedirs(cache_dir, exist_ok=True)

            parsed = urllib.parse.urlparse(url)
            ext = os.path.splitext(parsed.path)[1].lower()
            if ext not in THUMBNAIL_EXTENSIONS:
                ext = ".png"

            cache_name = hashlib.sha1(url.encode("utf-8")).hexdigest() + ext
            cache_path = os.path.join(cache_dir, cache_name)
            if os.path.isfile(cache_path):
                return cache_path

            try:
                with urllib.request.urlopen(url, timeout=8) as resp:
                    data = resp.read()
                if not data:
                    return None
                with open(cache_path, "wb") as cf:
                    cf.write(data)
                return cache_path
            except Exception:
                return None

        def _read_instance_icon_key(self, modpack_dir):
            cfg_path = os.path.join(modpack_dir, "instance.cfg")
            if not os.path.isfile(cfg_path):
                return None
            try:
                with open(cfg_path, "r", encoding="utf-8", errors="ignore") as cf:
                    for line in cf:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if line.startswith("iconKey="):
                            icon_key = line.split("=", 1)[1].strip()
                            if icon_key and icon_key.lower() != "default":
                                return icon_key
            except OSError:
                return None
            return None

        def _load_thumbnail_image(self, image_path, max_size=92):
            if not image_path:
                return None

            if Image is not None and ImageTk is not None:
                try:
                    pil_image = Image.open(image_path)
                    pil_image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
                    return ImageTk.PhotoImage(pil_image)
                except Exception:
                    pass

            image_path_lower = image_path.lower()
            if not image_path_lower.endswith((".png", ".gif")):
                return None
            try:
                image = tk.PhotoImage(file=image_path)
            except Exception:
                return None

            width = image.width()
            height = image.height()
            factor = max((width + max_size - 1) // max_size, (height + max_size - 1) // max_size, 1)
            if factor > 1:
                image = image.subsample(factor, factor)
            return image

        def render_modpack_cards(self, candidates):
            for child in self.cards_scroller.winfo_children():
                child.destroy()
            self.modpack_thumbnail_cache = []
            self.selected_card_widget = None
            self.modpack_cards_by_path = {}

            columns = self._get_card_columns()
            for col in range(columns):
                self.cards_scroller.grid_columnconfigure(col, weight=1)

            if not candidates:
                ctk.CTkLabel(
                    self.cards_scroller,
                    text="감지된 모드팩이 없습니다. 1단계에서 경로를 지정하고 탐지하세요.",
                    font=ctk.CTkFont(family=FONT_NAME, size=12),
                    text_color="#a1a1aa"
                ).grid(row=0, column=0, columnspan=columns, sticky="w", padx=8, pady=8)
                return

            for idx, (name, path, file_count) in enumerate(candidates):
                selected = (self.selected_modpack_path == path)
                card = ctk.CTkFrame(
                    self.cards_scroller,
                    fg_color="#25252b",
                    corner_radius=10,
                    border_width=2 if selected else 1,
                    border_color="#f97316" if selected else "#3f3f46"
                )
                card.grid(row=idx // columns, column=idx % columns, sticky="nsew", padx=5, pady=5)
                self.modpack_cards_by_path[path] = card

                if selected:
                    self.selected_card_widget = card

                thumbnail_wrap = ctk.CTkFrame(card, fg_color="#16161b", corner_radius=8)
                thumbnail_wrap.pack(fill="x", padx=8, pady=(8, 6))
                thumbnail_wrap.configure(height=84)
                thumbnail_wrap.pack_propagate(False)

                thumb_path = self._get_thumbnail_path(path)
                image = self._load_thumbnail_image(thumb_path, max_size=140)
                if image is not None:
                    self.modpack_thumbnail_cache.append(image)
                    tk.Label(
                        thumbnail_wrap,
                        image=image,
                        text="",
                        bg="#16161b",
                        bd=0,
                        highlightthickness=0,
                    ).pack(expand=True)
                else:
                    ctk.CTkLabel(
                        thumbnail_wrap,
                        text="NO\nTHUMB",
                        justify="center",
                        font=ctk.CTkFont(family=FONT_NAME, size=10, weight="bold"),
                        text_color="#fb923c"
                    ).pack(expand=True)

                ctk.CTkLabel(
                    thumbnail_wrap,
                    text=f"{file_count} FILES",
                    font=ctk.CTkFont(family=FONT_NAME, size=9, weight="bold"),
                    fg_color="#ea580c",
                    text_color="#fff7ed",
                    corner_radius=6,
                    padx=6,
                    pady=1,
                ).place(relx=0.98, rely=0.08, anchor="ne")

                ctk.CTkLabel(
                    card,
                    text=(name[:22] + "...") if len(name) > 22 else name,
                    anchor="w",
                    font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold")
                ).pack(fill="x", padx=8)
                ctk.CTkLabel(
                    card,
                    text="My Modpack Instance",
                    anchor="w",
                    font=ctk.CTkFont(family=FONT_NAME, size=9),
                    text_color="#9ca3af"
                ).pack(fill="x", padx=8, pady=(1, 0))

                def select_this(p=path, n=name):
                    if self.selected_card_widget is not None:
                        self.selected_card_widget.configure(border_color="#3f3f46", border_width=1)
                    self.selected_modpack_path = p
                    self.selected_modpack_label.configure(text=f"선택된 모드팩: {n}")
                    self.translate_selected_label.configure(text=f"선택 모드팩: {n}")
                    self.btn_go_translate.configure(state="normal")
                    self.log(f"✅ 모드팩 선택됨: {n}")
                    
                    target_card = self.modpack_cards_by_path.get(p)
                    if target_card:
                        target_card.configure(border_color="#f97316", border_width=2)
                        self.selected_card_widget = target_card

                ctk.CTkButton(
                    card,
                    text="선택",
                    width=64,
                    height=24,
                    fg_color="#ea580c",
                    hover_color="#c2410c",
                    font=ctk.CTkFont(family=FONT_NAME, size=10, weight="bold"),
                    command=select_this,
                ).pack(anchor="e", padx=8, pady=(6, 8))

        def auto_detect_instance_root(self):
            guessed_root = self._guess_instance_root()
            if not guessed_root:
                self.log("⚠️ 자동 탐색에서 인스턴스 루트를 찾지 못했습니다. 경로를 직접 지정해주세요.")
                return
            self.instance_path_entry.delete(0, "end")
            self.instance_path_entry.insert(0, guessed_root)
            self.save_user_settings()
            self.log(f"✅ 자동 탐색 경로 선택: {guessed_root}")
            self.scan_modpacks_from_entry()

        def _guess_instance_root(self):
            appdata = os.getenv("APPDATA", "")
            home_dir = os.path.expanduser("~")
            candidates = [
                os.path.join(appdata, "PrismLauncher", "instances"),
                os.path.join(appdata, "MultiMC", "instances"),
                os.path.join(home_dir, "curseforge", "minecraft", "Instances"),
                os.path.join(appdata, "ATLauncher", "instances"),
            ]
            for path in candidates:
                if os.path.isdir(path):
                    return path
            return None

        def pick_instance_root(self):
            root_dir = filedialog.askdirectory(title="모드팩 인스턴스 루트 선택")
            if not root_dir:
                return
            self.instance_path_entry.delete(0, "end")
            self.instance_path_entry.insert(0, root_dir)
            self.save_user_settings()
            self.scan_modpacks_from_entry()
            self.show_select_screen()

        def _show_scan_loading(self):
            if hasattr(self, "scan_overlay") and self.scan_overlay is not None:
                return
            self.scan_overlay = ctk.CTkFrame(self, fg_color="#06070a", corner_radius=0)
            self.scan_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)

            panel = ctk.CTkFrame(self.scan_overlay, fg_color="#12121a", corner_radius=16, border_width=1, border_color="#2a2a33")
            panel.place(relx=0.5, rely=0.5, anchor="center")

            ctk.CTkLabel(
                panel,
                text="모드팩 스캔 중",
                font=ctk.CTkFont(family=FONT_NAME, size=20, weight="bold"),
                text_color="#f8fafc",
            ).pack(padx=24, pady=(20, 6))

            ctk.CTkLabel(
                panel,
                text="인스턴스 폴더를 탐색하고 있습니다...",
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                text_color="#fbbf24",
            ).pack(padx=24, pady=(0, 12))

            bar = ctk.CTkProgressBar(panel, width=240, fg_color="#27272a", progress_color="#f97316")
            bar.pack(padx=24, pady=(0, 20))
            bar.set(0.1)
            self.scan_progress_bar = bar

        def _hide_scan_loading(self):
            if hasattr(self, "scan_overlay") and self.scan_overlay is not None:
                self.scan_overlay.destroy()
                self.scan_overlay = None
            self.scan_progress_bar = None

        def scan_modpacks_from_entry(self, show_screen=True):
            instance_root = self.instance_path_entry.get().strip()
            if not instance_root or not os.path.isdir(instance_root):
                messagebox.showwarning("경고", "유효한 인스턴스 루트 폴더를 먼저 선택해주세요.")
                return
            if self.scan_thread_active:
                self.log("🔄 이미 모드팩 스캔이 진행 중입니다.")
                return

            self.scan_thread_active = True
            self._show_scan_loading()
            if show_screen:
                self.show_select_screen()
            self.log(f"🔍 인스턴스 스캔 시작: {instance_root}")

            def run_scan():
                try:
                    candidates = self._scan_modpack_candidates(instance_root)
                    self.after(0, lambda: self._finish_scan(candidates))
                except Exception as exc:
                    self.after(0, lambda: self._handle_scan_error(exc))

            threading.Thread(target=run_scan, daemon=True).start()

        def _finish_scan(self, candidates):
            self.scan_thread_active = False
            self._hide_scan_loading()
            self._set_modpack_candidates(candidates)
            if candidates:
                self.log(f"✅ 모드팩 {len(candidates)}개를 탐지했습니다. 먼저 모드팩을 선택한 뒤 다음 단계로 이동하세요.")
            else:
                self.log("⚠️ 번역 가능한 모드팩을 찾지 못했습니다.")

        def _handle_scan_error(self, exc):
            self.scan_thread_active = False
            self._hide_scan_loading()
            self.log(f"❌ 모드팩 스캔 중 오류가 발생했습니다: {exc}")
            messagebox.showerror("오류", f"모드팩 스캔 중 오류가 발생했습니다.\n{exc}")

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
                        rel_path = os.path.relpath(full_path, modpack_dir)
                        zf.write(full_path, rel_path)
                        added_count += 1

            return temp_zip_path, added_count

        def run_selected_modpack(self):
            engine_key, api_key, is_paid = self.validate_inputs()
            if not engine_key:
                return

            modpack_dir = self.selected_modpack_path
            if not modpack_dir:
                selected_label = self.modpack_combo.get().strip()
                modpack_dir = self.detected_modpacks.get(selected_label)
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
                            self.set_status(f"⏳ [{idx}/{total_files}] [{filename}] 번역 대상 없음 - 건너뜀")
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
                                skipped_bad_json += 1
                                self.set_status(
                                    f"⏳ [{idx}/{total_files}] [{filename}] JSON 형식 오류로 건너뜀 "
                                    f"({e.lineno}:{e.colno})"
                                )
                                shutil.copy2(file_path, target_path)
                                continue
                        node_targets = []
                        collect_json_targets(data, node_targets)
                        if not node_targets:
                            skipped_no_targets += 1
                            self.set_status(f"⏳ [{idx}/{total_files}] [{filename}] 번역 대상 없음 - 건너뜀")
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

                if skipped_no_targets or skipped_bad_json:
                    self.log(
                        f"ℹ️ 스캔 요약: 번역 대상 없음 {skipped_no_targets}개, JSON 형식 오류 {skipped_bad_json}개 파일을 자동 건너뜀"
                    )

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
# � QuestTranslatorApp 메서드 인덱스
# ==============================================================================
"""
[섹션 1] 기본 설정 & 생명주기
  - __init__: 앱 초기화
  - _setup_ui: UI 전체 구성
  - on_close: 앱 종료 처리
  - load_user_settings: 설정 로드
  - save_user_settings: 설정 저장
  - _get_settings_path: 설정 파일 경로

[섹션 2] 화면 전환 & 네비게이션
  - show_home_screen: 홈 화면 표시
  - show_select_screen: 모드팩 선택 화면
  - show_quick_translate_screen: 빠른 번역 화면
  - show_translate_screen: 번역 설정 화면

[섹션 3] 모드팩 스캔 & 관리
  - scan_modpacks_from_entry: 경로에서 모드팩 스캔
  - _scan_modpack_candidates: 모드팩 후보 탐지
  - _set_modpack_candidates: 모드팩 목록 업데이트
  - _show_scan_loading: 스캔 로딩 표시
  - _hide_scan_loading: 스캔 로딩 숨김
  - _finish_scan: 스캔 완료 처리
  - _handle_scan_error: 스캔 오류 처리
  - auto_detect_instance_root: 자동 경로 탐지
  - _guess_instance_root: 경로 추측
  - pick_instance_root: 경로 수동 선택
  - _count_translatable_files: 번역 파일 개수 계산

[섹션 4] 모드팩 선택 & 검색
  - _on_modpack_search_change: 검색어 변경 처리
  - render_modpack_cards: 모드팩 카드 렌더링
  - select_modpack_from_path: 경로로 모드팩 선택
  - _get_card_columns: 반응형 열 수 계산

[섹션 5] 썸네일 & 이미지
  - _get_thumbnail_path: 썸네일 경로 찾기
  - _get_thumbnail_from_metadata: 메타데이터에서 찾기
  - _collect_thumbnail_candidates: 후보 수집
  - _resolve_thumbnail_candidate: 후보 해석
  - _download_thumbnail_to_cache: 다운로드 및 캐시
  - _read_instance_icon_key: 인스턴스 아이콘 키 읽기
  - _load_thumbnail_image: 이미지 로드

[섹션 6] 번역 기능
  - run_selected_modpack: 선택된 모드팩 번역 실행
  - run_single_file: 단일 파일 번역
  - run_zip_file: ZIP 파일 번역
  - _process_single_file: 단일 파일 처리
  - _process_zip_file: ZIP 파일 처리
  - _create_temp_zip_from_modpack: 모드팩을 ZIP으로 변환
  - _translate_jobs_parallel: 병렬 번역
  - _translate_jobs_sequential: 순차 번역
  - _notify_partial_result: 부분 결과 알림

[섹션 7] UI 레이아웃 & 반응성
  - _on_window_resize: 창 크기 변경 이벤트
  - _apply_responsive_layout: 반응형 레이아웃 적용
  - _arrange_path_buttons: 경로 버튼 배치
  - _arrange_quick_buttons: 빠른 번역 버튼 배치

[섹션 8] 시작 화면 & 로딩
  - _show_startup_loading: 시작 로딩 표시
  - _finish_startup_loading: 시작 로딩 완료
  - _fade_out_startup_overlay: 시작 로딩 페이드 아웃
  - _update_startup_status: 시작 상태 업데이트

[섹션 9] 설정 & 입력 검증
  - on_engine_change: 엔진 변경 처리
  - toggle_api_visibility: API 키 표시/숨김
  - validate_inputs: 입력값 검증
  - toggle_buttons: 버튼 활성화/비활성화
  - handle_file_drop: 파일 드래그 앤 드롭

[섹션 10] 로그 & 상태 표시
  - log: 로그 메시지 기록
  - set_status: 상태 메시지 설정
  - route_log: 번역 엔진 로그 라우팅
  - update_progress: 진행률 업데이트
  - show_messagebox: 메시지 박스 표시
  - request_cancel: 번역 취소 요청
  - is_cancelled: 취소 여부 확인
"""

# ==============================================================================
# �🚀 메인 프로그램 진입점
# ==============================================================================
if __name__ == "__main__":
    app = QuestTranslatorApp()
    app.mainloop()