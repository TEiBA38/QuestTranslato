import os
import sys
import shutil
import zipfile
import re

# Repo root
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from constants import APP_VERSION

def package_release(build_exe=False, fix_folder_name=True):
    print(f"📦 [배포 패키징 시작] 대상 버전: {APP_VERSION}")
    
    dist_source = os.path.join(REPO_ROOT, "dist")
    exe_source = os.path.join(dist_source, "QuestTranslatorPro.exe")
    
    if build_exe or not os.path.exists(exe_source):
        print("🔨 PyInstaller 빌드 실행 중...")
        spec_path = os.path.join(REPO_ROOT, "QuestTranslatorPro.spec")
        import subprocess
        res = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", spec_path], cwd=REPO_ROOT)
        if res.returncode != 0:
            print("❌ PyInstaller 빌드 실패!")
            return False

    if not os.path.exists(exe_source):
        print(f"❌ 실행 파일({exe_source})을 찾을 수 없습니다.")
        return False

    # 1. 릴리즈 대상 폴더 생성 (압축 해제 시 폴더명은 버전 없이 QuestTranslatorPro 로 고정)
    clean_ver = APP_VERSION.strip()
    folder_name = "QuestTranslatorPro" if fix_folder_name else f"QuestTranslatorPro_{clean_ver}"
    release_root = os.path.join(REPO_ROOT, "release")
    target_dir = os.path.join(release_root, folder_name)
    target_dist = os.path.join(target_dir, "dist")

    if os.path.exists(target_dir):
        shutil.rmtree(target_dir, ignore_errors=True)
    os.makedirs(target_dist, exist_ok=True)

    # 2. EXE 및 가이드 문서 복사
    print(f"📄 EXE 복사: {exe_source} ➡️ {target_dir}")
    shutil.copy2(exe_source, os.path.join(target_dir, "QuestTranslatorPro.exe"))

    # release 루트에도 단독 실행 파일 복사 (구버전 직접 exe 탐색 및 수동 다운로더 지원)
    shutil.copy2(exe_source, os.path.join(release_root, "QuestTranslatorPro.exe"))

    try:
        from scripts.generate_readme_txt import generate_readme_txt
        generate_readme_txt(REPO_ROOT)
    except Exception as e:
        print(f"⚠️ README.txt 자동 생성 건너뜀: {e}")

    readme_source = os.path.join(REPO_ROOT, "README.md")
    readme_txt_source = os.path.join(REPO_ROOT, "README.txt")
    if os.path.isfile(readme_source):
        shutil.copy2(readme_source, os.path.join(target_dir, "README.md"))
        print(f"📄 가이드 복사: README.md ➡️ {target_dir}")
    if os.path.isfile(readme_txt_source):
        shutil.copy2(readme_txt_source, os.path.join(target_dir, "README.txt"))
        print(f"📄 텍스트 가이드 복사: README.txt ➡️ {target_dir}")

    # 3. 필수 로컬 캐시 파일 복사 (dist/ 하위)
    essential_files = [
        "translation_memory.json",
        "translation_memory_items.json",
        "translation_memory_books.json",
        "translation_memory_words.json",
        "translation_words.json",
        "cloud_sync_state.json",
    ]

    # 모드팩별 특화 캐시 폴더 생성 및 복사
    target_modpack_dir = os.path.join(target_dist, "translation_modpack")
    os.makedirs(target_modpack_dir, exist_ok=True)

    src_modpack_dir = os.path.join(dist_source, "translation_modpack")
    modpack_copied = 0
    if os.path.isdir(src_modpack_dir):
        for f in os.listdir(src_modpack_dir):
            if f.endswith(".json") and not f.endswith(".bak"):
                shutil.copy2(os.path.join(src_modpack_dir, f), os.path.join(target_modpack_dir, f))
                modpack_copied += 1

    copied_files = 0
    total_bytes = 0
    for fname in sorted(set(essential_files)):
        src = os.path.join(dist_source, fname)
        if os.path.isfile(src):
            dst = os.path.join(target_dist, fname)
            shutil.copy2(src, dst)
            sz = os.path.getsize(dst)
            total_bytes += sz
            copied_files += 1

    print(f"💾 필수 캐시 파일 {copied_files}개 + 모드팩 캐시 {modpack_copied}개 복사 완료 ({total_bytes / 1024 / 1024:.2f} MB)")

    # 4. 캐시 포함 배포본 ZIP 생성 (구버전 호환을 위해 반드시 'windows' 키워드 포함)
    zip_with_cache_win = os.path.join(release_root, f"QuestTranslatorPro_{clean_ver}_with_cache_windows.zip")
    print(f"🗜️ [캐시 포함] 배포용 ZIP 압축 중: {zip_with_cache_win}")

    with zipfile.ZipFile(zip_with_cache_win, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(target_dir):
            for f in files:
                full_path = os.path.join(root, f)
                rel_path = os.path.relpath(full_path, release_root)
                zf.write(full_path, rel_path)

    # 호환용 복사본들 (구버전 updater의 'windows' 검색 및 명칭 변형 전수 지원)
    shutil.copy2(zip_with_cache_win, os.path.join(release_root, f"QuestTranslatorPro_{clean_ver}_with_cache.zip"))
    shutil.copy2(zip_with_cache_win, os.path.join(release_root, f"QuestTranslator_{clean_ver}_with_cache_windows.zip"))
    shutil.copy2(zip_with_cache_win, os.path.join(release_root, f"QuestTranslator_{clean_ver}_with_cache.zip"))
    shutil.copy2(zip_with_cache_win, os.path.join(release_root, f"QuestTranslatorPro_{clean_ver}.zip"))
    sz_with_cache = os.path.getsize(zip_with_cache_win) / 1024 / 1024

    # 5. 캐시 미포함 (Lite) 배포본 생성 (구버전 호환을 위해 반드시 'windows' 키워드 포함)
    lite_folder_name = "QuestTranslatorPro" if fix_folder_name else f"QuestTranslatorPro_{clean_ver}_without_cache"
    lite_target_dir = os.path.join(release_root, f"{folder_name}_lite_temp")
    if os.path.exists(lite_target_dir):
        shutil.rmtree(lite_target_dir, ignore_errors=True)
    os.makedirs(lite_target_dir, exist_ok=True)

    # Lite: 실행 파일 및 가이드 문서 포함 (폴더 내부는 QuestTranslatorPro로 패키징)
    shutil.copy2(exe_source, os.path.join(lite_target_dir, "QuestTranslatorPro.exe"))
    if os.path.isfile(readme_source):
        shutil.copy2(readme_source, os.path.join(lite_target_dir, "README.md"))
    if os.path.isfile(readme_txt_source):
        shutil.copy2(readme_txt_source, os.path.join(lite_target_dir, "README.txt"))
    os.makedirs(os.path.join(lite_target_dir, "dist"), exist_ok=True)
    os.makedirs(os.path.join(lite_target_dir, "dist", "translation_modpack"), exist_ok=True)

    zip_without_cache_win = os.path.join(release_root, f"QuestTranslatorPro_{clean_ver}_without_cache_windows.zip")
    print(f"🗜️ [캐시 미포함/Lite] 배포용 ZIP 압축 중: {zip_without_cache_win}")

    with zipfile.ZipFile(zip_without_cache_win, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(lite_target_dir):
            for f in files:
                full_path = os.path.join(root, f)
                rel_inside = os.path.relpath(full_path, lite_target_dir)
                archive_name = os.path.join("QuestTranslatorPro", rel_inside)
                zf.write(full_path, archive_name)

    # 임시 폴더 정리
    shutil.rmtree(lite_target_dir, ignore_errors=True)

    # 호환용 복사본들
    shutil.copy2(zip_without_cache_win, os.path.join(release_root, f"QuestTranslatorPro_{clean_ver}_without_cache.zip"))
    shutil.copy2(zip_without_cache_win, os.path.join(release_root, f"QuestTranslator_{clean_ver}_without_cache_windows.zip"))
    shutil.copy2(zip_without_cache_win, os.path.join(release_root, f"QuestTranslator_{clean_ver}_without_cache.zip"))
    sz_without_cache = os.path.getsize(zip_without_cache_win) / 1024 / 1024

    print(f"\n🎉 [배포 패키징 완료!]")
    print(f"📦 1. 캐시 포함 배포본 (Windows): {zip_with_cache_win} ({sz_with_cache:.2f} MB)")
    print(f"📦 2. 캐시 미포함 배포본 (Windows): {zip_without_cache_win} ({sz_without_cache:.2f} MB)")
    print(f"📦 3. 단독 실행 파일: {os.path.join(release_root, 'QuestTranslatorPro.exe')}")
    print(f"\n[압축 내부 폴더 구조]")
    print(f"QuestTranslatorPro/")
    print(f" ├── QuestTranslatorPro.exe")
    print(f" ├── README.md")
    print(f" ├── README.txt")
    print(f" └── dist/ (필수 캐시 및 모드팩 폴더)")

    return True

if __name__ == "__main__":
    build_flag = "--build" in sys.argv
    package_release(build_exe=build_flag)
