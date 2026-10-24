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
    
    # 4. 모드 제작자가 문자열 내부에 탭(\t)/개행 등 제어문자를 넣거나 파일 끝에 쓰레기 데이터를 남긴 경우까지 안전 파싱
    json_string_stripped = json_string.strip()
    try:
        return json.loads(json_string_stripped, strict=False)
    except json.JSONDecodeError as e:
        if "Extra data" in str(e):
            decoder = json.JSONDecoder(strict=False)
            obj, _ = decoder.raw_decode(json_string_stripped)
            return obj
        raise

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
                    
                # ⚠️ 이미 공식 한국어(ko_kr) 가이드북이 내장된 모드(Botania, Ars Nouveau 등)는 추출을 건너뛰어 인게임 중복 엔트리 버그 방지
                if any('/patchouli_books/' in n.lower() and '/ko_kr/' in n.lower() for n in zf.namelist()) or 'botania' in jar_file.lower():
                    continue

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

def get_guideme_target_path(zip_path, target_lang='ko_kr'):
    """
    GuideME MDX 가이드북(.md) 파일의 리소스팩 번역 경로(_ko_kr/)를 반환합니다.
    - assets/<ns>/guides/<book_ns>/<book_id>/path/to/page.md -> assets/<ns>/guides/<book_ns>/<book_id>/_<lang>/path/to/page.md
    - assets/<ns>/<custom_folder>/path/to/page.md -> assets/<ns>/<custom_folder>/_<lang>/path/to/page.md (ae2guide, mi_guidebook, yai_guidebook 등)
    """
    parts = zip_path.replace('\\', '/').split('/')
    if len(parts) >= 6 and parts[2] == 'guides':
        ns = parts[1]
        book_ns = parts[3]
        book_id = parts[4]
        rest = '/'.join(parts[5:])
        return f"assets/{ns}/guides/{book_ns}/{book_id}/_{target_lang}/{rest}"
    elif len(parts) >= 4:
        ns = parts[1]
        folder = parts[2]
        rest = '/'.join(parts[3:])
        return f"assets/{ns}/{folder}/_{target_lang}/{rest}"
    return None

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
    found_books = {"mcjty": {}, "forestry": {}, "markdown": {}, "eu2": {}, "pi_xml": {}, "guideme": {}}
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
                    all_names_lower = {n.lower() for n in all_names}
                    md_files = [
                        n for n in all_names
                        if n.lower().startswith('assets/') and n.endswith('.md')
                        and ('en_us' in n.lower())
                        and not (n.lower().replace('en_us', 'ko_kr') in all_names_lower)
                    ]
                    for f in md_files:
                        content = zf.read(f).decode('utf-8', errors='ignore')
                        if jar_file not in found_books["markdown"]:
                            found_books["markdown"][jar_file] = {}
                        found_books["markdown"][jar_file][f] = content
                    if jar_file in found_books["markdown"] and log_callback:
                        log_callback(f"🔍 {jar_file} 에서 마크다운 매뉴얼을 찾았습니다.")

                # 4. Extra Utilities 2 (en_us.json book)
                if 'extrautils2' in jar_file.lower():
                    all_names_lower = {n.lower() for n in all_names}
                    eu2_files = [
                        n for n in all_names
                        if n.lower().startswith('assets/extrautils2/lang/book/') and n.lower().endswith('en_us.json')
                        and not (n.lower().replace('en_us.json', 'ko_kr.json') in all_names_lower)
                    ]
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

                # 6. GuideME (Little Big Redstone, Modern Industrialization, AE2, ExtendedAE, etc.)
                _GUIDEME_OTHER_LANGS = {'_' + lang for lang in ['ko_kr','zh_cn','zh_tw','ja_jp','pt_br','ru_ru','de_de','fr_fr','es_es','it_it','pl_pl','nl_nl','sv_se','da_dk','nb_no','fi_fi','hu_hu','cs_cz','ro_ro','tr_tr','th_th','vi_vn','uk_ua','el_gr','bg_bg','hr_hr','lt_lt','lv_lv','sk_sk','sl_si','et_ee','ga_ie','mt_mt','ca_es','gl_es','eu_es']}
                all_names_lower = {n.lower() for n in all_names}
                for n in all_names:
                    nl = n.lower()
                    if nl.startswith('assets/') and nl.endswith('.md'):
                        parts = nl.split('/')
                        if len(parts) >= 4:
                            if any(p in _GUIDEME_OTHER_LANGS for p in parts):
                                continue
                            sub = parts[2]
                            if sub in ('guides', 'ae2guide') or 'guidebook' in sub:
                                target_ko = get_guideme_target_path(n, 'ko_kr')
                                if target_ko and target_ko.lower() in all_names_lower:
                                    continue
                                try:
                                    content = zf.read(n).decode('utf-8', errors='ignore')
                                    if jar_file not in found_books["guideme"]:
                                        found_books["guideme"][jar_file] = {}
                                    found_books["guideme"][jar_file][n] = content
                                except Exception:
                                    pass
                if jar_file in found_books["guideme"] and log_callback:
                    log_callback(f"📚 {jar_file} 에서 GuideME MDX 가이드북 {len(found_books['guideme'][jar_file])}개 페이지를 찾았습니다.")

        except Exception as e:
            if log_callback: log_callback(f"⚠️ {jar_file} 읽기 오류: {e}")
            
    # 5. Project Intelligence (Draconic Evolution XML)
    # config/brandon3055/ProjectIntelligence/ModDocs/
    config_dir = os.path.join(os.path.dirname(mods_dir), "config")
    pi_docs_dir = os.path.join(config_dir, "brandon3055", "ProjectIntelligence", "ModDocs")
    if os.path.isdir(pi_docs_dir):
        for root, dirs, files in os.walk(pi_docs_dir):
            for file in files:
                if file.lower().endswith('.xml') and ('en_us' in file.lower() or 'en_us' in root.lower()) and 'ko_kr' not in file.lower():
                    file_path = os.path.join(root, file)
                    try:
                        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                            content = f.read()
                        found_books["pi_xml"][file_path] = content
                    except Exception:
                        pass
        if found_books["pi_xml"] and log_callback:
            log_callback(f"🔍 Project Intelligence에서 다운로드된 XML 매뉴얼 {len(found_books['pi_xml'])}개를 찾았습니다.")

    # 6.1 GuideME in kubejs/assets/
    modpack_dir = os.path.dirname(mods_dir)
    kubejs_assets = os.path.join(modpack_dir, "kubejs", "assets")
    if os.path.isdir(kubejs_assets):
        _GUIDEME_OTHER_LANGS = {'_' + lang for lang in ['ko_kr','zh_cn','zh_tw','ja_jp','pt_br','ru_ru','de_de','fr_fr','es_es','it_it','pl_pl','nl_nl','sv_se','da_dk','nb_no','fi_fi','hu_hu','cs_cz','ro_ro','tr_tr','th_th','vi_vn','uk_ua','el_gr','bg_bg','hr_hr','lt_lt','lv_lv','sk_sk','sl_si','et_ee','ga_ie','mt_mt','ca_es','gl_es','eu_es']}
        for root, _, files in os.walk(kubejs_assets):
            for fn in files:
                if fn.lower().endswith('.md'):
                    fp = os.path.join(root, fn)
                    rel = os.path.relpath(fp, kubejs_assets).replace('\\', '/')
                    parts = rel.lower().split('/')
                    if len(parts) >= 3:
                        if any(p in _GUIDEME_OTHER_LANGS for p in parts):
                            continue
                        sub = parts[1]
                        if sub in ('guides', 'ae2guide') or 'guidebook' in sub:
                            try:
                                with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                                    content = f.read()
                                zip_path = f"assets/{rel}"
                                if "KubeJS" not in found_books["guideme"]:
                                    found_books["guideme"]["KubeJS"] = {}
                                found_books["guideme"]["KubeJS"][zip_path] = content
                            except Exception:
                                pass
        if "KubeJS" in found_books["guideme"] and log_callback:
            log_callback(f"📚 KubeJS 가상 리소스에서 GuideME 가이드북 {len(found_books['guideme']['KubeJS'])}개 페이지를 찾았습니다.")

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

        # options.txt의 DataVersion으로 판별 (Modrinth App 등 모든 런처 공통)
        opt_path = os.path.join(modpack_dir, 'options.txt')
        if os.path.exists(opt_path):
            with open(opt_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if line.startswith('version:'):
                        dv = int(line.split(':', 1)[1].strip())
                        if dv >= 3950: return 34
                        if dv >= 3460: return 15
                        if dv >= 3100: return 9
                        if dv >= 2860: return 8
                        if dv >= 2560: return 6
                        if dv >= 1500: return 4
                        return 3
    except Exception:
        pass
    return 15

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

def extract_lang_files_from_jars(mods_dir, log_callback=None, include_ko_kr=False):
    """
    mods 폴더 안의 모든 .jar 파일을 검사하여 assets/*/lang/en_us.lang 파일들을 찾습니다.
    include_ko_kr=True일 경우 모드 내장 한국어 파일(ko_kr.lang, ko_kr.json 등)도 함께 추출하여 반환합니다.
    반환값:
      - include_ko_kr=False: dict - { "jar_filename": { "zip_path": "lang_content_string" } }
      - include_ko_kr=True: (found_langs, found_ko_langs)
        found_ko_langs 형식: { "jar_filename": { "en_zip_path": ("ko_zip_path", "ko_content_string") } }
    """
    found_langs = {}
    found_ko_langs = {}
    if not os.path.isdir(mods_dir):
        if log_callback:
            log_callback(f"⚠️ mods 폴더를 찾을 수 없습니다: {mods_dir}")
        return (found_langs, found_ko_langs) if include_ko_kr else found_langs

    jar_files = [f for f in os.listdir(mods_dir) if f.lower().endswith('.jar')]
    
    if log_callback:
        log_callback(f"🔍 {len(jar_files)}개의 모드(.jar) 파일에서 언어 파일(en_us / ko_kr)을 스캔합니다...")

    global_ko_by_dir = {}  # { "assets/modid/lang": ("ko_zip_path", "ko_content_string") }

    for jar_file in jar_files:
        jar_path = os.path.join(mods_dir, jar_file)
        try:
            with zipfile.ZipFile(jar_path, 'r') as zf:
                namelist = zf.namelist()
                # assets/.../lang/en_us.lang 또는 en_us.json 찾기
                lang_files = [
                    name for name in namelist 
                    if (name.lower().endswith('en_us.lang') or name.lower().endswith('en_us.json') or name.lower().endswith('en_us.json5')) 
                    and name.startswith('assets/') 
                    and '/lang/' in name.lower()
                    and '/book/' not in name.lower()
                    and '/patchouli_books/' not in name.lower()
                    and '/lexicon/' not in name.lower()
                ]
                
                ko_files = []
                if include_ko_kr:
                    ko_files = [
                        name for name in namelist
                        if (name.lower().endswith('ko_kr.lang') or name.lower().endswith('ko_kr.json') or name.lower().endswith('ko_kr.json5'))
                        and name.startswith('assets/')
                        and '/lang/' in name.lower()
                        and '/book/' not in name.lower()
                        and '/patchouli_books/' not in name.lower()
                        and '/lexicon/' not in name.lower()
                    ]

                jar_ko_by_dir = {}
                for kf in ko_files:
                    try:
                        ko_data = zf.read(kf)
                        ko_text = ko_data.decode('utf-8', errors='ignore')
                        dir_key = os.path.dirname(kf).lower()
                        jar_ko_by_dir[dir_key] = (kf, ko_text)
                        global_ko_by_dir[dir_key] = (kf, ko_text)
                    except Exception as e:
                        logging.warning(f"Error parsing ko_kr lang {kf} in {jar_file}: {e}")

                if lang_files:
                    jar_langs = {}
                    jar_ko_matched = {}
                    for lf in lang_files:
                        try:
                            data = zf.read(lf)
                            text_content = data.decode('utf-8', errors='ignore')
                            jar_langs[lf] = text_content
                            dir_key = os.path.dirname(lf).lower()
                            if dir_key in jar_ko_by_dir:
                                jar_ko_matched[lf] = jar_ko_by_dir[dir_key]
                        except Exception as e:
                            logging.warning(f"Error parsing lang {lf} in {jar_file}: {e}")
                    
                    if jar_langs:
                        found_langs[jar_file] = jar_langs
                    if jar_ko_matched:
                        found_ko_langs[jar_file] = jar_ko_matched
        except Exception as e:
            logging.debug(f"Failed to read jar {jar_file}: {e}")

    # 다른 JAR(예: 별도 한글패치/리소스 모드 JAR)에 분리되어 있는 ko_kr 파일도 네임스페이스(assets/modid/lang) 기준으로 매칭
    if include_ko_kr and global_ko_by_dir:
        for jar_file, jar_langs in found_langs.items():
            for lf in jar_langs.keys():
                dir_key = os.path.dirname(lf).lower()
                if dir_key in global_ko_by_dir:
                    if jar_file not in found_ko_langs:
                        found_ko_langs[jar_file] = {}
                    if lf not in found_ko_langs[jar_file]:
                        found_ko_langs[jar_file][lf] = global_ko_by_dir[dir_key]

    # KubeJS / OpenLoader / Paxi 외부 가상 리소스팩 언어 파일 스캔
    modpack_dir = os.path.dirname(mods_dir)
    external_resource_roots = [
        os.path.join(modpack_dir, "kubejs", "assets"),
        os.path.join(modpack_dir, "resources"),
    ]
    for sub in ("openloader/resources", "paxi/resourcepacks", "global_packs/required_resources"):
        p = os.path.join(modpack_dir, *sub.split('/'))
        if os.path.isdir(p):
            try:
                for pack in os.listdir(p):
                    pack_assets = os.path.join(p, pack, "assets")
                    if os.path.isdir(pack_assets):
                        external_resource_roots.append(pack_assets)
            except Exception:
                pass

    ext_langs = {}
    ext_ko_langs = {}
    for assets_root in external_resource_roots:
        if not os.path.isdir(assets_root):
            continue
        try:
            for ns in os.listdir(assets_root):
                lang_dir = os.path.join(assets_root, ns, "lang")
                if not os.path.isdir(lang_dir):
                    continue
                # ⚠️ 절대 data/, models/, recipes/ 등 다른 폴더는 접근하지 않고 오직 assets/<ns>/lang/ 만 핀포인트 수집
                for fn in os.listdir(lang_dir):
                    fn_lower = fn.lower()
                    if fn_lower in ('en_us.json', 'en_us.lang', 'en_us.json5'):
                        fp = os.path.join(lang_dir, fn)
                        try:
                            with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                                content = f.read()
                            zip_path = f"assets/{ns}/lang/{fn}"
                            ext_langs[zip_path] = content
                            
                            ko_fn = fn_lower.replace('en_us', 'ko_kr')
                            ko_fp = os.path.join(lang_dir, ko_fn)
                            if os.path.isfile(ko_fp):
                                with open(ko_fp, 'r', encoding='utf-8', errors='ignore') as f_ko:
                                    ext_ko_langs[zip_path] = (f"assets/{ns}/lang/{ko_fn}", f_ko.read())
                        except Exception as e:
                            logging.debug(f"Error reading external lang file {fp}: {e}")
        except Exception as e:
            logging.debug(f"Error scanning external assets root {assets_root}: {e}")

    if ext_langs:
        found_langs["KubeJS_and_Custom_Packs"] = ext_langs
        if include_ko_kr and ext_ko_langs:
            found_ko_langs["KubeJS_and_Custom_Packs"] = ext_ko_langs
        if log_callback:
            log_callback(f"📦 KubeJS 및 외부 리소스에서 {len(ext_langs)}개의 추가 언어 파일을 발견했습니다.")

    return (found_langs, found_ko_langs) if include_ko_kr else found_langs

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
            merged_json_langs = {}
            merged_raw_langs = {}
            for jar_name, lang_dict in translated_langs.items():
                for original_zip_path, translated_content in lang_dict.items():
                    ko_kr_path = original_zip_path
                    for ext in ['.lang', '.json', '.json5']:
                        if ko_kr_path.lower().endswith('en_us' + ext):
                            ko_kr_path = ko_kr_path[:-len('en_us' + ext)] + 'ko_kr' + ext
                            break
                    if ko_kr_path.lower().endswith(('.json', '.json5')):
                        try:
                            data = _load_relaxed_json(translated_content)
                            if ko_kr_path not in merged_json_langs:
                                merged_json_langs[ko_kr_path] = {}
                            merged_json_langs[ko_kr_path].update(data)
                        except Exception:
                            merged_raw_langs[ko_kr_path] = translated_content
                    else:
                        merged_raw_langs[ko_kr_path] = translated_content

            for p, d in merged_json_langs.items():
                if p not in written_paths:
                    written_paths.add(p)
                    zf.writestr(p, json.dumps(d, ensure_ascii=False, indent=2))

            for p, content in merged_raw_langs.items():
                if p not in written_paths:
                    written_paths.add(p)
                    zf.writestr(p, content)

        # 2. Patchouli JSON 파일 (순수 assets/ 경로 리소스팩 패키징)
        if translated_books_map:
            for jar_name, files in translated_books_map.items():
                for zip_path, json_data in files.items():
                    write_path = zip_path
                    
                    # Patchouli 1.14+ 가이드북은 data/ 폴더에 존재하지만,
                    # 리소스팩 표준 적용을 위해 assets/ 경로로 깔끔하게 매핑합니다.
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
                    
                    if write_path not in written_paths:
                        written_paths.add(write_path)
                        zf.writestr(write_path, json.dumps(json_data, ensure_ascii=False, indent=2))
                    
                    # ko_kr 경로로도 복사본 저장 (한국어 클라이언트 완벽 호환)
                    if "/en_us/" in write_path:
                        ko_path = write_path.replace("/en_us/", "/ko_kr/")
                        if ko_path not in written_paths:
                            written_paths.add(ko_path)
                            zf.writestr(ko_path, json.dumps(json_data, ensure_ascii=False, indent=2))
        
        # 3. Custom Books (McJty, Forestry, etc)
        if custom_map:
            for book_type, type_dict in custom_map.items():
                if book_type == "guideme":
                    for jar_name, files in type_dict.items():
                        for zip_path, content in files.items():
                            target_path = get_guideme_target_path(zip_path, 'ko_kr')
                            if not target_path or target_path in written_paths:
                                continue
                            written_paths.add(target_path)
                            zf.writestr(target_path, content.encode('utf-8'))
                    continue
                if book_type == "pi_xml":
                    import temp_session_logger
                    if temp_session_logger.is_debug_mode():
                        # 디버그 모드 시 원본 모드팩 config 폴더 보호
                        continue
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


def apply_hybrid_patchouli_translations(mods_dir, translated_books_map, log_callback=None):
    """
    스마트 하이브리드 가이드북 번역기:
    마인크래프트 1.16 이하 버전에서 리소스팩을 읽지 못하는 패출리 모드(황혼의 숲, 비행기 등)를 자동 감지하여,
    원본 모드 JAR의 안전 백업(.bak)을 생성하고 JAR 내부의 책 JSON 텍스트만 안전하게 인플레이스 교체합니다.
    """
    if not mods_dir or not os.path.isdir(mods_dir) or not translated_books_map:
        return

    import tempfile, shutil

    for jar_name, files in translated_books_map.items():
        if not files:
            continue

        jar_path = os.path.join(mods_dir, jar_name)
        if not os.path.isfile(jar_path):
            continue

        # 공식 한국어(ko_kr)가 이미 완벽하게 내장된 모드는 절대 건드리지 않음
        try:
            with zipfile.ZipFile(jar_path, 'r') as zf_test:
                if any('/patchouli_books/' in n.lower() and '/ko_kr/' in n.lower() for n in zf_test.namelist()) or 'botania' in jar_name.lower():
                    continue
        except Exception:
            continue

        # 원본 .bak 백업 생성 (최초 1회 보존)
        bak_path = jar_path + ".bak"
        if not os.path.exists(bak_path):
            try:
                shutil.copy2(jar_path, bak_path)
                if log_callback:
                    log_callback(f"🛡️ [안전 백업] {jar_name} 원본 백업 파일(.bak) 생성 완료")
            except Exception as e:
                if log_callback:
                    log_callback(f"⚠️ {jar_name} 백업 생성 실패: {e}")

        # JAR 내부 책 JSON 인플레이스 교체
        file_map = {}
        for zip_path, json_data in files.items():
            if isinstance(json_data, (dict, list)):
                file_map[zip_path] = json.dumps(json_data, ensure_ascii=False, indent=2).encode('utf-8')
            elif isinstance(json_data, str):
                file_map[zip_path] = json_data.encode('utf-8')

        if not file_map:
            continue

        try:
            temp_dir = tempfile.mkdtemp()
            temp_jar = os.path.join(temp_dir, 'temp.jar')
            patched_count = 0

            seen_files = set()
            with zipfile.ZipFile(jar_path, 'r') as zin, zipfile.ZipFile(temp_jar, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    if item.filename in seen_files:
                        continue
                    seen_files.add(item.filename)
                    if item.filename in file_map:
                        zout.writestr(item.filename, file_map[item.filename])
                        patched_count += 1
                    else:
                        zout.writestr(item.filename, zin.read(item.filename))

            shutil.copy2(temp_jar, jar_path)
            shutil.rmtree(temp_dir)

            if log_callback:
                log_callback(f"✨ [스마트 하이브리드] '{jar_name}': 리소스팩 미지원 모드 자동 판별 ➡️ JAR 내부 가이드북 텍스트 {patched_count}개 파일 한글화 완료!")
        except Exception as err:
            if log_callback:
                log_callback(f"⚠️ '{jar_name}' 하이브리드 패치 중 오류: {err}")

def hotpatch_resource_pack(pack_zip_path, old_tgt, new_tgt):
    import tempfile
    import shutil
    
    if not os.path.exists(pack_zip_path):
        return
        
    temp_fd, temp_path = tempfile.mkstemp(suffix='.zip')
    os.close(temp_fd)
    
    changed = False
    
    seen_files = set()
    with zipfile.ZipFile(pack_zip_path, 'r') as zin:
        with zipfile.ZipFile(temp_path, 'w', zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename in seen_files:
                    continue
                seen_files.add(item.filename)
                content_bytes = zin.read(item.filename)
                
                if item.filename.endswith('.lang'):
                    try:
                        text = content_bytes.decode('utf-8')
                        new_lines = []
                        for line in text.split('\n'):
                            if '=' in line:
                                k, v = line.split('=', 1)
                                if v.strip() == old_tgt:
                                    line = f"{k}={new_tgt}"
                                    changed = True
                            new_lines.append(line)
                        content_bytes = '\n'.join(new_lines).encode('utf-8')
                    except Exception:
                        pass
                
                elif item.filename.endswith('.json') or item.filename.endswith('.json5'):
                    try:
                        text = content_bytes.decode('utf-8')
                        if old_tgt in text:
                            def replace_values(d):
                                nonlocal changed
                                if isinstance(d, dict):
                                    for k, v in d.items():
                                        if isinstance(v, str) and v == old_tgt:
                                            d[k] = new_tgt
                                            changed = True
                                        else:
                                            replace_values(v)
                                elif isinstance(d, list):
                                    for i, v in enumerate(d):
                                        if isinstance(v, str) and v == old_tgt:
                                            d[i] = new_tgt
                                            changed = True
                                        else:
                                            replace_values(v)
                                            
                            data = json.loads(text)
                            replace_values(data)
                            content_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
                    except Exception:
                        pass
                        
                zout.writestr(item, content_bytes)
                
    if changed:
        shutil.move(temp_path, pack_zip_path)
    else:
        os.remove(temp_path)



def apply_resource_pack_to_options(modpack_dir, pack_filename=None):
    """
    모드팩의 options.txt에 생성된 리소스팩을 최우선 순위로 자동 등록하고
    인게임 언어(lang:ko_kr) 설정을 함께 활성화합니다.
    반환값: (성공여부: bool, 메시지 또는 파일명: str)
    """
    import glob
    import shutil

    if not modpack_dir or not os.path.isdir(modpack_dir):
        return False, "유효한 모드팩 경로가 아닙니다."

    rp_dir = os.path.join(modpack_dir, "resourcepacks")
    os.makedirs(rp_dir, exist_ok=True)
    mp_name = os.path.basename(modpack_dir.rstrip(os.sep))

    # 모드팩 루트에 예전 리소스팩 zip이 잘못 놓여있다면 resourcepacks 폴더로 자동 이동
    for root_zip in glob.glob(os.path.join(modpack_dir, f"{mp_name}[*].zip")):
        if os.path.isfile(root_zip):
            dest_zip = os.path.join(rp_dir, os.path.basename(root_zip))
            try:
                if not os.path.exists(dest_zip):
                    shutil.move(root_zip, dest_zip)
                else:
                    os.remove(root_zip)
            except Exception:
                pass

    if not pack_filename:
        candidates = (
            glob.glob(os.path.join(rp_dir, f"{mp_name}[*].zip"))
            + glob.glob(os.path.join(rp_dir, "*[Korean].zip"))
            + glob.glob(os.path.join(rp_dir, "*Korean*.zip"))
            + glob.glob(os.path.join(rp_dir, "*한글*.zip"))
            + glob.glob(os.path.join(rp_dir, "QuestTranslatorPro_Pack.zip"))
        )
        if not candidates:
            return False, (
                "적용할 한글 리소스팩(.zip)을 resourcepacks 폴더에서 찾을 수 없습니다.\n"
                "먼저 '모드팩 전체 한글화'를 실행하여 리소스팩을 생성해주세요."
            )
        pack_filename = os.path.basename(candidates[0])

    p_format = get_pack_format(modpack_dir)
    options_path = os.path.join(modpack_dir, "options.txt")

    lines = []
    if os.path.isfile(options_path):
        try:
            with open(options_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
        except Exception as e:
            return False, f"options.txt 읽기 실패: {e}"

    use_file_prefix = p_format >= 4 or any(
        ("file/" in ln or '"vanilla"' in ln) for ln in lines if ln.startswith("resourcePacks:")
    )
    entry_name = f"file/{pack_filename}" if use_file_prefix else pack_filename

    found_rp = False
    found_incompat = False
    found_lang = False
    new_lines = []

    for line in lines:
        if line.startswith("resourcePacks:"):
            found_rp = True
            raw_val = line[len("resourcePacks:"):].strip()
            try:
                packs = json.loads(raw_val)
                if not isinstance(packs, list):
                    packs = []
            except Exception:
                packs = []

            if not packs and use_file_prefix:
                packs = ["vanilla", "mod_resources"]

            packs = [
                p for p in packs
                if p not in (entry_name, pack_filename, f"file/{pack_filename}")
            ]
            packs.append(entry_name)
            new_lines.append("resourcePacks:" + json.dumps(packs, ensure_ascii=False, separators=(",", ":")))

        elif line.startswith("incompatibleResourcePacks:"):
            found_incompat = True
            raw_val = line[len("incompatibleResourcePacks:"):].strip()
            try:
                incompat = json.loads(raw_val)
                if not isinstance(incompat, list):
                    incompat = []
            except Exception:
                incompat = []
            if entry_name not in incompat:
                incompat.append(entry_name)
            new_lines.append("incompatibleResourcePacks:" + json.dumps(incompat, ensure_ascii=False, separators=(",", ":")))

        elif line.startswith("lang:"):
            found_lang = True
            if "korean" in pack_filename.lower() or "한글" in pack_filename:
                new_lines.append("lang:ko_kr")
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    if not found_rp:
        default_packs = ["vanilla", "mod_resources", entry_name] if use_file_prefix else [entry_name]
        new_lines.append("resourcePacks:" + json.dumps(default_packs, ensure_ascii=False, separators=(",", ":")))
    if not found_incompat:
        new_lines.append("incompatibleResourcePacks:" + json.dumps([entry_name], ensure_ascii=False, separators=(",", ":")))
    if not found_lang and ("korean" in pack_filename.lower() or "한글" in pack_filename):
        new_lines.append("lang:ko_kr")

    try:
        with open(options_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(new_lines) + "\n")
        return True, pack_filename
    except Exception as e:
        return False, f"options.txt 저장 실패: {e}"


def restore_modpack_backups(modpack_dir):
    """
    모드팩의 한글화 내역을 원본으로 안전하게 롤백합니다:
    1) 퀘스트/설정/언어 파일 백업(.bak)을 원본으로 복원
    2) 스마트 하이브리드 패치로 수정된 모드 JAR(.jar.bak)을 원본으로 복원
    3) 생성된 한글 리소스팩(.zip) 삭제 및 options.txt 등록 해제
    반환값: (restored_quests_count, restored_jars_count, removed_packs_count)
    """
    import glob
    import shutil
    from constants import SCAN_IGNORE_DIRS

    restored_quests = 0
    restored_jars = 0
    removed_packs = 0

    if not modpack_dir or not os.path.isdir(modpack_dir):
        return 0, 0, 0

    # 1. 모드 JAR 원본 백업(.jar.bak) 복원
    mods_dir = os.path.join(modpack_dir, "mods")
    if os.path.isdir(mods_dir):
        for fname in os.listdir(mods_dir):
            if fname.lower().endswith(".jar.bak"):
                bak_path = os.path.join(mods_dir, fname)
                orig_path = bak_path[:-4]
                try:
                    shutil.copy2(bak_path, orig_path)
                    os.remove(bak_path)
                    restored_jars += 1
                except Exception:
                    pass

    # 2. 퀘스트 및 설정/언어 파일 원본 백업(.bak) 복원
    quest_bak_exts = (".snbt.bak", ".json.bak", ".hqm.bak", ".lang.bak", ".cfg.bak", ".txt.bak")
    for root, dirs, files_list in os.walk(modpack_dir):
        dirs[:] = [d for d in dirs if d.lower() not in SCAN_IGNORE_DIRS]
        for fname in files_list:
            if fname.lower().endswith(quest_bak_exts):
                bak_path = os.path.join(root, fname)
                orig_path = bak_path[:-4]
                try:
                    shutil.copy2(bak_path, orig_path)
                    os.remove(bak_path)
                    restored_quests += 1
                except Exception:
                    pass

    # Project Intelligence (Draconic Evolution) 직접 생성된 ko_KR XML 정리
    pi_dir = os.path.join(modpack_dir, "config", "brandon3055", "ProjectIntelligence", "ModDocs")
    if os.path.isdir(pi_dir):
        for root, _, files_list in os.walk(pi_dir):
            for fname in files_list:
                if "ko_kr" in fname.lower() and fname.lower().endswith(".xml"):
                    try:
                        os.remove(os.path.join(root, fname))
                        restored_quests += 1
                    except Exception:
                        pass

    # 3. 생성된 한글 리소스팩 삭제 및 options.txt 등록 해제
    mp_name = os.path.basename(modpack_dir.rstrip(os.sep))
    rp_dir = os.path.join(modpack_dir, "resourcepacks")
    pack_candidates = (
        glob.glob(os.path.join(modpack_dir, f"{mp_name}[*].zip"))
        + glob.glob(os.path.join(rp_dir, f"{mp_name}[*].zip"))
        + glob.glob(os.path.join(rp_dir, "*[Korean].zip"))
        + glob.glob(os.path.join(rp_dir, "*Korean*.zip"))
        + glob.glob(os.path.join(rp_dir, "*한글*.zip"))
        + [
            os.path.join(modpack_dir, "QuestTranslatorPro_Pack.zip"),
            os.path.join(rp_dir, "QuestTranslatorPro_Pack.zip"),
        ]
    )
    removed_pack_names = set()
    for p_path in set(pack_candidates):
        if os.path.isfile(p_path):
            try:
                removed_pack_names.add(os.path.basename(p_path))
                os.remove(p_path)
                removed_packs += 1
            except Exception:
                pass

    # options.txt에서 제거된 리소스팩 등록 해제
    options_path = os.path.join(modpack_dir, "options.txt")
    if os.path.isfile(options_path) and removed_pack_names:
        try:
            with open(options_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
            new_lines = []
            exclude_keys = set()
            for pn in removed_pack_names:
                exclude_keys.add(pn)
                exclude_keys.add(f"file/{pn}")
            for line in lines:
                if line.startswith("resourcePacks:") or line.startswith("incompatibleResourcePacks:"):
                    prefix, raw_val = line.split(":", 1)
                    try:
                        arr = json.loads(raw_val.strip())
                        if isinstance(arr, list):
                            arr = [x for x in arr if x not in exclude_keys]
                            line = f"{prefix}:" + json.dumps(arr, ensure_ascii=False, separators=(",", ":"))
                    except Exception:
                        pass
                new_lines.append(line)
            with open(options_path, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(new_lines) + "\n")
        except Exception:
            pass

    return restored_quests, restored_jars, removed_packs
