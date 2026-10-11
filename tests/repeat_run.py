#!/usr/bin/env python3
"""
tests/repeat_set.csv(50행, 사람이 폴더로 분류해둔 것 중 랜덤 추출)를 claude-haiku-5-5로
두 번 돌려서 회차 사이에 예측이 바뀌는 행이 있는지 본다. temperature를 0으로 고정할 수
없는 모델이라(classify.py의 CLASSIFY_TEMPERATURE 참고) 같은 입력에도 응답이 달라질 수
있다는 걸 직접 확인하는 재현성 실험이다.

classify.py는 건드리지 않는다 — classify.classify_paper()를 그대로 두 번 호출한다.
classify.py의 CLASSIFY_MODEL이 이미 claude-haiku-5-5인 상태를 그대로 쓴다(이 스크립트는
모델을 바꿔 끼우지 않음 — 다른 모델로 돌릴 거면 compare_models.py를 쓸 것).
"""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import classify  # noqa: E402
from anthropic.resources.messages.messages import Messages  # noqa: E402

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPEAT_SET_PATH = os.path.join(TESTS_DIR, "repeat_set.csv")
REPORT_PATH = os.path.join(TESTS_DIR, "repeat_report.md")

N_ROUNDS = 2

_last_call = {}
_original_create = Messages.create


def _patched_create(self, *args, **kwargs):
    try:
        resp = _original_create(self, *args, **kwargs)
    except Exception as e:
        _last_call["error"] = f"{type(e).__name__}: {e}"
        _last_call["usage"] = None
        raise
    _last_call["error"] = None
    _last_call["usage"] = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
    }
    return resp


Messages.create = _patched_create


def load_rows():
    with open(REPEAT_SET_PATH, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    if classify.CLASSIFY_MODEL != "claude-haiku-5-5":
        print(
            f"classify.CLASSIFY_MODEL이 claude-haiku-5-5가 아님(현재: {classify.CLASSIFY_MODEL!r}) "
            "— 이 스크립트는 claude-haiku-5-5 전용이라 중단."
        )
        sys.exit(1)

    rows = load_rows()
    rounds = []  # rounds[i] = {db_id: result_dict}
    usage_totals = []

    for round_idx in range(1, N_ROUNDS + 1):
        round_results = {}
        round_usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
        for row in rows:
            size_kb = int(row["file_size_kb"]) if row.get("file_size_kb") else None
            result = classify.classify_paper(row["title"], row["abstract"], file_size_kb=size_kb)
            err = _last_call.get("error")
            if err:
                print(f"\nAPI 호출 에러 (round={round_idx}, db_id={row['db_id']}) — 원문 그대로, 중단:\n")
                print(err)
                sys.exit(1)
            usage = _last_call.get("usage")
            if usage:
                round_usage["input_tokens"] += usage["input_tokens"]
                round_usage["output_tokens"] += usage["output_tokens"]
                round_usage["calls"] += 1
            round_results[row["db_id"]] = result
        rounds.append(round_results)
        usage_totals.append(round_usage)
        correct = sum(1 for row in rows if rounds[-1][row["db_id"]]["category"] == row["label"])
        print(f"회차 {round_idx}: {correct}/{len(rows)} 정답")

    write_report(rows, rounds, usage_totals)
    print(f"저장됨: {REPORT_PATH}")


def write_report(rows, rounds, usage_totals):
    n = len(rows)
    lines = []
    lines.append("# repeat_report.md — claude-haiku-5-5 재현성 실험 (같은 50행을 2회 호출)\n")
    lines.append(f"- 평가셋: `tests/repeat_set.csv` ({n}행, papers.db 중 사람이 폴더로 분류해둔 것에서 시드 42로 무작위 추출)")
    lines.append("- tests/eval_set.csv와 달리 경계 규칙 기반 수동 재검토를 하지 않은 순수 랜덤 샘플 — 라벨은 폴더 위치 그대로")
    lines.append(f"- 모델: `claude-haiku-5-5` (temperature 고정 불가 — classify.py의 CLASSIFY_TEMPERATURE 참고)")
    lines.append(f"- 회차 수: {N_ROUNDS}\n")

    lines.append("## 회차별 정확도\n")
    lines.append("| 회차 | 정답 수 | 정확도 |")
    lines.append("|---|---|---|")
    for i, round_results in enumerate(rounds, 1):
        correct = sum(1 for row in rows if round_results[row["db_id"]]["category"] == row["label"])
        lines.append(f"| {i} | {correct}/{n} | {correct/n*100:.1f}% |")
    lines.append("")

    changed = []
    for row in rows:
        preds = [r[row["db_id"]]["category"] for r in rounds]
        if len(set(preds)) > 1:
            changed.append((row, preds))

    lines.append(f"## 회차 사이에 바뀐 행 ({len(changed)}건)\n")
    if changed:
        header = "| db_id | label(정답) | " + " | ".join(f"회차{i}" for i in range(1, N_ROUNDS + 1)) + " |"
        sep = "|---|---|" + "---|" * N_ROUNDS
        lines.append(header)
        lines.append(sep)
        for row, preds in changed:
            lines.append(f"| {row['db_id']} | {row['label']} | " + " | ".join(preds) + " |")
    else:
        lines.append("없음 — 모든 행이 두 회차에서 동일하게 예측됨.")
    lines.append("")

    lines.append("## 전체 행별 결과\n")
    header = "| db_id | label(정답) | " + " | ".join(f"회차{i}" for i in range(1, N_ROUNDS + 1)) + " | 바뀜 |"
    sep = "|---|---|" + "---|" * N_ROUNDS + "---|"
    lines.append(header)
    lines.append(sep)
    for row in rows:
        preds = [rounds[i][row["db_id"]]["category"] for i in range(N_ROUNDS)]
        mark = "✅" if len(set(preds)) == 1 else "🔁"
        lines.append(f"| {row['db_id']} | {row['label']} | " + " | ".join(preds) + f" | {mark} |")
    lines.append("")

    lines.append("## 토큰 사용량 (실측)\n")
    lines.append("| 회차 | 호출 수 | input 토큰 | output 토큰 |")
    lines.append("|---|---|---|---|")
    total_input = total_output = 0
    for i, u in enumerate(usage_totals, 1):
        lines.append(f"| {i} | {u['calls']} | {u['input_tokens']} | {u['output_tokens']} |")
        total_input += u["input_tokens"]
        total_output += u["output_tokens"]
    cost = (total_input / 1_000_000) * 0.10 + (total_output / 1_000_000) * 0.50
    lines.append(f"\n**총 추정 비용: ${cost:.4f}** (claude-haiku-5-5, $0.10/$0.50 per MTok, 100K 토큰 이하 표준 구간)\n")

    lines.append("## API 호출 에러\n")
    lines.append("없음\n")

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    main()
