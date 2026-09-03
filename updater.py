import os
import sys
import re
import tempfile
import subprocess
import threading
import logging
import requests
import customtkinter as ctk
import tkinter as tk
from constants import APP_VERSION, FONT_NAME

GITHUB_REPO = "TEiBA38/QuestTranslato"
GITHUB_LATEST_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


def parse_version_tuple(ver_str):
    if not ver_str:
        return (0, 0, 0)
    clean = ver_str.strip().lstrip('vV')
    nums = re.findall(r'\d+', clean)
    if not nums:
        return (0, 0, 0)
    return tuple(int(n) for n in nums[:4])


def is_newer_version(current_ver, latest_ver):
    return parse_version_tuple(latest_ver) > parse_version_tuple(current_ver)


def check_for_updates(current_version=APP_VERSION, timeout=6.0):
    result = {
        "has_update": False,
        "latest_version": current_version,
        "current_version": current_version,
        "release_name": "",
        "release_body": "",
        "download_url": None,
        "file_size": 0,
        "asset_name": "",
    }

    try:
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": f"QuestTranslatorPro/{current_version}"
        }
        resp = requests.get(GITHUB_LATEST_API, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            logging.debug(f"GitHub Release 확인 응답 코드: {resp.status_code}")
            return result

        data = resp.json()
        tag_name = data.get("tag_name", "")
        if not tag_name:
            return result

        result["latest_version"] = tag_name
        result["release_name"] = data.get("name", tag_name)
        result["release_body"] = data.get("body", "")

        if is_newer_version(current_version, tag_name):
            result["has_update"] = True

            assets = data.get("assets", [])
            exe_asset = None
            zip_asset = None

            for asset in assets:
                name = asset.get("name", "").lower()
                if name.endswith(".exe"):
                    exe_asset = asset
                    break
                elif name.endswith(".zip") and "windows" in name:
                    zip_asset = asset

            chosen = exe_asset or zip_asset
            if chosen:
                result["download_url"] = chosen.get("browser_download_url")
                result["file_size"] = chosen.get("size", 0)
                result["asset_name"] = chosen.get("name", "")

    except Exception as e:
        logging.debug(f"업데이트 확인 중 오류 발생: {e}")

    return result


def download_file_with_progress(url, dest_path, progress_callback=None, cancel_check=None):
    headers = {
        "User-Agent": f"QuestTranslatorPro/{APP_VERSION}"
    }
    with requests.get(url, headers=headers, stream=True, timeout=30) as r:
        r.raise_for_status()
        total_length = int(r.headers.get('content-length', 0))
        downloaded = 0

        with open(dest_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=65536):
                if cancel_check and cancel_check():
                    return False
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total_length > 0:
                        progress_callback(downloaded, total_length)
    return True


def apply_update_and_restart(new_binary_path):
    if getattr(sys, 'frozen', False):
        target_exe_path = os.path.abspath(sys.executable)
    else:
        repo_root = os.path.dirname(os.path.abspath(__file__))
        target_exe_path = os.path.join(repo_root, "dist", "QuestTranslatorPro.exe")

    target_dir = os.path.dirname(target_exe_path)
    target_exe_name = os.path.basename(target_exe_path)

    bat_fd, bat_path = tempfile.mkstemp(suffix="_qtp_update.bat")
    os.close(bat_fd)

    bat_content = f"""@echo off
chcp 65001 >nul
timeout /t 2 /nobreak >nul
copy /y "{new_binary_path}" "{target_exe_path}" >nul
if errorlevel 1 (
    timeout /t 2 /nobreak >nul
    copy /y "{new_binary_path}" "{target_exe_path}" >nul
)
cd /d "{target_dir}"
start "" "{target_exe_path}"
del /f /q "{new_binary_path}" >nul 2>&1
(goto) 2>nul & del "%~f0"
"""

    with open(bat_path, "w", encoding="utf-8") as f:
        f.write(bat_content)

    startupinfo = None
    creationflags = 0
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS

    subprocess.Popen(
        ["cmd.exe", "/c", bat_path],
        cwd=tempfile.gettempdir(),
        startupinfo=startupinfo,
        creationflags=creationflags,
        close_fds=True
    )

    sys.exit(0)


class UpdateDialog(ctk.CTkToplevel):
    def __init__(self, parent, update_info):
        super().__init__(parent)
        self.parent = parent
        self.update_info = update_info
        self.is_downloading = False
        self.is_cancelled = False
        self.temp_dest = None

        self.title("Quest Translator Pro 업데이트")
        self.geometry("520x360")
        self.resizable(False, False)
        self.configure(fg_color="#121217")

        # 모달 창 설정
        self.transient(parent)
        self.grab_set()

        # 화면 중앙 배치
        self.update_idletasks()
        try:
            x = parent.winfo_x() + (parent.winfo_width() - 520) // 2
            y = parent.winfo_y() + (parent.winfo_height() - 360) // 2
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass

        self._build_ui()

    def _build_ui(self):
        latest = self.update_info.get("latest_version", "")
        current = self.update_info.get("current_version", "")
        rel_name = self.update_info.get("release_name", latest)
        file_size = self.update_info.get("file_size", 0)
        size_mb = f"{file_size / (1024*1024):.1f} MB" if file_size else "알 수 없음"

        # 헤더
        header_frame = ctk.CTkFrame(self, fg_color="#1c1917", corner_radius=12, border_width=1, border_color="#ea580c")
        header_frame.pack(fill="x", padx=20, pady=(20, 12))

        ctk.CTkLabel(header_frame, text=f"✨ 새로운 버전 ({latest}) 출시!",
                     font=ctk.CTkFont(family=FONT_NAME, size=18, weight="bold"),
                     text_color="#fb923c").pack(anchor="w", padx=16, pady=(12, 2))

        ctk.CTkLabel(header_frame, text=f"현재 버전: {current}  ➔  새 버전: {latest} (파일 크기: {size_mb})",
                     font=ctk.CTkFont(family=FONT_NAME, size=12),
                     text_color="#a8a29e").pack(anchor="w", padx=16, pady=(0, 12))

        # 릴리즈 노트 미리보기
        note_box = ctk.CTkTextbox(self, height=110, fg_color="#09090b", border_width=1, border_color="#27272a", font=ctk.CTkFont(family=FONT_NAME, size=11))
        note_box.pack(fill="x", padx=20, pady=(0, 12))
        body = self.update_info.get("release_body", "").strip() or "상세 릴리즈 노트가 제공되지 않았습니다."
        note_box.insert("1.0", f"[{rel_name}]\n\n{body}")
        note_box.configure(state="disabled")

        # 진행률 바 & 상태 레이블
        self.status_label = ctk.CTkLabel(self, text="업데이트를 진행하려면 [지금 업데이트]를 누르세요.",
                                         font=ctk.CTkFont(family=FONT_NAME, size=12),
                                         text_color="#d4d4d8")
        self.status_label.pack(anchor="w", padx=24, pady=(0, 4))

        self.prog_bar = ctk.CTkProgressBar(self, height=8, fg_color="#27272a", progress_color="#ea580c")
        self.prog_bar.pack(fill="x", padx=20, pady=(0, 16))
        self.prog_bar.set(0)

        # 버튼 영역
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=(0, 16))

        self.close_btn = ctk.CTkButton(btn_frame, text="나중에", width=90, height=36,
                                       fg_color="#27272a", hover_color="#3f3f46", text_color="#a1a1aa",
                                       command=self._on_close)
        self.close_btn.pack(side="right", padx=(8, 0))

        self.update_btn = ctk.CTkButton(btn_frame, text="🚀 지금 업데이트", width=140, height=36,
                                        font=ctk.CTkFont(family=FONT_NAME, size=13, weight="bold"),
                                        fg_color="#ea580c", hover_color="#c2410c", text_color="#ffffff",
                                        command=self._start_download)
        self.update_btn.pack(side="right")

    def _start_download(self):
        url = self.update_info.get("download_url")
        if not url:
            self.status_label.configure(text="❌ 다운로드 URL을 찾을 수 없습니다.", text_color="#ef4444")
            return

        self.is_downloading = True
        self.update_btn.configure(state="disabled", text="다운로드 중...")
        self.close_btn.configure(text="취소")

        threading.Thread(target=self._download_worker, args=(url,), daemon=True).start()

    def _download_worker(self, url):
        try:
            temp_dir = tempfile.gettempdir()
            self.temp_dest = os.path.join(temp_dir, f"QuestTranslatorPro_update_{os.getpid()}.exe")

            def on_prog(downloaded, total):
                if total > 0:
                    pct = downloaded / total
                    dl_mb = downloaded / (1024 * 1024)
                    tot_mb = total / (1024 * 1024)
                    self.after(0, lambda: (
                        self.prog_bar.set(pct),
                        self.status_label.configure(text=f"다운로드 중... {int(pct*100)}% ({dl_mb:.1f}MB / {tot_mb:.1f}MB)")
                    ))

            def check_cancel():
                return self.is_cancelled

            ok = download_file_with_progress(url, self.temp_dest, on_prog, check_cancel)
            if ok and not self.is_cancelled:
                self.after(0, lambda: self.status_label.configure(
                    text="✅ 다운로드 완료! 프로그램을 교체하고 재시작합니다...", text_color="#22c55e"
                ))
                self.after(1200, lambda: apply_update_and_restart(self.temp_dest))
            elif self.is_cancelled:
                self.after(0, lambda: self.status_label.configure(text="업데이트가 취소되었습니다."))
        except Exception as e:
            self.after(0, lambda: (
                self.status_label.configure(text=f"❌ 다운로드 오류: {e}", text_color="#ef4444"),
                self.update_btn.configure(state="normal", text="다시 시도")
            ))

    def _on_close(self):
        if self.is_downloading:
            self.is_cancelled = True
        self.grab_release()
        self.destroy()


def show_update_dialog(parent, update_info):
    return UpdateDialog(parent, update_info)
