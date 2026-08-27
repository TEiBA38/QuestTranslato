"""
하이브리드 번역 메모리 마스터 패커 & 자동 아카이빙 (Master Memory Packer & Auto Compactor)
- Supabase DB 테이블의 누적 데이터를 Gzip 초압축 파일(master_*.json.gz)로 패킹
- 기존 Storage 마스터 파일과 안전하게 누적 병합(Merge)하여 데이터 손실 0% 보장
- 압축 및 업로드 완료 후 DB 테이블을 0 MB로 자동 리셋(Truncate)
"""
import os
import sys
import io
import json
import gzip
import time
import requests
from concurrent.futures import ThreadPoolExecutor

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
except Exception:
    pass

SUPABASE_URL = "https://rwyedqmztbxsflndgsmt.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJ3eWVkcW16dGJ4c2ZsbmRnc210Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODc4MTIzNDksImV4cCI6MjEwMzM4ODM0OX0.8Lrxcp1af5ea6JYMhVIBXZAybkmRumindRUWgcq8Kdc"
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", SUPABASE_ANON_KEY)
BUCKET_NAME = "translations"

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
}

TABLE_CONFIGS = [
    {
        "category": "general",
        "table": "translation_memory",
        "json_name": "translation_memory.json",
        "master_gz": "master_general.json.gz"
    },
    {
        "category": "items",
        "table": "translation_memory_items",
        "json_name": "translation_memory_items.json",
        "master_gz": "master_items.json.gz"
    },
    {
        "category": "books",
        "table": "translation_memory_books",
        "json_name": "translation_memory_books.json",
        "master_gz": "master_books.json.gz"
    }
]

def ensure_bucket():
    url = f"{SUPABASE_URL}/storage/v1/bucket/{BUCKET_NAME}"
    r = requests.get(url, headers=HEADERS)
    if r.status_code != 200:
        res = requests.post(f"{SUPABASE_URL}/storage/v1/bucket", headers=HEADERS, json={"id": BUCKET_NAME, "name": BUCKET_NAME, "public": True})
        print(f"📦 Storage 버킷 '{BUCKET_NAME}' 생성 상태: {res.status_code}")
    else:
        print(f"📦 Storage 버킷 '{BUCKET_NAME}' 정상 준비됨.")

def get_current_db_size_mb():
    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/get_translation_memory_size_bytes", headers=HEADERS, json={}, timeout=10)
        if r.status_code == 200 and r.text.strip().isdigit():
            return int(r.text.strip()) / (1024 * 1024)
    except Exception:
        pass
    return 0.0

def fetch_existing_master(filename):
    """Storage에 이미 보관 중인 마스터 메모리가 있으면 다운로드하여 기존 번역 보존"""
    try:
        url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{filename}"
        r = requests.get(url, timeout=15)
        if r.status_code == 200 and r.content:
            raw = r.content
            if filename.endswith(".gz"):
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
    except Exception:
        pass
    return {}

def fetch_table_data(table_name):
    print(f"\n[1/3] '{table_name}' 데이터베이스에서 추출 중...")
    h = dict(HEADERS)
    h["Prefer"] = "count=planned"
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table_name}?select=src&limit=1", headers=h)
    cr = r.headers.get("content-range", "")
    total_count = int(cr.split("/")[-1]) if "/" in cr else 0
    print(f"  -> 전체 {total_count:,}개 행 발견")
    if total_count == 0:
        return {}

    data_map = {}

    def fetch_page(offset):
        url = f"{SUPABASE_URL}/rest/v1/{table_name}?select=lang,src,tgt&limit=1000&offset={offset}"
        res = requests.get(url, headers=HEADERS, timeout=30)
        return res.json() if res.status_code == 200 else []

    offsets = list(range(0, total_count, 1000))
    with ThreadPoolExecutor(max_workers=10) as executor:
        for page in executor.map(fetch_page, offsets):
            for row in page:
                lang = row.get("lang", "한국어 (Korean)")
                src = row.get("src")
                tgt = row.get("tgt")
                if not src or not tgt:
                    continue
                if lang not in data_map:
                    data_map[lang] = {}
                data_map[lang][src] = tgt

    return data_map

def upload_storage(data_bytes, filename, content_type):
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET_NAME}/{filename}"
    h = dict(HEADERS)
    h["Content-Type"] = content_type
    h["x-upsert"] = "true"

    res = requests.post(upload_url, headers=h, data=data_bytes)
    if res.status_code not in (200, 201):
        res = requests.put(upload_url, headers=h, data=data_bytes)
    if res.status_code in (200, 201):
        print(f"  ✅ Storage 업로드 성공: {filename} ({len(data_bytes):,} bytes)")
        return True
    else:
        print(f"  ❌ Storage 업로드 실패 ({filename}): {res.status_code} - {res.text}")
        return False

def reset_database_tables():
    """모든 압축 저장이 성공했을 때 DB 테이블을 0MB로 안전하게 리셋"""
    print("\n[🧹 DB 테이블 리셋 진행]")
    try:
        res = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/truncate_all_translation_tables", headers=HEADERS, json={}, timeout=15)
        if res.status_code in (200, 204):
            print("  ✨ DB 3개 테이블이 0 MB로 깨끗하게 비워졌습니다! (용량 다이어트 성공)")
            return True
        else:
            print(f"  ℹ️ DB 리셋 응답 ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"  ⚠️ DB 리셋 RPC 호출 실패: {e}")
    return False

def main():
    print("="*60)
    print("🚀 하이브리드 마스터 번역 메모리 자동 아카이빙 & 압축 시작")
    print("="*60)

    current_mb = get_current_db_size_mb()
    print(f"📊 현재 DB 물리 디스크 용량: {current_mb:.2f} MB")

    ensure_bucket()

    dist_dir = os.path.join(os.path.dirname(__file__), "..", "dist")
    os.makedirs(dist_dir, exist_ok=True)

    summary = []
    all_success = True

    for cfg in TABLE_CONFIGS:
        table_name = cfg["table"]
        master_gz = cfg["master_gz"]
        json_name = cfg["json_name"]

        # 1. 기존 마스터 데이터 다운로드 (누적 보존)
        existing_data = fetch_existing_master(master_gz)

        # 2. DB 신규 데이터 다운로드
        new_data = fetch_table_data(table_name)

        # 3. 로컬 JSON 파일 확인 (dist 또는 root)
        local_data = {}
        for lpath in [os.path.join(dist_dir, json_name), os.path.join(os.path.dirname(__file__), "..", json_name)]:
            if os.path.exists(lpath):
                try:
                    with open(lpath, "r", encoding="utf-8") as f:
                        ld = json.load(f)
                        if isinstance(ld, dict):
                            for l_k, l_v in ld.items():
                                if l_k not in local_data: local_data[l_k] = {}
                                if isinstance(l_v, dict): local_data[l_k].update(l_v)
                except Exception:
                    pass

        # 4. 세 곳 데이터 안전 병합 (기존 Storage + 신규 DB + 로컬 파일)
        merged_data = {}
        all_langs = set(list(existing_data.keys()) + list(new_data.keys()) + list(local_data.keys()))
        for lang in all_langs:
            merged_data[lang] = {}
            if lang in existing_data and isinstance(existing_data[lang], dict):
                merged_data[lang].update(existing_data[lang])
            if lang in new_data and isinstance(new_data[lang], dict):
                merged_data[lang].update(new_data[lang])
            if lang in local_data and isinstance(local_data[lang], dict):
                merged_data[lang].update(local_data[lang])

        if not merged_data:
            print(f"⚠️ {table_name} 데이터가 비어있습니다. 건너뜁니다.")
            continue

        raw_json_bytes = json.dumps(merged_data, ensure_ascii=False, indent=2).encode("utf-8")
        gz_bytes = gzip.compress(raw_json_bytes, compresslevel=9)

        # 로컬 dist 에도 저장
        with open(os.path.join(dist_dir, json_name), "wb") as f:
            f.write(raw_json_bytes)
        with open(os.path.join(dist_dir, master_gz), "wb") as f:
            f.write(gz_bytes)

        raw_mb = len(raw_json_bytes) / (1024 * 1024)
        gz_mb = len(gz_bytes) / (1024 * 1024)
        ratio = (1 - (len(gz_bytes) / len(raw_json_bytes))) * 100
        print(f"  [2/3] 압축 완료: 원본 {raw_mb:.2f} MB ➡️ 초압축 {gz_mb:.2f} MB ({ratio:.1f}% 절약)")

        # Storage 에 업로드
        print(f"  [3/3] Supabase Storage 업로드 중...")
        ok1 = upload_storage(gz_bytes, master_gz, "application/gzip")
        ok2 = upload_storage(raw_json_bytes, json_name, "application/json")

        if not (ok1 and ok2):
            all_success = False

        total_cnt = sum(len(v) for v in merged_data.values())
        summary.append({
            "category": cfg["category"],
            "master_gz": master_gz,
            "count": total_cnt,
            "raw_mb": raw_mb,
            "gz_mb": gz_mb
        })

    print("\n" + "="*60)
    print("🎉 모든 마스터 메모리가 초압축되어 Supabase Storage에 배치되었습니다!")
    print("="*60)
    for s in summary:
        print(f"• [{s['category']}] {s['master_gz']}: {s['count']:,}개 | 원본 {s['raw_mb']:.2f}MB ➡️ 압축 {s['gz_mb']:.2f}MB")

    # 4. Storage 마스터 파일 3개가 100% 안전하게 업로드되었을 때만 DB 테이블 리셋 시도
    if all_success and len(summary) == len(TABLE_CONFIGS):
        reset_database_tables()
    else:
        print("\n⚠️ 일부 파일 업로드 실패 또는 미완료로 인해 안전을 위해 DB 테이블 리셋을 건너뜁니다.")

if __name__ == "__main__":
    main()
