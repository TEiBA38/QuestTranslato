"""
하이브리드 번역 메모리 마스터 패커 (Master Memory Packer)
Supabase DB 테이블의 36만 개 데이터를 Gzip 초압축 파일로 패킹하여
Supabase Storage ('translations' 버킷)에 master_*.json.gz 로 업로드합니다.
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

SUPABASE_URL = "https://oanjweqyvvdrbmvqoqrs.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im9hbmp3ZXF5dnZkcmJtdnFvcXJzIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODc3NTkwMDgsImV4cCI6MjEwMzMzNTAwOH0.3HbUVkupPoyMfzjMPSkAmGQ0qydp6yjDrxfSoGAghC8"
BUCKET_NAME = "translations"

HEADERS = {
    "apikey": SUPABASE_ANON_KEY,
    "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
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
    else:
        print(f"  ❌ Storage 업로드 실패 ({filename}): {res.status_code} - {res.text}")

def main():
    print("="*60)
    print("🚀 하이브리드 마스터 번역 메모리 패킹 시작 (Master Gzip Pack)")
    print("="*60)
    ensure_bucket()

    dist_dir = os.path.join(os.path.dirname(__file__), "..", "dist")
    os.makedirs(dist_dir, exist_ok=True)

    summary = []

    for cfg in TABLE_CONFIGS:
        table_name = cfg["table"]
        master_gz = cfg["master_gz"]
        json_name = cfg["json_name"]

        data = fetch_table_data(table_name)
        if not data:
            print(f"⚠️ {table_name} 데이터가 비어있습니다. 건너뜁니다.")
            continue

        raw_json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        gz_bytes = gzip.compress(raw_json_bytes, compresslevel=9)

        # 1. 로컬 dist 에도 저장
        with open(os.path.join(dist_dir, json_name), "wb") as f:
            f.write(raw_json_bytes)
        with open(os.path.join(dist_dir, master_gz), "wb") as f:
            f.write(gz_bytes)

        raw_mb = len(raw_json_bytes) / (1024 * 1024)
        gz_mb = len(gz_bytes) / (1024 * 1024)
        ratio = (1 - (len(gz_bytes) / len(raw_json_bytes))) * 100
        print(f"  [2/3] 압축 완료: 원본 {raw_mb:.2f} MB ➡️ 초압축 {gz_mb:.2f} MB ({ratio:.1f}% 절약)")

        # 2. Supabase Storage 에 업로드
        print(f"  [3/3] Supabase Storage 업로드 중...")
        upload_storage(gz_bytes, master_gz, "application/gzip")
        upload_storage(raw_json_bytes, json_name, "application/json")

        total_cnt = sum(len(v) for v in data.values())
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

if __name__ == "__main__":
    main()

