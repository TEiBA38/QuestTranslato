"""
QuestTranslator Pro - 피드백 및 버그 제보 매니저 (Cloudflare Worker 서버리스 연동)
- Cloudflare Worker 프록시(POST /feedback/report)를 통한 100% 안전한 디스코드 제보 및 스레드 생성
- 3초 주기 실시간 폴링(POST /feedback/check)을 통한 답변 및 ⭕/❌ 반응 감지 & 즉시 회수
- 로컬 클라이언트에서 디스코드 봇 토큰 및 웹훅 URL 노출을 원천 제거
"""
import os
import json
import time
import uuid
import platform
import threading
from datetime import datetime
import requests

try:
    import customtkinter as ctk
    import tkinter as tk
    from tkinter import messagebox
except Exception:
    ctk = None
    tk = None
    messagebox = None

from constants import FONT_NAME, APP_VERSION


def _get_worker_url():
    try:
        import translation_memory
        w_url = translation_memory.get_worker_url()
        if w_url:
            return w_url.rstrip("/")
    except Exception:
        pass
    return "https://quest-translator-sync.kimraon84.workers.dev"


def _get_tickets_file():
    from translation_memory import _get_cache_dir
    cache_dir = _get_cache_dir()
    return os.path.join(cache_dir, "my_feedback_tickets.json")


def save_local_ticket(ticket_data):
    """유저가 제보한 티켓 정보를 로컬에 저장"""
    try:
        t_file = _get_tickets_file()
        tickets = []
        if os.path.exists(t_file):
            try:
                with open(t_file, "r", encoding="utf-8") as f:
                    tickets = json.load(f)
            except Exception:
                tickets = []
        existing_idx = next((i for i, t in enumerate(tickets) if t.get("ticket_id") == ticket_data.get("ticket_id")), None)
        if existing_idx is not None:
            tickets[existing_idx] = ticket_data
        else:
            tickets.insert(0, ticket_data)

        with open(t_file, "w", encoding="utf-8") as f:
            json.dump(tickets, f, ensure_ascii=False, indent=2)
    except Exception as e:
        import logging
        logging.warning(f"Failed to save local ticket: {e}")


def save_all_local_tickets(tickets):
    try:
        t_file = _get_tickets_file()
        with open(t_file, "w", encoding="utf-8") as f:
            json.dump(tickets, f, ensure_ascii=False, indent=2)
    except Exception as e:
        import logging
        logging.warning(f"Failed to save all tickets: {e}")


def delete_local_ticket(ticket_id):
    """로컬 저장소에서 특정 티켓 삭제"""
    try:
        tickets = get_local_tickets()
        new_tickets = [t for t in tickets if t.get("ticket_id") != ticket_id]
        save_all_local_tickets(new_tickets)
        return new_tickets
    except Exception as e:
        import logging
        logging.warning(f"Failed to delete local ticket: {e}")
        return []


def get_local_tickets():
    """로컬에 저장된 내 제보 내역 목록 반환"""
    try:
        t_file = _get_tickets_file()
        if os.path.exists(t_file):
            with open(t_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def is_ticket_active(t):
    """
    티켓이 현재 감시 대상인지 확인:
    - 피드백: 'confirmed'(확인함) 상태가 아니면 계속 감시. 'confirmed'라도 15분간 재클릭 감지
    - 버그 제보: 'resolved'(해결됨) 상태가 아니면 계속 감시. 'resolved'라도 15분간 재클릭 감지
    """
    cat = t.get("category", "bug")
    st = t.get("status")
    done_status = "confirmed" if cat == "feedback" else "resolved"

    if st != done_status:
        return True

    up_at_str = t.get("status_updated_at")
    if up_at_str:
        try:
            up_dt = datetime.strptime(up_at_str, "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - up_dt).total_seconds() < 900:  # 15분 동안 반응 변경 계속 감시
                return True
        except Exception:
            pass
    return False


def has_unresolved_tickets():
    """감시 대상인 활성 티켓이 있는지 확인"""
    tickets = get_local_tickets()
    return any(is_ticket_active(t) for t in tickets)


def generate_ticket_id(category="bug"):
    date_str = datetime.now().strftime("%Y%m%d")
    short_uid = uuid.uuid4().hex[:4].upper()
    prefix = "BUG" if category == "bug" else "FB"
    return f"{prefix}-{date_str}-{short_uid}"


def send_discord_report(ticket_id, category, title, description, system_info, log_text=None, webhook_url=None, bot_token=None):
    """
    Cloudflare Worker(POST /feedback/report)로 제보 전송하여
    안전하게 디스코드 웹훅 발송, 하위 스레드 생성 및 ⭕/❌ 기본 반응 부착
    """
    worker_url = _get_worker_url()
    endpoint = f"{worker_url}/feedback/report"

    payload = {
        "ticket_id": ticket_id,
        "category": category,
        "title": title,
        "description": description,
        "system_info": system_info,
        "log_text": log_text
    }

    resp = requests.post(endpoint, json=payload, timeout=25)
    if resp.status_code != 200:
        try:
            err_detail = resp.json().get("error", "")
        except Exception:
            err_detail = resp.text[:200]
        raise RuntimeError(f"서버 오류 (HTTP {resp.status_code}): {err_detail}")
    try:
        data = resp.json()
    except Exception:
        if "QuestTranslator Cloudflare R2 Sync Server OK" in resp.text:
            raise RuntimeError(
                "Cloudflare Worker에 최신 코드가 아직 배포(Deploy)되지 않았습니다.\n"
                "Cloudflare 대시보드(Workers & Pages) -> quest-translator-sync -> Edit code에서\n"
                "최신 cloudflare_worker.js 코드를 붙여넣고 [Deploy]를 눌러주세요."
            )
        raise RuntimeError(f"서버 응답 오류 (JSON 파싱 실패):\n{resp.text[:200]}")

    return {
        "ticket_id": ticket_id,
        "channel_id": data.get("channel_id"),
        "message_id": data.get("message_id"),
        "thread_id": data.get("thread_id")
    }


def check_developer_replies(force_all=False):
    """
    Cloudflare Worker(POST /feedback/check)를 통해
    3초마다 디스코드 스레드 답글 및 ⭕/❌ 개발자 반응을 실시간 확인하고 회수(2 -> 1)
    """
    tickets = get_local_tickets()
    if not tickets:
        return {"new_replies": [], "status_updates": []}

    # 활성 티켓 필터링 (수동 새로고침 시 전체 조회, 백그라운드 시 활성 티켓만 조회)
    active_tickets = tickets if force_all else [t for t in tickets if is_ticket_active(t)]
    if not active_tickets:
        return {"new_replies": [], "status_updates": []}

    worker_url = _get_worker_url()
    endpoint = f"{worker_url}/feedback/check"

    tickets_payload = []
    for t in active_tickets:
        if t.get("thread_id") or t.get("message_id"):
            known_ids = [r.get("msg_id") for r in t.get("replies", []) if r.get("msg_id")]
            tickets_payload.append({
                "ticket_id": t.get("ticket_id"),
                "category": t.get("category", "bug"),
                "thread_id": t.get("thread_id"),
                "channel_id": t.get("channel_id"),
                "message_id": t.get("message_id"),
                "status": t.get("status"),
                "known_msg_ids": known_ids
            })

    if not tickets_payload:
        return {"new_replies": [], "status_updates": []}

    try:
        resp = requests.post(endpoint, json={"tickets": tickets_payload}, timeout=10)
        if resp.status_code != 200:
            return {"new_replies": [], "status_updates": []}

        data = resp.json()
        new_replies = data.get("new_replies", [])
        status_updates = data.get("status_updates", [])
        deleted_ticket_ids = data.get("deleted_ticket_ids", [])
        has_update = False

        # 통신 중 사용자가 새 제보를 올리거나 삭제했을 수 있으므로 최신 파일을 다시 읽어 병합 (유실 방지)
        tickets = get_local_tickets()

        # 디스코드에서 원본 메시지가 삭제된 티켓은 사용자 로컬 목록에서도 즉시 자동 제거
        if deleted_ticket_ids:
            tickets = [t for t in tickets if t.get("ticket_id") not in deleted_ticket_ids]
            has_update = True

        tickets_by_id = {t.get("ticket_id"): t for t in tickets}

        if new_replies:
            for r in new_replies:
                t = tickets_by_id.get(r.get("ticket_id"))
                if t:
                    r["category"] = t.get("category", "bug")
                    r["title"] = t.get("title", "")
                    replies = t.setdefault("replies", [])
                    replies.append({
                        "msg_id": r.get("msg_id"),
                        "author": r.get("author"),
                        "content": r.get("reply"),
                        "timestamp": r.get("timestamp")
                    })
                    notified = set(t.get("notified_reply_ids", []))
                    notified.add(r.get("msg_id"))
                    t["notified_reply_ids"] = list(notified)
                    if t.get("status") not in ("resolved", "rejected", "confirmed"):
                        t["status"] = "answered"
                    t["status_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    has_update = True

        if status_updates:
            for s in status_updates:
                t = tickets_by_id.get(s.get("ticket_id"))
                if t:
                    s["category"] = t.get("category", "bug")
                    s["title"] = t.get("title", "")
                    t["status"] = s.get("status")
                    t["status_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    has_update = True

        if has_update:
            save_all_local_tickets(tickets)

        return {
            "new_replies": new_replies,
            "status_updates": status_updates,
            "deleted_ticket_ids": deleted_ticket_ids
        }
    except Exception:
        return {"new_replies": [], "status_updates": [], "deleted_ticket_ids": []}


class FeedbackDialog(ctk.CTkToplevel):
    def __init__(self, parent, webhook_url=None, bot_token=None):
        super().__init__(parent)
        self.parent = parent
        self.worker_url = _get_worker_url()

        self.title("피드백 및 버그 제보")
        self.geometry("560x700")
        self.resizable(False, False)
        self.configure(fg_color="#121217")

        self.transient(parent)
        self.grab_set()

        self._build_ui()
        self._center_window()

    def _center_window(self):
        self.update_idletasks()
        w = self.winfo_width()
        h = self.winfo_height()
        x = (self.winfo_screenwidth() // 2) - (w // 2)
        y = (self.winfo_screenheight() // 2) - (h // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _build_ui(self):
        container = ctk.CTkFrame(self, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=20, pady=16)

        header_frame = ctk.CTkFrame(container, fg_color="transparent")
        header_frame.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(
            header_frame,
            text="💬 피드백 / 버그 제보 센터",
            font=ctk.CTkFont(family=FONT_NAME, size=20, weight="bold"),
            text_color="#f8fafc"
        ).pack(side="left")

        self.current_tab = "new"
        self.tab_btn_frame = ctk.CTkSegmentedButton(
            container,
            values=["✍️ 새 제보 작성", "📬 내 제보 및 답변"],
            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
            selected_color="#f97316",
            selected_hover_color="#fb923c",
            unselected_color="#272732",
            unselected_hover_color="#373746",
            text_color="#f8fafc",
            command=self._on_tab_changed
        )
        self.tab_btn_frame.set("✍️ 새 제보 작성")
        self.tab_btn_frame.pack(fill="x", pady=(0, 14))

        self.content_container = ctk.CTkFrame(container, fg_color="transparent")
        self.content_container.pack(fill="both", expand=True)

        self._build_new_report_tab()
        self._build_my_tickets_tab()
        self._show_tab("new")

    def _on_tab_changed(self, value):
        if "새 제보" in value:
            self._show_tab("new")
        else:
            self._show_tab("history")
            self._refresh_tickets_list()

    def _show_tab(self, tab_key):
        self.current_tab = tab_key
        if tab_key == "new":
            self.history_frame.pack_forget()
            self.new_report_frame.pack(fill="both", expand=True)
        else:
            self.new_report_frame.pack_forget()
            self.history_frame.pack(fill="both", expand=True)

    def _build_new_report_tab(self):
        self.new_report_frame = ctk.CTkFrame(self.content_container, fg_color="transparent")

        cat_card = ctk.CTkFrame(self.new_report_frame, fg_color="#181820", corner_radius=12, border_width=1, border_color="#272732")
        cat_card.pack(fill="x", pady=(0, 10))

        cat_inner = ctk.CTkFrame(cat_card, fg_color="transparent")
        cat_inner.pack(fill="x", padx=14, pady=10)

        ctk.CTkLabel(
            cat_inner,
            text="제보 유형:",
            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
            text_color="#cbd5e1"
        ).pack(side="left", padx=(0, 14))

        self.category_var = tk.StringVar(value="bug")
        self.radio_bug = ctk.CTkRadioButton(
            cat_inner,
            text="🐞 버그 제보",
            variable=self.category_var,
            value="bug",
            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
            text_color="#f87171",
            fg_color="#ef4444"
        )
        self.radio_bug.pack(side="left", padx=(0, 16))

        self.radio_fb = ctk.CTkRadioButton(
            cat_inner,
            text="💡 기능 건의 및 피드백",
            variable=self.category_var,
            value="feedback",
            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
            text_color="#60a5fa",
            fg_color="#3b82f6"
        )
        self.radio_fb.pack(side="left")

        ctk.CTkLabel(
            self.new_report_frame,
            text="제목",
            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
            text_color="#e2e8f0"
        ).pack(anchor="w", pady=(0, 4))

        self.title_entry = ctk.CTkEntry(
            self.new_report_frame,
            placeholder_text="예: [ATM9] 1.20.1 퀘스트 번역 도중 튕김 현상 발생",
            font=ctk.CTkFont(family=FONT_NAME, size=12),
            height=36,
            fg_color="#181820",
            border_color="#2e2e3a",
            text_color="#f8fafc"
        )
        self.title_entry.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(
            self.new_report_frame,
            text="상세 설명",
            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
            text_color="#e2e8f0"
        ).pack(anchor="w", pady=(0, 4))

        self.desc_textbox = ctk.CTkTextbox(
            self.new_report_frame,
            font=ctk.CTkFont(family=FONT_NAME, size=12),
            height=130,
            fg_color="#181820",
            border_width=1,
            border_color="#2e2e3a",
            text_color="#f8fafc"
        )
        self.desc_textbox.pack(fill="x", pady=(0, 10))

        opts_card = ctk.CTkFrame(self.new_report_frame, fg_color="#181820", corner_radius=10, border_width=1, border_color="#272732")
        opts_card.pack(fill="x", pady=(0, 14))

        opts_inner = ctk.CTkFrame(opts_card, fg_color="transparent")
        opts_inner.pack(fill="x", padx=12, pady=10)

        self.attach_log_var = tk.BooleanVar(value=True)
        self.attach_log_cb = ctk.CTkCheckBox(
            opts_inner,
            text="최근 실행/에러 로그 및 기본 사양 정보 함께 전송 (권장)",
            variable=self.attach_log_var,
            font=ctk.CTkFont(family=FONT_NAME, size=11),
            text_color="#cbd5e1",
            fg_color="#f97316",
            hover_color="#fb923c"
        )
        self.attach_log_cb.pack(anchor="w")

        info_preview = self._get_system_info_summary()
        self.info_label = ctk.CTkLabel(
            opts_inner,
            text=f"• 첨부 정보: {info_preview}",
            font=ctk.CTkFont(family=FONT_NAME, size=10),
            text_color="#64748b"
        )
        self.info_label.pack(anchor="w", padx=(24, 0), pady=(3, 0))

        btn_row = ctk.CTkFrame(self.new_report_frame, fg_color="transparent")
        btn_row.pack(fill="x", side="bottom")

        self.cancel_btn = ctk.CTkButton(
            btn_row,
            text="닫기",
            width=80,
            height=36,
            fg_color="#27272a",
            hover_color="#3f3f46",
            text_color="#d4d4d8",
            font=ctk.CTkFont(family=FONT_NAME, size=12),
            command=self.destroy
        )
        self.cancel_btn.pack(side="left")

        self.submit_btn = ctk.CTkButton(
            btn_row,
            text="🚀 제보 및 스레드 생성",
            height=36,
            fg_color="#f97316",
            hover_color="#fb923c",
            text_color="#ffffff",
            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
            command=self._on_submit
        )
        self.submit_btn.pack(side="right", fill="x", expand=True, padx=(10, 0))

        self.status_label = ctk.CTkLabel(
            self.new_report_frame,
            text="",
            font=ctk.CTkFont(family=FONT_NAME, size=11),
            text_color="#f97316"
        )
        self.status_label.pack(side="bottom", pady=(0, 4))

    def _build_my_tickets_tab(self):
        self.history_frame = ctk.CTkFrame(self.content_container, fg_color="transparent")

        top_bar = ctk.CTkFrame(self.history_frame, fg_color="transparent")
        top_bar.pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(
            top_bar,
            text="내가 보낸 제보와 디스코드 개발자 답변 내역입니다.",
            font=ctk.CTkFont(family=FONT_NAME, size=11),
            text_color="#94a3b8"
        ).pack(side="left")

        self.refresh_btn = ctk.CTkButton(
            top_bar,
            text="🔄 상태 및 답변 새로고침",
            width=140,
            height=28,
            fg_color="#272732",
            hover_color="#3f3f4e",
            text_color="#f8fafc",
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
            command=self._on_manual_refresh_replies
        )
        self.refresh_btn.pack(side="right")

        self.tickets_scroll = ctk.CTkScrollableFrame(
            self.history_frame,
            fg_color="#15151c",
            corner_radius=12,
            border_width=1,
            border_color="#23232c"
        )
        self.tickets_scroll.pack(fill="both", expand=True, pady=(0, 10))

    def _refresh_tickets_list(self):
        for child in self.tickets_scroll.winfo_children():
            child.destroy()

        tickets = get_local_tickets()
        if not tickets:
            empty_box = ctk.CTkFrame(self.tickets_scroll, fg_color="transparent")
            empty_box.pack(fill="both", expand=True, pady=60)
            ctk.CTkLabel(
                empty_box,
                text="📭 등록된 제보 내역이 없습니다.",
                font=ctk.CTkFont(family=FONT_NAME, size=14, weight="bold"),
                text_color="#64748b"
            ).pack()
            return

        for ticket in tickets:
            self._render_ticket_card(ticket)

    def _render_ticket_card(self, ticket):
        card = ctk.CTkFrame(self.tickets_scroll, fg_color="#1c1c24", corner_radius=10, border_width=1, border_color="#2a2a35")
        card.pack(fill="x", padx=4, pady=6)

        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=12, pady=(10, 4))

        is_bug = (ticket.get("category") == "bug")
        badge_text = "🐞 버그" if is_bug else "💡 피드백"
        badge_color = "#ef4444" if is_bug else "#3b82f6"

        ctk.CTkLabel(
            header,
            text=badge_text,
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
            text_color="#ffffff",
            fg_color=badge_color,
            corner_radius=6,
            padx=6,
            pady=2
        ).pack(side="left")

        ctk.CTkLabel(
            header,
            text=f" #{ticket.get('ticket_id', 'UNKNOWN')}",
            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
            text_color="#f8fafc"
        ).pack(side="left", padx=(6, 0))

        # 상태 배지
        cat = ticket.get("category", "bug")
        status = ticket.get("status")

        if cat == "feedback":
            if status == "confirmed":
                st_text = "🟢 확인함"
                st_color = "#10b981"
            elif status == "answered":
                st_text = "💬 답변 완료"
                st_color = "#059669"
            else:
                st_text = "🟡 확인하지 않음"
                st_color = "#d97706"
        else:
            if status == "resolved":
                st_text = "🟢 해결됨"
                st_color = "#10b981"
            elif status == "answered":
                st_text = "💬 답변 완료"
                st_color = "#059669"
            else:
                st_text = "🔴 해결안됨"
                st_color = "#ef4444"

        ctk.CTkLabel(
            header,
            text=st_text,
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
            text_color="#ffffff",
            fg_color=st_color,
            corner_radius=6,
            padx=6,
            pady=2
        ).pack(side="left", padx=(8, 0))

        # 언제든 목록에서 삭제할 수 있는 [✕ 삭제] 버튼
        is_highlighted = (status in ("resolved", "confirmed", "rejected"))
        del_btn = ctk.CTkButton(
            header,
            text="✕ 삭제",
            width=50,
            height=22,
            fg_color="#7f1d1d" if is_highlighted else "#272732",
            hover_color="#991b1b" if is_highlighted else "#3f3f4e",
            text_color="#fca5a5" if is_highlighted else "#cbd5e1",
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
            command=lambda t=ticket: self._delete_ticket(t)
        )
        del_btn.pack(side="right", padx=(6, 0))

        ctk.CTkLabel(
            header,
            text=ticket.get("created_at", "")[:16],
            font=ctk.CTkFont(family=FONT_NAME, size=10),
            text_color="#64748b"
        ).pack(side="right")

        # 제목
        ctk.CTkLabel(
            card,
            text=ticket.get("title", "제목 없음"),
            font=ctk.CTkFont(family=FONT_NAME, size=13, weight="bold"),
            text_color="#e2e8f0",
            anchor="w"
        ).pack(fill="x", padx=12, pady=(2, 6))

        # 답변 영역
        replies = ticket.get("replies", [])
        if replies:
            reply_box = ctk.CTkFrame(card, fg_color="#13241b", corner_radius=8, border_width=1, border_color="#1e3a2b")
            reply_box.pack(fill="x", padx=12, pady=(0, 10))

            for r in replies:
                author = r.get("author", "개발자")
                content = r.get("content", "")
                r_time = r.get("timestamp", "")[:16].replace("T", " ")
                ctk.CTkLabel(
                    reply_box,
                    text=f"💬 {author}님의 답변 ({r_time}):",
                    font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
                    text_color="#34d399",
                    anchor="w"
                ).pack(fill="x", padx=10, pady=(6, 2))
                ctk.CTkLabel(
                    reply_box,
                    text=content,
                    font=ctk.CTkFont(family=FONT_NAME, size=11),
                    text_color="#e2e8f0",
                    justify="left",
                    wraplength=460,
                    anchor="w"
                ).pack(fill="x", padx=10, pady=(0, 8))
        else:
            status_box = ctk.CTkFrame(card, fg_color="#20202a", corner_radius=6)
            status_box.pack(fill="x", padx=12, pady=(0, 10))
            ctk.CTkLabel(
                status_box,
                text="⏳ 개발자가 디스코드에서 제보를 확인 중입니다. 답변 작성 시 여기에 표시됩니다.",
                font=ctk.CTkFont(family=FONT_NAME, size=10),
                text_color="#94a3b8"
            ).pack(padx=8, pady=4)

    def _delete_ticket(self, ticket):
        t_id = ticket.get("ticket_id")
        title = ticket.get("title", "")
        if messagebox.askyesno("제보 삭제", f"'{title}' 제보를 목록에서 삭제하시겠습니까?", parent=self):
            delete_local_ticket(t_id)
            self._refresh_tickets_list()

    def _on_manual_refresh_replies(self):
        self.refresh_btn.configure(state="disabled", text="확인 중...")

        def bg_check():
            try:
                res = check_developer_replies(force_all=True)
                new_replies = res.get("new_replies", [])
                status_updates = res.get("status_updates", [])
                deleted_ids = res.get("deleted_ticket_ids", [])
                self.after(0, lambda: self._on_manual_refresh_done(len(new_replies), len(status_updates), len(deleted_ids)))
            except Exception as e:
                self.after(0, lambda: self._on_manual_refresh_done(-1, -1, 0, str(e)))

        threading.Thread(target=bg_check, daemon=True).start()

    def _on_manual_refresh_done(self, new_count, st_count, del_count=0, err=None):
        self.refresh_btn.configure(state="normal", text="🔄 상태 및 답변 새로고침")
        self._refresh_tickets_list()
        if err:
            messagebox.showwarning("새로고침 오류", f"확인 중 오류: {err}", parent=self)
        elif new_count > 0 or st_count > 0 or del_count > 0:
            msgs = []
            if new_count > 0:
                msgs.append(f"새로운 답변 {new_count}건")
            if st_count > 0:
                msgs.append(f"상태 업데이트 {st_count}건")
            if del_count > 0:
                msgs.append(f"삭제된 제보 {del_count}건 자동 정리")
            messagebox.showinfo("업데이트 완료", f"{' 및 '.join(msgs)}이 반영되었습니다!", parent=self)
        else:
            messagebox.showinfo("확인 완료", "새로운 답변이나 상태 변경이 없습니다. 현재 최신 상태입니다.", parent=self)

    def _get_system_info_summary(self):
        os_str = f"{platform.system()} {platform.release()}"
        engine_str = getattr(self.parent.app_state, 'selected_engine', 'Gemini')
        return f"OS: {os_str} | v{APP_VERSION} | 엔진: {engine_str}"

    def _collect_system_info(self):
        engine_name = getattr(self.parent.app_state, 'selected_engine', 'None')
        ai_model = getattr(self.parent.app_state, 'selected_ai_model', '')
        modpack_dir = getattr(self.parent, 'selected_modpack_path', '')
        modpack_name = os.path.basename(modpack_dir.rstrip(os.sep)) if modpack_dir else "선택 안 됨"

        return {
            "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
            "version": APP_VERSION,
            "engine": f"{engine_name} ({ai_model})" if ai_model else engine_name,
            "modpack": modpack_name
        }

    def _collect_recent_logs(self):
        try:
            if hasattr(self.parent, "log_textbox"):
                full_text = self.parent.log_textbox.get("1.0", "end")
                lines = [l for l in full_text.splitlines() if l.strip()]
                return "\n".join(lines[-60:])
        except Exception:
            pass
        return ""

    def _on_submit(self):
        title = self.title_entry.get().strip()
        desc = self.desc_textbox.get("1.0", "end").strip()

        if not title:
            messagebox.showwarning("입력 필요", "제보 제목을 입력해주세요.", parent=self)
            self.title_entry.focus()
            return

        if not desc:
            messagebox.showwarning("입력 필요", "제보할 내용을 작성해주세요.", parent=self)
            self.desc_textbox.focus()
            return

        category = self.category_var.get()
        ticket_id = generate_ticket_id(category)
        sys_info = self._collect_system_info()
        recent_log = self._collect_recent_logs() if self.attach_log_var.get() else ""

        self.submit_btn.configure(state="disabled", text="전송 및 스레드 생성 중...")
        self.cancel_btn.configure(state="disabled")
        self.status_label.configure(text="Cloudflare 서버를 통해 디스코드로 안전하게 전송 중입니다...")

        def worker():
            try:
                res = send_discord_report(
                    ticket_id=ticket_id,
                    category=category,
                    title=title,
                    description=desc,
                    system_info=sys_info,
                    log_text=recent_log
                )

                default_status = "rejected" if category == "bug" else "unconfirmed"
                save_local_ticket({
                    "ticket_id": ticket_id,
                    "category": category,
                    "title": title,
                    "description": desc,
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": default_status,
                    "channel_id": res.get("channel_id"),
                    "message_id": res.get("message_id"),
                    "thread_id": res.get("thread_id"),
                    "notified_reply_ids": [],
                    "replies": []
                })

                self.after(0, lambda: self._on_success(ticket_id, res.get("thread_id")))
            except Exception as e:
                err_str = str(e)
                self.after(0, lambda: self._on_failure(err_str))

        threading.Thread(target=worker, daemon=True).start()

    def _on_success(self, ticket_id, thread_id):
        cat = self.category_var.get()
        cat_name = "버그 제보" if cat == "bug" else "피드백"
        th_info = "스레드 및 반응 생성 완료" if thread_id else "메시지 전송 완료"
        if hasattr(self.parent, "log"):
            self.parent.log(f"📨 [{cat_name} 전송] 티켓: {ticket_id} ({th_info})")

        # 새 제보가 추가되었으므로 백그라운드 3초 폴링 자동 재개
        if hasattr(self.parent, "_schedule_feedback_poll"):
            self.parent._schedule_feedback_poll(500)

        guide_msg = (
            "개발자가 스레드에 답변을 남기거나 ⭕(해결됨) / ❌(해결안됨) 반응을 누르면 번역기에서 자동으로 알림을 받으실 수 있습니다."
            if cat == "bug" else
            "개발자가 스레드에 답변을 남기거나 ✅(확인함) 반응을 누르면 번역기에서 자동으로 [확인함] 알림을 받으실 수 있습니다."
        )

        messagebox.showinfo(
            "제보 완료",
            f"제보가 성공적으로 등록되었습니다!\n\n"
            f"🎫 티켓 번호: {ticket_id}\n\n"
            f"디스코드 채널에 전용 스레드가 자동 생성되었습니다.\n"
            f"{guide_msg}",
            parent=self
        )
        self.destroy()

    def _on_failure(self, err_msg):
        self.submit_btn.configure(state="normal", text="🚀 제보 및 스레드 생성")
        self.cancel_btn.configure(state="normal")
        self.status_label.configure(text="")

        messagebox.showerror(
            "전송 실패",
            f"제보 전송 중 오류가 발생했습니다:\n\n{err_msg}\n\n인터넷 연결 상태를 확인 후 다시 시도해주세요.",
            parent=self
        )
