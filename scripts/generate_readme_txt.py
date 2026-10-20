import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def generate_readme_txt(repo_root=None):
    if repo_root is None:
        repo_root = REPO_ROOT

    md_path = os.path.join(repo_root, "README.md")
    txt_path = os.path.join(repo_root, "README.txt")

    if not os.path.exists(md_path):
        print(f"README.md를 찾을 수 없습니다: {md_path}")
        return False

    with open(md_path, "r", encoding="utf-8") as f:
        md_content = f.read()

    # 1. Code blocks (```bash ... ```) with optional leading spaces
    txt = re.sub(
        r"^[ \t]*```(?:[a-zA-Z0-9_-]+)?\r?\n(.*?)\r?\n[ \t]*```",
        lambda m: "\n".join("      " + line.strip() for line in m.group(1).splitlines()),
        md_content,
        flags=re.DOTALL | re.MULTILINE,
    )

    # 2. Strip HTML tags like <div align="...">, </div>, <br> (requiring word boundary so <-> is preserved)
    txt = re.sub(r"</?(?:div|span|p|br|hr|b|i|center|a)\b[^>]*>", "", txt, flags=re.IGNORECASE)

    # 3. Clean up Markdown links [Text](URL) -> Text (URL)
    txt = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", txt)

    # 4. Clean markdown bold/italic
    txt = re.sub(r"\*\*([^*]+)\*\*", r"\1", txt)
    txt = re.sub(r"\*([^*]+)\*", r"\1", txt)

    # 5. Clean inline code backticks `code` -> 'code'
    txt = re.sub(r"`([^`]+)`", r"'\1'", txt)

    # 6. Clean alerts and blockquotes (only at line start)
    txt = re.sub(r"^[ \t]*>[ \t]*\[!TIP\]", "[TIP]", txt, flags=re.MULTILINE)
    txt = re.sub(r"^[ \t]*>[ \t]*\[!NOTE\]", "[NOTE]", txt, flags=re.MULTILINE)
    txt = re.sub(r"^[ \t]*>[ \t]*", "  ", txt, flags=re.MULTILINE)

    # 7. Clean headings
    txt = re.sub(
        r"^#\s+(.+)$",
        r"==================================================\n\1\n==================================================",
        txt,
        flags=re.MULTILINE,
    )
    txt = re.sub(
        r"^##\s+(.+)$",
        r"\n--------------------------------------------------\n■ \1\n--------------------------------------------------",
        txt,
        flags=re.MULTILINE,
    )
    txt = re.sub(r"^###\s+(.+)$", r"\n▶ \1", txt, flags=re.MULTILINE)
    txt = re.sub(r"^####\s+(.+)$", r"\n- \1", txt, flags=re.MULTILINE)

    # 8. Normalize empty lines
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()

    with open(txt_path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(txt)

    print(f"📄 README.txt 동기화 생성 완료 ➡️ {txt_path}")
    return True


if __name__ == "__main__":
    generate_readme_txt()

