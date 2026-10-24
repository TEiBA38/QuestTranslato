import re
import json

# ==========================================
# McJty Manual Parser (.txt) - XNet, RFTools, etc.
# ==========================================

def parse_mcjty_line(line):
    """
    McJty 매뉴얼의 각 줄에서 태그({b}, {l:id} 등)와 실제 텍스트를 분리합니다.
    """
    text = line.strip()
    if not text:
        return "", ""
    
    # 태그가 앞에 있는 경우 (예: {l:intro}Introduction)
    m = re.match(r'^(\{[a-zA-Z0-9:]+\})(.*)$', text)
    if m:
        return m.group(1), m.group(2).strip()
    
    return "", text

def extract_mcjty_texts(mcjty_map):
    """
    mcjty_map: { jar_name: { zip_path: text_content } }
    반환값: unique_texts (list), parsed_map (dict)
    """
    unique_texts = set()
    parsed_map = {}
    
    for jar_name, zip_dict in mcjty_map.items():
        parsed_map[jar_name] = {}
        for zip_path, content in zip_dict.items():
            lines = content.split('\n')
            parsed_lines = []
            for line in lines:
                tag, real_text = parse_mcjty_line(line)
                if real_text:
                    unique_texts.add(real_text)
                parsed_lines.append((tag, real_text))
            parsed_map[jar_name][zip_path] = parsed_lines
            
    return list(unique_texts), parsed_map

def assemble_mcjty_books(parsed_map, translation_dict):
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
    마크다운은 문단(\n\n) 단위로 쪼개서 번역을 요청하여 누락을 방지합니다.
    """
    import re
    unique_texts = set()
    for jar_name, zip_dict in markdown_map.items():
        for zip_path, content in zip_dict.items():
            chunks = content.split('\n\n')
            for chunk in chunks:
                chunk = chunk.strip()
                # 글자가 하나라도 포함된 문단만 번역 (단순 기호, 이미지 태그만 있는 경우 제외)
                if chunk and re.search(r'[a-zA-Z]', chunk) and not chunk.startswith('!['):
                    unique_texts.add(chunk)
    return list(unique_texts)

def assemble_markdown_books(markdown_map, translation_dict):
    import re
    final_map = {}
    for jar_name, zip_dict in markdown_map.items():
        final_map[jar_name] = {}
        for zip_path, content in zip_dict.items():
            chunks = content.split('\n\n')
            out_chunks = []
            for chunk in chunks:
                clean_chunk = chunk.strip()
                if clean_chunk and re.search(r'[a-zA-Z]', clean_chunk) and not clean_chunk.startswith('!['):
                    out_chunks.append(translation_dict.get(clean_chunk, chunk))
                else:
                    out_chunks.append(chunk)
            final_map[jar_name][zip_path] = '\n\n'.join(out_chunks)
    return final_map

# ==========================================
# Extra Utilities 2 Manual Parser (.json)
# ==========================================

def extract_eu2_texts(eu2_map):
    """
    eu2_map: { jar_name: { zip_path: json_data } }
    반환값: unique_texts (list)
    """
    unique_texts = set()
    for jar_name, zip_dict in eu2_map.items():
        for zip_path, json_data in zip_dict.items():
            if 'title' in json_data and isinstance(json_data['title'], str):
                unique_texts.add(json_data['title'])
            if 'pages' in json_data and isinstance(json_data['pages'], list):
                for page in json_data['pages']:
                    if isinstance(page, dict):
                        for k in ['title', 'text']:
                            if k in page and isinstance(page[k], str) and page[k].strip():
                                unique_texts.add(page[k])
    return list(unique_texts)

def assemble_eu2_books(eu2_map, translation_dict):
    import copy
    final_map = {}
    for jar_name, zip_dict in eu2_map.items():
        final_map[jar_name] = {}
        for zip_path, json_data in zip_dict.items():
            cloned = copy.deepcopy(json_data)
            if 'title' in cloned and isinstance(cloned['title'], str):
                cloned['title'] = translation_dict.get(cloned['title'], cloned['title'])
            if 'pages' in cloned and isinstance(cloned['pages'], list):
                for page in cloned['pages']:
                    if isinstance(page, dict):
                        for k in ['title', 'text']:
                            if k in page and isinstance(page[k], str) and page[k].strip():
                                page[k] = translation_dict.get(page[k], page[k])
            final_map[jar_name][zip_path] = cloned
    return final_map

# ==========================================
# Project Intelligence Manual Parser (.xml)
# ==========================================

def extract_pi_xml_texts(xml_map):
    """
    xml_map: { file_path: xml_content_string }
    반환값: unique_texts (list)
    XML 태그 내부의 텍스트를 정규식으로 추출합니다.
    주로 <Page ...> ... </Page>, <Text> ... </Text>, <Title> ... </Title> 등
    """
    unique_texts = set()
    # CDATA나 일반 텍스트 노드를 찾기 위한 정규식
    # <Tag>텍스트</Tag> 형태를 추출
    pattern = re.compile(r'>\s*([^<]+?)\s*</')
    for path, content in xml_map.items():
        matches = pattern.findall(content)
        for match in matches:
            text = match.strip()
            if text and not text.isnumeric():
                unique_texts.add(text)
                
    return list(unique_texts)

def assemble_pi_xml_books(xml_map, translation_dict):
    final_map = {}
    for path, content in xml_map.items():
        new_content = content
        
        # 긴 텍스트부터 치환하여 부분 치환 오류 방지
        sorted_texts = sorted(translation_dict.keys(), key=len, reverse=True)
        
        for orig in sorted_texts:
            trans = translation_dict[orig]
            if not trans or orig == trans:
                continue
            # XML 태그 사이의 정확한 텍스트만 치환하기 위해 정규식 사용
            # lookbehind 에서는 가변길이(\s*)를 지원하지 않으므로 그룹 캡처로 대체
            escaped_orig = re.escape(orig)
            pattern = re.compile(f"(>\\s*)({escaped_orig})(\\s*</)")
            
            def repl(match):
                return match.group(1) + trans + match.group(3)
                
            new_content = pattern.sub(repl, new_content)
            
        final_map[path] = new_content
    return final_map

# ==========================================
# GuideME MDX Guidebook Parser (.md)
# ==========================================

def extract_guideme_texts(guideme_map):
    """
    GuideME MDX 가이드북(.md) 파일들에서 YAML 메타데이터와 마크다운 본문 텍스트를 추출합니다.
    JSX 커스텀 컴포넌트(<RecipeFor />, <GameScene> 등)와 마크다운 링크 주소는 번역 방지 마스킹을 수행합니다.
    guideme_map: { jar_name: { zip_path: content_string } }
    반환값: (unique_texts, parsed_map)
    """
    unique_texts = set()
    parsed_map = {}
    
    for jar_name, zip_dict in guideme_map.items():
        parsed_map[jar_name] = {}
        for zip_path, content in zip_dict.items():
            frontmatter = ''
            body = content
            has_fm = content.startswith('---')
            if has_fm:
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    frontmatter = parts[1]
                    body = parts[2]
                    
            # YAML Frontmatter에서 title 추출 (오직 title만 번역 대상)
            title_match = re.search(r'title:\s*["\']?(.*?)["\']?\s*$', frontmatter, re.MULTILINE)
            title = None
            if title_match:
                title = title_match.group(1).strip('"').strip("'")
            if title and re.search(r'[a-zA-Z]', title):
                unique_texts.add(title)
                
            tag_map = {}
            tag_idx = 0
            def mask_tag(match):
                nonlocal tag_idx
                token = f'__GMTAG_{tag_idx}__'
                tag_map[token] = match.group(0)
                tag_idx += 1
                return token
                
            url_map = {}
            url_idx = 0
            def mask_url(match):
                nonlocal url_idx
                token = f'__GMURL_{url_idx}__'
                url_map[token] = match.group(2)
                url_idx += 1
                return f'[{match.group(1)}]({token})'

            # 1. 마크다운 링크의 목적지 URL/파일경로 마스킹: [텍스트](경로) -> [텍스트](__GMURL_x__)
            masked_body = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', mask_url, body)
            # 2. JSX 커스텀 태그 마스킹: <Tag attr="..." /> -> __GMTAG_x__
            masked_body = re.sub(r'<[^>]+>', mask_tag, masked_body)
            
            raw_chunks = masked_body.split('\n\n')
            parsed_chunks = []
            
            for c in raw_chunks:
                c_clean = c.strip()
                # 토큰을 제거했을 때 실제 번역할 영문 텍스트가 존재하는지 확인
                pure_text = re.sub(r'__GMTAG_\d+__|__GMURL_\d+__', '', c_clean).strip()
                if pure_text and re.search(r'[a-zA-Z]', pure_text):
                    unique_texts.add(c_clean)
                    parsed_chunks.append((c_clean, True, c))
                else:
                    parsed_chunks.append((c_clean, False, c))
                    
            parsed_map[jar_name][zip_path] = {
                'frontmatter': frontmatter,
                'title': title,
                'has_fm': has_fm,
                'chunks': parsed_chunks,
                'tag_map': tag_map,
                'url_map': url_map
            }
            
    return list(unique_texts), parsed_map

def assemble_guideme_books(parsed_map, translation_dict):
    """
    번역된 텍스트와 마스킹된 JSX 태그, 링크 주소를 원본 MDX 구조에 맞게 복원 및 재조립합니다.
    parsed_map: { jar_name: { zip_path: meta_dict } }
    translation_dict: { orig_text: trans_text }
    반환값: final_map { jar_name: { zip_path: final_content } }
    """
    final_map = {}
    for jar_name, zip_dict in parsed_map.items():
        final_map[jar_name] = {}
        for zip_path, meta in zip_dict.items():
            fm = meta['frontmatter']
            title = meta['title']
            if title and title in translation_dict:
                trans_title = translation_dict[title]
                fm = re.sub(r'(title:\s*["\']?)(.*?)(["\']?\s*$)', rf'\g<1>{trans_title}\g<3>', fm, flags=re.MULTILINE)
                
            out_chunks = []
            for clean, is_trans, orig in meta['chunks']:
                if is_trans and clean in translation_dict:
                    out_chunks.append(translation_dict[clean])
                else:
                    out_chunks.append(orig)
                    
            body = '\n\n'.join(out_chunks)
            
            # 마스킹된 링크 주소 복원
            for token, url in meta['url_map'].items():
                body = body.replace(token, url)
                
            # 마스킹된 JSX 태그 복원
            for token, tag in meta['tag_map'].items():
                body = body.replace(token, tag)
                
            if meta['has_fm']:
                final_content = f'---{fm}---' + body
            else:
                final_content = body
                
            final_map[jar_name][zip_path] = final_content
            
    return final_map
