#!/usr/bin/env python3
"""
Article_Collection에 이미 분류되어 있는 기존 논문들을 papers.db에 등록하는
1회성 시드 스크립트. 폴더 구조 자체가 이미 정답 레이블이므로 Claude API를
전혀 호출하지 않는다 — category에는 파일이 들어있는 최상위 폴더명을 그대로 쓴다.

INSERT OR IGNORE + filepath UNIQUE 제약 덕분에 몇 번을 다시 실행해도 안전하다:
이미 등록된 파일은 조용히 건너뛰고, 새로 추가된 파일/폴더만 반영된다.
"""
import os
import sqlite3

import classify

ARTICLE_ROOT = os.path.expanduser("~/Article_Collection")


def seed_existing(db_path=None):
    db_path = db_path or classify.DB_PATH
    conn = sqlite3.connect(db_path)
    classify.init_db(conn)  # 테이블 생성/마이그레이션은 classify.py의 정의를 그대로 재사용

    inserted = 0
    skipped_non_dir = []
    for category in sorted(os.listdir(ARTICLE_ROOT)):
        cat_path = os.path.join(ARTICLE_ROOT, category)
        if not os.path.isdir(cat_path):
            # 최상위에 폴더 없이 떠 있는 파일(미분류 상태로 둬야 하는 것들)은 건너뛴다.
            skipped_non_dir.append(category)
            continue
        for root, _, files in os.walk(cat_path):
            for f in files:
                if not f.lower().endswith(".pdf"):
                    continue
                abs_path = os.path.abspath(os.path.join(root, f))
                cur = conn.execute(
                    "INSERT OR IGNORE INTO papers "
                    "(filename, filepath, category, summary, tags, confidence, reason) "
                    "VALUES (?, ?, ?, NULL, NULL, 'existing', "
                    "'기존 아카이브 폴더 구조에서 가져옴 (API 미호출)')",
                    (f, abs_path, category),
                )
                inserted += cur.rowcount

    conn.commit()
    print(f"시드 완료: 이번 실행에서 {inserted}건 신규 삽입 (이미 있던 건 자동 스킵)")
    if skipped_non_dir:
        print(f"폴더가 아니라 건너뜀(최상위 미분류 파일 등): {skipped_non_dir}")
    conn.close()
    return inserted


if __name__ == "__main__":
    seed_existing()
