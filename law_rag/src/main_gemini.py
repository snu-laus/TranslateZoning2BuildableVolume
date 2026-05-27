"""
Gemini 3 Flash 기반 RAG 파이프라인.

기존 main.py의 검색(TF-IDF/BM25/Dense) 로직을 그대로 재사용하고,
LLM 답변 생성만 Gemini 3 Flash API로 교체한 버전이다.

사용법:
    # 단일 질의
    python src/main_gemini.py ask "질문" --top-k 20 --retrieval-mode tfidf

    # 인덱싱만
    python src/main_gemini.py index
"""

import argparse
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import List

import requests
from dotenv import load_dotenv

# main.py 로직 재사용
from main import (
    DATA_DIR,
    OUTPUT_DIR,
    Chunk,
    build_index,
    load_or_build_index,
    retrieve,
    cmd_index,
)

load_dotenv()

# ── Gemini 3 Flash 설정 ─────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-3-flash-preview"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)


def build_prompt_gemini(question: str, contexts: List[Chunk]) -> str:
    """Gemini 전용 프롬프트 — 분량 제한 완화, 상세 답변 유도."""
    if not contexts:
        context_block = "(검색된 근거 없음)"
    else:
        parts = []
        for i, c in enumerate(contexts, start=1):
            parts.append(f"[근거 {i}] 파일: {c.source}\n{c.text}")
        context_block = "\n\n".join(parts)

    return f"""너는 건축 법규 전문 검토 도우미다.
반드시 아래 근거 문맥에 기반해서만 답해라.
근거가 충분하지 않으면 추측하지 말고 '제공된 자료에서 확답할 수 없습니다.'라고 답해라.

출력 규칙:
- 답변은 충분히 상세하게 작성하되, 불필요한 반복은 금지한다.
- 핵심 조문의 원문을 인용하되, 필요 시 항·호 단위로 정리한다.
- 적용 대상·수치·예외를 구분하여 체계적으로 서술한다.
- 관련 없는 근거는 사용하지 않는다.

답변 형식:
1) 요약 (3~5개 불릿)
2) 상세 근거
   - [법령/조문] 해당 내용 정리
3) 예외·특례 (해당 시)

[질문]
{question}

[근거 문맥]
{context_block}
""".strip()


def ask_gemini(
    prompt: str,
    max_tokens: int = 1024,
    temperature: float = 0.2,
    max_retries: int = 5,
) -> str:
    """Gemini 3 Flash에 프롬프트를 보내고 텍스트 답변을 반환한다."""
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY가 설정되지 않았습니다. .env 파일을 확인하세요."
        )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": temperature,
        },
        "systemInstruction": {
            "parts": [
                {
                    "text": (
                        "너는 건축 법규 검토 도우미다. "
                        "반드시 제공된 근거 문맥에 기반해서만 답하라. "
                        "근거가 충분하지 않으면 추측하지 말고 "
                        "'제공된 자료에서 확답할 수 없습니다.'라고 답하라."
                    )
                }
            ]
        },
    }

    backoff = 10
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                GEMINI_URL,
                params={"key": GEMINI_API_KEY},
                json=payload,
                timeout=120,
            )

            if resp.status_code == 429:
                wait = min(backoff * (2 ** (attempt - 1)), 120)
                print(
                    f"  ⏳ 429 Rate Limit — {wait}초 대기 후 재시도 ({attempt}/{max_retries})"
                )
                time.sleep(wait)
                continue

            resp.raise_for_status()
            data = resp.json()

            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts).strip()
                if text:
                    return text

            return (
                f"[Gemini 응답 파싱 실패] {json.dumps(data, ensure_ascii=False)[:500]}"
            )

        except requests.exceptions.Timeout:
            print(f"  ⏳ Timeout — 재시도 ({attempt}/{max_retries})")
            time.sleep(5)
        except requests.exceptions.RequestException as e:
            if attempt < max_retries:
                print(f"  ⚠️ 요청 오류: {e} — 재시도 ({attempt}/{max_retries})")
                time.sleep(5)
            else:
                return f"[Gemini API 오류] {e}"

    return "[Gemini 최대 재시도 초과]"


def _slugify_question(question: str, max_len: int = 40) -> str:
    cleaned = re.sub(r"\s+", "-", question.strip())
    cleaned = re.sub(r"[^0-9A-Za-z가-힣\-_]", "", cleaned)
    return cleaned[:max_len] if cleaned else "question"


def save_qa_log(args, contexts: List[Chunk], answer: str) -> Path:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify_question(args.question)
    filename = f"gem_{timestamp}_{slug}.json"
    output_path = output_dir / filename

    log = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "question": args.question,
        "answer": answer,
        "retrieval": {
            "mode": args.retrieval_mode,
            "alpha": (
                args.alpha
                if args.retrieval_mode
                in {
                    "hybrid",
                    "hybrid_tfidf_bm25",
                    "hybrid_bm25_dense",
                    "hybrid_tfidf_dense",
                }
                else None
            ),
            "top_k": args.top_k,
            "retrieved_count": len(contexts),
            "contexts": [
                {"rank": i + 1, "source": c.source, "text": c.text}
                for i, c in enumerate(contexts)
            ],
        },
        "llm": {
            "provider": "gemini",
            "model": GEMINI_MODEL,
            "max_tokens": args.max_tokens,
        },
    }

    output_path.write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return output_path


def cmd_ask_gemini(args):
    index = load_or_build_index(Path(args.data_dir))
    contexts = retrieve(
        index,
        args.question,
        top_k=args.top_k,
        mode=args.retrieval_mode,
        alpha=args.alpha,
    )

    print(f"\n=== 검색된 근거 ({len(contexts)}개) ===")
    if not contexts:
        print("검색 결과가 없습니다.")
    else:
        for i, c in enumerate(contexts, start=1):
            article_id = c.metadata.get("article_id", "")
            snippet = c.text[:120].replace("\n", " ")
            print(f"[{i}] {article_id} | {c.source[:60]}: {snippet}...")

    prompt = build_prompt_gemini(args.question, contexts)

    print(f"\n⏳ Gemini 3 Flash ({GEMINI_MODEL}) 호출 중...")
    answer = ask_gemini(prompt, max_tokens=args.max_tokens)

    print("\n=== 답변 (Gemini 3 Flash) ===")
    print(answer)

    if not args.no_save_json:
        saved_path = save_qa_log(args, contexts, answer)
        print(f"\nJSON 저장 완료: {saved_path}")


def main():
    parser = argparse.ArgumentParser(
        description="건축 법규 RAG — Gemini 3 Flash 백엔드"
    )
    sub = parser.add_subparsers(required=True)

    # index 커맨드 (main.py 재사용)
    p_index = sub.add_parser("index", help="data 폴더의 md 파일을 인덱싱")
    p_index.add_argument(
        "--data-dir", default=str(DATA_DIR), help="마크다운 데이터 폴더"
    )
    p_index.set_defaults(func=cmd_index)

    # ask 커맨드 (Gemini 백엔드)
    p_ask = sub.add_parser("ask", help="질문하고 Gemini 3 Flash 답변 받기")
    p_ask.add_argument("question", help="질문 문장")
    p_ask.add_argument("--data-dir", default=str(DATA_DIR), help="마크다운 데이터 폴더")
    p_ask.add_argument("--top-k", type=int, default=20, help="검색할 근거 개수")
    p_ask.add_argument(
        "--retrieval-mode",
        choices=[
            "tfidf",
            "bm25",
            "dense",
            "hybrid",
            "hybrid_tfidf_bm25",
            "hybrid_bm25_dense",
            "hybrid_tfidf_dense",
        ],
        default="tfidf",
        help="검색 방식",
    )
    p_ask.add_argument(
        "--alpha", type=float, default=0.5, help="하이브리드 가중치 (0~1)"
    )
    p_ask.add_argument(
        "--max-tokens", type=int, default=1024, help="LLM 최대 생성 토큰 수"
    )
    p_ask.add_argument(
        "--output-dir",
        default=str(OUTPUT_DIR),
        help="질문/답변 JSON 저장 폴더",
    )
    p_ask.add_argument(
        "--no-save-json",
        action="store_true",
        help="질문/답변 JSON 저장 비활성화",
    )
    p_ask.set_defaults(func=cmd_ask_gemini)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
