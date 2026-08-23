import re
import json

# ==========================================
# XNet Manual Parser (.txt)
# ==========================================

def parse_xnet_line(line):
    """
    XNet 매뉴얼의 각 줄에서 태그({b}, {l:id} 등)와 실제 텍스트를 분리합니다.
    """
    text = line.strip()
    if not text:
        return "", ""
    
    # 태그가 앞에 있는 경우 (예: {l:intro}Introduction)
    m = re.match(r'^(\{[a-zA-Z0-9:]+\})(.*)$', text)
    if m:
        return m.group(1), m.group(2).strip()
    
    return "", text

def extract_xnet_texts(xnet_map):
    """
    xnet_map: { jar_name: { zip_path: text_content } }
    반환값: unique_texts (list), parsed_map (dict)
    """
    unique_texts = set()
    parsed_map = {}
    
    for jar_name, zip_dict in xnet_map.items():
        parsed_map[jar_name] = {}
        for zip_path, content in zip_dict.items():
            lines = content.split('\n')
            parsed_lines = []
            for line in lines:
                tag, real_text = parse_xnet_line(line)
                if real_text:
                    unique_texts.add(real_text)
                parsed_lines.append((tag, real_text))
            parsed_map[jar_name][zip_path] = parsed_lines
            
    return list(unique_texts), parsed_map

def assemble_xnet_books(parsed_map, translation_dict):
    """
    translation_dict: { orig_text: translated_text }
    반환값: final_map { jar_name: { zip_path: final_content } }
    """
    final_map = {}
    for jar_name, zip_dict in parsed_map.items():
        final_map[jar_name] = {}
        for zip_path, lines in zip_dict.items():
            out_lines = []
            for tag, real_text in lines:
                if real_text:
                    trans = translation_dict.get(real_text, real_text)
                    out_lines.append(f"{tag}{trans}" if tag else trans)
                else:
                    out_lines.append("")
            final_map[jar_name][zip_path] = "\n".join(out_lines)
    return final_map

# ==========================================
# Forestry Manual Parser (.json)
# ==========================================

def _extract_forestry_texts_recursive(node, unique_texts):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ('title', 'text') and isinstance(v, str) and v.strip():
                # HTML tags나 특수 포맷 무시 (그대로 번역기에 전달)
                unique_texts.add(v)
            else:
                _extract_forestry_texts_recursive(v, unique_texts)
    elif isinstance(node, list):
        for item in node:
            _extract_forestry_texts_recursive(item, unique_texts)

def extract_forestry_texts(forestry_map):
    """
    forestry_map: { jar_name: { zip_path: json_data } }
    반환값: unique_texts (list)
    """
    unique_texts = set()
    for jar_name, zip_dict in forestry_map.items():
        for zip_path, json_data in zip_dict.items():
            _extract_forestry_texts_recursive(json_data, unique_texts)
    return list(unique_texts)

def _assemble_forestry_books_recursive(node, translation_dict):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ('title', 'text') and isinstance(v, str) and v.strip():
                node[k] = translation_dict.get(v, v)
            else:
                _assemble_forestry_books_recursive(v, translation_dict)
    elif isinstance(node, list):
        for item in node:
            _assemble_forestry_books_recursive(item, translation_dict)

def assemble_forestry_books(forestry_map, translation_dict):
    import copy
    final_map = {}
    for jar_name, zip_dict in forestry_map.items():
        final_map[jar_name] = {}
        for zip_path, json_data in zip_dict.items():
            # 원본 보존을 위해 깊은 복사
            cloned = copy.deepcopy(json_data)
            _assemble_forestry_books_recursive(cloned, translation_dict)
            final_map[jar_name][zip_path] = cloned
    return final_map

# ==========================================
# Markdown Manual Parser (.md)
# ==========================================

def extract_markdown_texts(markdown_map):
    """
    마크다운은 파일 전체를 통째로 하나의 텍스트로 취급하여 원본 서식을 최대한 보존합니다.
    """
    unique_texts = set()
    for jar_name, zip_dict in markdown_map.items():
        for zip_path, content in zip_dict.items():
            if content.strip():
                unique_texts.add(content.strip())
    return list(unique_texts)

def assemble_markdown_books(markdown_map, translation_dict):
    final_map = {}
    for jar_name, zip_dict in markdown_map.items():
        final_map[jar_name] = {}
        for zip_path, content in zip_dict.items():
            if content.strip():
                final_map[jar_name][zip_path] = translation_dict.get(content.strip(), content)
            else:
                final_map[jar_name][zip_path] = content
    return final_map
