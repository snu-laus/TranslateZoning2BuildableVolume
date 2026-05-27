"""
2단계 RASE 구조화 스크립트.
Q0~Q3 retrieval JSON의 contexts를 LLM에 전달하여 RASE JSON을 생성한다.

사용법:
    python src/run_rase.py
"""

import json
import os
import re
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "outputs" / "rase_outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Gemini 3 Flash 설정 ──
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3-flash-preview:generateContent"
)
MAX_TOKENS = 8192

# ── Q0~Q3 JSON 파일 경로 ──
Q_FILES = [
    (
        "Q0_포괄질의_BuildingEnvelope",
        PROJECT_ROOT
        / "outputs"
        / "qa_logs"
        / "20260311_162155_서울시-일반주거지역의-특정-필지에서-특정-용도의-건축물을-신축할-때-해당.json",
    ),
    (
        "Q1_일조권사선제한",
        PROJECT_ROOT
        / "outputs"
        / "qa_logs"
        / "20260311_162338_건축법-시행령-제86조에-규정된-정북방향-일조권-사선제한에-대해-1-적용.json",
    ),
    (
        "Q2_이격거리",
        PROJECT_ROOT
        / "outputs"
        / "qa_logs"
        / "20260311_162519_건축법-제58조-및-시행령-제80조의2-서울특별시-건축조례-제30조-및-.json",
    ),
    (
        "Q3_높이제한",
        PROJECT_ROOT
        / "outputs"
        / "qa_logs"
        / "20260311_162705_건축법-제60조-및-시행령-제82조-서울특별시-건축조례-제33조에-규정된.json",
    ),
]

SYSTEM_MSG = (
    "너는 건축 법규 RASE 구조화 시스템이다. "
    "입력된 법규 조문을 RASE(Requirement, Applicability, Selection, Exception) JSON으로 변환하라. "
    "JSON 외 텍스트 출력 금지."
)

# ── 간결한 Few-shot 예시 ──
FEW_SHOT = '{"articles":[{"article_id":"제86조","article_title":"일조 등의 확보를 위한 건축물의 높이 제한","source":"건축법 시행령","R":{"description":"정북방향 인접 대지의 일조권 확보를 위해 건축물 높이를 제한"},"A":{"applicable_zones":["전용주거지역","일반주거지역"],"applicable_buildings":["모든 건축물"],"description":"전용·일반주거지역 건축물"},"S":{"conditions":[{"height_range":"9m 이하","rule":"1.5m 이상 이격","reference_point":"정북방향 인접 대지경계선"},{"height_range":"9m 초과","rule":"높이의 1/2 이상 이격","reference_point":"정북방향 인접 대지경계선"}],"description":"높이별 이격거리"},"E":{"exceptions":[{"condition":"건축협정구역 내 공동주택","modified_rule":"20% 완화","source":"시행령 제110조의7"}],"description":"건축협정 시 완화"}}]}'

RASE_PROMPT = """[질의] {question}

[법규 조문]
{contexts}

위 법규 조문 중 질의와 관련된 것만 골라 RASE JSON으로 구조화하라.
부칙·개정이력은 무시하라. 출력 형식 예시:
{fewshot}"""


def ask_gemini_json(prompt: str) -> str:
    """Gemini 3 Flash REST API — responseMimeType: application/json, 429 backoff."""
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY 환경변수가 설정되지 않았습니다. .env 파일을 확인하세요."
        )
    for attempt in range(5):
        resp = requests.post(
            f"{GEMINI_URL}?key={GEMINI_API_KEY}",
            headers={"Content-Type": "application/json"},
            json={
                "systemInstruction": {"parts": [{"text": SYSTEM_MSG}]},
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.2,
                    "maxOutputTokens": MAX_TOKENS,
                    "responseMimeType": "application/json",
                },
            },
            timeout=120,
        )
        if resp.status_code == 429:
            wait = 2**attempt * 10  # 10, 20, 40, 80, 160초
            print(f"   ⏳ 429 Rate Limit — {wait}초 대기 후 재시도...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        payload = resp.json()
        try:
            return payload["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError):
            print(
                f"   ⚠️  Gemini 응답 구조 이상: {json.dumps(payload, ensure_ascii=False)[:300]}"
            )
            return ""
    raise RuntimeError("Gemini API 429 에러 5회 연속 — rate limit 초과")


def validate_rase_schema(obj) -> list:
    """RASE JSON 스키마 검증. articles 배열을 반환, 실패 시 빈 리스트."""
    articles = None
    if isinstance(obj, list):
        articles = obj
    elif isinstance(obj, dict):
        # "articles" 키 또는 배열 값을 가진 첫 키
        if "articles" in obj and isinstance(obj["articles"], list):
            articles = obj["articles"]
        else:
            for v in obj.values():
                if isinstance(v, list) and len(v) > 0:
                    articles = v
                    break
            if articles is None:
                # 단일 조문 객체일 수 있음
                if "article_id" in obj:
                    articles = [obj]
    if not articles:
        return []
    # 각 원소가 dict이고 필수 키가 있는지 확인
    required = {"article_id", "R", "S"}
    valid = [a for a in articles if isinstance(a, dict) and required.issubset(a.keys())]
    return valid


def extract_contexts_text(data: dict, max_contexts: int = 10) -> str:
    """retrieval JSON에서 상위 N개 contexts를 텍스트로 추출."""
    contexts = data["retrieval"]["contexts"][:max_contexts]
    parts = []
    for ctx in contexts:
        src = ctx["source"][:60]
        parts.append(f"--- [rank {ctx['rank']}] {src} ---\n{ctx['text']}")
    return "\n\n".join(parts)


def extract_json_from_response(response: str):
    """LLM 응답에서 JSON 배열/객체를 추출."""
    # format: json 이면 전체가 JSON일 가능성 높음
    try:
        obj = json.loads(response)
        return obj
    except json.JSONDecodeError:
        pass
    # try ```json block
    m = re.search(r"```(?:json)?\s*(\[.*?\]|\{.*?\})\s*```", response, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # try raw [ ] or { }
    for o, c in [("[", "]"), ("{", "}")]:
        try:
            s = response.index(o)
            e = response.rindex(c) + 1
            return json.loads(response[s:e])
        except (ValueError, json.JSONDecodeError):
            continue
    return None


def main():
    all_results = {}

    for q_label, filepath in Q_FILES:
        # 이미 성공한 결과가 있으면 스킵
        rase_path = OUTPUT_DIR / f"{q_label}_rase.json"
        if rase_path.exists():
            print(f"\n⏭️  {q_label}: 이미 존재 — 스킵 ({rase_path.name})")
            with open(rase_path, encoding="utf-8") as f:
                all_results[q_label] = json.load(f)
            continue

        if not filepath.exists():
            print(f"⚠️  파일 없음: {filepath}")
            continue

        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)

        question = data["question"]
        contexts_text = extract_contexts_text(data)

        print(f"\n{'='*60}")
        print(f"{q_label}: {question[:60]}...")
        print(f"컨텍스트: 상위 10개, {len(contexts_text)}자")

        prompt = RASE_PROMPT.format(
            question=question, contexts=contexts_text, fewshot=FEW_SHOT
        )

        # 최대 3회 재시도
        MAX_RETRY = 3
        validated = []
        raw = ""
        for attempt in range(1, MAX_RETRY + 1):
            raw = ask_gemini_json(prompt)
            parsed = extract_json_from_response(raw)
            if parsed is not None:
                validated = validate_rase_schema(parsed)
            if validated:
                break
            print(f"   ⚠️  시도 {attempt}/{MAX_RETRY}: 스키마 검증 실패")
            print(f"      raw 앞 200자: {raw[:200]}")

        if validated:
            print(f"✅ {len(validated)}개 조문 구조화 완료")
            for item in validated:
                if isinstance(item, dict):
                    print(
                        f"   - {item.get('article_id', '?')} "
                        f"({item.get('source', '?')})"
                    )
                else:
                    print(f"   - (비정상 원소: {str(item)[:60]})")
            all_results[q_label] = validated

            # 개별 저장
            out = OUTPUT_DIR / f"{q_label}_rase.json"
            out.write_text(
                json.dumps(validated, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"   저장: {out}")
        else:
            print(f"❌ JSON 파싱 실패 — raw 텍스트 저장")
            out = OUTPUT_DIR / f"{q_label}_raw.txt"
            out.write_text(raw, encoding="utf-8")
            all_results[q_label] = {"error": "parse_failed", "raw": raw[:200]}

    # ── 통합 저장 ──
    all_path = OUTPUT_DIR / "all_rase.json"
    all_path.write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n{'='*60}")
    print(f"전체 결과: {all_path}")


if __name__ == "__main__":
    main()
