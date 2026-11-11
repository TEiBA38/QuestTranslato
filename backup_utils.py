# -*- coding: utf-8 -*-
"""
backup_utils.py - 백업 파일 초고속 압축 및 안전 복원 모듈

1. gzip (compresslevel=1) 고속 압축으로 CPU 부담 없이 디스크 용량 50~80% 절감
2. 임시 파일(.tmp) 생성 후 원자적 교체(os.replace)로 저장 중 크래시 시 파일 손상 완전 방지
3. 비압축 .bak 및 압축 .bak.gz 양방향 투명 지원 (100% 하위 호환)
4. 기존 비압축 백업 파일 자동 압축 마이그레이션 지원
"""

import os
import gzip
import shutil
import logging


def create_compressed_backup(src_path, dst_bak_path=None):
    """
    주어진 파일을 초고속(compresslevel=1)으로 압축하여 .bak.gz 백업을 생성합니다.
    기존에 남아있는 비압축 .bak 파일이 있다면 용량 회수를 위해 안전하게 삭제합니다.
    """
    if not src_path or not os.path.isfile(src_path):
        return None

    if dst_bak_path is None:
        dst_bak_path = src_path + ".bak.gz"

    tmp_path = dst_bak_path + ".tmp"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(dst_bak_path)), exist_ok=True)
        with open(src_path, 'rb') as f_in:
            with gzip.open(tmp_path, 'wb', compresslevel=1) as f_out:
                shutil.copyfileobj(f_in, f_out, 1024 * 1024)
        os.replace(tmp_path, dst_bak_path)

        # 기존의 비압축 .bak 파일이 있으면 제거하여 디스크 용량 즉시 회수
        legacy_bak = src_path + ".bak"
        if os.path.exists(legacy_bak) and legacy_bak != dst_bak_path:
            try:
                os.remove(legacy_bak)
            except Exception:
                pass

        return dst_bak_path
    except Exception as e:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        logging.warning(f"[Backup] 압축 백업 실패 ({src_path} -> {dst_bak_path}): {e}")
        # 폴백: 일반 복사
        try:
            fallback_bak = src_path + ".bak"
            shutil.copy2(src_path, fallback_bak)
            return fallback_bak
        except Exception:
            return None


def restore_backup(bak_path, orig_path=None):
    """
    .bak.gz 또는 .bak 백업 파일을 원본 위치로 복원하고 백업 파일을 정리합니다.
    """
    if not bak_path or not os.path.isfile(bak_path):
        return False

    if orig_path is None:
        if bak_path.endswith(".bak.gz"):
            orig_path = bak_path[:-7]
        elif bak_path.endswith(".bak"):
            orig_path = bak_path[:-4]
        else:
            return False

    tmp_orig = orig_path + ".tmp"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(orig_path)), exist_ok=True)
        if bak_path.endswith(".gz"):
            with gzip.open(bak_path, 'rb') as f_in:
                with open(tmp_orig, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out, 1024 * 1024)
        else:
            shutil.copy2(bak_path, tmp_orig)

        os.replace(tmp_orig, orig_path)
        try:
            os.remove(bak_path)
        except Exception:
            pass
        return True
    except Exception as e:
        if os.path.exists(tmp_orig):
            try:
                os.remove(tmp_orig)
            except Exception:
                pass
        logging.error(f"[Backup] 복원 실패 ({bak_path} -> {orig_path}): {e}")
        return False


def read_backup_json(bak_path):
    """
    .bak 또는 .bak.gz 파일에서 JSON을 읽어 파싱합니다 (캐시 손상 시 긴급 복구용).
    """
    import json
    if not bak_path or not os.path.isfile(bak_path):
        return None
    try:
        if bak_path.endswith(".gz"):
            with gzip.open(bak_path, 'rt', encoding='utf-8') as f:
                return json.load(f)
        else:
            with open(bak_path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        return None


def migrate_existing_backups_to_compressed(directory):
    """
    지정된 디렉토리의 모든 기존 .bak 파일들을 검색하여 .bak.gz로 압축 변환하고
    비압축 .bak 파일을 삭제하여 디스크 용량을 즉시 확보합니다.
    """
    migrated_count = 0
    saved_bytes = 0
    if not directory or not os.path.isdir(directory):
        return migrated_count, saved_bytes

    for root, _, files in os.walk(directory):
        for fname in files:
            if fname.endswith(".bak"):
                bak_path = os.path.join(root, fname)
                orig_size = os.path.getsize(bak_path)
                gz_path = bak_path + ".gz"
                try:
                    with open(bak_path, 'rb') as f_in:
                        with gzip.open(gz_path + ".tmp", 'wb', compresslevel=1) as f_out:
                            shutil.copyfileobj(f_in, f_out, 1024 * 1024)
                    os.replace(gz_path + ".tmp", gz_path)
                    new_size = os.path.getsize(gz_path)
                    os.remove(bak_path)
                    migrated_count += 1
                    saved_bytes += max(0, orig_size - new_size)
                except Exception as e:
                    if os.path.exists(gz_path + ".tmp"):
                        try:
                            os.remove(gz_path + ".tmp")
                        except Exception:
                            pass
    return migrated_count, saved_bytes

