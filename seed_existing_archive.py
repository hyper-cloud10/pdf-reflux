#!/usr/bin/env python3
"""
Article_Collection에 이미 분류되어 있는 기존 논문들을 papers.db에 등록하는
시드 스크립트. 폴더 구조 자체가 이미 정답 레이블이므로 Claude API를
전혀 호출하지 않는다 — category에는 파일이 들어있는 최상위 폴더명을 그대로 쓴다.

대상은 WSL 로컬 복사본이 아니라 Windows 원본(classify.ARTICLE_COLLECTION_ROOT)이다.
과거에는 WSL 복사본(~/Article_Collection)을 rsync해서 시드했었는데, 그 이후
Windows 원본 쪽에 파일이 더 늘어나(예: 특정 카테고리 폴더) 두 복사본이 갈라졌다.

filepath가 아니라 (filename, category)로 "이미 등록됐는지"를 판단한다 — WSL 복사본
경로로 이미 등록된 파일이 Windows 원본 경로로 다시 걸려도 중복으로 또 넣지 않기
위해서다(두 경로 문자열이 달라서 filepath UNIQUE만으로는 이걸 못 걸러낸다).
그래서 이 스크립트를 몇 번 다시 돌려도 "진짜 새 파일"만 추가된다.
"""
import os
import sqlite3

import classify

ARTICLE_ROOT = classify.ARTICLE_COLLECTION_ROOT


def already_seeded(conn, filename, category):
    """같은 (filename, category)가 이미 papers 테이블에 있으면 True.
    WSL 복사본 경로로 먼저 등록된 파일이 Windows 원본 스캔에서 다시 걸려도
    filepath 문자열이 달라 중복 삽입되는 걸 막기 위한 논리적 중복 판정."""
    return (
        conn.execute(
            "SELECT 1 FROM papers WHERE filename = ? AND category = ?",
            (filename, category),
        ).fetchone()
        is not None
    )


def seed_existing(db_path=None, only_categories=None):
    """
    only_categories를 주면 그 폴더(들)만 스캔한다. Windows 원본 전체를 스캔하면
    아직 정리가 안 된 다른 폴더까지 한꺼번에 끌려들어올 수 있어서, 특정 폴더만
    확인된 상태로 반영하고 싶을 때 쓴다 (예: seed_existing(only_categories=["특정 카테고리"])).
    """
    db_path = db_path or classify.DB_PATH
    conn = sqlite3.connect(db_path)
    classify.init_db(conn)  # 테이블 생성/마이그레이션은 classify.py의 정의를 그대로 재사용

    inserted = 0
    skipped_existing = 0
    skipped_non_dir = []
    categories = sorted(only_categories) if only_categories else sorted(os.listdir(ARTICLE_ROOT))
    for category in categories:
        cat_path = os.path.join(ARTICLE_ROOT, category)
        if not os.path.isdir(cat_path):
            # 최상위에 폴더 없이 떠 있는 파일(미분류 상태로 둬야 하는 것들)은 건너뛴다.
            skipped_non_dir.append(category)
            continue
        for root, _, files in os.walk(cat_path):
            for f in files:
                if not f.lower().endswith(".pdf"):
                    continue
                if already_seeded(conn, f, category):
                    skipped_existing += 1
                    continue
                abs_path = os.path.abspath(os.path.join(root, f))
                conn.execute(
                    "INSERT OR IGNORE INTO papers "
                    "(filename, filepath, category, summary, tags, confidence, reason, status) "
                    "VALUES (?, ?, ?, NULL, NULL, 'existing', "
                    "'기존 아카이브 폴더 구조에서 가져옴 (API 미호출)', ?)",
                    (f, abs_path, category, classify.STATUS_DONE),
                )
                inserted += 1

    conn.commit()
    print(f"시드 완료: 이번 실행에서 {inserted}건 신규 삽입, {skipped_existing}건은 이미 등록돼서 스킵")
    if skipped_non_dir:
        print(f"폴더가 아니라 건너뜀(최상위 미분류 파일 등): {skipped_non_dir}")
    conn.close()
    return inserted


if __name__ == "__main__":
    seed_existing()
