#!/usr/bin/env python3
"""
tests/eval_set.csv(11행)을 claude-haiku-5-5로 다시 한 번만 호출해서, classify_paper()가
파싱해서 버리기 전의 raw 응답 텍스트/stop_reason/output 토큰 수를 그대로 tests/raw_5_5.txt에 남긴다.

분류 로직은 전혀 건드리지 않는다 — classify.classify_paper()를 그대로 호출한다. 다만 그
호출이 내부적으로 쓰는 client.messages.create()를 옆에서 가로채서(계측용 패치), 응답
객체(raw text, stop_reason, usage)를 classify_paper()가 파싱하기 전 상태로 따로 기록한다.
classify_paper()의 리턴값(최종 category/confidence/reason/error_type)은 그대로 같이 써서
"파싱 실패"와 "모델이 실제로 미분류를 선택"을 구분하는 데 쓴다:
  - error_type == "parse_error"      -> JSON 파싱 자체가 실패함
  - error_type == "invalid_category" -> JSON은 파싱됐지만 category가 whitelist 밖
  - category == "미분류" and error_type is None -> 모델이 유효한 JSON으로 미분류를 직접 선택

tests/needs_review.csv는 쓰지 않는다. classify.py/categories.yaml/git 상태는 건드리지 않는다.
"""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import classify  # noqa: E402
from anthropic.resources.messages.messages import Messages  # noqa: E402

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
EVAL_SET_PATH = os.path.join(TESTS_DIR, "eval_set.csv")
RAW_OUT_PATH = os.path.join(TESTS_DIR, "raw_5_5.txt")

MODEL_ID = "claude-haiku-5-5"

_last_call = {}
_original_create = Messages.create


def _patched_create(self, *args, **kwargs):
    try:
        resp = _original_create(self, *args, **kwargs)
    except Exception as e:
        _last_call["error"] = f"{type(e).__name__}: {e}"
        _last_call["raw"] = None
        raise
    _last_call["error"] = None
    # classify.py(및 이 계측 코드가 처음 그랬던 것처럼) content[0]이 항상 텍스트 블록이라고
    # 가정하면 안 된다 — claude-haiku-5-5는 기본적으로 adaptive thinking이 켜져 있어서
    # 응답에 ThinkingBlock(.text 속성 없음)이 TextBlock보다 먼저 올 수 있다.
    # 여기서는 순수 계측 목적으로만 안전하게 type=='text'인 블록을 찾는다
    # (classify.py 자체의 파싱 로직은 손대지 않음).
    block_types = [getattr(b, "type", None) for b in resp.content]
    text_block = next((b for b in resp.content if getattr(b, "type", None) == "text"), None)
    _last_call["raw"] = {
        "text": text_block.text if text_block is not None else None,
        "block_types": block_types,
        "stop_reason": resp.stop_reason,
        "output_tokens": resp.usage.output_tokens,
        "input_tokens": resp.usage.input_tokens,
        "thinking_tokens": getattr(resp.usage.output_tokens_details, "thinking_tokens", None)
        if getattr(resp.usage, "output_tokens_details", None)
        else None,
    }
    return resp


Messages.create = _patched_create


def load_eval_rows():
    with open(EVAL_SET_PATH, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def classify_with_model(model_id, title, abstract):
    original_model = classify.CLASSIFY_MODEL
    classify.CLASSIFY_MODEL = model_id
    try:
        return classify.classify_paper(title, abstract)
    finally:
        classify.CLASSIFY_MODEL = original_model


def mismatch_reason(label, result, raw):
    category = result["category"]
    error_type = result.get("error_type")

    if category == label:
        return "정답"
    if error_type == "api_error_AttributeError":
        # classify.py:345 response.content[0].text 가 ThinkingBlock(.text 없음)을 먼저
        # 만나 터진 경우 — 모델의 실제 분류 판단이 아니라 classify.py 쪽 코드 버그로
        # "미분류"가 찍힌 것. raw["block_types"]로 실제로 thinking이 먼저 왔는지 확인 가능.
        return f"classify.py 코드 버그로 인한 거짓 미분류 (content block 순서={raw['block_types']}, 모델의 실제 판단 아님)"
    if error_type == "no_text_block":
        # classify.py의 새 명시적 처리: 응답에 type=="text"인 블록이 전혀 없었던 경우
        # (예: thinking만 하다가 max_tokens에 걸려 잘림). 모델의 분류 판단이 아니라
        # 텍스트 자체가 없었다는 사실을 그대로 보여준다.
        return f"텍스트 블록 없음 (content block 순서={raw['block_types']}, stop_reason={raw['stop_reason']})"
    if error_type == "parse_error":
        return "JSON 파싱 실패"
    if error_type == "invalid_category":
        return "whitelist 밖 카테고리 응답 (JSON은 유효, category 값이 28개 밖)"
    if category == "미분류" and error_type is None:
        return "모델이 유효한 JSON으로 미분류를 직접 선택"
    return f"다른 유효 카테고리로 오분류 ({category!r})"


def main():
    rows = load_eval_rows()
    records = []
    raw_blocks = []

    for row in rows:
        result = classify_with_model(MODEL_ID, row["title"], row["abstract"])
        err = _last_call.get("error")
        if err:
            print(f"\nAPI 호출 에러 (db_id={row['db_id']}) — 원문 그대로, 재시도 없이 중단:\n")
            print(err)
            sys.exit(1)

        raw = _last_call["raw"]
        reason = mismatch_reason(row["label"], result, raw)

        records.append(
            {
                "db_id": row["db_id"],
                "label": row["label"],
                "predicted": result["category"],
                "error_type": result.get("error_type"),
                "stop_reason": raw["stop_reason"],
                "output_tokens": raw["output_tokens"],
                "mismatch_reason": reason,
            }
        )

        raw_blocks.append(
            "=" * 70
            + f"\ndb_id: {row['db_id']}\n"
            + f"label(정답): {row['label']}\n"
            + f"title: {row['title']}\n"
            + f"stop_reason: {raw['stop_reason']}\n"
            + f"output_tokens(전체): {raw['output_tokens']}\n"
            + f"thinking_tokens(그중): {raw['thinking_tokens']}\n"
            + f"input_tokens: {raw['input_tokens']}\n"
            + f"content block 순서: {raw['block_types']}\n"
            + f"parsed_category(classify_paper() 리턴값): {result['category']} (error_type={result.get('error_type')})\n"
            + f"판정: {reason}\n"
            + "--- raw response text block 그대로 (text 타입 블록이 없으면 None) ---\n"
            + str(raw["text"])
            + "\n"
        )

    with open(RAW_OUT_PATH, "w", encoding="utf-8") as f:
        f.write(f"claude-haiku-5-5 raw 응답 전체 기록 — tests/eval_set.csv {len(rows)}행\n")
        f.write("\n".join(raw_blocks))

    print(f"저장됨: {RAW_OUT_PATH}\n")
    print(f"{'db_id':>6} | {'label':<20} | {'predicted':<10} | {'stop_reason':<10} | {'out_tok':>7} | 판정")
    for r in records:
        print(
            f"{r['db_id']:>6} | {r['label']:<20} | {r['predicted']:<10} | "
            f"{str(r['stop_reason']):<10} | {r['output_tokens']:>7} | {r['mismatch_reason']}"
        )


if __name__ == "__main__":
    main()
