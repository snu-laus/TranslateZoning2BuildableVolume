"""
Gemini 3 Flash — Q0~Q4 배치 실행 스크립트.

사용법:
    python src/run_gemini_batch.py
"""

import json
import sys
import time
from pathlib import Path

# src/ 디렉토리를 path에 추가
sys.path.insert(0, str(Path(__file__).resolve().parent))

from main import DATA_DIR, OUTPUT_DIR, load_or_build_index, retrieve
from main_gemini import (
    GEMINI_MODEL,
    ask_gemini,
    build_prompt_gemini,
    _slugify_question,
)
from datetime import datetime

# ── Q0~Q4 질의 정의 ─────────────────────────────────────────────────────────
QUERIES = [
    (
        "Q0_포괄질의",
        "서울시 일반주거지역의 특정 필지에서 특정 용도의 건축물을 신축할 때, "
        "해당 필지의 3차원 건축가능 영역(Building Envelope)을 결정하기 위해 "
        "적용해야 하는 법적 제한 조건을 모두 나열하고, 각 조건의 근거 법령·조례 "
        "조문을 원문과 함께 제시하라.",
    ),
    (
        "Q1_일조권사선제한",
        "건축법 시행령 제86조에 규정된 정북방향 일조권 사선제한에 대해, "
        "(1) 적용 대상 용도지역 및 건축물 유형, "
        "(2) 이격거리 기산점·방향·높이 구간별 산정 방식, "
        "(3) 공동주택 등 예외 조항을 관련 법령 원문과 함께 제시하라.",
    ),
    (
        "Q2_이격거리",
        "건축법 제58조 및 시행령 제80조의2, 서울특별시 건축조례 제30조 및 "
        "별표 4에 규정된 인접대지경계선으로부터의 건축물 이격거리(대지 안의 공지)에 대해, "
        "(1) 용도지역·건축물 용도별 적용 수치, "
        "(2) 건축선으로부터의 이격 기준, "
        "(3) 예외 조항을 관련 원문과 함께 제시하라.",
    ),
    (
        "Q3_높이제한",
        "건축법 제60조 및 시행령 제82조, 서울특별시 건축조례 제33조에 규정된 "
        "가로구역별 건축물 높이 제한에 대해, "
        "(1) 일반주거지역(제1·2·3종)에서의 적용 기준, "
        "(2) 높이 산정 방법, "
        "(3) 완화 조건을 관련 원문과 함께 제시하라.",
    ),
    (
        "Q4_건폐율용적률",
        "서울시 일반주거지역(제1·2·3종)에서 적용되는 건폐율, 용적률, "
        "절대높이 제한에 대해 종별 수치와 건축물 용도에 따른 차이를 "
        "법령 원문과 함께 제시하라.",
    ),
]

TOP_K = 20
RETRIEVAL_MODE = "tfidf"
MAX_TOKENS = 4096
INTER_QUERY_DELAY = 5  # 쿼리 간 대기 (초) — rate limit 회피


def main():
    print("=" * 60)
    print("Gemini 3 Flash RAG — Q0~Q4 배치 실행")
    print(f"모델: {GEMINI_MODEL} | 검색: {RETRIEVAL_MODE} | top_k: {TOP_K}")
    print("=" * 60)

    # 인덱스 로드
    index = load_or_build_index(DATA_DIR)
    print(f"인덱스 로드 완료: {len(index['chunks'])}개 청크\n")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results_summary = []

    for i, (label, question) in enumerate(QUERIES):
        print(f"\n{'─' * 60}")
        print(f"[{label}] ({i+1}/{len(QUERIES)})")
        print(f"질의: {question[:80]}...")
        print(f"{'─' * 60}")

        # 1) 검색
        contexts = retrieve(index, question, top_k=TOP_K, mode=RETRIEVAL_MODE)
        print(f"  검색 결과: {len(contexts)}개 청크")
        for j, c in enumerate(contexts[:5], start=1):
            aid = c.metadata.get("article_id", "")
            print(f"    [{j}] {aid} | {c.source[:50]}")

        # 2) Gemini 호출
        prompt = build_prompt_gemini(question, contexts)
        print(f"\n  ⏳ Gemini 호출 중...")
        answer = ask_gemini(prompt, max_tokens=MAX_TOKENS)
        print(f"  ✅ 답변 수신 ({len(answer)}자)")

        # 3) 답변 미리보기
        preview = answer[:300].replace("\n", "\n    ")
        print(f"\n    {preview}...")

        # 4) JSON 저장 (gem_ 접두사)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = _slugify_question(question)
        filename = f"gem_{timestamp}_{slug}.json"
        output_path = OUTPUT_DIR / filename

        log = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "query_label": label,
            "question": question,
            "answer": answer,
            "retrieval": {
                "mode": RETRIEVAL_MODE,
                "top_k": TOP_K,
                "retrieved_count": len(contexts),
                "contexts": [
                    {
                        "rank": j + 1,
                        "source": c.source,
                        "article_id": c.metadata.get("article_id", ""),
                        "text": c.text,
                    }
                    for j, c in enumerate(contexts)
                ],
            },
            "llm": {
                "provider": "gemini",
                "model": GEMINI_MODEL,
                "max_tokens": MAX_TOKENS,
            },
        }

        output_path.write_text(
            json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n  💾 저장: {output_path.name}")
        results_summary.append((label, len(answer), output_path.name))

        # rate limit 회피
        if i < len(QUERIES) - 1:
            print(f"\n  ⏳ {INTER_QUERY_DELAY}초 대기...")
            time.sleep(INTER_QUERY_DELAY)

    # 최종 요약
    print(f"\n\n{'=' * 60}")
    print("실행 완료 요약")
    print(f"{'=' * 60}")
    for label, ans_len, fname in results_summary:
        print(f"  {label:20s} | {ans_len:5d}자 | {fname}")
    print(f"\n저장 폴더: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
