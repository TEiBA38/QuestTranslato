"""
session_logger.py - 앱 실행~종료 전 과정의 성공/실패 및 크래시를 단일 .txt 세션 파일로 기록·수집하는 진단 로거.
표준 라이브러리만 사용하므로 UI/백엔드 모든 모듈에서 순환 참조 없이 안전하게 import할 수 있습니다.
"""
import datetime
import faulthandler
import glob
import os
import re
import sys
import threading
import traceback
from typing import Optional


_APP_EXIT_MARKER = "APP_EXIT |"
_MAX_REPORT_BYTES = 200_000

_MASK_PATTERNS = [
    # Discord webhook URLs
    (re.compile(r'https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/[^\s"\'<>]+', re.IGNORECASE), "[REDACTED_DISCORD_WEBHOOK]"),
    # Gemini API keys
    (re.compile(r'\bAIza[0-9A-Za-z\-_]{20,}\b'), "[REDACTED_GEMINI_KEY]"),
    # Anthropic API keys
    (re.compile(r'\bsk-ant-[0-9A-Za-z\-_]{16,}\b'), "[REDACTED_ANTHROPIC_KEY]"),
    # OpenAI / DeepSeek / generic sk- keys
    (re.compile(r'\bsk-[0-9A-Za-z\-_]{16,}\b'), "[REDACTED_API_KEY]"),
    # Authorization Bearer / Bot tokens
    (re.compile(r'\b(Bearer|Bot)\s+[A-Za-z0-9\-._~+/=]{16,}', re.IGNORECASE), r"\1 [REDACTED_TOKEN]"),
    # Windows user profile paths (C:\Users\<username>\...)
    (re.compile(r'([A-Za-z]:\\Users\\)([^\\/:*?"<>|\r\n]+)', re.IGNORECASE), r"\1<USER>"),
    (re.compile(r'([A-Za-z]:/Users/)([^/:*?"<>|\r\n]+)', re.IGNORECASE), r"\1<USER>"),
    # macOS / Linux user home paths
    (re.compile(r'(/(?:Users|home)/)([^/\s:*?"<>|\r\n]+)'), r"\1<USER>"),
]


def sanitize_text(text: str) -> str:
    """API 키, 토큰, Discord 웹훅, 사용자 계정 경로 등 민감정보를 자동 마스킹합니다."""
    if not text:
        return ""
    out = str(text)
    for pattern, repl in _MASK_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def _default_log_dir() -> str:
    import tempfile
    appdata = os.getenv("APPDATA") or os.path.expanduser("~")
    primary = os.path.join(appdata, "QuestTranslatorPro", "logs")
    try:
        os.makedirs(primary, exist_ok=True)
        return primary
    except Exception:
        fallback = os.path.join(tempfile.gettempdir(), "QuestTranslatorPro_logs")
        os.makedirs(fallback, exist_ok=True)
        return fallback


class SessionDiagnosticLogger:
    """앱 실행마다 session_*.txt 파일을 생성해 단계별 [OK]/[WARN]/[FAIL]/[CRASH]를 기록하고,
    직전 비정상 종료(크래시) 세션 탐지·민감정보 마스킹·200KB 안전 절단 번들 생성을 전담합니다."""

    def __init__(self, log_dir: Optional[str] = None, max_files: int = 5):
        self._lock = threading.Lock()
        self.log_dir = log_dir or _default_log_dir()
        os.makedirs(self.log_dir, exist_ok=True)
        self.max_files = max(2, int(max_files))
        self.session_path: Optional[str] = None
        self.previous_crashed_session_path: Optional[str] = None
        self._fault_fh = None
        self._exited = False
        self._hooks_installed = False
        self._orig_sys_excepthook = None
        self._orig_thread_excepthook = None
        self._start_session()

    def _is_crashed_session_file(self, filepath: str) -> bool:
        """직전 세션 파일에 [CRASH]/Fatal 에러가 있거나 정상 종료(APP_EXIT) 마커가 없으면 크래시로 판별합니다."""
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if not content.strip():
                return False
            if "[CRASH]" in content or "Fatal Python error" in content:
                return True
            if _APP_EXIT_MARKER not in content:
                return True
            return False
        except Exception:
            return False

    def _start_session(self):
        # 1. 새 파일 생성 전, 직전 세션 파일이 비정상 종료(크래시)였는지 먼저 판별
        existing = sorted(
            glob.glob(os.path.join(self.log_dir, "session_*.txt")),
            key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0.0,
        )
        if existing:
            latest_prev = existing[-1]
            if self._is_crashed_session_file(latest_prev):
                self.previous_crashed_session_path = latest_prev

        # 2. 새 세션 파일 생성 (마이크로초 포함해 빠른 재실행 시 파일명 충돌 방지)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        pid = os.getpid()
        self.session_path = os.path.join(self.log_dir, f"session_{ts}_{pid}.txt")
        try:
            with open(self.session_path, "w", encoding="utf-8") as f:
                f.write(f"# QuestTranslator Pro Session Log ({ts}, PID={pid})\n")
        except Exception:
            self.session_path = None

        # 3. 오래된 세션 파일 로테이션 (최근 max_files개 유지, 크래시 직전 파일은 항상 보존)
        all_files = sorted(
            glob.glob(os.path.join(self.log_dir, "session_*.txt")),
            key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0.0,
        )
        while len(all_files) > self.max_files:
            oldest = all_files.pop(0)
            if oldest in (self.session_path, self.previous_crashed_session_path):
                continue
            try:
                os.remove(oldest)
            except Exception:
                pass

    def _append_raw(self, text: str, fsync: bool = False):
        if not self.session_path:
            return
        safe = sanitize_text(text)
        with self._lock:
            try:
                with open(self.session_path, "a", encoding="utf-8") as f:
                    f.write(safe)
                    f.flush()
                    if fsync:
                        try:
                            os.fsync(f.fileno())
                        except Exception:
                            pass
            except Exception:
                pass

    def log_event(self, status: str, stage: str, detail: str = ""):
        """단계별 실행 결과를 타임스탬프와 함께 세션 .txt에 한 줄로 기록합니다."""
        st = str(status or "INFO").upper().strip()
        if st not in ("OK", "WARN", "FAIL", "CRASH", "INFO"):
            st = "INFO"
        tag = f"[{st}]".ljust(7)
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        clean_detail = str(detail or "").replace("\r", " ").replace("\n", " ").strip()
        if clean_detail:
            line = f"[{ts}] {tag} {stage} | {clean_detail}\n"
        else:
            line = f"[{ts}] {tag} {stage} |\n"
        self._append_raw(line, fsync=(st in ("FAIL", "CRASH")))

    def log_crash(self, source: str, exc_type, exc_value, exc_tb):
        """미처리 예외(크래시)와 전체 스택트레이스를 세션 .txt 끝에 즉시 기록·동기화합니다."""
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        tb_str = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        block = (
            f"\n[{ts}] [CRASH] {source} | {getattr(exc_type, '__name__', str(exc_type))}: {exc_value}\n"
            f"{tb_str}\n"
        )
        self._append_raw(block, fsync=True)

    def install_crash_hooks(self, tk_app=None):
        """sys.excepthook, threading.excepthook, Tkinter report_callback_exception, faulthandler를 일괄 훅킹합니다."""
        if not self._hooks_installed:
            self._hooks_installed = True
            self._orig_sys_excepthook = sys.excepthook
            self._orig_thread_excepthook = getattr(threading, "excepthook", None)

            def _sys_hook(exc_type, exc_value, exc_tb):
                if not issubclass(exc_type, KeyboardInterrupt):
                    self.log_crash("UNCAUGHT_EXCEPTION", exc_type, exc_value, exc_tb)
                if self._orig_sys_excepthook:
                    self._orig_sys_excepthook(exc_type, exc_value, exc_tb)

            sys.excepthook = _sys_hook

            if hasattr(threading, "excepthook"):
                def _thread_hook(args):
                    if not issubclass(args.exc_type, SystemExit):
                        t_name = getattr(args.thread, "name", "Thread")
                        self.log_crash(f"THREAD_EXCEPTION({t_name})", args.exc_type, args.exc_value, args.exc_traceback)
                    if self._orig_thread_excepthook:
                        self._orig_thread_excepthook(args)

                threading.excepthook = _thread_hook

            # 네이티브 C 레벨 크래시(Segfault 등) 스택 덤프 활성화
            if self.session_path:
                try:
                    self._fault_fh = open(self.session_path, "a", encoding="utf-8")
                    faulthandler.enable(file=self._fault_fh, all_threads=True)
                except Exception:
                    self._fault_fh = None

        if tk_app is not None:
            orig_tk_hook = getattr(tk_app, "report_callback_exception", None)

            def _tk_hook(exc_type, exc_value, exc_tb):
                self.log_crash("TK_CALLBACK_EXCEPTION", exc_type, exc_value, exc_tb)
                if orig_tk_hook:
                    orig_tk_hook(exc_type, exc_value, exc_tb)

            try:
                tk_app.report_callback_exception = _tk_hook
            except Exception:
                pass

    def mark_exit(self, detail: str = "정상 종료"):
        """정상 종료 마커를 기록합니다 (다음 실행 시 크래시 여부를 판별하는 기준)."""
        if self._exited:
            return
        self._exited = True
        self.log_event("OK", "APP_EXIT", detail)
        if self._fault_fh is not None:
            try:
                faulthandler.disable()
            except Exception:
                pass
            try:
                self._fault_fh.close()
            except Exception:
                pass
            self._fault_fh = None

    def has_previous_crash(self) -> bool:
        return bool(self.previous_crashed_session_path and os.path.isfile(self.previous_crashed_session_path))

    def _read_file_safe(self, filepath: Optional[str]) -> str:
        if not filepath or not os.path.isfile(filepath):
            return ""
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception:
            return ""

    def _truncate_safely(self, header: str, body: str, max_bytes: int = _MAX_REPORT_BYTES) -> str:
        full = header + body
        if len(full.encode("utf-8")) <= max_bytes:
            return full

        lines = body.splitlines()
        important = []
        i = 0
        while i < len(lines):
            ln = lines[i]
            if "[CRASH]" in ln or "[FAIL]" in ln or "Fatal Python error" in ln:
                important.append(ln)
                if "[CRASH]" in ln or "Fatal Python error" in ln:
                    for extra in lines[i + 1:i + 15]:
                        important.append(extra)
                    i += 15
                    continue
            i += 1

        important_block = ""
        if important:
            important_block = "\n--- [보존된 FAIL / CRASH 핵심 기록] ---\n" + "\n".join(important[-120:]) + "\n"

        prefix = header + important_block + "\n--- [최근 세션 로그 (용량 제한으로 앞부분 생략)] ---\n"
        budget = max(4096, max_bytes - len(prefix.encode("utf-8")) - 128)

        tail_lines = []
        used = 0
        for ln in reversed(lines):
            b_len = len((ln + "\n").encode("utf-8"))
            if used + b_len > budget:
                break
            tail_lines.append(ln)
            used += b_len
        tail_lines.reverse()
        return prefix + "\n".join(tail_lines) + "\n"

    def build_report_bundle(self, system_info: Optional[dict] = None, max_bytes: int = _MAX_REPORT_BYTES) -> str:
        """표준 진단 헤더 + 현재 세션 + (존재 시) 직전 크래시 세션을 마스킹·용량 제한 적용해 .txt 전문으로 반환합니다."""
        info = system_info or {}
        header_lines = [
            "================ [SESSION HEADER] ================",
            f"App: QuestTranslator Pro v{info.get('version', 'unknown')} ({info.get('runtime', 'EXE' if getattr(sys, 'frozen', False) else 'Python')})",
            f"OS: {info.get('os', sys.platform)}",
            f"Engine: {info.get('engine', 'N/A')} | Tier: {info.get('tier', 'N/A')}",
            f"Target Lang: {info.get('target_lang', 'N/A')} | Modpack: {info.get('modpack', '선택 안 됨')}",
            f"Cache: {info.get('cache_summary', 'N/A')} | Cloud Sync: {info.get('cloud_sync', 'N/A')}",
            f"Previous Crash Detected: {'YES' if self.has_previous_crash() else 'NO'}",
            "==================================================",
            "",
        ]
        header = sanitize_text("\n".join(header_lines) + "\n")

        sections = []
        if self.has_previous_crash():
            prev_name = os.path.basename(self.previous_crashed_session_path)
            prev_text = self._read_file_safe(self.previous_crashed_session_path).strip()
            sections.append(f"=== [직전 크래시 세션: {prev_name}] ===\n{prev_text}\n")

        cur_name = os.path.basename(self.session_path) if self.session_path else "session_current.txt"
        cur_text = self._read_file_safe(self.session_path).strip()
        sections.append(f"=== [현재 세션: {cur_name}] ===\n{cur_text}\n")

        body = sanitize_text("\n".join(sections))
        return self._truncate_safely(header, body, max_bytes=max_bytes)


default_session_logger = SessionDiagnosticLogger()


def log_event(status: str, stage: str, detail: str = ""):
    default_session_logger.log_event(status, stage, detail)


def log_crash(source: str, exc_type, exc_value, exc_tb):
    default_session_logger.log_crash(source, exc_type, exc_value, exc_tb)


def install_crash_hooks(tk_app=None):
    default_session_logger.install_crash_hooks(tk_app=tk_app)


def mark_exit(detail: str = "정상 종료"):
    default_session_logger.mark_exit(detail=detail)


def has_previous_crash() -> bool:
    return default_session_logger.has_previous_crash()


def build_report_bundle(system_info: Optional[dict] = None, max_bytes: int = _MAX_REPORT_BYTES) -> str:
    return default_session_logger.build_report_bundle(system_info=system_info, max_bytes=max_bytes)
