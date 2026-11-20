"""
memory_editor_window.py - 인게임 오역 수정기(Memory Editor) 독립 창 클래스.
기존 ui_screens.py의 1,460줄 중첩 함수를 독립 대분류 클래스(MemoryEditorWindow)로 분리했습니다.
"""
import os
import re
import time
import threading
import logging
import translation_memory
import translation_editor

try:
    import customtkinter as ctk
    import tkinter as tk
    from tkinter import messagebox, ttk
except Exception:
    ctk = None
    tk = None
    messagebox = None
    ttk = None

from constants import FONT_NAME, has_hangul


class BatchReplaceConfirmDialog:
    """일괄 치환 실행 전 커스텀 확인창(치환 대상 목록 펼치기 · 전체선택/해제 · 개별 제외 · 위치 이동)을 전담합니다."""

    def __init__(
        self,
        parent_editor,
        *,
        valid_count: int,
        preview_changes: list,
        vanilla_skipped_count: int,
        find_term: str,
        repl_label: str,
        err_rate: float,
        homonym_warns: list,
        on_jump_row,
        on_confirm,
    ):
        self.parent_editor = parent_editor
        self.valid_count = valid_count
        self.preview_changes = preview_changes
        self.vanilla_skipped_count = vanilla_skipped_count
        self.find_term = find_term
        self.repl_label = repl_label
        self.err_rate = err_rate
        self.homonym_warns = homonym_warns
        self.on_jump_row = on_jump_row
        self.on_confirm = on_confirm
        self.window = None
        self._build()

    def _build(self):
        confirm_win = ctk.CTkToplevel(self.parent_editor)
        self.window = confirm_win
        confirm_win.title("⚡ 일괄 치환 확인")
        base_h = 350 if self.homonym_warns else 285
        confirm_win.geometry(f"620x{base_h}")
        confirm_win.minsize(560, base_h)
        confirm_win.transient(self.parent_editor)
        confirm_win.grab_set()

        info_box = ctk.CTkFrame(confirm_win, fg_color="#16161d", corner_radius=8)
        info_box.pack(fill="x", padx=16, pady=(14, 8))

        summary_lines = (
            f"정말 이렇게 수정하시겠습니까?\n\n"
            f"• 선택된 항목: 총 {self.valid_count:,}개  ➡️  실제 치환 대상: {len(self.preview_changes):,}개"
            + (f" (바닐라 공식 {self.vanilla_skipped_count}개 제외)" if self.vanilla_skipped_count > 0 else "") + "\n"
            f"• 찾을 단어: '{self.find_term}'   ➡️   바꿀 번역: {self.repl_label}\n"
            f"• 오역률 감지: {self.err_rate:.1f}%"
        )
        ctk.CTkLabel(
            info_box, text=summary_lines,
            font=ctk.CTkFont(family=FONT_NAME, size=12),
            text_color="#e4e4e7", justify="left"
        ).pack(anchor="w", padx=14, pady=12)

        if self.homonym_warns:
            warn_box = ctk.CTkFrame(confirm_win, fg_color="#2a1c08", border_width=1, border_color="#f59e0b", corner_radius=8)
            warn_box.pack(fill="x", padx=16, pady=(0, 8))
            ctk.CTkLabel(
                warn_box, text="\n".join(self.homonym_warns),
                font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
                text_color="#fbbf24", wraplength=565, justify="left"
            ).pack(anchor="w", padx=12, pady=8)

        # 개별 항목 체크 상태 (기본 모두 True; 목록 펼치기에서 개별 체크 해제 가능)
        item_keep_vars = [tk.BooleanVar(value=True) for _ in self.preview_changes]
        expand_state = {"open": False, "built_count": 0}

        list_container = ctk.CTkFrame(confirm_win, fg_color="#101015", corner_radius=8)
        list_toolbar = ctk.CTkFrame(list_container, fg_color="#16161d", corner_radius=6)
        lbl_preview_sel = ctk.CTkLabel(
            list_toolbar, text=f"적용 대상: {len(self.preview_changes):,} / {len(self.preview_changes):,}건",
            font=ctk.CTkFont(family=FONT_NAME, size=11), text_color="#a1a1aa"
        )

        def _update_preview_sel_count():
            cnt = sum(1 for v in item_keep_vars if v.get())
            lbl_preview_sel.configure(text=f"적용 대상: {cnt:,} / {len(self.preview_changes):,}건")

        def _set_all_preview(val: bool):
            for v in item_keep_vars:
                v.set(val)
            _update_preview_sel_count()

        ctk.CTkButton(
            list_toolbar, text="☑ 전체 선택", width=86, height=24,
            font=ctk.CTkFont(family=FONT_NAME, size=11),
            fg_color="#27272a", hover_color="#3f3f46",
            command=lambda: _set_all_preview(True)
        ).pack(side="left", padx=(6, 4), pady=5)
        ctk.CTkButton(
            list_toolbar, text="☐ 전체 해제", width=86, height=24,
            font=ctk.CTkFont(family=FONT_NAME, size=11),
            fg_color="#27272a", hover_color="#3f3f46",
            command=lambda: _set_all_preview(False)
        ).pack(side="left", padx=4, pady=5)
        lbl_preview_sel.pack(side="right", padx=10, pady=5)
        list_toolbar.pack(fill="x", padx=4, pady=(4, 2))

        scroll_list = None
        btn_load_more = None

        def _render_more_preview_rows(batch_limit=150):
            nonlocal btn_load_more
            if scroll_list is None:
                return
            start_i = expand_state["built_count"]
            end_i = min(len(self.preview_changes), start_i + batch_limit)
            for p_idx in range(start_i, end_i):
                idx_in_list, it_obj, old_t, new_t = self.preview_changes[p_idx]
                row_f = ctk.CTkFrame(scroll_list, fg_color="#181820", corner_radius=6)
                row_f.pack(fill="x", padx=4, pady=3)

                chk = ctk.CTkCheckBox(
                    row_f, text="", variable=item_keep_vars[p_idx],
                    width=22, checkbox_width=18, checkbox_height=18,
                    fg_color="#ea580c", hover_color="#c2410c",
                    command=_update_preview_sel_count
                )
                chk.pack(side="left", padx=(8, 4), pady=6)

                ctk.CTkButton(
                    row_f, text="📍위치", width=48, height=22,
                    font=ctk.CTkFont(family=FONT_NAME, size=10),
                    fg_color="#27272a", hover_color="#3f3f46",
                    command=lambda ti=idx_in_list: self.on_jump_row(ti)
                ).pack(side="right", padx=6, pady=6)

                txt_preview = f"[{it_obj['category']}] {it_obj['src']}\n   {old_t}  ➡️  {new_t}"
                ctk.CTkLabel(
                    row_f, text=txt_preview,
                    font=ctk.CTkFont(family=FONT_NAME, size=11),
                    text_color="#d4d4d8", justify="left", anchor="w", wraplength=540
                ).pack(side="left", fill="x", expand=True, padx=4, pady=5)

            expand_state["built_count"] = end_i
            remaining = len(self.preview_changes) - end_i
            if remaining > 0:
                if btn_load_more is None or not btn_load_more.winfo_exists():
                    btn_load_more = ctk.CTkButton(
                        scroll_list,
                        text=f"▼ 다음 항목 더 보기 (남은 {remaining:,}건)",
                        height=28, fg_color="#27272a", hover_color="#3f3f46",
                        font=ctk.CTkFont(family=FONT_NAME, size=11),
                        command=lambda: _render_more_preview_rows(150)
                    )
                else:
                    btn_load_more.configure(text=f"▼ 다음 항목 더 보기 (남은 {remaining:,}건)")
                    btn_load_more.pack_forget()
                btn_load_more.pack(fill="x", padx=8, pady=6)
            elif btn_load_more is not None and btn_load_more.winfo_exists():
                btn_load_more.destroy()

        def _toggle_homonym_list():
            nonlocal scroll_list
            if not expand_state["open"]:
                expand_state["open"] = True
                btn_toggle_list.configure(
                    text=f"▼ 치환 대상 목록 접기 (총 {len(self.preview_changes):,}건 · 원치 않는 항목은 체크 해제)"
                )
                confirm_win.geometry("720x640")
                list_container.pack(fill="both", expand=True, padx=16, pady=(0, 8), before=btn_bar)
                if scroll_list is None:
                    scroll_list = ctk.CTkScrollableFrame(list_container, fg_color="#101015", corner_radius=6)
                    scroll_list.pack(fill="both", expand=True, padx=4, pady=4)
                    _render_more_preview_rows(150)
            else:
                expand_state["open"] = False
                btn_toggle_list.configure(
                    text=f"▶ 치환 대상 목록 펼치기 ({len(self.preview_changes):,}건 하나씩 확인·제외 가능)"
                )
                list_container.pack_forget()
                confirm_win.geometry(f"620x{base_h}")

        toggle_fg = "#b45309" if self.homonym_warns else "#27272a"
        toggle_hover = "#92400e" if self.homonym_warns else "#3f3f46"
        btn_toggle_list = ctk.CTkButton(
            confirm_win,
            text=f"▶ 치환 대상 목록 펼치기 ({len(self.preview_changes):,}건 하나씩 확인·제외 가능)",
            height=30, fg_color=toggle_fg, hover_color=toggle_hover,
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
            command=_toggle_homonym_list
        )
        btn_toggle_list.pack(fill="x", padx=16, pady=(0, 8))

        btn_bar = ctk.CTkFrame(confirm_win, fg_color="transparent")
        btn_bar.pack(fill="x", padx=16, pady=(4, 14), side="bottom")

        def _on_confirm_click():
            chosen_changes = [
                ch for ch, kv in zip(self.preview_changes, item_keep_vars) if kv.get()
            ]
            confirm_win.destroy()
            self.on_confirm(chosen_changes)

        ctk.CTkButton(
            btn_bar, text="✅ 체크된 항목 치환 실행", height=34,
            fg_color="#ea580c", hover_color="#c2410c",
            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
            command=_on_confirm_click
        ).pack(side="left", fill="x", expand=True, padx=(0, 6))

        ctk.CTkButton(
            btn_bar, text="취소", width=110, height=34,
            fg_color="#3f3f46", hover_color="#52525b",
            font=ctk.CTkFont(family=FONT_NAME, size=12),
            command=confirm_win.destroy
        ).pack(side="right")


class AiRetranslateController:
    """오역 수정기의 AI 재번역 파이프라인(진행 대기창 · 취소 가드 · O/X 한 줄 비교 검토창 · 일괄 적용)을 전담합니다."""

    def __init__(self, app, parent_editor, apply_batch_hotpatch_fn, refresh_search_fn):
        self.app = app
        self.parent_editor = parent_editor
        self.apply_batch_hotpatch = apply_batch_hotpatch_fn
        self.refresh_search = refresh_search_fn

    def start(self, selected_items: list):
        if not selected_items:
            self.app.show_messagebox("warning", "경고", "AI로 재번역할 항목을 하나 이상 선택(체크)해주세요.")
            return

        # 1. Tier 0 바닐라 마인크래프트 공식 항목 사전 제외 (절대 불변)
        valid_items = []
        vanilla_skipped_count = 0
        for item in selected_items:
            if item.get("is_vanilla") or translation_memory.is_vanilla_official_key(item["src"], item["category"]):
                vanilla_skipped_count += 1
            else:
                valid_items.append(item)

        if vanilla_skipped_count > 0:
            self.app.log(
                f"🔒 [공식 번역 보호] 선택 항목 중 바닐라 마인크래프트 공식 명칭 {vanilla_skipped_count}개는 "
                f"절대 불변 항목이므로 AI 재번역 대상에서 자동 제외되었습니다."
            )

        if not valid_items:
            self.app.show_messagebox(
                "warning", "재번역 불가",
                "선택된 모든 항목이 바닐라 마인크래프트 공식 명칭이므로 재번역할 수 없습니다."
            )
            return

        # 2. 메인 화면 번역 설정 및 API 키 검증 (그대로 가져오기)
        engine_key, api_key, is_paid, ai_model, target_lang, custom_url = self.app.validate_inputs()
        if not engine_key:
            return

        # 3. 사용자 시작 확인
        model_display = ai_model or engine_key
        target_tier = translation_memory.get_model_tier(ai_model)
        confirm_msg = (
            f"선택한 {len(valid_items)}개 항목을 AI로 재번역하시겠습니까?\n\n"
            f"• 적용 모델: {model_display}\n"
            f"• 대상 언어: {target_lang}\n"
            + (f"• 바닐라 공식 {vanilla_skipped_count}개 자동 제외됨\n" if vanilla_skipped_count > 0 else "") +
            f"\n(완료 후 한 줄 비교 로그를 검토하고 적용 여부(O/X)를 결정할 수 있습니다.)"
        )
        if not messagebox.askyesno("AI 재번역 시작", confirm_msg, parent=self.parent_editor):
            return

        # 4. 프로그레스/대기 팝업 표시
        prog_win = ctk.CTkToplevel(self.parent_editor)
        prog_win.title("🤖 AI 재번역 진행 중...")
        prog_win.geometry("380x140")
        prog_win.resizable(False, False)
        prog_win.grab_set()

        ctk.CTkLabel(
            prog_win, text=f"🤖 {len(valid_items)}개 항목 AI 재번역 중...",
            font=ctk.CTkFont(family=FONT_NAME, size=13, weight="bold"),
            text_color="#38bdf8"
        ).pack(pady=(20, 6))

        ctk.CTkLabel(
            prog_win, text=f"모델: {model_display} | 잠시만 기다려주세요...",
            font=ctk.CTkFont(family=FONT_NAME, size=11),
            text_color="#94a3b8"
        ).pack(pady=(0, 10))

        prog_bar = ctk.CTkProgressBar(prog_win, width=300, mode="indeterminate")
        prog_bar.pack(pady=(0, 15))
        prog_bar.start()

        retrans_cancelled = [False]

        def _on_prog_close():
            retrans_cancelled[0] = True
            try:
                if prog_win.winfo_exists():
                    prog_win.destroy()
            except Exception:
                pass

        prog_win.protocol("WM_DELETE_WINDOW", _on_prog_close)

        # 5. 백그라운드 스레드에서 API 호출
        def _run():
            texts_to_trans = [it["src"] for it in valid_items]
            active_glossary = getattr(self.app.app_state, 'glossary', None) if hasattr(self.app, 'app_state') else None
            try:
                results = translation_editor.retranslate_texts(
                    texts_to_trans, engine_key, api_key, is_paid=is_paid,
                    ai_model=ai_model, target_lang=target_lang,
                    custom_url=custom_url, glossary=active_glossary
                )
            except Exception as e:
                self.app.after(0, lambda: _on_error(str(e)))
                return

            self.app.after(0, lambda: _on_finish(results))

        def _on_error(err_msg):
            if prog_win.winfo_exists():
                prog_win.destroy()
            if retrans_cancelled[0] or not self.parent_editor.winfo_exists():
                return
            self.app.show_messagebox("error", "오류", f"AI 재번역 중 오류가 발생했습니다:\n{err_msg}")

        def _on_finish(results):
            if prog_win.winfo_exists():
                prog_win.destroy()
            if retrans_cancelled[0] or not self.parent_editor.winfo_exists():
                return
            self.open_review_dialog(valid_items, results, model_display, target_tier)

        threading.Thread(target=_run, daemon=True).start()

    def open_review_dialog(self, items, new_translations, model_name, m_tier):
        review_win = ctk.CTkToplevel(self.parent_editor)
        review_win.title("🤖 AI 재번역 결과 검토 & 적용 (O/X)")
        review_win.geometry("980x640")
        review_win.minsize(820, 500)
        review_win.grab_set()

        top_frame = ctk.CTkFrame(review_win, fg_color="transparent")
        top_frame.pack(fill="x", padx=16, pady=(16, 8))

        ctk.CTkLabel(
            top_frame, text=f"✨ {len(items)}개 항목 AI 재번역 완료",
            font=ctk.CTkFont(family=FONT_NAME, size=15, weight="bold"),
            text_color="#38bdf8"
        ).pack(anchor="w")

        changed_count = sum(1 for it, nt in zip(items, new_translations) if nt != it["tgt"])
        ctk.CTkLabel(
            top_frame,
            text=f"• 적용 모델: {model_name} | 변경 감지: {changed_count}개 (동일: {len(items) - changed_count}개)\n"
                 f"• 아래 한 줄 비교 로그를 검토한 후 [✅ 적용하기 (O)] 또는 [❌ 취소 (X)]를 선택하세요.",
            font=ctk.CTkFont(family=FONT_NAME, size=11),
            justify="left", text_color="#cbd5e1"
        ).pack(anchor="w", pady=(4, 0))

        box_frame = ctk.CTkFrame(review_win, fg_color="#121216", corner_radius=8, border_width=1, border_color="#2b2b36")
        box_frame.pack(fill="both", expand=True, padx=16, pady=8)

        txt_box = ctk.CTkTextbox(
            box_frame,
            font=ctk.CTkFont(family="Consolas" if os.name == 'nt' else FONT_NAME, size=11),
            fg_color="transparent", text_color="#f1f5f9", wrap="none"
        )
        txt_box.pack(fill="both", expand=True, padx=8, pady=8)

        for idx, (item, new_t) in enumerate(zip(items, new_translations), 1):
            clean_src = item['src'].replace('\n', ' ').strip()
            clean_old = item['tgt'].replace('\n', ' ').strip()
            clean_new = new_t.replace('\n', ' ').strip()
            line = f"[{idx:02d}] 원문: {clean_src} ｜ 기존 번역: {clean_old} ➡️ AI 재번역: {clean_new}\n"
            txt_box.insert("end", line)

        txt_box.configure(state="disabled")

        btn_row = ctk.CTkFrame(review_win, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(8, 16))

        def on_apply():
            batch_changes = []
            mem_updates = []
            for item, new_t in zip(items, new_translations):
                old_t = item["tgt"]
                if new_t != old_t:
                    batch_changes.append({
                        "category": item["category"], "src": item["src"],
                        "old_tgt": old_t, "new_tgt": new_t
                    })
                    item["tgt"] = new_t
                    mem_updates.append((item["category"], item["src"], new_t))

            if batch_changes:
                translation_memory.default_memory_store.bulk_update(
                    mem_updates, target_lang="한국어(Korean)", force_override=True, tier=m_tier
                )
                translation_memory.default_memory_store.save(flush_now=False)
                translation_memory.default_memory_store.flush_commits(sync_wait=False)
                threading.Thread(target=self.apply_batch_hotpatch, args=(batch_changes,), daemon=True).start()
                translation_memory.dispatch_batch_audit_report(
                    batch_changes,
                    details=f"AI 재번역 ({model_name}) 적용 ({len(batch_changes)}건)"
                )
                self.app.show_messagebox("info", "적용 완료", f"총 {len(batch_changes)}개 항목의 AI 재번역이 성공적으로 적용되었습니다!")
            else:
                self.app.show_messagebox("info", "안내", "기존 번역과 달라진 항목이 없어 적용되지 않았습니다.")

            review_win.destroy()
            self.refresh_search()

        ctk.CTkButton(
            btn_row, text="✅ 적용하기 (O)", width=160, height=34,
            font=ctk.CTkFont(family=FONT_NAME, size=12, weight="bold"),
            fg_color="#15803d", hover_color="#166534",
            command=on_apply
        ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            btn_row, text="❌ 취소 (X)", width=120, height=34,
            font=ctk.CTkFont(family=FONT_NAME, size=12),
            fg_color="#334155", hover_color="#475569",
            command=review_win.destroy
        ).pack(side="right")


class MemoryEditorWindow:
    """인게임 오역 수정기(검색 · 결과 내 이동 · 단건 수정 · 일괄 치환 · AI 재번역 · 수동 추가) 전담 창 클래스."""

    def __init__(self, app):
        self.app = app
        self.window = None
        self._build()

    def __getattr__(self, name):
        return getattr(self.app, name)

    def _build(self):
        editor = ctk.CTkToplevel(self.app)
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
            placeholder_text="검색어 (한/영, 쉼표 ','로 AND 검색 예: log, 통나무)...",
            font=ctk.CTkFont(family=FONT_NAME, size=12), width=280
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

        try:
            import temp_session_logger
            if temp_session_logger.is_debug_mode():
                def _load_debug_seed_list():
                    cat_var.set("all")
                    query_var.set("[TEST]")
                    do_search()

                ctk.CTkButton(
                    search_row, text="🧪 테스트 단어", width=98, height=28,
                    font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
                    fg_color="#6d28d9", hover_color="#5b21b6",
                    command=_load_debug_seed_list
                ).pack(side="left", padx=(0, 6))
        except Exception:
            pass

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

        lbl_homonym_warn = ctk.CTkLabel(
            search_card, text="", anchor="w",
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
            text_color="#fbbf24"
        )

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
        replace_row.pack(fill="x", padx=4, pady=(8, 4))

        ctk.CTkLabel(
            replace_row, text="찾을 단어:",
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"), text_color="#cbd5e1"
        ).pack(side="left", padx=(0, 4))

        find_var = ctk.StringVar()
        entry_find = ctk.CTkEntry(
            replace_row, textvariable=find_var, placeholder_text="예: 통나무, Mekanism (한글/영어 가능)",
            font=ctk.CTkFont(family=FONT_NAME, size=11), width=230
        )
        entry_find.pack(side="left", padx=(0, 8))

        ctk.CTkLabel(
            replace_row, text="➡️ 바꿀 단어:",
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"), text_color="#cbd5e1"
        ).pack(side="left", padx=(0, 4))

        replace_var = ctk.StringVar()
        entry_replace = ctk.CTkEntry(
            replace_row, textvariable=replace_var, placeholder_text="예: 원목, 팅커스 (공백 시 삭제)",
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

        replace_hint_row = ctk.CTkFrame(panel_replace, fg_color="transparent")
        replace_hint_row.pack(fill="x", padx=4, pady=(0, 6))

        ctk.CTkLabel(
            replace_hint_row,
            text="💡 한글(예: 통나무➡️원목)뿐 아니라 영단어(예: Mekanism➡️메카니즘) 입력 시 띄어쓰기 위치·음운 매칭으로 표기가 다른 오역도 일괄 치환합니다.",
            font=ctk.CTkFont(family=FONT_NAME, size=10),
            text_color="#94a3b8"
        ).pack(side="left")

        chk_batch_glossary_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            replace_hint_row, text="📖 용어집(Glossary)에도 등록",
            variable=chk_batch_glossary_var,
            font=ctk.CTkFont(family=FONT_NAME, size=10, weight="bold"),
            text_color="#38bdf8", checkmark_color="#ffffff", fg_color="#0284c7",
            checkbox_width=16, checkbox_height=16
        ).pack(side="right")

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

        btn_ai_retranslate = ctk.CTkButton(
            toolbar, text="🤖 선택 항목 AI 재번역", width=155, height=26,
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
            fg_color="#7c3aed", hover_color="#6d28d9",
            command=lambda: do_ai_retranslate()
        )
        btn_ai_retranslate.pack(side="left", padx=(12, 0))

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

        # 결과 내 위치 찾기 바 (체크 선택 상태 유지한 채 원하는 행으로 바로 스크롤/이동)
        in_list_bar = ctk.CTkFrame(editor, fg_color="#18181d", corner_radius=8, border_width=1, border_color="#27272a")
        in_list_bar.pack(fill="x", padx=14, pady=(0, 6))

        ctk.CTkLabel(
            in_list_bar, text="📍 결과 내 이동:",
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"), text_color="#cbd5e1"
        ).pack(side="left", padx=(10, 6), pady=5)

        in_list_var = ctk.StringVar()
        entry_in_list = ctk.CTkEntry(
            in_list_bar, textvariable=in_list_var,
            placeholder_text="결과 내 이동 (한/영 동시 지원, 쉼표 ',' 입력 시 AND 조건 예: log, 통나무)...",
            font=ctk.CTkFont(family=FONT_NAME, size=11), width=370, height=26
        )
        entry_in_list.pack(side="left", padx=(0, 6), pady=5)

        btn_jump_prev = ctk.CTkButton(
            in_list_bar, text="▲ 이전", width=62, height=24,
            font=ctk.CTkFont(family=FONT_NAME, size=11),
            fg_color="#27272a", hover_color="#3f3f46",
            command=lambda: jump_in_results(-1)
        )
        btn_jump_prev.pack(side="left", padx=(0, 4), pady=5)

        btn_jump_next = ctk.CTkButton(
            in_list_bar, text="▼ 다음", width=62, height=24,
            font=ctk.CTkFont(family=FONT_NAME, size=11, weight="bold"),
            fg_color="#0284c7", hover_color="#0369a1",
            command=lambda: jump_in_results(1)
        )
        btn_jump_next.pack(side="left", padx=(0, 8), pady=5)

        lbl_in_list_pos = ctk.CTkLabel(
            in_list_bar, text="",
            font=ctk.CTkFont(family=FONT_NAME, size=11), text_color="#94a3b8"
        )
        lbl_in_list_pos.pack(side="left", pady=5)

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
            for i in range(len(current_items)):
                iid = str(i)
                if select_all:
                    checked_iids.add(iid)
                tree.set(iid, "check", chk_char)
                if (i + 1) % 500 == 0:
                    try:
                        editor.update_idletasks()
                    except Exception:
                        pass
            update_selected_count()

        def on_item_click(item):
            selected_item["category"] = item["category"]
            selected_item["src"] = item["src"]
            selected_item["tgt"] = item["tgt"]
            selected_item["is_vanilla"] = item.get("is_vanilla", False)

            if item.get("is_vanilla"):
                lbl_original.configure(text=f"{item['src']}  🔒 [바닐라 마인크래프트 공식 번역 - 수정 불가]")
                entry_translated.configure(state="normal")
                entry_translated.delete(0, 'end')
                entry_translated.insert(0, item["tgt"])
                entry_translated.configure(state="disabled")
                btn_save.configure(state="disabled", fg_color="#3f3f46", text="🔒 바닐라 공식 (수정 불가)")
            else:
                lbl_original.configure(text=item["src"])
                entry_translated.configure(state="normal")
                entry_translated.delete(0, 'end')
                entry_translated.insert(0, item["tgt"])
                btn_save.configure(state="normal", fg_color="#15803d", text="💾 저장 및 즉시 적용")

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
                cat_display = f"[{item['category']}]" + (" 🔒바닐라" if item.get("is_vanilla") else (" 🔒공식" if item.get("is_official") else ""))
                tree.item(iid, values=(chk_char, cat_display, item["src"], item["tgt"]))
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

        def register_glossary_rule(src, tgt, force=False):
            if (not force and not chk_glossary_var.get()) or not src or not tgt:
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
            lbl_in_list_pos.configure(text="")

            q = query_var.get().strip()
            if len(q) < 1:
                lbl_homonym_warn.pack_forget()
                update_selected_count()
                return

            q_parts = [p.strip() for p in q.split(",") if p.strip()]
            homonym_msgs = []
            for qp in q_parts:
                hm = translation_editor.check_homonym_warning(qp)
                if hm and hm not in homonym_msgs:
                    homonym_msgs.append(hm)
            if homonym_msgs:
                joined_hm = "\n".join(homonym_msgs)
                lbl_homonym_warn.configure(text=joined_hm)
                lbl_homonym_warn.pack(fill="x", padx=12, pady=(0, 8))
                self.log(joined_hm)
            else:
                lbl_homonym_warn.pack_forget()

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
                cat_display = f"[{r['category']}]" + (" 🔒바닐라" if r.get("is_vanilla") else (" 🔒공식" if r.get("is_official") else ""))
                tree.insert("", "end", iid=iid, values=("☐", cat_display, r["src"], r["tgt"]))
                if (i + 1) % 500 == 0:
                    try:
                        editor.update_idletasks()
                    except Exception:
                        pass
            update_selected_count()

        entry_search.bind("<Return>", do_search)

        def do_save():
            if not selected_item["src"]:
                self.show_messagebox("warning", "안내", "수정할 항목을 아래 목록에서 먼저 선택해 주세요.")
                return
            src_val = selected_item["src"]
            if selected_item.get("is_vanilla") or translation_memory.is_vanilla_official_key(src_val, selected_item["category"]):
                self.show_messagebox(
                    "warning", "수정 불가",
                    "바닐라 마인크래프트 공식 명칭은 게임 시스템 무결성을 위해 오역 수정기로 수정할 수 없습니다."
                )
                return

            new_val = entry_translated.get().strip()
            if not new_val:
                self.show_messagebox("warning", "경고", "번역문을 입력해 주세요.")
                return
            old_val = selected_item["tgt"]

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

            # 캐시 업데이트 및 Cloudflare 커밋 큐 자동 등록 (파일 핫패치는 백그라운드로 실행해 UI 프리징 차단)
            translation_memory.update_memory_entry(selected_item["category"], src_val, new_val)
            threading.Thread(target=apply_hotpatch, args=(old_val, new_val), daemon=True).start()
            register_glossary_rule(src_val, new_val)

            # 디스코드 노란색 임베드 + 초록색 롤백 버튼 감사 로그 전송
            translation_memory.dispatch_audit_report(
                selected_item["category"], src_val, old_val, new_val,
                error_rate=err_rate, validity=val_rate, verdict=verdict, details=det
            )
            selected_item["tgt"] = new_val
            self.show_messagebox("info", "성공", f"수정사항이 저장되었습니다! (오역률: {err_rate:.1f}%)")
            do_search()

        def jump_in_results(direction=1):
            """현재 검색된 목록(current_items) 안에서 체크 선택을 풀지 않고 키워드가 있는 위치로 바로 스크롤/이동합니다.
            영어 원문/한글 번역문 동시 검색 및 쉼표(,) 구분 다중 키워드 AND 조건을 지원합니다."""
            kw = in_list_var.get().strip().lower()
            terms = [t.strip() for t in kw.split(",") if t.strip()] if kw else []
            if not terms or not current_items:
                lbl_in_list_pos.configure(text="")
                return

            mc_fmt_re = re.compile(r'[§&][0-9a-fk-orA-FK-OR]')
            matches = []
            for i, it in enumerate(current_items):
                src_l = mc_fmt_re.sub('', str(it.get("src", ""))).lower()
                tgt_l = mc_fmt_re.sub('', str(it.get("tgt", ""))).lower()
                if all((t in src_l or t in tgt_l) for t in terms):
                    matches.append(i)
            if not matches:
                lbl_in_list_pos.configure(text="일치 항목 없음", text_color="#f87171")
                return

            sel = tree.selection()
            cur_idx = int(sel[0]) if (sel and sel[0].isdigit()) else (-1 if direction > 0 else len(current_items))

            target_pos = 0
            if direction >= 0:
                later = [pos for pos, m_idx in enumerate(matches) if m_idx > cur_idx]
                target_pos = later[0] if later else 0
            else:
                earlier = [pos for pos, m_idx in enumerate(matches) if m_idx < cur_idx]
                target_pos = earlier[-1] if earlier else len(matches) - 1

            target_idx = matches[target_pos]
            iid = str(target_idx)
            tree.selection_set(iid)
            tree.focus(iid)
            tree.see(iid)
            on_item_click(current_items[target_idx])
            lbl_in_list_pos.configure(
                text=f"{target_pos + 1} / {len(matches)}건 일치 (체크 선택 유지 중)",
                text_color="#38bdf8"
            )

        entry_in_list.bind("<Return>", lambda _e: jump_in_results(1))

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

            # Tier 0 바닐라 마인크래프트 공식 항목 사전 필터링 (절대 불변)
            valid_entries = []  # [(list_idx, item_dict), ...]
            vanilla_skipped_count = 0
            for idx_in_list in selected_indices:
                item = current_items[idx_in_list]
                if item.get("is_vanilla") or translation_memory.is_vanilla_official_key(item["src"], item["category"]):
                    vanilla_skipped_count += 1
                else:
                    valid_entries.append((idx_in_list, item))

            if vanilla_skipped_count > 0:
                self.log(
                    f"🔒 [공식 번역 보호] 선택 항목 중 바닐라 마인크래프트 공식 명칭 {vanilla_skipped_count}개는 "
                    f"절대 불변 항목이므로 치환 대상에서 안전하게 자동 제외되었습니다."
                )

            if not valid_entries:
                self.show_messagebox(
                    "warning", "치환 불가",
                    "선택된 모든 항목이 바닐라 마인크래프트 공식 명칭이므로 치환할 수 없습니다."
                )
                return

            # 오역률 산출
            target_basis = query_var.get().strip() or find_term or "단어"
            err_rate, val_rate, verdict, det = translation_memory.calculate_translation_error_rate(
                target_basis, repl_term or find_term, reference_hint=find_term
            )

            # 동음의이어 체크 및 로그 경고
            find_terms = [t.strip() for t in re.split(r'[,|/]', find_term) if t.strip()] if find_term else []
            homonym_warns = []
            for ft_chk in find_terms:
                w_msg = translation_editor.check_homonym_warning(ft_chk)
                if w_msg:
                    homonym_warns.append(w_msg)
                    self.log(w_msg)

            # 치환 예정 항목 사전 계산 (메모 캐시 적용으로 수천 건도 수 ms 내 즉시 계산)
            preview_changes = []  # [(idx_in_list, item, old_tgt, new_tgt), ...]
            for idx_in_list, item in valid_entries:
                old_tgt = item["tgt"]
                new_tgt = old_tgt
                if find_terms:
                    for ft in find_terms:
                        new_tgt = translation_editor.replace_term_in_translation(
                            item["src"], new_tgt, ft, repl_term, is_deletion=is_deletion
                        )
                else:
                    new_tgt = repl_term
                if is_deletion:
                    new_tgt = re.sub(r'\s+', ' ', new_tgt).strip()
                if new_tgt != old_tgt:
                    preview_changes.append((idx_in_list, item, old_tgt, new_tgt))

            if not preview_changes:
                self.show_messagebox(
                    "info", "변경 대상 없음",
                    f"선택된 {len(valid_entries):,}개 항목 중 '{find_term}'에 해당하여 변경될 항목이 없습니다."
                )
                return

            q_term = query_var.get().strip()
            add_glossary_rule = bool(chk_batch_glossary_var.get() and q_term and not is_deletion)

            def _jump_to_editor_row(target_idx):
                iid = str(target_idx)
                try:
                    tree.selection_set(iid)
                    tree.focus(iid)
                    tree.see(iid)
                    if 0 <= target_idx < len(current_items):
                        on_item_click(current_items[target_idx])
                except Exception:
                    pass

            def _execute_confirmed_batch(chosen_changes):
                if not chosen_changes:
                    self.show_messagebox("info", "취소됨", "체크된 치환 대상 항목이 없어 작업을 취소했습니다.")
                    return

                prog_win = ctk.CTkToplevel(editor)
                prog_win.title("⚡ 일괄 치환 진행 중...")
                prog_win.geometry("380x130")
                prog_win.resizable(False, False)
                prog_win.grab_set()
                ctk.CTkLabel(
                    prog_win, text=f"⚡ {len(chosen_changes):,}개 항목 일괄 치환 및 패치 중...",
                    font=ctk.CTkFont(family=FONT_NAME, size=13, weight="bold"),
                    text_color="#fbbf24"
                ).pack(pady=(20, 10))
                prog_bar = ctk.CTkProgressBar(prog_win, width=300, mode="indeterminate")
                prog_bar.pack(pady=(0, 15))
                prog_bar.start()

                def _run_batch():
                    updated_count = len(chosen_changes)
                    batch_changes = []
                    mem_updates = []
                    ui_updates = []
                    batch_error = None

                    try:
                        for idx_in_list, item, old_tgt, new_tgt in chosen_changes:
                            batch_changes.append({
                                "category": item["category"], "src": item["src"],
                                "old_tgt": old_tgt, "new_tgt": new_tgt
                            })
                            mem_updates.append((item["category"], item["src"], new_tgt))
                            ui_updates.append((idx_in_list, new_tgt))

                        if updated_count > 0:
                            translation_memory.default_memory_store.bulk_update(
                                mem_updates, target_lang="한국어(Korean)", force_override=True
                            )
                            translation_memory.default_memory_store.save(flush_now=False)
                            translation_memory.default_memory_store.flush_commits(sync_wait=False)
                            apply_batch_hotpatch(batch_changes)

                            if not is_deletion and repl_term:
                                try:
                                    import translation_word
                                    words_updated = False
                                    for ft in find_terms:
                                        if re.search(r'[A-Za-z]', ft) and not has_hangul(ft):
                                            if translation_word.register_word(ft, repl_term, is_official=True):
                                                words_updated = True
                                        else:
                                            with translation_word._lock:
                                                for d_obj in (translation_word._proper_nouns, translation_word._common_words):
                                                    for w_k, w_v in list(d_obj.items()):
                                                        if w_v == ft:
                                                            d_obj[w_k] = repl_term
                                                            translation_word._dirty = True
                                                            words_updated = True
                                    if words_updated:
                                        translation_word.save_words(force=True)
                                except Exception:
                                    pass

                            action_text = f"단어 삭제: '{find_term}' 제거" if is_deletion else f"일괄 치환: '{find_term}' ➡️ '{repl_term}'"
                            translation_memory.dispatch_batch_audit_report(
                                batch_changes, error_rate=err_rate, validity=val_rate, verdict=verdict,
                                details=f"{action_text} (총 {updated_count}건)",
                                find_term=find_term,
                                repl_term="[단어 삭제]" if is_deletion else repl_term
                            )
                    except Exception as exc:
                        batch_error = str(exc)

                    def _on_batch_done():
                        try:
                            if prog_win.winfo_exists():
                                prog_win.destroy()
                        except Exception:
                            pass
                        if not editor.winfo_exists():
                            return
                        if batch_error:
                            self.show_messagebox("error", "오류", f"일괄 치환 중 오류가 발생했습니다:\n{batch_error}")
                            return
                        for idx_in_list, new_tgt in ui_updates:
                            if idx_in_list < len(current_items):
                                current_items[idx_in_list]["tgt"] = new_tgt
                        if add_glossary_rule:
                            register_glossary_rule(q_term, repl_term, force=True)
                        self.show_messagebox("info", "완료", f"총 {updated_count}개 항목이 {repl_label}(으)로 일괄 수정되었습니다.")
                        if editor.winfo_exists():
                            do_search()

                    self.after(0, _on_batch_done)

                threading.Thread(target=_run_batch, daemon=True).start()

            BatchReplaceConfirmDialog(
                editor,
                valid_count=len(valid_entries),
                preview_changes=preview_changes,
                vanilla_skipped_count=vanilla_skipped_count,
                find_term=find_term,
                repl_label=repl_label,
                err_rate=err_rate,
                homonym_warns=homonym_warns,
                on_jump_row=_jump_to_editor_row,
                on_confirm=_execute_confirmed_batch,
            )

        self.ai_retranslator = AiRetranslateController(self.app, editor, apply_batch_hotpatch, do_search)

        def do_ai_retranslate():
            selected_indices = sorted([int(iid) for iid in checked_iids if iid.isdigit() and int(iid) < len(current_items)])
            selected_items = [current_items[i] for i in selected_indices]
            self.ai_retranslator.start(selected_items)

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
                if add_glossary_var.get():
                    register_glossary_rule(src, tgt, force=True)
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


        self.window = editor
