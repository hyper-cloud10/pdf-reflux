#!/usr/bin/env python3
"""
tests/eval_set.csv(11행, 라벨 확정분)을 claude-haiku-4-5-20251001과 claude-haiku-5-5로
각각 한 번씩 분류해 정확도를 비교한다.

classify.py의 classify_paper()를 그대로 import해서 쓴다 — 분류 로직/프롬프트/
화이트리스트 검증 로직은 전혀 건드리지 않는다. 모델만 호출 직전에 classify.CLASSIFY_MODEL
속성을 임시로 바꿔서 전달하고, 호출이 끝나면 원래 값(classify.py 파일에 있는 값, 현재는
uncommitted 변경으로 claude-haiku-5-5)으로 되돌린다 — classify.py 파일 자체는 쓰지 않는다.

tests/needs_review.csv는 읽지 않는다(채점 제외).

API 키는 classify.py가 이미 하는 방식(.env -> ANTHROPIC_API_KEY 환경변수) 그대로 쓴다.
이 스크립트는 키를 파일에 쓰거나 출력하지 않는다.

API 호출 자체가 실패하면(네트워크/인증/레이트리밋/서버오류) 원문 에러를 보여주고 즉시 멈춘다
(재시도 없음). 모델이 응답은 했지만 JSON 파싱 실패/whitelist 밖 카테고리인 경우는
classify_paper()가 이미 안전하게 "미분류"로 처리하는 정상 동작이므로 멈추지 않고 보고서에
그 사실을 기록한다.
"""
import csv
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import classify  # noqa: E402  (sys.path 조정 후 import 필요)
from anthropic.resources.messages.messages import Messages  # noqa: E402

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
EVAL_SET_PATH = os.path.join(TESTS_DIR, "eval_set.csv")
REPORT_PATH = os.path.join(TESTS_DIR, "eval_report.md")

MODELS = ["claude-haiku-4-5-20251001", "claude-haiku-5-5"]

# 2026-10-11 platform.claude.com/docs/en/about-claude/pricing 확인 (<=100K 토큰 표준 구간)
PRICING_PER_MTOK = {
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
}

_API_KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_-]+")


def _redact(text):
    return _API_KEY_PATTERN.sub("sk-ant-...<redacted>", text)


# --- 토큰 사용량/실패 원문을 가로채기 위한 계측용 패치 ---
# classify_paper()의 분류 로직 자체는 건드리지 않는다 — 실제 API 호출을 감싸서
# 응답의 usage만 옆에서 기록하고, 예외는 그대로 다시 던져서 classify_paper()의
# 기존 except 블록이 평소와 똑같이 처리하도록 둔다.
_last_call = {}
_original_create = Messages.create


def _patched_create(self, *args, **kwargs):
    try:
        resp = _original_create(self, *args, **kwargs)
    except Exception as e:
        _last_call["error"] = f"{type(e).__name__}: {_redact(str(e))}"
        _last_call["usage"] = None
        raise
    _last_call["error"] = None
    _last_call["usage"] = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
    }
    return resp


Messages.create = _patched_create


def load_eval_rows():
    with open(EVAL_SET_PATH, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def classify_with_model(model_id, title, abstract):
    """
    classify.classify_paper()를 그대로 호출하되, 이 호출에 한해서만 모델을 바꾼다.
    CLASSIFY_MODEL만 바꾸고 CLASSIFY_MAX_TOKENS를 그대로 두면 import 시점 값(현재
    classify.py 파일 기준 1000)이 고정돼서, claude-haiku-4-5-20251001 호출도 실제
    운영값(300)이 아니라 1000으로 나가는 틈이 있었다 — classify.py 자체의 규칙
    (claude-haiku-5-5면 1000, 아니면 300)을 여기서도 그대로 재적용해서 맞춘다.
    """
    original_model = classify.CLASSIFY_MODEL
    original_max_tokens = classify.CLASSIFY_MAX_TOKENS
    original_temperature = classify.CLASSIFY_TEMPERATURE
    classify.CLASSIFY_MODEL = model_id
    classify.CLASSIFY_MAX_TOKENS = 1000 if model_id == "claude-haiku-5-5" else 300
    # claude-haiku-5-5는 temperature 파라미터 자체를 거부한다(400); 그 외 모델은
    # 재현성을 위해 0으로 고정한다 — classify.py의 CLASSIFY_TEMPERATURE 규칙과 동일.
    classify.CLASSIFY_TEMPERATURE = None if model_id == "claude-haiku-5-5" else 0
    try:
        return classify.classify_paper(title, abstract)
    finally:
        classify.CLASSIFY_MODEL = original_model
        classify.CLASSIFY_MAX_TOKENS = original_max_tokens
        classify.CLASSIFY_TEMPERATURE = original_temperature


def diagnose(row, result):
    label = row["label"]
    category = result["category"]
    error_type = result.get("error_type")

    if category == label:
        return "정답"

    if error_type == "invalid_category":
        m = re.search(r"알 수 없는 카테고리 응답: '(.*)'", result.get("reason", ""))
        raw = m.group(1) if m else result.get("reason", "?")
        return f"모델 문제 — whitelist 밖 카테고리 응답({raw!r}) → classify_paper()가 강제로 미분류 처리"

    if error_type == "parse_error":
        return "모델 문제 — 응답 JSON 파싱 실패 (모델이 지시된 JSON 형식을 지키지 않음)"

    if category == "미분류" and error_type is None:
        return "모델 문제 — 모델이 스스로 확신 부족 판단(경계 규칙 적용 결과 미분류). 라벨은 경계규칙 근거로 확정된 상태라 라벨 문제로 보지 않음."

    return f"모델 문제 — 다른 카테고리로 분류됨({category!r}). 라벨은 경계규칙 근거로 확정된 상태라 모델 쪽 판단 차이로 봄."


def main():
    rows = load_eval_rows()
    all_results = {m: [] for m in MODELS}
    usage_totals = {m: {"input_tokens": 0, "output_tokens": 0, "calls": 0} for m in MODELS}

    for model_id in MODELS:
        for row in rows:
            result = classify_with_model(model_id, row["title"], row["abstract"])
            err = _last_call.get("error")
            if err:
                print(f"\nAPI 호출 에러 (model={model_id}, db_id={row['db_id']}) — 원문 그대로, 재시도 없이 중단:\n")
                print(err)
                sys.exit(1)

            usage = _last_call.get("usage")
            if usage:
                usage_totals[model_id]["input_tokens"] += usage["input_tokens"]
                usage_totals[model_id]["output_tokens"] += usage["output_tokens"]
                usage_totals[model_id]["calls"] += 1

            all_results[model_id].append(
                {
                    "db_id": row["db_id"],
                    "label": row["label"],
                    "predicted": result["category"],
                    "confidence": result["confidence"],
                    "reason": result["reason"],
                    "error_type": result.get("error_type"),
                    "diagnosis": diagnose(row, result),
                }
            )

    write_report(rows, all_results, usage_totals)
    print(f"저장됨: {REPORT_PATH}")


def write_report(rows, all_results, usage_totals):
    n = len(rows)
    lines = []
    lines.append("# eval_report.md — Claude Haiku 4.5 vs Claude Haiku 5.5 분류 비교\n")
    lines.append(f"- 평가셋: `tests/eval_set.csv` ({n}행, 사람이 실제로 분류해둔 폴더 기반 정답 라벨)")
    lines.append("- `tests/needs_review.csv`(경계 사례 4행)는 채점에서 제외")
    lines.append(f"- 비교 모델: `{MODELS[0]}` (기존) vs `{MODELS[1]}` (전환 검토 대상)")
    lines.append("- 분류 로직/프롬프트/화이트리스트 검증: `classify.py`의 `classify_paper()`를 그대로 호출, 변경 없음\n")

    lines.append("## 모델별 정확도\n")
    lines.append("| 모델 | 정답 수 | 정확도 |")
    lines.append("|---|---|---|")
    accuracy = {}
    for model_id in MODELS:
        correct = sum(1 for r in all_results[model_id] if r["predicted"] == r["label"])
        accuracy[model_id] = (correct, n)
        lines.append(f"| `{model_id}` | {correct}/{n} | {correct/n*100:.1f}% |")
    lines.append("")

    for model_id in MODELS:
        lines.append(f"## `{model_id}` — 행별 결과\n")
        lines.append("| db_id | 정답(label) | 예측 | 일치 | 신뢰도 | 원인 진단 |")
        lines.append("|---|---|---|---|---|---|")
        for r in all_results[model_id]:
            match = "✅" if r["predicted"] == r["label"] else "❌"
            lines.append(
                f"| {r['db_id']} | {r['label']} | {r['predicted']} | {match} | {r['confidence']} | {r['diagnosis']} |"
            )
        lines.append("")

    lines.append("## 토큰 사용량 및 비용 (실제 측정값)\n")
    lines.append("| 모델 | 호출 수 | 총 input 토큰 | 총 output 토큰 | input 단가/MTok | output 단가/MTok | 추정 비용 |")
    lines.append("|---|---|---|---|---|---|---|")
    total_cost = 0.0
    for model_id in MODELS:
        u = usage_totals[model_id]
        p = PRICING_PER_MTOK[model_id]
        cost = (u["input_tokens"] / 1_000_000) * p["input"] + (u["output_tokens"] / 1_000_000) * p["output"]
        total_cost += cost
        lines.append(
            f"| `{model_id}` | {u['calls']} | {u['input_tokens']} | {u['output_tokens']} "
            f"| ${p['input']:.2f} | ${p['output']:.2f} | ${cost:.4f} |"
        )
    lines.append(f"\n**총 추정 비용: ${total_cost:.4f}**")
    lines.append(
        "\n(가격 출처: platform.claude.com/docs/en/about-claude/pricing, 2026-10-11 확인, "
        "두 모델 모두 이번 호출 길이는 100K 토큰 이하 표준 구간)\n"
    )

    lines.append("## API 호출 에러\n")
    lines.append("없음 — 22회 호출(11행 × 2모델) 전부 정상 완료.\n")

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    main()
