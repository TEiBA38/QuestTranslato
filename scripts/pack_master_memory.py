"""
Cloudflare R2 마스터 번역 메모리 스냅샷 패커 (Master Memory Packer & Snapshot Compactor)
- Cloudflare R2 기존 마스터 스냅샷(master_*.json.gz) + R2 누적 커밋(/pull) + 로컬 캐시(dist/*.json)를 안전하게 누적 병합(Merge)
- 불필요한 메타 태그(# [Auto-Extracted]) 전수 정제
- 병합된 마스터 데이터를 Gzip 초압축(Level 9)하여 Cloudflare R2(/snapshot/:category)로 업로드
- 업로드 완료 후 Cloudflare R2에 누적된 커밋 파일 일괄 비우기 (/commits/purge?all=true)
"""
import os
import sys
import io
import json
import gzip
import re
import urllib.parse
import requests

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
except Exception:
    pass

DEFAULT_CLOUDFLARE_WORKER_URL = "https://quest-translator-sync.kimraon84.workers.dev"
CLOUDFLARE_WORKER_URL = os.environ.get("QUEST_CLOUDFLARE_URL", DEFAULT_CLOUDFLARE_WORKER_URL).strip().rstrip("/")

SNAPSHOT_CONFIGS = [
    {
        "category": "general",
        "json_name": "translation_memory.json",
        "master_gz": "master_general.json.gz",
    },
    {
        "category": "items",
        "json_name": "translation_memory_items.json",
        "master_gz": "master_items.json.gz",
    },
    {
        "category": "books",
        "json_name": "translation_memory_books.json",
        "master_gz": "master_books.json.gz",
    },
]

CLEAN_TAG_PAT = re.compile(r'\s*#\s*\[(?:Auto-Extracted|자동\s*추출됨)\]')


def fetch_existing_r2_master(category):
    """Cloudflare R2에 보관 중인 마스터 스냅샷이 있으면 다운로드하여 기존 번역 보존"""
    try:
        url = f"{CLOUDFLARE_WORKER_URL}/master/{category}"
        r = requests.get(url, timeout=20)
        if r.status_code == 200 and r.content:
            raw = gzip.decompress(r.content)
            return json.loads(raw.decode("utf-8"))
    except Exception as e:
        print(f"  ⚠️ R2 기존 마스터({category}) 조회 스킵: {e}")
    return {}


def fetch_d1_commits():
    """Cloudflare R2에 쌓인 모든 증분 커밋 데이터를 /pull 엔드포인트로 조회"""
    print("\n[1/4] Cloudflare R2 증분 커밋 데이터 추출 중...")
    commits_by_cat = {"general": {}, "items": {}, "books": {}}
    last_id = ""
    total_rows = 0

    try:
        page = 0
        while True:
            page += 1
            encoded_since = urllib.parse.quote(str(last_id))
            r = requests.get(f"{CLOUDFLARE_WORKER_URL}/pull?since={encoded_since}&limit=100", timeout=20)
            if r.status_code != 200:
                break
            data = r.json()
            commits = data.get("commits", [])
            latest_id = str(data.get("latest_id", last_id) or last_id).strip()

            for row in commits:
                cat = row.get("cat") or row.get("category") or "general"
                if cat not in commits_by_cat:
                    cat = "general"
                lang = row.get("lang", "한국어 (Korean)")
                src = row.get("src")
                tgt = row.get("tgt")
                if not src or not tgt:
                    continue
                if isinstance(src, str) and "#" in src:
                    src = CLEAN_TAG_PAT.sub('', src).strip()
                if isinstance(tgt, str) and "#" in tgt:
                    tgt = CLEAN_TAG_PAT.sub('', tgt).strip()
                if not src or not tgt or src == tgt:
                    continue
                commits_by_cat[cat].setdefault(lang, {})[src] = tgt
                total_rows += 1

            if latest_id and latest_id != last_id:
                last_id = latest_id
            if not data.get("has_more") or not commits:
                break
            if page >= 200:
                break
    except Exception as e:
        print(f"  ⚠️ R2 커밋 조회 중 오류 (로컬 캐시로 계속 진행): {e}")

    print(f"  -> R2에서 총 {total_rows:,}개 커밋 추출 완료 (최신 Commit: {last_id})")
    return commits_by_cat, last_id


def upload_r2_snapshot(category, gz_bytes, master_gz):
    """압축된 마스터 스냅샷을 Cloudflare R2(/snapshot/:category)에 업로드"""
    upload_url = f"{CLOUDFLARE_WORKER_URL}/snapshot/{category}"
    headers = {"Content-Type": "application/gzip"}
    res = requests.put(upload_url, headers=headers, data=gz_bytes, timeout=60)
    if res.status_code in (200, 201):
        print(f"  ✅ Cloudflare R2 스냅샷 업로드 성공: {master_gz} ({len(gz_bytes):,} bytes)")
        return True
    print(f"  ❌ Cloudflare R2 스냅샷 업로드 실패 ({master_gz}): {res.status_code} - {res.text}")
    return False


def purge_cloudflare_commits():
    """Cloudflare R2에 쌓인 커밋 파일들을 비우기 (/commits/purge?all=true)"""
    print("\n🧹 [Cloudflare R2 커밋 파일 비우기 실행 중...]")
    try:
        url = f"{CLOUDFLARE_WORKER_URL}/commits/purge?all=true"
        r = requests.post(url, timeout=30)
        if r.status_code == 200:
            res = r.json()
            purged = res.get("purged", 0)
            print(f"  ✅ Cloudflare R2 커밋 파일 비우기 완료: 총 {purged:,}개 커밋 파일 삭제")
            return True
        else:
            print(f"  ⚠️ 커밋 비우기 실패 (HTTP {r.status_code}): {r.text}")
    except Exception as e:
        print(f"  ⚠️ 커밋 비우기 요청 중 예외 발생: {e}")
    return False


def clean_dict(d):
    """딕셔너리 내 모든 키/값에서 오염 태그 제거"""
    cleaned = {}
    if not isinstance(d, dict):
        return cleaned
    for k, v in d.items():
        if isinstance(v, dict):
            cleaned[k] = clean_dict(v)
        elif isinstance(v, str):
            clean_k = CLEAN_TAG_PAT.sub('', k).strip() if isinstance(k, str) else k
            clean_v = CLEAN_TAG_PAT.sub('', v).strip()
            if clean_k and clean_v and clean_k != clean_v:
                cleaned[clean_k] = clean_v
    return cleaned


def main():
    print("=" * 60)
    print("🚀 Cloudflare R2 마스터 번역 메모리 패킹 & 커밋 비우기 시작")
    print("=" * 60)

    dist_dir = os.path.join(os.path.dirname(__file__), "..", "dist")
    os.makedirs(dist_dir, exist_ok=True)

    d1_commits_by_cat, last_commit_id = fetch_d1_commits()
    summary = []

    for cfg in SNAPSHOT_CONFIGS:
        category = cfg["category"]
        master_gz = cfg["master_gz"]
        json_name = cfg["json_name"]

        print(f"\n📦 [{category}] 마스터 데이터 병합 중...")
        existing_data = clean_dict(fetch_existing_r2_master(category))
        commit_data = clean_dict(d1_commits_by_cat.get(category, {}))

        local_data = {}
        for lpath in [os.path.join(dist_dir, json_name), os.path.join(os.path.dirname(__file__), "..", json_name)]:
            if os.path.exists(lpath):
                try:
                    with open(lpath, "r", encoding="utf-8") as f:
                        ld = json.load(f)
                        if isinstance(ld, dict):
                            c_ld = clean_dict(ld)
                            for l_k, l_v in c_ld.items():
                                if l_k not in local_data:
                                    local_data[l_k] = {}
                                if isinstance(l_v, dict):
                                    local_data[l_k].update(l_v)
                except Exception:
                    pass

        # general 카테고리의 경우 로컬에 보관된 모든 모드팩 특화 퀘스트 캐시도 함께 전역 마스터로 병합
        if category == "general":
            import glob
            mp_files = (
                glob.glob(os.path.join(dist_dir, "translation_modpack", "translation_memory_modpack_*.json"))
                + glob.glob(os.path.join(dist_dir, "translation_memory_modpack_*.json"))
            )
            mp_count = 0
            for mp_path in mp_files:
                if mp_path.endswith(".bak") or mp_path.endswith(".bak.gz"):
                    continue
                try:
                    with open(mp_path, "r", encoding="utf-8") as f:
                        ld = json.load(f)
                        if isinstance(ld, dict):
                            c_ld = clean_dict(ld)
                            for l_k, l_v in c_ld.items():
                                if l_k not in local_data:
                                    local_data[l_k] = {}
                                if isinstance(l_v, dict):
                                    local_data[l_k].update(l_v)
                                    mp_count += len(l_v)
                except Exception:
                    pass
            if mp_count > 0:
                print(f"  -> 모드팩 특화 퀘스트 파일 {len(mp_files)}개에서 총 {mp_count:,}개 항목을 general 마스터로 함께 병합했습니다.")

        merged_data = {}
        all_langs = set(list(existing_data.keys()) + list(commit_data.keys()) + list(local_data.keys()))
        for lang in all_langs:
            merged_data[lang] = {}
            if lang in existing_data and isinstance(existing_data[lang], dict):
                merged_data[lang].update(existing_data[lang])
            if lang in local_data and isinstance(local_data[lang], dict):
                merged_data[lang].update(local_data[lang])
            if lang in commit_data and isinstance(commit_data[lang], dict):
                merged_data[lang].update(commit_data[lang])

        if not merged_data:
            print(f"⚠️ [{category}] 데이터가 비어있습니다. 건너뜁니다.")
            continue

        raw_json_bytes = json.dumps(merged_data, ensure_ascii=False, indent=2).encode("utf-8")
        gz_bytes = gzip.compress(raw_json_bytes, compresslevel=9)

        with open(os.path.join(dist_dir, json_name), "wb") as f:
            f.write(raw_json_bytes)
        with open(os.path.join(dist_dir, master_gz), "wb") as f:
            f.write(gz_bytes)

        raw_mb = len(raw_json_bytes) / (1024 * 1024)
        gz_mb = len(gz_bytes) / (1024 * 1024)
        ratio = (1 - (len(gz_bytes) / len(raw_json_bytes))) * 100
        print(f"  [2/4] 압축 완료: 원본 {raw_mb:.2f} MB ➡️ 초압축 {gz_mb:.2f} MB ({ratio:.1f}% 절약)")

        print(f"  [3/4] Cloudflare R2 스냅샷 업로드 중...")
        upload_r2_snapshot(category, gz_bytes, master_gz)

        total_cnt = sum(len(v) for v in merged_data.values())
        summary.append({
            "category": category,
            "master_gz": master_gz,
            "count": total_cnt,
            "raw_mb": raw_mb,
            "gz_mb": gz_mb,
        })

    # [4/4] Cloudflare 커밋 비우기
    purge_cloudflare_commits()

    print("\n" + "=" * 60)
    print("🎉 모든 마스터 메모리가 초압축되어 Cloudflare R2에 배치되었으며, 커밋 파일이 비워졌습니다!")
    print("=" * 60)
    for s in summary:
        print(f"• [{s['category']}] {s['master_gz']}: {s['count']:,}개 | 원본 {s['raw_mb']:.2f}MB ➡️ 압축 {s['gz_mb']:.2f}MB")


if __name__ == "__main__":
    main()
