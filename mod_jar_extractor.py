import os
import zipfile
import json
import logging
from io import BytesIO

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
                book_files = [
                    name for name in zf.namelist() 
                    if name.lower().endswith('.json') and 
                       ('patchouli_books/' in name.lower() or 'guideapi/' in name.lower())
                ]
                
                if book_files:
                    jar_books = {}
                    for bf in book_files:
                        try:
                            data = zf.read(bf)
                            # 간단한 JSON 검증
                            json_data = json.loads(data.decode('utf-8', errors='ignore'))
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
        zf.writestr('pack.mcmeta', json.dumps(pack_mcmeta, ensure_ascii=False, indent=2))

        # 2. 번역된 파일들을 원래 경로에서 언어 코드만 ko_kr로 변경하여 zip에 쓰기
        written_paths = set()
        for jar_name, files in translated_books_map.items():
            for zip_path, json_data in files.items():
                # 언어 폴더명을 ko_kr로 변경 (예: .../en_us/entries/... -> .../ko_kr/entries/...)
                # 대소문자 모두 처리
                new_zip_path = zip_path.replace('/en_us/', '/ko_kr/').replace('/en_US/', '/ko_kr/')
                
                # 추가적으로 최상단 폴더가 다른 언어일 가능성도 대비 (거의 없지만 방어적 코드)
                if '/en_us/' not in zip_path.lower():
                    # 만약 언어 코드가 명시되지 않았다면 그냥 원본 덮어쓰기
                    pass
                    
                if new_zip_path in written_paths:
                    continue
                written_paths.add(new_zip_path)
                
                json_str = json.dumps(json_data, ensure_ascii=False, indent=2)
                zf.writestr(new_zip_path, json_str)

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
                # assets/.../lang/en_us.lang 찾기
                lang_files = [
                    name for name in zf.namelist() 
                    if name.lower().endswith('en_us.lang') and name.startswith('assets/')
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
        zf.writestr("pack.mcmeta", json.dumps(pack_mcmeta, indent=4, ensure_ascii=False))

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
