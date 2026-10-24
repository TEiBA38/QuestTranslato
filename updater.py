import os
import sys
import re
import time
import shutil
import base64
import zipfile
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


def _get_updater_log_path():
    appdata_dir = os.getenv("APPDATA") or os.path.expanduser("~")
    log_dir = os.path.join(appdata_dir, "QuestTranslatorPro", "logs")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "updater.log")


def _log_updater_event(msg):
    try:
        logging.info(f"[Updater] {msg}")
        log_path = _get_updater_log_path()
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [Python] {msg}\n")
    except Exception:
        pass


def cleanup_old_update_temp_files():
    """이전 업데이트 시 남은 임시 ZIP/EXE/PS1 파일 정리"""
    try:
        temp_dir = tempfile.gettempdir()
        my_pid = str(os.getpid())
        now = time.time()
        for fname in os.listdir(temp_dir):
            low = fname.lower()
            if (
                low.startswith("questtranslatorpro_update_")
                or low.startswith("questtranslatorpro_extracted_")
                or low.startswith("qt_updater_")
            ):
                if my_pid in fname:
                    continue
                fpath = os.path.join(temp_dir, fname)
                try:
                    # 생성된 지 60초 이상 지난 임시 파일만 안전하게 삭제
                    if now - os.path.getmtime(fpath) > 60:
                        os.remove(fpath)
                except Exception:
                    pass
    except Exception:
        pass


def parse_version_tuple(ver_str):
    if not ver_str:
        return (0, 0, 0, 0)
    clean = ver_str.strip().lstrip('vV')
    is_hotfix = 1 if 'hotfix' in clean.lower() else 0
    nums = [int(n) for n in re.findall(r'\d+', clean)]
    if not nums:
        return (0, 0, 0, 0)
    while len(nums) < 3:
        nums.append(0)
    if len(nums) == 3:
        nums.append(is_hotfix)
    return tuple(nums[:4])


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
                elif name.endswith(".zip"):
                    # 업데이트용으로 without_cache(경량) 우선 선택, 없으면 일반 zip 선택
                    if zip_asset and "without_cache" in zip_asset.get("name", "").lower():
                        continue
                    if "without_cache" in name:
                        zip_asset = asset
                    elif not zip_asset:
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

        if total_length > 0 and downloaded != total_length:
            raise IOError(f"다운로드가 불완전합니다 ({downloaded}/{total_length} bytes)")
        if downloaded < 1024 * 1024:
            raise IOError(f"다운로드된 파일 크기가 비정상적으로 작습니다 ({downloaded} bytes)")

    return True


def _validate_pe_executable(exe_path, min_size_bytes=5 * 1024 * 1024):
    """추출/다운로드된 EXE 파일이 유효한 Windows PE 실행 파일인지 검증"""
    if not exe_path or not os.path.isfile(exe_path):
        raise FileNotFoundError(f"업데이트 실행 파일을 찾을 수 없습니다: {exe_path}")
    size = os.path.getsize(exe_path)
    if size < min_size_bytes:
        raise IOError(f"실행 파일 크기가 비정상적입니다 ({size} bytes)")
    with open(exe_path, "rb") as f:
        magic = f.read(2)
    if magic != b"MZ":
        raise IOError("유효한 Windows 실행 파일(PE MZ 헤더)이 아닙니다.")
    return size


def apply_update_and_restart(new_binary_path, target_exe_path=None):
    try:
        if not target_exe_path:
            if getattr(sys, 'frozen', False):
                target_exe_path = os.path.abspath(sys.executable)
            else:
                repo_root = os.path.dirname(os.path.abspath(__file__))
                target_exe_path = os.path.join(repo_root, "dist", "QuestTranslatorPro.exe")
        else:
            target_exe_path = os.path.abspath(target_exe_path)

        new_binary_path = os.path.abspath(new_binary_path)
        _validate_pe_executable(new_binary_path)

        target_dir = os.path.dirname(target_exe_path)
        current_pid = os.getpid()
        parent_pid = os.getppid()
        log_file_path = _get_updater_log_path()

        _log_updater_event(
            f"apply_update_and_restart 시작: child_pid={current_pid}, boot_pid={parent_pid}, "
            f"new_bin={new_binary_path}, target={target_exe_path}"
        )

        # Escape single quotes for PowerShell single-quoted string literals
        safe_target = target_exe_path.replace("'", "''")
        safe_new = new_binary_path.replace("'", "''")
        safe_dir = target_dir.replace("'", "''")
        safe_log = log_file_path.replace("'", "''")

        ps_script = f"""$ErrorActionPreference = 'Continue'
$target = '{safe_target}'
$newBin = '{safe_new}'
$workDir = '{safe_dir}'
$logFile = '{safe_log}'
$childPid = {current_pid}
$bootPid = {parent_pid}

function Write-UpdaterLog([string]$msg) {{
    try {{
        $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss.fff')
        Add-Content -LiteralPath $logFile -Value "[$ts] [PS] $msg" -Encoding UTF8 -ErrorAction SilentlyContinue
    }} catch {{}}
}}

Write-UpdaterLog "=== Updater PowerShell Started (childPid=$childPid, bootPid=$bootPid) ==="

# 1. PyInstaller 환경 변수 완전 초기화 (자식 프로세스 충돌 원천 차단)
Remove-Item Env:_MEI* -ErrorAction SilentlyContinue
Remove-Item Env:_PYI* -ErrorAction SilentlyContinue
Remove-Item Env:PYINSTALLER* -ErrorAction SilentlyContinue
Remove-Item Env:TCL_LIBRARY -ErrorAction SilentlyContinue
Remove-Item Env:TK_LIBRARY -ErrorAction SilentlyContinue
Remove-Item Env:OPENSSL_MODULES -ErrorAction SilentlyContinue
if ($env:PATH) {{
    $env:PATH = ($env:PATH -split ';' | Where-Object {{ $_ -and ($_ -notlike '*_MEI*') }}) -join ';'
}}

# 2. Python 자식 프로세스($childPid) 종료 대기 (최대 3초 대기 후 미종료 시 강제 종료)
for ($w = 0; $w -lt 15; $w++) {{
    if (-not (Get-Process -Id $childPid -ErrorAction SilentlyContinue)) {{
        break
    }}
    Start-Sleep -Milliseconds 200
}}
if (Get-Process -Id $childPid -ErrorAction SilentlyContinue) {{
    Write-UpdaterLog "childPid $childPid still alive after 3s; force stopping"
    Stop-Process -Id $childPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 300
}}
Write-UpdaterLog "childPid $childPid terminated"

# 3. PyInstaller 부트로더 부모 프로세스 및 동일 경로($target) 실행 프로세스 종료 대기 (최대 2초)
#    부트로더가 _MEI 임시 폴더를 정리하고 스스로 종료할 시간을 부여함 (-Name 필터로 1ms 초고속 조회)
$targetFull = [System.IO.Path]::GetFullPath($target)
$procName = [System.IO.Path]::GetFileNameWithoutExtension($target)
for ($w = 0; $w -lt 10; $w++) {{
    $running = @(Get-Process -Name $procName -ErrorAction SilentlyContinue | Where-Object {{
        try {{
            (-not $_.Path) -or ([System.IO.Path]::GetFullPath($_.Path) -ieq $targetFull)
        }} catch {{
            $true
        }}
    }})
    if ($running.Count -eq 0) {{
        break
    }}
    Start-Sleep -Milliseconds 200
}}

# 2초 후에도 남아있는 동일 경로 프로세스가 있다면 강제 종료하여 파일 잠금 해제 보장
$stuckProcs = @(Get-Process -Name $procName -ErrorAction SilentlyContinue | Where-Object {{
    try {{
        (-not $_.Path) -or ([System.IO.Path]::GetFullPath($_.Path) -ieq $targetFull)
    }} catch {{
        $true
    }}
}})
foreach ($sp in $stuckProcs) {{
    Write-UpdaterLog "Force stopping remaining process PID=$($sp.Id)"
    Stop-Process -Id $sp.Id -Force -ErrorAction SilentlyContinue
}}
if ($stuckProcs.Count -gt 0) {{
    Start-Sleep -Milliseconds 400
}}
Write-UpdaterLog "All old processes for $targetFull cleared"

# 4. 파일 교체 시도 (최대 20회 재시도, NTFS Rename 안전망 포함)
$copied = $false
$oldBak = "$target.old"
for ($i = 0; $i -lt 20; $i++) {{
    try {{
        Copy-Item -LiteralPath $newBin -Destination $target -Force -ErrorAction Stop
        $copied = $true
        Write-UpdaterLog "Copy-Item succeeded on attempt $($i + 1)"
        break
    }} catch {{
        $errMsg = $_.Exception.Message
        Write-UpdaterLog "Copy-Item attempt $($i + 1) failed: $errMsg. Trying NTFS rename fallback..."
        try {{
            if (Test-Path -LiteralPath $oldBak) {{
                Remove-Item -LiteralPath $oldBak -Force -ErrorAction SilentlyContinue
            }}
            if (Test-Path -LiteralPath $target) {{
                Rename-Item -LiteralPath $target -NewName ([System.IO.Path]::GetFileName($oldBak)) -Force -ErrorAction Stop
            }}
            Copy-Item -LiteralPath $newBin -Destination $target -Force -ErrorAction Stop
            $copied = $true
            Remove-Item -LiteralPath $oldBak -Force -ErrorAction SilentlyContinue
            Write-UpdaterLog "NTFS rename + Copy-Item succeeded on attempt $($i + 1)"
            break
        }} catch {{
            Write-UpdaterLog "Fallback attempt $($i + 1) failed: $($_.Exception.Message)"
            if ((-not (Test-Path -LiteralPath $target)) -and (Test-Path -LiteralPath $oldBak)) {{
                try {{
                    Rename-Item -LiteralPath $oldBak -NewName ([System.IO.Path]::GetFileName($target)) -Force -ErrorAction SilentlyContinue
                }} catch {{}}
            }}
            Start-Sleep -Milliseconds 500
        }}
    }}
}}

# 5. 교체 완료 후 프로그램 재실행 및 임시 파일 정리
if ($copied) {{
    try {{
        $proc = Start-Process -FilePath $target -WorkingDirectory $workDir -PassThru -ErrorAction Stop
        Write-UpdaterLog "Launched updated executable: PID=$($proc.Id) Path=$target"
    }} catch {{
        Write-UpdaterLog "Start-Process failed ($($_.Exception.Message)), retrying via explorer.exe..."
        try {{
            Start-Process -FilePath 'explorer.exe' -ArgumentList "`"$target`""
            Write-UpdaterLog "Launched updated executable via explorer.exe"
        }} catch {{
            Write-UpdaterLog "CRITICAL: Failed to launch updated executable: $($_.Exception.Message)"
        }}
    }}
    Start-Sleep -Seconds 2
    Remove-Item -LiteralPath $newBin -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $oldBak -Force -ErrorAction SilentlyContinue
    Write-UpdaterLog "=== Updater Finished Successfully ==="
}} else {{
    Write-UpdaterLog "CRITICAL: Failed to replace binary after 20 attempts"
}}
Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
"""

        ps1_path = os.path.join(tempfile.gettempdir(), f"qt_updater_{current_pid}.ps1")
        with open(ps1_path, "w", encoding="utf-8-sig") as f:
            f.write(ps_script)

        clean_env = os.environ.copy()
        for k in list(clean_env.keys()):
            k_up = k.upper()
            if (
                k_up.startswith("_MEI")
                or k_up.startswith("_PYI")
                or k_up.startswith("PYINSTALLER")
                or k_up in ("TCL_LIBRARY", "TK_LIBRARY", "OPENSSL_MODULES")
            ):
                clean_env.pop(k, None)

        if "PATH" in clean_env and clean_env["PATH"]:
            clean_env["PATH"] = os.pathsep.join(
                p for p in clean_env["PATH"].split(os.pathsep)
                if p and "_MEI" not in p
            )

        startupinfo = None
        creationflags = 0
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW

        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-WindowStyle", "Hidden",
                "-File", ps1_path
            ],
            cwd=tempfile.gettempdir(),
            env=clean_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=startupinfo,
            creationflags=creationflags,
            close_fds=True
        )

        _log_updater_event(f"PowerShell 업데이터 스크립트 실행 완료 ({ps1_path}). os._exit(0) 호출.")
        os._exit(0)
    except Exception as e:
        _log_updater_event(f"apply_update_and_restart 치명적 오류: {e}")
        try:
            from tkinter import messagebox
            messagebox.showerror("업데이트 적용 오류", f"프로그램 교체 중 오류가 발생했습니다:\n{e}")
        except Exception:
            pass


class UpdateDialog(ctk.CTkToplevel):
    def __init__(self, parent, update_info):
        super().__init__(parent)
        self.parent = parent
        self.update_info = update_info
        self.is_downloading = False
        self.is_cancelled = False
        self._restart_scheduled = False
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

        if getattr(sys, 'frozen', False):
            exe_path = os.path.abspath(sys.executable).lower()
            temp_path = tempfile.gettempdir().lower()
            if temp_path in exe_path or "\\temp\\" in exe_path or "\\temp1_" in exe_path:
                from tkinter import messagebox
                messagebox.showwarning(
                    "압축 파일 내 실행 감지",
                    "⚠️ 현재 프로그램이 압축 파일(.zip) 내부에서 직접 실행되었습니다.\n\n"
                    "Windows 보안 정책상 압축 파일 내부에서는 실행 파일을 교체할 수 없습니다.\n\n"
                    "다운로드 폴더의 ZIP 파일 압축을 완전히 해제하신 후,\n"
                    "추출된 폴더 안의 QuestTranslatorPro.exe를 실행하여 업데이트해 주세요!"
                )
                self.status_label.configure(text="⚠️ 압축을 해제한 폴더에서 실행해 주세요.", text_color="#ef4444")
                return

        self.is_downloading = True
        self.is_cancelled = False
        self._restart_scheduled = False
        self.update_btn.configure(state="disabled", text="다운로드 중...")
        self.close_btn.configure(text="취소")

        self.download_state = {
            "progress": (0, 0),
            "status_msg": "다운로드 준비 중...",
            "status_color": "#d4d4d8",
            "is_done": False,
            "is_cancelled": False,
            "error": None,
            "target_path": None,
        }

        threading.Thread(target=self._download_worker, args=(url,), daemon=True).start()
        self.after(50, self._poll_download)

    def _download_worker(self, url):
        download_target = None
        try:
            cleanup_old_update_temp_files()
            temp_dir = tempfile.gettempdir()
            asset_name = (self.update_info.get("asset_name") or url).lower()
            is_zip = asset_name.endswith(".zip")

            if is_zip:
                download_target = os.path.join(temp_dir, f"QuestTranslatorPro_update_{os.getpid()}.zip")
            else:
                download_target = os.path.join(temp_dir, f"QuestTranslatorPro_update_{os.getpid()}.exe")

            _log_updater_event(f"다운로드 시작: url={url} -> {download_target}")

            def on_prog(downloaded, total):
                self.download_state["progress"] = (downloaded, total)

            def check_cancel():
                return self.is_cancelled

            ok = download_file_with_progress(url, download_target, on_prog, check_cancel)
            if ok and not self.is_cancelled:
                if is_zip:
                    self.download_state["status_msg"] = "📦 압축 파일에서 실행 파일을 추출하는 중..."
                    self.download_state["status_color"] = "#fb923c"
                    extracted_exe = os.path.join(temp_dir, f"QuestTranslatorPro_extracted_{os.getpid()}.exe")
                    with zipfile.ZipFile(download_target, 'r') as zf:
                        exe_entry = None
                        for name in zf.namelist():
                            if os.path.basename(name).lower() == "questtranslatorpro.exe":
                                exe_entry = name
                                break
                        if not exe_entry:
                            raise Exception("압축 파일 내에서 QuestTranslatorPro.exe를 찾을 수 없습니다.")
                        with zf.open(exe_entry) as src_f, open(extracted_exe, 'wb') as dst_f:
                            shutil.copyfileobj(src_f, dst_f, length=1024 * 1024)

                    # 추출 완료 후 대용량 ZIP 임시 파일 즉시 삭제 (Temp 용량 누적 방지)
                    try:
                        os.remove(download_target)
                    except Exception:
                        pass

                    _validate_pe_executable(extracted_exe)
                    self.download_state["target_path"] = extracted_exe
                else:
                    _validate_pe_executable(download_target)
                    self.download_state["target_path"] = download_target

                _log_updater_event(f"다운로드 및 검증 완료: target_path={self.download_state['target_path']}")
                self.download_state["status_msg"] = "✅ 다운로드 완료! 프로그램을 교체하고 재시작합니다..."
                self.download_state["status_color"] = "#22c55e"
                self.download_state["is_done"] = True
            elif self.is_cancelled:
                self.download_state["is_cancelled"] = True
                if download_target and os.path.exists(download_target):
                    try:
                        os.remove(download_target)
                    except Exception:
                        pass
        except Exception as e:
            _log_updater_event(f"다운로드/추출 오류: {e}")
            self.download_state["error"] = str(e)
            if download_target and os.path.exists(download_target):
                try:
                    os.remove(download_target)
                except Exception:
                    pass

    def _poll_download(self):
        st = getattr(self, "download_state", None)
        if not st:
            return

        dl, tot = st["progress"]
        if tot > 0:
            pct = dl / tot
            dl_mb = dl / (1024 * 1024)
            tot_mb = tot / (1024 * 1024)
            self.prog_bar.set(pct)
            if not st["is_done"] and not st["error"]:
                self.status_label.configure(
                    text=f"다운로드 중... {int(pct*100)}% ({dl_mb:.1f}MB / {tot_mb:.1f}MB)",
                    text_color="#d4d4d8"
                )

        if st["status_msg"]:
            self.status_label.configure(text=st["status_msg"], text_color=st["status_color"])

        if st["error"]:
            self.status_label.configure(text=f"❌ 다운로드 오류: {st['error']}", text_color="#ef4444")
            self.update_btn.configure(state="normal", text="다시 시도")
            self.close_btn.configure(state="normal", text="닫기")
            return

        if st["is_cancelled"]:
            self.status_label.configure(text="업데이트가 취소되었습니다.", text_color="#a1a1aa")
            return

        if st["is_done"]:
            if not getattr(self, "_restart_scheduled", False):
                self._restart_scheduled = True
                self.close_btn.configure(state="disabled")
                target_path = st["target_path"]
                self.after(800, lambda: apply_update_and_restart(target_path))
            return

        self.after(50, self._poll_download)

    def _on_close(self):
        if getattr(self, "_restart_scheduled", False):
            return
        if self.is_downloading:
            self.is_cancelled = True
        self.grab_release()
        self.destroy()


def show_update_dialog(parent, update_info):
    return UpdateDialog(parent, update_info)
