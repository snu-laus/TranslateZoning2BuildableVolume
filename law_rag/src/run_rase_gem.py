"""
Gemini RAG 결과(gem_*.json)를 입력으로 RASE JSON을 생성한다.

사용법:
    python src/run_rase_gem.py
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
QA_DIR = PROJECT_ROOT / "outputs" / "qa_logs"
OUTPUT_DIR = PROJECT_ROOT / "outputs" / "rase_outputs_gem"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3-flash-preview:generateContent"
)
MAX_TOKENS = 8192

TARGET_LABELS = [
    "Q0_포괄질의",
    "Q1_일조권사선제한",
    "Q2_이격거리",
    "Q3_높이제한",
]

SYSTEM_MSG = (
    "너는 건축 법규 RASE 구조화 시스템이다. "
    "입력된 법규 조문을 RASE(Requirement, Applicability, Selection, Exception) JSON으로 변환하라. "
    "JSON 외 텍스트 출력 금지."
)

FEW_SHOT = '{"articles":[{"article_id":"제86조","article_title":"일조 등의 확보를 위한 건축물의 높이 제한","source":"건축법 시행령","R":{"description":"정북방향 인접 대지의 일조권 확보를 위해 건축물 높이를 제한"},"A":{"applicable_zones":["전용주거지역","일반주거지역"],"applicable_buildings":["모든 건축물"],"description":"전용·일반주거지역 건축물"},"S":{"conditions":[{"height_range":"9m 이하","rule":"1.5m 이상 이격","reference_point":"정북방향 인접 대지경계선"},{"height_range":"9m 초과","rule":"높이의 1/2 이상 이격","reference_point":"정북방향 인접 대지경계선"}],"description":"높이별 이격거리"},"E":{"exceptions":[{"condition":"건축협정구역 내 공동주택","modified_rule":"20% 완화","source":"시행령 제110조의7"}],"description":"건축협정 시 완화"}}]}'

RASE_PROMPT = """[질의] {question}

[법규 조문]
{contexts}

위 법규 조문 중 질의와 관련된 것만 골라 RASE JSON으로 구조화하라.
부칙·개정이력은 무시하라. 출력 형식 예시:
{fewshot}"""


def find_latest_gem_inputs() -> list:
    files = sorted(QA_DIR.glob("gem_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    by_label = {}

    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        label = data.get("query_label")
        if label in TARGET_LABELS and label not in by_label:
            by_label[label] = path
        if len(by_label) == len(TARGET_LABELS):
            break

    results = []
    for label in TARGET_LABELS:
        if label in by_label:
            results.append((label, by_label[label]))
    return results


def ask_gemini_json(prompt: str) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY 환경변수가 설정되지 않았습니다.")

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
            wait = 2**attempt * 10
            print(f"   429 Rate Limit, {wait}초 대기 후 재시도")
            time.sleep(wait)
            continue

        resp.raise_for_status()
        payload = resp.json()
        try:
            return payload["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError):
            return ""

    raise RuntimeError("Gemini API 429 에러 5회 연속")


def validate_rase_schema(obj) -> list:
    articles = None
    if isinstance(obj, list):
        articles = obj
    elif isinstance(obj, dict):
        if "articles" in obj and isinstance(obj["articles"], list):
            articles = obj["articles"]
        elif "article_id" in obj:
            articles = [obj]
        else:
            for v in obj.values():
                if isinstance(v, list) and v:
                    articles = v
                    break

    if not articles:
        return []

    required = {"article_id", "R", "S"}
    return [a for a in articles if isinstance(a, dict) and required.issubset(a.keys())]


def extract_contexts_text(data: dict, max_contexts: int = 10) -> str:
    contexts = data["retrieval"]["contexts"][:max_contexts]
    parts = []
    for ctx in contexts:
        src = ctx.get("source", "")[:80]
        parts.append(f"--- [rank {ctx.get('rank', '?')}] {src} ---\n{ctx.get('text', '')}")
    return "\n\n".join(parts)


def extract_json_from_response(response: str):
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        pass

    m = re.search(r"```(?:json)?\s*(\[.*?\]|\{.*?\})\s*```", response, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    for op, cp in [("[", "]"), ("{", "}")]:
        try:
            s = response.index(op)
            e = response.rindex(cp) + 1
            return json.loads(response[s:e])
        except (ValueError, json.JSONDecodeError):
            continue

    return None


def to_output_label(query_label: str) -> str:
    mapping = {
        "Q0_포괄질의": "Q0_포괄질의_BuildingEnvelope",
        "Q1_일조권사선제한": "Q1_일조권사선제한",
        "Q2_이격거리": "Q2_이격거리",
        "Q3_높이제한": "Q3_높이제한",
    }
    return mapping.get(query_label, query_label)


def main():
    inputs = find_latest_gem_inputs()
    if not inputs:
        raise RuntimeError("gem_*.json 입력 파일을 찾지 못했습니다.")

    print("입력 파일:")
    for label, path in inputs:
        print(f" - {label}: {path.name}")

    all_results = {}

    for query_label, filepath in inputs:
        raw_data = json.loads(filepath.read_text(encoding="utf-8"))
        question = raw_data["question"]
        contexts_text = extract_contexts_text(raw_data)
        out_label = to_output_label(query_label)

        print(f"\n{'=' * 60}")
        print(f"{out_label}: {question[:70]}...")
        print(f"contexts 길이: {len(contexts_text)}")

        prompt = RASE_PROMPT.format(
            question=question,
            contexts=contexts_text,
            fewshot=FEW_SHOT,
        )

        validated = []
        raw = ""
        for attempt in range(1, 4):
            raw = ask_gemini_json(prompt)
            parsed = extract_json_from_response(raw)
            if parsed is not None:
                validated = validate_rase_schema(parsed)
            if validated:
                break
            print(f"   시도 {attempt}/3 실패")

        if validated:
            out_path = OUTPUT_DIR / f"{out_label}_rase.json"
            out_path.write_text(
                json.dumps(validated, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            all_results[out_label] = validated
            print(f"   완료: {len(validated)}개 조문 -> {out_path.name}")
        else:
            fail_path = OUTPUT_DIR / f"{out_label}_raw.txt"
            fail_path.write_text(raw, encoding="utf-8")
            all_results[out_label] = {"error": "parse_failed", "raw": raw[:200]}
            print(f"   실패: raw 저장 -> {fail_path.name}")

    all_path = OUTPUT_DIR / "all_rase.json"
    all_path.write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n전체 저장: {all_path}")


if __name__ == "__main__":
    main()
