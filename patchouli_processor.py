import re
import json

# Patchouli formatting tags: $(...)
# e.g., $(l) for bold, $(br) for newline, $(#ff0000) for color, $(item) ...
# We will replace them with HTML-like placeholders <p0>, <p1> which translation engines handle better without adding spaces.
PATCHOULI_TAG_REGEX = re.compile(r'\$\([^)]+\)')

def protect_patchouli_formatting(text):
    """
    텍스트 내의 Patchouli 매크로 태그들을 찾아서 번역기가 훼손하지 못하도록 <p0>, <p1> 등으로 치환합니다.
    아이템이나 멀티블록 이름의 경우 번역 후 원본 영어를 병기하도록 특수 태그를 추가합니다.
    반환값: (치환된 텍스트, 치환 매핑 dict)
    """
    mapping = {}
    
    # 1. 아이템, 블럭, 멀티블럭 이름 뒤에 원본 영어 병기 태그 추가
    def append_english_replacer(match):
        full_macro = match.group(0)
        inner_text = match.group(2)
        idx = len(mapping)
        placeholder = f"<e{idx}>"
        # $(#909090) 색상 코드를 사용하여 회색으로 (Original Name) 추가
        mapping[placeholder] = f" $(#909090)({inner_text})$()"
        return full_macro + placeholder

    # $(item)...$(), $(thing)...$(), $(block)...$() 매칭
    text = re.sub(r'(\$\((?:item|thing|block)\))(.*?)(\$\(\))', append_english_replacer, text)
    
    # 2. 일반 Patchouli 태그 보호
    def replacer(match):
        idx = len(mapping)
        placeholder = f"<p{idx}>"
        mapping[placeholder] = match.group(0)
        return placeholder

    protected_text = PATCHOULI_TAG_REGEX.sub(replacer, text)
    return protected_text, mapping

def restore_patchouli_formatting(translated_text, mapping):
    """
    번역된 텍스트에서 <p0> 등의 치환자를 원래 Patchouli 기호로 되돌립니다.
    """
    restored_text = translated_text
    # 역순으로 바꾸는 것이 안전함 (p10이 p1 때문에 먼저 바뀌는 현상 방지. 혹은 정규식 사용)
    for placeholder, original_tag in sorted(mapping.items(), key=lambda x: int(x[0][2:-1]), reverse=True):
        # 때때로 번역기가 <p0> 를 < p0 > 이나 <P0>로 훼손할 수 있으므로 보정
        letter = placeholder[1]
        num = placeholder[2:-1]
        bad_patterns = [
            placeholder,
            placeholder.replace("<", "< ").replace(">", " >"),
            placeholder.upper(),
            f"< {letter}{num} >",
            f"<{letter.upper()}{num}>"
        ]
        for bp in bad_patterns:
            restored_text = restored_text.replace(bp, original_tag)
            
    # 간혹 띄어쓰기가 남았을 경우 (예: "안녕 $(l) 세계") 처리 - 한국어 조사나 띄어쓰기에 맞춰 보정할 수도 있으나 원본 유지 우선
    return restored_text

def is_i18n_key(text):
    """마인크래프트 내부 언어 키(예: item.twilightforest.guide)인지 확인"""
    if not isinstance(text, str):
        return False
    t = text.strip()
    prefixes = ['item.', 'block.', 'tile.', 'entity.', 'advancements.', 'gui.', 'container.', 'effect.', 'biome.', 'key.', 'patchouli.', 'stat.']
    if any(t.startswith(p) for p in prefixes):
        return True
    if t.count('.') >= 2 and ' ' not in t and '\n' not in t:
        return True
    return False

def collect_patchouli_targets(node, target_list):
    """
    Patchouli JSON 구조에서 번역해야 할 텍스트 노드들을 재귀적으로 수집합니다.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            # Patchouli 번역 대상 키: name, description, text, title, landing_text, subtitle, header, caption, link_text
            if k in ["name", "description", "text", "title", "landing_text", "subtitle", "header", "caption", "link_text"] and isinstance(v, str) and v.strip():
                # 마인크래프트 I18N 언어 키는 번역하지 않고 원본 키를 유지 (책 안 열리는 치명적 버그 방지)
                if is_i18n_key(v):
                    continue
                # 이미 한국어가 포함된 경우(이미 번역된 페이지) 불필요한 재번역 건너뜀
                if any(0xAC00 <= ord(c) <= 0xD7A3 for c in v):
                    continue
                if re.search(r'[a-zA-Z]', v):
                    target_list.append((node, k, v))
            elif isinstance(v, (dict, list)):
                collect_patchouli_targets(v, target_list)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                collect_patchouli_targets(item, target_list)
