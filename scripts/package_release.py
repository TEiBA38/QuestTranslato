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

def package_release(build_exe=False):
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

    # 1. 릴리즈 대상 폴더 생성
    clean_ver = APP_VERSION.strip()
    folder_name = f"QuestTranslator_{clean_ver}"
    release_root = os.path.join(REPO_ROOT, "release")
    target_dir = os.path.join(release_root, folder_name)
    target_dist = os.path.join(target_dir, "dist")

    if os.path.exists(target_dir):
        shutil.rmtree(target_dir, ignore_errors=True)
    os.makedirs(target_dist, exist_ok=True)

    # 2. EXE 복사
    print(f"📄 EXE 복사: {exe_source} ➡️ {target_dir}")
    shutil.copy2(exe_source, os.path.join(target_dir, "QuestTranslatorPro.exe"))

    # 3. 필수 마스터 캐시 및 데이터 파일 복사 (dist/ 하위)
    essential_files = [
        "translation_memory.json",
        "translation_memory_items.json",
        "translation_memory_books.json",
        "translation_memory_words.json",
        "translation_words.json",
        "master_general.json.gz",
        "master_items.json.gz",
        "master_books.json.gz",
        "cloud_sync_state.json",
    ]

    # 모드팩별 특화 캐시도 포함
    for f in os.listdir(dist_source):
        if f.startswith("translation_memory_modpack_") and f.endswith(".json") and not f.endswith(".bak"):
            essential_files.append(f)

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

    print(f"💾 필수 캐시 파일 {copied_files}개 복사 완료 ({total_bytes / 1024 / 1024:.2f} MB)")

    # 4. 캐시 포함 배포본 ZIP 생성
    zip_with_cache = os.path.join(release_root, f"{folder_name}_with_cache.zip")
    zip_default = os.path.join(release_root, f"{folder_name}.zip")
    print(f"🗜️ [캐시 포함] 배포용 ZIP 압축 중: {zip_with_cache}")

    with zipfile.ZipFile(zip_with_cache, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(target_dir):
            for f in files:
                full_path = os.path.join(root, f)
                rel_path = os.path.relpath(full_path, release_root)
                zf.write(full_path, rel_path)

    # 기본 이름으로도 복사 유지 (기존 다운로더 호환)
    shutil.copy2(zip_with_cache, zip_default)
    sz_with_cache = os.path.getsize(zip_with_cache) / 1024 / 1024

    # 5. 캐시 미포함 (Lite) 배포본 생성
    lite_folder_name = f"QuestTranslator_{clean_ver}_without_cache"
    lite_target_dir = os.path.join(release_root, lite_folder_name)
    if os.path.exists(lite_target_dir):
        shutil.rmtree(lite_target_dir, ignore_errors=True)
    os.makedirs(lite_target_dir, exist_ok=True)

    # Lite: 실행 파일만 포함 (클라우드 동기화로 필요시 캐시 자동 다운로드)
    shutil.copy2(exe_source, os.path.join(lite_target_dir, "QuestTranslatorPro.exe"))
    os.makedirs(os.path.join(lite_target_dir, "dist"), exist_ok=True)

    zip_without_cache = os.path.join(release_root, f"{folder_name}_without_cache.zip")
    print(f"🗜️ [캐시 미포함/Lite] 배포용 ZIP 압축 중: {zip_without_cache}")

    with zipfile.ZipFile(zip_without_cache, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(lite_target_dir):
            for f in files:
                full_path = os.path.join(root, f)
                rel_path = os.path.relpath(full_path, release_root)
                zf.write(full_path, rel_path)

    sz_without_cache = os.path.getsize(zip_without_cache) / 1024 / 1024

    print(f"\n🎉 [배포 패키징 완료!]")
    print(f"📦 1. 캐시 포함 배포본: {zip_with_cache} ({sz_with_cache:.2f} MB)")
    print(f"📦 2. 캐시 미포함 배포본: {zip_without_cache} ({sz_without_cache:.2f} MB)")
    print(f"📁 배포 폴더: {target_dir}")
    print(f"\n[구조 (캐시 포함)]")
    print(f"{folder_name}/")
    print(f" ├── QuestTranslatorPro.exe")
    print(f" └── dist/ (마스터 캐시 {copied_files}개)")
    print(f"\n[구조 (캐시 미포함)]")
    print(f"{lite_folder_name}/")
    print(f" ├── QuestTranslatorPro.exe")
    print(f" └── dist/ (빈 폴더, 클라우드 자동 동기화)")

    return True

if __name__ == "__main__":
    build_flag = "--build" in sys.argv
    package_release(build_exe=build_flag)
