#!/usr/bin/env python3
"""
papers.db에서 사람이 이미 폴더로 분류해둔 행(reason="기존 아카이브 폴더 구조에서 가져옴
(API 미호출)") 중 파일이 실제로 존재하는 것만 모아, 고정 시드로 50건을 무작위 추출해
tests/repeat_set.csv로 저장한다.

tests/eval_set.csv(11행)와 달리 경계 규칙 기반으로 사람이 한 건씩 재검토하지 않은
순수 랜덤 샘플이다 — 재현성(같은 입력에 모델이 매번 같은 답을 내는지) 실험용이므로
라벨 품질 감사는 이번에는 하지 않는다. 라벨은 여전히 "폴더 위치"일 뿐, 수동 재검증은
안 했다는 점을 결과 해석 시 감안해야 한다.
"""
import csv
import os
import random
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import classify  # noqa: E402
import pdfplumber  # noqa: E402

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_DIR, "papers.db")
OUT_PATH = os.path.join(PROJECT_DIR, "tests", "repeat_set.csv")

SEED = 42
N = 50


def extract_abstract_snippet(file_path, max_chars=600):
    try:
        with pdfplumber.open(file_path) as pdf:
            full_text = ""
            for page in pdf.pages[:2]:
                full_text += (page.extract_text() or "") + "\n"
    except Exception as e:
        return None, f"extract_fail:{e.__class__.__name__}"

    m = re.search(r"abstract\b[:\s]*", full_text, re.IGNORECASE)
    if m:
        return full_text[m.end() : m.end() + max_chars].strip(), "abstract_section"
    lines = [l.strip() for l in full_text.splitlines() if l.strip()]
    return " ".join(lines[1:])[:max_chars].strip(), "raw_fallback"


def main():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, filename, filepath, category FROM papers "
        "WHERE reason='기존 아카이브 폴더 구조에서 가져옴 (API 미호출)'"
    ).fetchall()
    existing = [r for r in rows if os.path.exists(r[2])]

    random.seed(SEED)
    shuffled = existing[:]
    random.shuffle(shuffled)

    out_rows = []
    skipped = []
    for pid, fname, fpath, category in shuffled:
        if len(out_rows) >= N:
            break
        title = classify.extract_title(fpath)
        if not title:
            skipped.append((pid, fname, "title 추출 실패"))
            continue
        abstract, method = extract_abstract_snippet(fpath)
        out_rows.append(
            {
                "db_id": pid,
                "title": title,
                "abstract": abstract or "",
                "label": category,
                "source_filename": fname,
                "file_size_kb": classify.get_file_size_kb(fpath) or "",
            }
        )

    with open(OUT_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=["db_id", "title", "abstract", "label", "source_filename", "file_size_kb"]
        )
        w.writeheader()
        w.writerows(out_rows)

    print(f"저장됨: {OUT_PATH} ({len(out_rows)}행, 시드={SEED})")
    if skipped:
        print(f"제목 추출 실패로 제외됨: {len(skipped)}건")
        for pid, fname, why in skipped:
            print(f"  - {pid} {fname}: {why}")


if __name__ == "__main__":
    main()
