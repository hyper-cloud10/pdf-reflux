#!/usr/bin/env python3
import hashlib
import json
import os
import shutil
import sqlite3
import sys

try:
    import anthropic
except ImportError:
    anthropic = None

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    import yaml
except ImportError:
    yaml = None

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PROJECT_DIR, "papers.db")

# 분류된 파일을 실제로 옮겨 담을 원본(Windows) 아카이브 경로.
# WSL 로컬 복사본(~/Article_Collection)이 아니라 원본 경로로 옮긴다.
ARTICLE_COLLECTION_ROOT = "/mnt/c/Users/사용자/Documents/Article_Collection"

# PowerShell이 wsl -d Ubuntu -- bash -c "..."로 이 스크립트를 호출하므로
# 작업 디렉터리가 프로젝트 폴더가 아닐 수 있다. .env 경로를 스크립트 위치 기준으로 고정해서
# 어디서 호출되든 같은 .env(ANTHROPIC_API_KEY=... 한 줄)를 읽도록 한다.
if load_dotenv is not None:
    load_dotenv(os.path.join(PROJECT_DIR, ".env"))

# 분류용 기본 모델. 빠르고 저렴해서 다운로드 즉시 분류하는 이 워크플로우에 적합.
CLASSIFY_MODEL = "claude-haiku-4-5-20251001"

CATEGORIES_CONFIG_PATH = os.path.join(PROJECT_DIR, "categories.yaml")

# "특정 경계 규칙 전용 카테고리" 분류 판단에만 쓰이는 개인 상태. 프로젝트(git) 소속이 아니라
# 사용자 개인의 홈 디렉터리 로컬 상태라서(학기마다 바뀜) categories.yaml과 다른 자리에 둔다.
EXPERIMENT_CONTEXT_PATH = os.path.expanduser("~/.yactopia_experiment_context")

# 카테고리 목록과 경계 규칙은 사용자마다 다르므로 코드에 하드코딩하지 않고
# categories.yaml(개인 폴더 구조, git에는 안 올라감)에서 읽는다.
# 템플릿은 categories.example.yaml 참고.
SYSTEM_PROMPT_TEMPLATE = """당신은 학술 논문 제목(및 필요시 초록 일부)을 보고 아래 {count}개 카테고리 중 가장 적합한 것 하나를 고르는 분류기입니다.
카테고리명은 기존 폴더명 그대로이며, 이름을 바꾸거나 새로 만들지 않습니다.

[카테고리 {count}개]
{categories_block}
{experiment_context_block}
[경계 규칙 — 반드시 순서대로 적용]
{boundary_rules}

[확신이 낮을 때]
위 규칙을 적용해도 애매하면 강제로 추측하지 말고 category를 "미분류"로 둔다.

반드시 아래 JSON 형식으로만 응답한다. 다른 텍스트를 덧붙이지 않는다.
{{"category": "<{count}개 중 하나 또는 미분류>", "confidence": "high|medium|low", "reason": "<한 문장 이유>"}}"""


def load_categories_config(path=None):
    """
    categories.yaml을 읽어 (categories 리스트, boundary_rules 문자열)을 반환한다.
    yaml 미설치, 파일 없음, 형식 불량 등 어떤 이유로든 읽기에 실패하면
    예외를 던지지 않고 (None, None)을 반환한다 — 호출부가 안전하게 폴백하도록.
    """
    path = path or CATEGORIES_CONFIG_PATH
    if yaml is None or not os.path.exists(path):
        return None, None

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return None, None

    categories = data.get("categories")
    if not isinstance(categories, list) or not categories:
        return None, None

    boundary_rules = data.get("boundary_rules") or ""
    return categories, boundary_rules


def load_experiment_context(path=None):
    """
    ~/.yactopia_experiment_context(사용자 개인 홈 디렉터리 상태, git과 무관)에서
    current_topic을 읽는다. "특정 경계 규칙 전용 카테고리" 경계 규칙 판단에만 쓰인다.
    yaml 미설치, 파일 없음, 형식 불량, current_topic 비어있음 등 어떤 이유로든
    읽기에 실패하면 예외를 던지지 않고 None을 반환한다 — 이 경우 해당 경계 규칙은
    프롬프트에서 통째로 빠지고 분류는 평소대로(특정 경계 규칙 전용 카테고리 없이) 동작한다.
    """
    path = path or EXPERIMENT_CONTEXT_PATH
    if yaml is None or not os.path.exists(path):
        return None

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return None

    topic = data.get("current_topic")
    if not isinstance(topic, str) or not topic.strip():
        return None

    return topic.strip()


def build_system_prompt(categories, boundary_rules, experiment_context=None):
    experiment_context_block = ""
    if experiment_context:
        experiment_context_block = (
            "\n[사용자의 현재 실험 컨텍스트 — \"특정 경계 규칙 전용 카테고리\" 판단에만 참고]\n"
            f"{experiment_context}\n"
        )

    return SYSTEM_PROMPT_TEMPLATE.format(
        count=len(categories),
        categories_block=", ".join(categories),
        experiment_context_block=experiment_context_block,
        boundary_rules=boundary_rules.strip(),
    )


CATEGORIES, _BOUNDARY_RULES = load_categories_config()
EXPERIMENT_CONTEXT = load_experiment_context()
if CATEGORIES is not None:
    VALID_CATEGORIES = set(CATEGORIES) | {"미분류"}
    SYSTEM_PROMPT = build_system_prompt(CATEGORIES, _BOUNDARY_RULES, EXPERIMENT_CONTEXT)
else:
    # categories.yaml이 없으면(새 클론 직후 등) 분류 자체를 할 수 없다.
    # classify_paper()가 이 상태를 감지해서 "미분류"로 안전하게 떨어진다.
    VALID_CATEGORIES = {"미분류"}
    SYSTEM_PROMPT = None

VALID_CONFIDENCES = {"high", "medium", "low"}

# 요청 상태 머신: 요청중(API 호출 착수, 결과 미확정) -> 완료(결과 확정, 성공이든
# 실패든 상관없이 classify_paper()가 뭔가 결정적인 결과를 반환한 시점).
# 중간에 프로세스가 죽으면 행이 STATUS_IN_PROGRESS에 멈춰 있는 것으로 그 사실을 알 수 있다.
STATUS_IN_PROGRESS = "요청중"
STATUS_DONE = "완료"


def init_db(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS papers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL UNIQUE,
            category TEXT,
            summary TEXT,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    migrate_add_tags_column(conn)
    migrate_add_classification_columns(conn)
    migrate_add_error_tracking_columns(conn)
    migrate_add_status_column(conn)


def migrate_add_tags_column(conn):
    """papers 테이블에 tags 컬럼이 없으면 추가한다. 이미 있으면 건너뜀."""
    columns = [row[1] for row in conn.execute("PRAGMA table_info(papers)").fetchall()]
    if "tags" not in columns:
        conn.execute("ALTER TABLE papers ADD COLUMN tags TEXT")
        conn.commit()


def migrate_add_classification_columns(conn):
    """papers 테이블에 confidence/reason 컬럼이 없으면 추가한다. 이미 있으면 건너뜀."""
    columns = [row[1] for row in conn.execute("PRAGMA table_info(papers)").fetchall()]
    if "confidence" not in columns:
        conn.execute("ALTER TABLE papers ADD COLUMN confidence TEXT")
    if "reason" not in columns:
        conn.execute("ALTER TABLE papers ADD COLUMN reason TEXT")
    conn.commit()


def migrate_add_error_tracking_columns(conn):
    """
    papers 테이블에 file_hash/error_type 컬럼이 없으면 추가한다. 이미 있으면 건너뜀.
    - file_hash: 파일 내용 기반 식별자(SHA-256). 경로가 바뀌어도(이동/재다운로드) 같은
      파일인지 나중에 대조할 수 있게 하기 위함.
    - error_type: category가 "미분류"로 떨어진 게 실제 분류 결과(확신 부족)인지,
      아니면 시스템/API 오류 때문인지, 오류라면 어떤 종류인지 구분하기 위함.
      정상 분류 성공 시에는 NULL.
    """
    columns = [row[1] for row in conn.execute("PRAGMA table_info(papers)").fetchall()]
    if "file_hash" not in columns:
        conn.execute("ALTER TABLE papers ADD COLUMN file_hash TEXT")
    if "error_type" not in columns:
        conn.execute("ALTER TABLE papers ADD COLUMN error_type TEXT")
    conn.commit()


def migrate_add_status_column(conn):
    """
    papers 테이블에 status 컬럼('요청중'/'완료')을 추가한다. 이미 있으면 건너뜀.
    이 컬럼이 생기기 전에 들어온 기존 행들은 전부 이미 분류가 끝난 상태로 들어온
    것들이므로(과거엔 분류 완료 후에만 INSERT했음), 마이그레이션 시점에 한 번
    status='완료'로 일괄 채워서 "컬럼 추가 이전 행 = 아직 처리 안 한 행"으로
    잘못 해석돼 재분류되는 걸 막는다.
    """
    columns = [row[1] for row in conn.execute("PRAGMA table_info(papers)").fetchall()]
    if "status" not in columns:
        conn.execute("ALTER TABLE papers ADD COLUMN status TEXT")
        conn.execute("UPDATE papers SET status = ? WHERE status IS NULL", (STATUS_DONE,))
        conn.commit()


def compute_file_hash(file_path):
    """파일 내용의 SHA-256(hex)을 계산한다. 실패해도 예외를 던지지 않고 None을 반환."""
    try:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def is_prelab_mode():
    return os.path.exists(os.path.expanduser("~/.yactopia_prelab_mode"))


def get_tags():
    return "예비레포트용" if is_prelab_mode() else None


def extract_title(file_path):
    """
    PDF에서 제목을 뽑아낸다. 메타데이터 Title을 우선 쓰고,
    없거나 파일명 찌꺼기(.pdf/.indd/.doc로 끝나는 등)로 보이면
    첫 페이지에서 4자보다 긴 첫 줄을 제목으로 간주한다.
    추출 실패 시 None을 반환하며 예외를 던지지 않는다.
    """
    if pdfplumber is None:
        return None

    try:
        with pdfplumber.open(file_path) as pdf:
            meta_title = (pdf.metadata or {}).get("Title")
            if meta_title and meta_title.strip() and not meta_title.strip().lower().endswith(
                (".pdf", ".indd", ".doc")
            ):
                return meta_title.strip()

            if pdf.pages:
                text = pdf.pages[0].extract_text() or ""
                for line in text.splitlines():
                    line = line.strip()
                    if len(line) > 4:
                        return line
    except Exception:
        return None

    return None


def _classify_api_error(e):
    """
    API 호출 실패 예외를 정규화된 error_type 문자열로 분류한다.
    핵심 목적: 429(rate limit)로 내려오지만 실제로는 크레딧(잔액) 부족인 경우를
    "rate_limit"과 구분해서 "insufficient_balance"로 표시하는 것 — 전자는 시간이
    지나면 재시도할 가치가 있지만, 후자는 재시도해도 해결되지 않는 상태이기 때문.
    """
    status_code = getattr(e, "status_code", None)
    message = str(e).lower()

    is_rate_limit = status_code == 429 or (
        anthropic is not None
        and isinstance(e, getattr(anthropic, "RateLimitError", ()))
    )
    if is_rate_limit:
        if "credit" in message or "balance" in message:
            return "insufficient_balance"
        return "rate_limit"

    if anthropic is not None and isinstance(e, getattr(anthropic, "AuthenticationError", ())):
        return "auth_error"
    if anthropic is not None and isinstance(e, getattr(anthropic, "APIConnectionError", ())):
        return "network_error"

    return f"api_error_{e.__class__.__name__}"


def classify_paper(title, abstract=None):
    """
    제목(및 선택적으로 초록 일부)을 Claude API로 분류한다.
    반환값: {"category": str, "confidence": "high"|"medium"|"low", "reason": str,
             "error_type": str|None}
    anthropic 패키지 미설치, API 호출 실패, 응답 형식 불일치 등 어떤 이유로든
    신뢰할 수 있는 결과를 얻지 못하면 예외를 던지지 않고 "미분류"로 안전하게 떨어진다.
    error_type은 "미분류"가 실제 분류 판단(확신 부족)인지 시스템/API 오류 때문인지
    구분하기 위한 값이고, 정상 분류 성공 시에는 None이다.
    """
    if anthropic is None:
        return {
            "category": "미분류",
            "confidence": "low",
            "reason": "anthropic 패키지 미설치",
            "error_type": "missing_dependency",
        }
    if SYSTEM_PROMPT is None:
        return {
            "category": "미분류",
            "confidence": "low",
            "reason": "categories.yaml 없음/읽기 실패 (categories.example.yaml을 복사해서 만드세요)",
            "error_type": "missing_config",
        }

    user_content = f"제목: {title}"
    if abstract:
        user_content += f"\n\n초록: {abstract}"

    try:
        # max_retries=0: SDK 기본 재시도(429 등에서 지수 백오프로 자동 재시도)를 끈다.
        # 잔액부족도 429로 내려올 때가 있는데, 이건 재시도해도 해결되지 않는 상태라서
        # 재시도로 시간만 날리지 말고 바로 실패를 기록하고 넘어간다.
        client = anthropic.Anthropic(max_retries=0)  # ANTHROPIC_API_KEY 환경변수를 사용
        response = client.messages.create(
            model=CLASSIFY_MODEL,
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        raw_text = response.content[0].text
    except Exception as e:
        return {
            "category": "미분류",
            "confidence": "low",
            "reason": f"API 호출 실패: {e.__class__.__name__}",
            "error_type": _classify_api_error(e),
        }

    return _parse_classification_response(raw_text)


def _parse_classification_response(raw_text):
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        # 모델이 "다른 텍스트를 덧붙이지 않는다"는 지시에도 ```json ... ``` 코드펜스로
        # 감싸서 응답할 때가 있다. 첫 '{'부터 마지막 '}'까지만 다시 잘라서 한 번 더 시도.
        data = None
        if isinstance(raw_text, str):
            start, end = raw_text.find("{"), raw_text.rfind("}")
            if start != -1 and end > start:
                try:
                    data = json.loads(raw_text[start : end + 1])
                except (json.JSONDecodeError, TypeError):
                    data = None
        if data is None:
            return {
                "category": "미분류",
                "confidence": "low",
                "reason": "응답 JSON 파싱 실패",
                "error_type": "parse_error",
            }

    category = data.get("category")
    confidence = data.get("confidence")
    reason = data.get("reason", "")

    if category not in VALID_CATEGORIES:
        return {
            "category": "미분류",
            "confidence": "low",
            "reason": f"알 수 없는 카테고리 응답: {category!r}",
            "error_type": "invalid_category",
        }
    if confidence not in VALID_CONFIDENCES:
        confidence = "low"

    return {"category": category, "confidence": confidence, "reason": reason, "error_type": None}


def move_to_category(file_path, category, root=None):
    """
    분류된 파일을 ARTICLE_COLLECTION_ROOT/<category>/ 로 실제로 옮긴다.
    "미분류"는 호출부에서 걸러내야 한다(여기서도 방어적으로 한 번 더 막음).

    반환값: (성공 여부, 최종 경로, 메시지)
      - 성공하면 최종 경로 = 이동된 새 경로
      - 실패/이동 안 함이면 최종 경로 = 원래 file_path (DB에는 원래 경로를 써야 하므로)

    안전장치:
      - category는 categories.yaml에서 읽은 VALID_CATEGORIES(고정 화이트리스트)에
        있어야만 한다 — 모델이나 파일명이 임의로 category 문자열을 주입해도
        폴더 경로를 벗어날 수 없다.
      - 그래도 목적지가 실제로 root 하위인지 os.path.commonpath로 한 번 더 검증한다.
      - 목적지에 동명 파일이 이미 있으면 덮어쓰지 않고 이동을 건너뛴다.
    """
    root = root or ARTICLE_COLLECTION_ROOT

    if category == "미분류" or category not in VALID_CATEGORIES:
        return False, file_path, f"이동 대상 아님(category={category!r})"

    root_abs = os.path.abspath(root)
    dest_dir = os.path.join(root_abs, category)

    if os.path.commonpath([root_abs, os.path.abspath(dest_dir)]) != root_abs:
        return False, file_path, "목적지가 아카이브 루트 밖임(차단)"

    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        return False, file_path, f"폴더 생성 실패: {e.__class__.__name__}"

    dest_path = os.path.join(dest_dir, os.path.basename(file_path))

    if os.path.abspath(dest_path) == os.path.abspath(file_path):
        return True, file_path, "이미 목적지에 있음"

    if os.path.exists(dest_path):
        return False, file_path, f"목적지에 동명 파일 이미 존재: {dest_path}"

    try:
        shutil.move(file_path, dest_path)
    except OSError as e:
        return False, file_path, f"이동 실패: {e.__class__.__name__}"

    return True, dest_path, None


def get_status(conn, filepath):
    """filepath(절대경로 기준)로 기존 행의 status를 조회한다. 행이 없으면 None."""
    abs_path = os.path.abspath(filepath)
    row = conn.execute("SELECT status FROM papers WHERE filepath = ?", (abs_path,)).fetchone()
    return row[0] if row else None


def mark_in_progress(conn, filename, filepath, file_hash):
    """
    API 호출 직전에 이 요청을 status=STATUS_IN_PROGRESS로 먼저 INSERT하고 즉시 커밋한다.
    분류 결과를 받기 전에 이 커밋이 먼저 끝나 있어야, 처리 도중 프로세스가 죽어도
    재시작 시 행 상태만 보고 "이거 처리하다 말았구나"를 알 수 있다(결과 저장과는
    별도 트랜잭션).
    반환값: 이 행의 id (나중에 mark_done에서 같은 행을 찾아 업데이트하는 데 씀).
    """
    cur = conn.execute(
        "INSERT INTO papers (filename, filepath, tags, file_hash, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (filename, os.path.abspath(filepath), get_tags(), file_hash, STATUS_IN_PROGRESS),
    )
    conn.commit()
    return cur.lastrowid


def mark_done(conn, row_id, filename, filepath, result):
    """
    분류 결과 저장과 status=STATUS_DONE 전환을 하나의 UPDATE(=하나의 커밋)로 같이 쓴다.
    상태 변경과 결과 저장을 분리하면 그 사이에 죽었을 때 "완료 표시는 됐는데 결과는
    없는" 혹은 그 반대의 일관성 깨진 행이 생길 수 있어서, 반드시 한 번에 같이 쓴다.
    """
    conn.execute(
        "UPDATE papers SET filename = ?, filepath = ?, category = ?, confidence = ?, "
        "reason = ?, error_type = ?, status = ? WHERE id = ?",
        (
            filename,
            filepath,
            result["category"],
            result["confidence"],
            result["reason"],
            result.get("error_type"),
            STATUS_DONE,
            row_id,
        ),
    )
    conn.commit()


def main():
    if len(sys.argv) != 2:
        print(f"사용법: {sys.argv[0]} <파일경로>", file=sys.stderr)
        sys.exit(1)

    # 인자로 들어온 경로를 절대경로로 정규화해서 저장한다. 정규화 없이 그대로 저장하면
    # 같은 파일도 상대/절대경로 표기가 다를 때 filepath UNIQUE 제약이 중복을 못 걸러낸다.
    file_path = os.path.abspath(sys.argv[1])
    filename = os.path.basename(file_path)

    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)

        status = get_status(conn, file_path)
        if status == STATUS_DONE:
            print(f"이미 처리됨, 건너뜀: {filename}")
            return
        if status == STATUS_IN_PROGRESS:
            # 다른 호출이 이미 처리 중이거나, 지난 번 처리 도중 죽어서 멈춰 있는 행이다.
            # 어느 쪽이든 여기서 또 API를 쏘지 않는다 — 같은 요청을 중복 전송하지 않기 위함.
            print(f"이미 처리 중(또는 중단됨), 건너뜀: {filename}")
            return

        # 파일 내용 해시는 경로가 바뀌어도(이동/재다운로드) 같은 파일인지 나중에
        # 대조할 수 있게 하기 위해 분류 전에 미리 계산해둔다.
        file_hash = compute_file_hash(file_path)

        # API 호출(비용 발생) 직전에 status='요청중'부터 커밋한다.
        row_id = mark_in_progress(conn, filename, file_path, file_hash)

        title = extract_title(file_path)
        if title:
            result = classify_paper(title)
        else:
            result = {
                "category": "미분류",
                "confidence": "low",
                "reason": "PDF에서 제목 추출 실패",
                "error_type": None,
            }

        # 실제 카테고리가 나온 경우에만 파일을 옮긴다. "미분류"는 원래 자리(Downloads 등)에 그대로 둔다.
        moved, final_path, move_msg = move_to_category(file_path, result["category"])
        if move_msg and result["category"] != "미분류":
            print(f"  (이동 알림: {move_msg})", file=sys.stderr)
        final_filename = os.path.basename(final_path)

        mark_done(conn, row_id, final_filename, final_path, result)

        where = f" → {final_path}" if moved and final_path != file_path else ""
        err_note = f" [error_type={result['error_type']}]" if result.get("error_type") else ""
        print(f"저장됨: {filename} (분류: {result['category']}, 근거: {result['reason']}){where}{err_note}")
    except sqlite3.IntegrityError:
        print("이미 등록된 파일")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
