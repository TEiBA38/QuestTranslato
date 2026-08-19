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

def create_resource_pack(output_zip_path, translated_books_map, pack_description="Translated Guidebooks"):
    """
    번역된 JSON 데이터(translated_books_map)를 바탕으로 마인크래프트 리소스팩/데이터팩(.zip)을 생성합니다.
    translated_books_map 형식: { "jar_filename": { "zip_path": translated_json_data } }
    """
    with zipfile.ZipFile(output_zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # 1. pack.mcmeta 생성 (리소스팩 식별자)
        # pack_format 3 (1.12.2), 최신 버전에서도 호환성을 위해 보통 3~9 사용 (데이터팩은 별도지만, 둘 다 포함되게 작성 가능)
        pack_mcmeta = {
            "pack": {
                "pack_format": 3,
                "description": pack_description
            }
        }
        zf.writestr('pack.mcmeta', json.dumps(pack_mcmeta, ensure_ascii=False, indent=2))

        # 2. 번역된 파일들을 원래 경로에서 언어 코드만 ko_kr로 변경하여 zip에 쓰기
        for jar_name, files in translated_books_map.items():
            for zip_path, json_data in files.items():
                # 언어 폴더명을 ko_kr로 변경 (예: .../en_us/entries/... -> .../ko_kr/entries/...)
                # 대소문자 모두 처리
                new_zip_path = zip_path.replace('/en_us/', '/ko_kr/').replace('/en_US/', '/ko_kr/')
                
                # 추가적으로 최상단 폴더가 다른 언어일 가능성도 대비 (거의 없지만 방어적 코드)
                if '/en_us/' not in zip_path.lower():
                    # 만약 언어 코드가 명시되지 않았다면 그냥 원본 덮어쓰기
                    pass
                    
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

def create_lang_resource_pack(translated_langs, output_zip_path):
    """
    번역된 lang 데이터를 ko_kr.lang 파일명으로 바꾸어 리소스팩으로 묶습니다.
    translated_langs: { "jar_name": { "assets/modid/lang/en_us.lang": "번역된_lang_내용" } }
    """
    with zipfile.ZipFile(output_zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # pack.mcmeta 생성 (1.12.2 = format 3)
        pack_mcmeta = {
            "pack": {
                "pack_format": 3,
                "description": "QuestTranslatorPro Lang Translations"
            }
        }
        zf.writestr("pack.mcmeta", json.dumps(pack_mcmeta, indent=4, ensure_ascii=False))

        # 번역된 파일 추가
        for jar_name, lang_dict in translated_langs.items():
            for original_zip_path, translated_content in lang_dict.items():
                # en_us.lang -> ko_kr.lang 로 변경
                ko_kr_path = original_zip_path.replace('en_us.lang', 'ko_kr.lang').replace('en_US.lang', 'ko_kr.lang')
                zf.writestr(ko_kr_path, translated_content)
