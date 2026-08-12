"""
모드팩 스캔, 카드 렌더링, 썸네일 로딩 관련 메서드 믹스인.
"""
import os
import json
import threading
import hashlib
import urllib.parse
import urllib.request
import importlib

try:
    import customtkinter as ctk
    import tkinter as tk
    from tkinter import filedialog, messagebox
except Exception:
    ctk = None
    tk = None
    filedialog = None
    messagebox = None

try:
    Image = importlib.import_module("PIL.Image")
    ImageTk = importlib.import_module("PIL.ImageTk")
except Exception:
    Image = None
    ImageTk = None

from constants import FONT_NAME, TARGET_EXTENSIONS, SCAN_IGNORE_DIRS

SCAN_EXCLUDE_CANDIDATE_DIRS = {'translation_output', 'install'}
THUMBNAIL_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".ico")


class ModpackMixin:
    # ====================================================================
    # 모드팩 스캔
    # ====================================================================

    def _count_translatable_files(self, base_dir):
        count = 0
        already_translated = False
        sample_scanned = 0
        for root, dirs, files_list in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d.lower() not in SCAN_IGNORE_DIRS]
            for filename in files_list:
                if filename.lower().endswith(TARGET_EXTENSIONS) or filename.lower().endswith('.lang'):
                    if filename.lower().endswith(TARGET_EXTENSIONS):
                        count += 1
                    if not already_translated:
                        is_quest_file = filename.lower().endswith(('.snbt', '.hqm', '.json', '.lang', '.cfg', '.txt')) and ('quest' in root.lower() or 'hqm' in root.lower() or filename.lower().endswith(('.snbt', '.hqm')))
                        if is_quest_file:
                            if filename.lower() in ["ko_kr.json", "ko_kr.lang", "ko_kr.snbt"]:
                                already_translated = True
                            elif sample_scanned < 100:
                                try:
                                    with open(os.path.join(root, filename), 'r', encoding='utf-8', errors='ignore') as f:
                                        content = f.read(8192)
                                        for ch in content:
                                            code = ord(ch)
                                            if (0xAC00 <= code <= 0xD7A3) or (0x3040 <= code <= 0x30FF) or (0x4E00 <= code <= 0x9FFF) or (0x0400 <= code <= 0x04FF):
                                                already_translated = True
                                                break
                                    sample_scanned += 1
                                except Exception:
                                    pass
        return count, already_translated

    def _scan_modpack_candidates(self, instance_root):
        candidates = []
        for entry in os.scandir(instance_root):
            if not entry.is_dir():
                continue
            name_lower = entry.name.lower()
            if name_lower in SCAN_IGNORE_DIRS or name_lower in SCAN_EXCLUDE_CANDIDATE_DIRS:
                continue
            file_count, already_translated = self._count_translatable_files(entry.path)
            if file_count > 0:
                candidates.append((entry.name, entry.path, file_count, already_translated))
        candidates.sort(key=lambda item: (-item[2], item[0].lower()))
        return candidates

    def _set_modpack_candidates(self, candidates):
        self.detected_modpacks = {}
        self.modpack_entries = list(candidates)
        self.selected_modpack_path = None
        self.modpack_search_entry.delete(0, "end")
        if not candidates:
            self.render_modpack_cards([])
            return
        for cand in candidates:
            name, path, file_count = cand[0], cand[1], cand[2]
            self.detected_modpacks[path] = (name, file_count)
        self.render_modpack_cards(candidates)

    def scan_modpacks_from_entry(self, show_screen=True):
        instance_root = self.instance_path_entry.get().strip()
        if not instance_root or not os.path.isdir(instance_root):
            messagebox.showwarning("경고", "유효한 인스턴스 루트 폴더를 먼저 선택해주세요.")
            return
        if getattr(self.app_state, "scan_thread_active", False):
            self.log("🔄 이미 모드팩 스캔이 진행 중입니다.")
            return

        self.app_state.scan_thread_active = True
        if hasattr(self, "btn_rescan_modpacks"):
            self.btn_rescan_modpacks.configure(state="disabled")
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
        self.app_state.scan_thread_active = False
        if hasattr(self, "btn_rescan_modpacks"):
            self.btn_rescan_modpacks.configure(state="normal")
        self._hide_scan_loading()
        self._set_modpack_candidates(candidates)
        if candidates:
            self.log(f"✅ 모드팩 {len(candidates)}개를 탐지했습니다. 먼저 모드팩을 선택한 뒤 다음 단계로 이동하세요.")
        else:
            self.log("⚠️ 번역 가능한 모드팩을 찾지 못했습니다.")

    def _handle_scan_error(self, exc):
        self.app_state.scan_thread_active = False
        self._hide_scan_loading()
        self.log(f"❌ 모드팩 스캔 중 오류가 발생했습니다: {exc}")
        messagebox.showerror("오류", f"모드팩 스캔 중 오류가 발생했습니다.\n{exc}")

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

        ctk.CTkLabel(panel, text="모드팩 스캔 중",
                     font=ctk.CTkFont(family=FONT_NAME, size=20, weight="bold"),
                     text_color="#f8fafc").pack(padx=24, pady=(20, 6))
        ctk.CTkLabel(panel, text="인스턴스 폴더를 탐색하고 있습니다...",
                     font=ctk.CTkFont(family=FONT_NAME, size=12),
                     text_color="#fbbf24").pack(padx=24, pady=(0, 12))

        bar = ctk.CTkProgressBar(panel, width=240, fg_color="#27272a", progress_color="#f97316")
        bar.pack(padx=24, pady=(0, 20))
        bar.set(0.1)
        self.scan_progress_bar = bar

    def _hide_scan_loading(self):
        if hasattr(self, "scan_overlay") and self.scan_overlay is not None:
            self.scan_overlay.destroy()
            self.scan_overlay = None
        self.scan_progress_bar = None

    # ====================================================================
    # 모드팩 선택 & 렌더링
    # ====================================================================

    def _get_card_columns(self):
        width = max(self.cards_scroller.winfo_width(), self.winfo_width())
        if width >= 1500: return 6
        if width >= 1200: return 5
        if width >= 980:  return 4
        if width >= 760:  return 3
        return 2

    def _on_modpack_search_change(self, event=None):
        search_text = self.modpack_search_entry.get().strip().lower()
        filtered = [
            cand for cand in self.modpack_entries
            if search_text in cand[0].lower() or search_text in cand[1].lower()
        ] if search_text else self.modpack_entries
        self.render_modpack_cards(filtered)

    def select_modpack_from_path(self, modpack_dir):
        if modpack_dir and modpack_dir in self.detected_modpacks:
            self.selected_modpack_path = modpack_dir
            n = os.path.basename(modpack_dir)
            self.selected_modpack_label.configure(text=f"선택된 모드팩: {n}")
            self.translate_selected_label.configure(text=f"선택 모드팩: {n}")
            self.log(f"✅ 모드팩 선택됨: {n}")
            self.btn_go_translate.configure(state="normal")
        else:
            self.selected_modpack_path = None
            self.selected_modpack_label.configure(text="선택된 모드팩: 없음")
            self.translate_selected_label.configure(text="선택 모드팩: 없음")
            self.btn_go_translate.configure(state="disabled")

    def render_modpack_cards(self, candidates):
        # 1. [잔상 방지 패치] 스크롤러 내부 캔버스 배경색 강제 지정
        try:
            self.cards_scroller._parent_canvas.configure(bg="#12121a")
        except Exception:
            pass

        # 2. 위젯 초기화
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
                text="감지된 모드팩이 없습니다.",
                font=ctk.CTkFont(family=FONT_NAME, size=12),
                text_color="#a1a1aa"
            ).grid(row=0, column=0, columnspan=columns, sticky="w", padx=8, pady=8)
            return

        for idx, candidate in enumerate(candidates):
            name = candidate[0]
            path = candidate[1]
            file_count = candidate[2]
            already_translated = candidate[3] if len(candidate) > 3 else False

            selected = (self.selected_modpack_path == path)
            
            # [잔상 방지 패치] bg_color를 부모 배경색과 동일하게 지정
            card = ctk.CTkFrame(
                self.cards_scroller,
                fg_color="#25252b",
                bg_color="#12121a", # 부모(스크롤러) 배경색
                corner_radius=10,
                border_width=2 if selected else 1,
                border_color="#f97316" if selected else "#3f3f46"
            )
            card.grid(row=idx // columns, column=idx % columns, sticky="nsew", padx=5, pady=5)
            self.modpack_cards_by_path[path] = card
            if selected:
                self.selected_card_widget = card

            thumbnail_wrap = ctk.CTkFrame(
                card, 
                fg_color="transparent",
                bg_color="transparent"
            )
            thumbnail_wrap.pack(fill="x", padx=8, pady=(8, 4))

            thumb_label = ctk.CTkLabel(thumbnail_wrap, text="⏳", justify="center",
                                       font=ctk.CTkFont(family=FONT_NAME, size=14),
                                       text_color="#a1a1aa")
            thumb_label.pack(expand=True, pady=4)

            self._load_thumbnail_async(path, thumb_label)

            ctk.CTkLabel(thumbnail_wrap, text=f"{file_count} FILES",
                         font=ctk.CTkFont(family=FONT_NAME, size=9, weight="bold"),
                         fg_color="#ea580c", text_color="#fff7ed",
                         corner_radius=6, padx=6, pady=1).place(relx=0.98, rely=0.08, anchor="ne")

            if already_translated or path in getattr(self.app_state, 'translated_history', {}):
                history_time = getattr(self.app_state, 'translated_history', {}).get(path, "원본")
                ctk.CTkLabel(thumbnail_wrap, text="번역됨" if history_time == "원본" else "한글화됨",
                             font=ctk.CTkFont(family=FONT_NAME, size=10, weight="bold"),
                             fg_color="#16a34a", text_color="#f0fdf4",
                             corner_radius=6, padx=6, pady=1).place(relx=0.02, rely=0.08, anchor="nw")
                subtitle_text = f"번역됨: {history_time}" if history_time != "원본" else "이미 번역이 포함된 팩입니다"
                subtitle_color = "#34d399"
            else:
                subtitle_text = "My Modpack Instance"
                subtitle_color = "#9ca3af"

            ctk.CTkLabel(card,
                         text=(name[:22] + "...") if len(name) > 22 else name,
                         anchor="w", font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold")
                         ).pack(fill="x", padx=8)
            ctk.CTkLabel(card, text=subtitle_text, anchor="w",
                         font=ctk.CTkFont(family=FONT_NAME, size=9),
                         text_color=subtitle_color).pack(fill="x", padx=8, pady=(1, 0))

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

            ctk.CTkButton(card, text="선택", width=64, height=24,
                          fg_color="#ea580c", hover_color="#c2410c",
                          font=ctk.CTkFont(family=FONT_NAME, size=10, weight="bold"),
                          command=select_this).pack(anchor="e", padx=8, pady=(6, 8))
    # ====================================================================
    # 썸네일 & 이미지 로직 (사용자님의 유연한 로직 100% 통합)
    # ====================================================================

    def _load_thumbnail_async(self, path, thumb_label):
        def worker():
            cache_val = self._get_thumbnail_path(path)
            if cache_val:
                img = self._load_thumbnail_image(cache_val, max_size=140)
                if img:
                    self.after(0, lambda: thumb_label.configure(image=img, text=""))
                else:
                    self.after(0, lambda: thumb_label.configure(text="No Image"))
            else:
                self.after(0, lambda: thumb_label.configure(text="No Image"))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_thumbnail(self, label_widget, image):
        if not label_widget.winfo_exists():
            return
        if image is not None:
            self.modpack_thumbnail_cache.append(image)
            label_widget.configure(text="", image=image)
        else:
            label_widget.configure(text="NO\nTHUMB",
                                   font=ctk.CTkFont(family=FONT_NAME, size=10, weight="bold"),
                                   text_color="#fb923c")

    def _get_thumbnail_path(self, modpack_dir):
        if not hasattr(self, "thumbnail_path_cache"):
            self.thumbnail_path_cache = {}
            
        cached = self.thumbnail_path_cache.get(modpack_dir)
        if cached:
            if os.path.isfile(cached):
                return cached
            self.thumbnail_path_cache.pop(modpack_dir, None)

        # 1. 직관적인 기본 파일명 탐색 (사용자님의 기존 로직)
        for possible_icon in ["icon.png", "folder.png", "modpack.png"]:
            p = os.path.join(modpack_dir, possible_icon)
            if os.path.exists(p):
                self.thumbnail_path_cache[modpack_dir] = p
                return p

        # 2. 다양한 하위 디렉토리 탐색
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

        # 3. Prism/MultiMC 공용 아이콘 키 폴더 탐색
        icon_key = self._read_instance_icon_key(modpack_dir)
        if icon_key:
            icons_dir = os.path.normpath(os.path.join(modpack_dir, "..", "icons"))
            for ext in THUMBNAIL_EXTENSIONS:
                full_path = os.path.join(icons_dir, f"{icon_key}{ext}")
                if os.path.isfile(full_path):
                    self.thumbnail_path_cache[modpack_dir] = full_path
                    return full_path

        # 4. JSON 메타데이터 내부 깊은 탐색 (사용자님의 강력한 재귀 탐색 로직)
        metadata_thumb = self._get_thumbnail_from_metadata(modpack_dir)
        if metadata_thumb:
            self.thumbnail_path_cache[modpack_dir] = metadata_thumb
            return metadata_thumb

        # 5. 최후의 수단: 폴더 내 이미지 파일 무작위 탐색
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
        for filename in ("minecraftinstance.json", "instance.json", "manifest.json", "launcher_manifest.json"):
            metadata_path = os.path.join(modpack_dir, filename)
            if not os.path.isfile(metadata_path):
                continue
            try:
                with open(metadata_path, "r", encoding="utf-8", errors="ignore") as mf:
                    data = json.load(mf)
            except Exception:
                continue
                
            candidates = []
            self._collect_thumbnail_candidates(data, candidates) # 재귀 파싱 호출
            
            for candidate in candidates:
                resolved = self._resolve_thumbnail_candidate(candidate, modpack_dir)
                if resolved:
                    return resolved
        return None

    # 사용자님의 원본 재귀 파싱 함수 (매우 뛰어난 로직!)
    def _collect_thumbnail_candidates(self, value, out):
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(child, str) and any(t in str(key).lower() for t in ("thumbnail", "icon", "image", "logo", "customImageSelection")):
                    out.append(child)
                self._collect_thumbnail_candidates(child, out)
        elif isinstance(value, list):
            for item in value:
                self._collect_thumbnail_candidates(item, out)

    def _resolve_thumbnail_candidate(self, candidate, modpack_dir):
        if not candidate:
            return None
        candidate = str(candidate).strip()
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
            maybe = local_path + ext
            if os.path.isfile(maybe):
                return maybe
        return None

    def _download_thumbnail_to_cache(self, url):
        # User-Agent 헤더 추가 (API 서버 차단 방지)
        cache_dir = os.path.join(os.path.dirname(getattr(self, "settings_path", "")), "thumb_cache")
        if not cache_dir.strip(os.sep): cache_dir = "thumb_cache"
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
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
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
            
        # 1. PIL (Pillow) 렌더링 시도
        if Image is not None and ImageTk is not None:
            try:
                pil_image = Image.open(image_path).convert("RGBA")
                resample_mode = getattr(Image, "Resampling", Image).LANCZOS
                try:
                    ImageOps = importlib.import_module("PIL.ImageOps")
                    pil_image = ImageOps.fit(pil_image, (max_size, max_size), method=resample_mode)
                except Exception:
                    pil_image.thumbnail((max_size, max_size), resample_mode)
                return ctk.CTkImage(light_image=pil_image, dark_image=pil_image, size=pil_image.size)
            except Exception:
                pass
                
        # 2. 사용자님의 Tkinter 기본 렌더링 폴백(Fallback)
        if not image_path.lower().endswith((".png", ".gif")):
            return None
        try:
            image = tk.PhotoImage(file=image_path)
        except Exception:
            return None
            
        factor = max((image.width() + max_size - 1) // max_size,
                     (image.height() + max_size - 1) // max_size, 1)
        if factor > 1:
            image = image.subsample(factor, factor)
        return image
