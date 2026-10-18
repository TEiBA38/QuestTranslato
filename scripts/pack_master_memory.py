"""
Cloudflare R2 + D1 마스터 번역 메모리 스냅샷 패커 (Master Memory Packer & Snapshot Compactor)
- Cloudflare R2 기존 마스터 스냅샷(master_*.json.gz) + D1 누적 커밋(/pull) + 로컬 캐시(dist/*.json)를 안전하게 누적 병합(Merge)
- 병합된 마스터 데이터를 Gzip 초압축(Level 9)하여 Cloudflare R2(/snapshot/:category)로 업로드
"""
import os
import sys
import io
import json
import gzip
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
    """Cloudflare D1에 쌓인 모든 증분 커밋 데이터를 /pull 엔드포인트로 조회"""
    print("\n[1/3] Cloudflare D1 증분 커밋 데이터 추출 중...")
    commits_by_cat = {"general": {}, "items": {}, "books": {}}
    last_id = 0
    total_rows = 0

    try:
        while True:
            r = requests.get(f"{CLOUDFLARE_WORKER_URL}/pull?since={last_id}&limit=5000", timeout=20)
            if r.status_code != 200:
                break
            data = r.json()
            commits = data.get("commits", [])
            latest_id = int(data.get("latest_id", last_id) or last_id)

            for row in commits:
                cat = row.get("category", "general")
                if cat not in commits_by_cat:
                    cat = "general"
                lang = row.get("lang", "한국어 (Korean)")
                src = row.get("src")
                tgt = row.get("tgt")
                if not src or not tgt:
                    continue
                commits_by_cat[cat].setdefault(lang, {})[src] = tgt
                total_rows += 1

            if latest_id > last_id:
                last_id = latest_id
            if not data.get("has_more"):
                break
    except Exception as e:
        print(f"  ⚠️ D1 커밋 조회 중 오류: {e}")

    print(f"  -> D1에서 총 {total_rows:,}개 커밋 추출 완료 (최신 Commit #{last_id})")
    return commits_by_cat


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


def main():
    print("=" * 60)
    print("🚀 Cloudflare R2 + D1 마스터 번역 메모리 스냅샷 패킹 시작")
    print("=" * 60)

    dist_dir = os.path.join(os.path.dirname(__file__), "..", "dist")
    os.makedirs(dist_dir, exist_ok=True)

    d1_commits_by_cat = fetch_d1_commits()
    summary = []

    for cfg in SNAPSHOT_CONFIGS:
        category = cfg["category"]
        master_gz = cfg["master_gz"]
        json_name = cfg["json_name"]

        print(f"\n📦 [{category}] 마스터 데이터 병합 중...")
        existing_data = fetch_existing_r2_master(category)
        commit_data = d1_commits_by_cat.get(category, {})

        local_data = {}
        for lpath in [os.path.join(dist_dir, json_name), os.path.join(os.path.dirname(__file__), "..", json_name)]:
            if os.path.exists(lpath):
                try:
                    with open(lpath, "r", encoding="utf-8") as f:
                        ld = json.load(f)
                        if isinstance(ld, dict):
                            for l_k, l_v in ld.items():
                                if l_k not in local_data:
                                    local_data[l_k] = {}
                                if isinstance(l_v, dict):
                                    local_data[l_k].update(l_v)
                except Exception:
                    pass

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
        print(f"  [2/3] 압축 완료: 원본 {raw_mb:.2f} MB ➡️ 초압축 {gz_mb:.2f} MB ({ratio:.1f}% 절약)")

        print(f"  [3/3] Cloudflare R2 스냅샷 업로드 중...")
        upload_r2_snapshot(category, gz_bytes, master_gz)

        total_cnt = sum(len(v) for v in merged_data.values())
        summary.append({
            "category": category,
            "master_gz": master_gz,
            "count": total_cnt,
            "raw_mb": raw_mb,
            "gz_mb": gz_mb,
        })

    print("\n" + "=" * 60)
    print("🎉 모든 마스터 메모리가 초압축되어 Cloudflare R2에 배치되었습니다!")
    print("=" * 60)
    for s in summary:
        print(f"• [{s['category']}] {s['master_gz']}: {s['count']:,}개 | 원본 {s['raw_mb']:.2f}MB ➡️ 압축 {s['gz_mb']:.2f}MB")


if __name__ == "__main__":
    main()
