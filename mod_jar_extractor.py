import os
import zipfile
import json
import logging
import re
from io import BytesIO

def _load_relaxed_json(json_string):
    """
    Minecraft 모드 JSON 파일들은 종종 주석(//, /* */)이나 후행 쉼표(trailing commas)를 포함합니다.
    이를 제거하고 파싱하여 JSONDecodeError를 방지합니다.
    """
    # 1. /* ... */ 다중 줄 주석 제거
    json_string = re.sub(r'/\*.*?\*/', '', json_string, flags=re.DOTALL)
    
    # 2. // 한 줄 주석 제거
    lines = []
    for line in json_string.splitlines():
        in_string = False
        escape = False
        comment_idx = -1
        for i, char in enumerate(line):
            if escape:
                escape = False
                continue
            if char == '\\':
                escape = True
            elif char == '"':
                in_string = not in_string
            elif char == '/' and i + 1 < len(line) and line[i+1] == '/' and not in_string:
                comment_idx = i
                break
        
        if comment_idx != -1:
            line = line[:comment_idx]
        lines.append(line)
        
    json_string = '\n'.join(lines)
    
    # 3. Trailing commas (], 또는 }, 전에 있는 ,) 제거
    json_string = re.sub(r',\s*([\]}])', r'\1', json_string)
    
    return json.loads(json_string)

def find_patchouli_books_in_jars(mods_dir, log_callback=None):
    """
    mods 폴더 안의 모든 .jar 파일을 검사하여 patchouli_books 폴더 내의 .json 파일들을 찾습니다.
    반환값: dict - { "jar_filename": { "zip_path": json_data } }
    """
    found_books = {}
    if not os.path.isdir(mods_dir):
        if log_callback:
            log_callback(f"⚠️ mods 폴더를 찾을 수 없습니다: {mods_dir}")
        return found_books

    jar_files = [f for f in os.listdir(mods_dir) if f.lower().endswith('.jar')]
    
    if log_callback:
        log_callback(f"🔍 {len(jar_files)}개의 모드(.jar) 파일에서 가이드북 데이터를 스캔합니다...")

    for jar_file in jar_files:
        jar_path = os.path.join(mods_dir, jar_file)
        try:
            with zipfile.ZipFile(jar_path, 'r') as zf:
                # patchouli_books 경로를 포함하는 .json 파일 찾기 (1.12.2는 assets/, 1.14+는 data/)
                # ⚠️ en_us 폴더와 book.json(메타데이터)만 추출. ko_kr, zh_cn 등 이미 번역된 다른 언어 파일은 제외하여 API 낭비 방지
                _OTHER_LANG_FOLDERS = {'ko_kr','zh_cn','zh_tw','ja_jp','pt_br','ru_ru','de_de','fr_fr','es_es','it_it','pl_pl','nl_nl','sv_se','da_dk','nb_no','fi_fi','hu_hu','cs_cz','ro_ro','tr_tr','th_th','vi_vn','uk_ua','el_gr','bg_bg','hr_hr','lt_lt','lv_lv','sk_sk','sl_si','et_ee','ga_ie','mt_mt','ca_es','gl_es','eu_es'}
                def _is_non_english_lang_path(path):
                    parts = path.split('/')
                    for part in parts:
                        if part in _OTHER_LANG_FOLDERS:
                            return True
                    return False
                    
                book_files = [
                    name for name in zf.namelist() 
                    if name.lower().endswith('.json') and 
                       ('patchouli_books/' in name.lower() or 'guideapi/' in name.lower()) and
                       'models/' not in name.lower() and
                       'blockstates/' not in name.lower() and
                       'textures/' not in name.lower() and
                       not _is_non_english_lang_path(name.lower())
                ]
                
                if book_files:
                    jar_books = {}
                    for bf in book_files:
                        try:
                            data = zf.read(bf)
                            # 간단한 JSON 검증 및 주석 제거
                            json_data = _load_relaxed_json(data.decode('utf-8', errors='ignore'))
                            jar_books[bf] = json_data
                        except Exception as e:
                            if log_callback:
                                log_callback(f"⚠️ {jar_file} 내부의 {bf} 파싱 실패: {e}")
                            
                    if jar_books:
                        found_books[jar_file] = jar_books
                        if log_callback:
                            log_callback(f"📚 {jar_file} 에서 {len(jar_books)}개의 가이드북 페이지를 찾았습니다.")
        except zipfile.BadZipFile:
            pass # 손상된 jar 파일 무시
        except Exception as e:
            if log_callback:
                log_callback(f"⚠️ {jar_file} 스캔 중 오류 발생: {e}")

    return found_books

def find_custom_guidebooks_in_jars(mods_dir, log_callback=None):
    """
    McJty(XNet/RFTools), Forestry, OpenComputers, EU2 등의 커스텀 가이드북을 찾습니다.
    반환값 dict - { 
        "mcjty": { "jar_filename": { "zip_path": text_content_string } },
        "forestry": { "jar_filename": { "zip_path": json_data } },
        "markdown": { "jar_filename": { "zip_path": text_content_string } },
        "eu2": { "jar_filename": { "zip_path": json_data } }
    }
    """
    found_books = {"mcjty": {}, "forestry": {}, "markdown": {}, "eu2": {}, "pi_xml": {}}
    if not os.path.isdir(mods_dir):
        return found_books

    jar_files = [f for f in os.listdir(mods_dir) if f.lower().endswith('.jar')]
    
    for jar_file in jar_files:
        jar_path = os.path.join(mods_dir, jar_file)
        try:
            with zipfile.ZipFile(jar_path, 'r') as zf:
                all_names = zf.namelist()
                
                # 1. McJty (XNet, RFTools, RFToolsDim, etc)
                if any(x in jar_file.lower() for x in ['xnet', 'rftools']):
                    # 텍스트 매뉴얼 파일들
                    mcjty_files = [n for n in all_names if n.lower().startswith('assets/') and '/text/manual' in n.lower() and n.endswith('.txt')]
                    for f in mcjty_files:
                        content = zf.read(f).decode('utf-8', errors='ignore')
                        if jar_file not in found_books["mcjty"]:
                            found_books["mcjty"][jar_file] = {}
                        found_books["mcjty"][jar_file][f] = content
                    if jar_file in found_books["mcjty"] and log_callback:
                        log_callback(f"🔍 {jar_file} 에서 McJty 매뉴얼을 찾았습니다.")
                        
                # 2. Forestry (json files)
                if 'forestry' in jar_file.lower():
                    forestry_files = [n for n in all_names if n.lower().startswith('assets/forestry/manual/en_us/') and n.endswith('.json')]
                    for f in forestry_files:
                        try:
                            content = _load_relaxed_json(zf.read(f).decode('utf-8', errors='ignore'))
                            if jar_file not in found_books["forestry"]:
                                found_books["forestry"][jar_file] = {}
                            found_books["forestry"][jar_file][f] = content
                        except Exception:
                            pass
                    if jar_file in found_books["forestry"] and log_callback:
                        log_callback(f"🔍 {jar_file} 에서 Forestry 매뉴얼을 찾았습니다.")
                        
                # 3. Markdown (OpenComputers, BuildCraft)
                if 'opencomputers' in jar_file.lower() or 'buildcraft' in jar_file.lower():
                    md_files = [n for n in all_names if n.lower().startswith('assets/') and n.endswith('.md')]
                    for f in md_files:
                        content = zf.read(f).decode('utf-8', errors='ignore')
                        if jar_file not in found_books["markdown"]:
                            found_books["markdown"][jar_file] = {}
                        found_books["markdown"][jar_file][f] = content
                    if jar_file in found_books["markdown"] and log_callback:
                        log_callback(f"🔍 {jar_file} 에서 마크다운 매뉴얼을 찾았습니다.")

                # 4. Extra Utilities 2 (en_us.json book)
                if 'extrautils2' in jar_file.lower():
                    eu2_files = [n for n in all_names if n.lower().startswith('assets/extrautils2/lang/book/') and n.endswith('.json')]
                    for f in eu2_files:
                        try:
                            content = _load_relaxed_json(zf.read(f).decode('utf-8', errors='ignore'))
                            if jar_file not in found_books["eu2"]:
                                found_books["eu2"][jar_file] = {}
                            found_books["eu2"][jar_file][f] = content
                        except Exception:
                            pass
                    if jar_file in found_books["eu2"] and log_callback:
                        log_callback(f"🔍 {jar_file} 에서 EU2 매뉴얼을 찾았습니다.")

        except Exception as e:
            if log_callback: log_callback(f"⚠️ {jar_file} 읽기 오류: {e}")
            
    # 5. Project Intelligence (Draconic Evolution XML)
    # config/brandon3055/ProjectIntelligence/ModDocs/
    config_dir = os.path.join(os.path.dirname(mods_dir), "config")
    pi_docs_dir = os.path.join(config_dir, "brandon3055", "ProjectIntelligence", "ModDocs")
    if os.path.isdir(pi_docs_dir):
        for root, dirs, files in os.walk(pi_docs_dir):
            for file in files:
                if file.endswith('.xml'):
                    file_path = os.path.join(root, file)
                    try:
                        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                            content = f.read()
                        found_books["pi_xml"][file_path] = content
                    except Exception:
                        pass
        if found_books["pi_xml"] and log_callback:
            log_callback(f"🔍 Project Intelligence에서 다운로드된 XML 매뉴얼 {len(found_books['pi_xml'])}개를 찾았습니다.")

    return found_books

def get_pack_format(modpack_dir):
    try:
        if not modpack_dir: return 3
        # CurseForge
        cf_json = os.path.join(modpack_dir, 'minecraftinstance.json')
        if os.path.exists(cf_json):
            with open(cf_json, 'r', encoding='utf-8') as f:
                data = json.load(f)
                mc_version = data.get('baseModLoader', {}).get('minecraftVersion', '')
                if not mc_version:
                    mc_version = data.get('gameVersion', '')
                if mc_version:
                    return _version_to_format(mc_version)
        
        # Prism Launcher / MultiMC
        mmc_cfg = os.path.join(modpack_dir, 'instance.cfg')
        if os.path.exists(mmc_cfg):
            with open(mmc_cfg, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.startswith('IntendedVersion='):
                        mc_version = line.split('=')[1].strip()
                        return _version_to_format(mc_version)
    except Exception:
        pass
    return 3

def _version_to_format(version_str):
    if version_str.startswith("1.11") or version_str.startswith("1.12"): return 3
    if version_str.startswith("1.13") or version_str.startswith("1.14"): return 4
    if version_str.startswith("1.15") or version_str == "1.16" or version_str == "1.16.1": return 5
    if version_str.startswith("1.16."): return 6
    if version_str.startswith("1.17"): return 7
    if version_str.startswith("1.18"): return 8
    if version_str.startswith("1.19.3"): return 12
    if version_str.startswith("1.19.4"): return 13
    if version_str.startswith("1.19"): return 9
    if version_str.startswith("1.20.1") or version_str.startswith("1.20"): return 15
    if version_str.startswith("1.21"): return 34
    return 3

def create_resource_pack(output_zip_path, translated_books_map, pack_description="Translated Guidebooks", modpack_dir=None):
    """
    번역된 JSON 데이터(translated_books_map)를 바탕으로 마인크래프트 리소스팩/데이터팩(.zip)을 생성합니다.
    translated_books_map 형식: { "jar_filename": { "zip_path": translated_json_data } }
    """
    p_format = get_pack_format(modpack_dir)
    with zipfile.ZipFile(output_zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # 1. pack.mcmeta 생성 (리소스팩 식별자)
        pack_mcmeta = {
            "pack": {
                "pack_format": p_format,
                "description": pack_description
            }
        }
        zf.writestr('pack.mcmeta', json.dumps(pack_mcmeta, ensure_ascii=True, indent=2))
        # 1x1 투명 PNG (pack.png 없으면 1.12.2에서 비호환 판정 받을 수 있음)
        import base64
        BLANK_PNG = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='
        )
        zf.writestr('pack.png', BLANK_PNG)

        # 2. 번역된 파일들을 원본 경로 그대로(en_us) 덮어쓰기하여 zip에 쓰기
        # 클라이언트 언어가 영어이거나 모드가 en_us를 하드코딩한 경우를 위해 en_us 경로를 유지하는 것이 가장 안전합니다.
        written_paths = set()
        for jar_name, files in translated_books_map.items():
            for zip_path, json_data in files.items():
                write_path = zip_path
                
                # Patchouli 1.14+ 가이드북은 data/ 폴더에 존재하지만,
                # 유저가 리소스팩으로 적용하기 위해서는 assets/ 경로로 매핑해야 엔진이 인식합니다.
                if write_path.startswith("data/") and "/patchouli_books/" in write_path:
                    parts = write_path.split('/')
                    if len(parts) >= 5:
                        namespace = parts[1]
                        book_id = parts[3]
                        if parts[4] == 'en_us':
                            write_path = "assets/" + "/".join(parts[1:])
                        else:
                            rest = "/".join(parts[4:])
                            write_path = f"assets/{namespace}/patchouli_books/{book_id}/en_us/{rest}"
                            
                if write_path in written_paths:
                    continue
                written_paths.add(write_path)
                
                json_str = json.dumps(json_data, ensure_ascii=False, indent=2)
                zf.writestr(write_path, json_str)

def extract_lang_files_from_jars(mods_dir, log_callback=None):
    """
    mods 폴더 안의 모든 .jar 파일을 검사하여 assets/*/lang/en_us.lang 파일들을 찾습니다.
    반환값: dict - { "jar_filename": { "zip_path": "lang_content_string" } }
    """
    found_langs = {}
    if not os.path.isdir(mods_dir):
        if log_callback:
            log_callback(f"⚠️ mods 폴더를 찾을 수 없습니다: {mods_dir}")
        return found_langs

    jar_files = [f for f in os.listdir(mods_dir) if f.lower().endswith('.jar')]
    
    if log_callback:
        log_callback(f"🔍 {len(jar_files)}개의 모드(.jar) 파일에서 en_us.lang 파일을 스캔합니다...")

    for jar_file in jar_files:
        jar_path = os.path.join(mods_dir, jar_file)
        try:
            with zipfile.ZipFile(jar_path, 'r') as zf:
                # assets/.../lang/en_us.lang 또는 en_us.json 찾기
                lang_files = [
                    name for name in zf.namelist() 
                    if (name.lower().endswith('en_us.lang') or name.lower().endswith('en_us.json') or name.lower().endswith('en_us.json5')) 
                    and name.startswith('assets/') 
                    and '/lang/' in name.lower()
                    and '/book/' not in name.lower()
                    and '/patchouli_books/' not in name.lower()
                    and '/lexicon/' not in name.lower()
                ]
                
                if lang_files:
                    jar_langs = {}
                    for lf in lang_files:
                        try:
                            data = zf.read(lf)
                            text_content = data.decode('utf-8', errors='ignore')
                            jar_langs[lf] = text_content
                        except Exception as e:
                            logging.warning(f"Error parsing lang {lf} in {jar_file}: {e}")
                    
                    if jar_langs:
                        found_langs[jar_file] = jar_langs
        except Exception as e:
            logging.debug(f"Failed to read jar {jar_file}: {e}")

    return found_langs

def create_lang_resource_pack(translated_langs, output_zip_path, modpack_dir=None):
    """
    번역된 lang 데이터를 ko_kr.lang 파일명으로 바꾸어 리소스팩으로 묶습니다.
    translated_langs: { "jar_name": { "assets/modid/lang/en_us.lang": "번역된_lang_내용" } }
    """
    p_format = get_pack_format(modpack_dir)
    with zipfile.ZipFile(output_zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # pack.mcmeta 생성
        pack_mcmeta = {
            "pack": {
                "pack_format": p_format,
                "description": "QuestTranslatorPro Lang Translations"
            }
        }
        zf.writestr("pack.mcmeta", json.dumps(pack_mcmeta, indent=4, ensure_ascii=True))
        # 1x1 투명 PNG (pack.png 없으면 1.12.2에서 비호환 판정 받을 수 있음)
        import base64
        BLANK_PNG = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='
        )
        zf.writestr('pack.png', BLANK_PNG)

        # 번역된 파일 추가
        written_paths = set()
        for jar_name, lang_dict in translated_langs.items():
            for original_zip_path, translated_content in lang_dict.items():
                # en_us.lang -> ko_kr.lang 로 변경
                ko_kr_path = original_zip_path.replace('en_us.lang', 'ko_kr.lang').replace('en_US.lang', 'ko_kr.lang')
                if ko_kr_path in written_paths:
                    continue
                written_paths.add(ko_kr_path)
                zf.writestr(ko_kr_path, translated_content)

def create_combined_resource_pack(translated_langs, translated_books_map, output_zip_path, modpack_dir=None, custom_map=None):
    """
    .lang 번역본과 Patchouli 가이드북 번역본을 하나의 리소스팩 zip으로 통합 생성합니다.
    translated_langs: { "jar_name": { "assets/modid/lang/en_us.lang": "번역내용" } }
    translated_books_map: { "jar_name": { "zip_path": json_data } }
    """
    import base64
    BLANK_PNG = base64.b64decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='
    )
    p_format = get_pack_format(modpack_dir)
    with zipfile.ZipFile(output_zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        pack_mcmeta = {
            "pack": {
                "pack_format": p_format,
                "description": "QuestTranslatorPro Korean Translation"
            }
        }
        zf.writestr("pack.mcmeta", json.dumps(pack_mcmeta, indent=2, ensure_ascii=True))
        zf.writestr("pack.png", BLANK_PNG)

        written_paths = set()

        # 1. .lang 및 .json 파일 (en_us -> ko_kr)
        if translated_langs:
            for jar_name, lang_dict in translated_langs.items():
                for original_zip_path, translated_content in lang_dict.items():
                    ko_kr_path = original_zip_path
                    for ext in ['.lang', '.json', '.json5']:
                        if ko_kr_path.lower().endswith('en_us' + ext):
                            ko_kr_path = ko_kr_path[:-len('en_us' + ext)] + 'ko_kr' + ext
                            break
                    if ko_kr_path in written_paths:
                        continue
                    written_paths.add(ko_kr_path)
                    zf.writestr(ko_kr_path, translated_content)

        # 2. Patchouli JSON 파일 (원본 경로 en_us 그대로 덮어쓰기)
        if translated_books_map:
            for jar_name, files in translated_books_map.items():
                for zip_path, json_data in files.items():
                    if zip_path in written_paths:
                        continue
                    written_paths.add(zip_path)
                    zf.writestr(zip_path, json.dumps(json_data, ensure_ascii=True, indent=2))
        
        # 3. Custom Books (McJty, Forestry, etc)
        if custom_map:
            for book_type, type_dict in custom_map.items():
                if book_type == "pi_xml":
                    # PI XML은 리소스팩이 아닌 로컬 파일 시스템(config 폴더)에 직접 저장합니다.
                    for path, content in type_dict.items():
                        # en_US를 ko_KR로 변경하여 저장
                        new_path = path.replace('en_US', 'ko_KR')
                        try:
                            with open(new_path, 'w', encoding='utf-8') as f:
                                f.write(content)
                        except Exception:
                            pass
                    continue
                
                for jar_name, files in type_dict.items():
                    for zip_path, content in files.items():
                        new_path = zip_path.replace('en_us', 'ko_kr').replace('en_US', 'ko_kr')
                        if new_path in written_paths:
                            continue
                        written_paths.add(new_path)
                        
                        # 중요: EU2는 JSON 파일을 읽을 때 시스템 기본 인코딩(CP949)을 사용해서 깨짐 현상이 발생합니다.
                        # 이를 방지하기 위해 ensure_ascii=True로 설정하여 순수 ASCII와 \uXXXX 유니코드 이스케이프로 저장합니다.
                        if isinstance(content, dict) or isinstance(content, list):
                            zf.writestr(new_path, json.dumps(content, ensure_ascii=True, indent=2))
                        else:
                            zf.writestr(new_path, content.encode('utf-8'))
