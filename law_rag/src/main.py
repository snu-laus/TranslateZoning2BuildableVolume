import argparse
import json
import math
import pickle
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List

import numpy as np
import requests
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.decomposition import TruncatedSVD


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CACHE_PATH = PROJECT_ROOT / "outputs" / "cache" / "index_cache.pkl"
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "qwen3.5:9b"
OUTPUT_DIR = PROJECT_ROOT / "outputs" / "qa_logs"


@dataclass
class Chunk:
    source: str
    text: str
    metadata: dict = field(default_factory=dict)


def load_markdown_files(data_dir: Path) -> List[tuple[str, str]]:
    files = sorted(data_dir.rglob("*.md"))
    documents = []
    for file in files:
        text = file.read_text(encoding="utf-8")
        documents.append((str(file.relative_to(data_dir)), text))
    return documents


# -- Sliding-window chunking (legacy, 비교 실험용) ----------------------------
def _chunk_text_sliding(
    source: str, text: str, chunk_size: int = 900, overlap: int = 120
) -> List[Chunk]:
    """고정 길이 슬라이딩 윈도우 청킹 (기존 방식)."""
    cleaned = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not cleaned:
        return []

    chunks: List[Chunk] = []
    start = 0
    while start < len(cleaned):
        end = min(len(cleaned), start + chunk_size)
        chunk = cleaned[start:end].strip()
        if chunk:
            chunks.append(Chunk(source=source, text=chunk))
        if end == len(cleaned):
            break
        start = max(end - overlap, start + 1)
    return chunks


# -- Structural chunking (조문 단위) -----------------------------------------
_ARTICLE_START_RE = re.compile(
    r"^\s*(?:#{1,6}\s+)?-?\s*(제\d+조(?:의\d+)?)(?!제)\s*(?:\(([^)]*)\))?",
    re.MULTILINE,
)
_ADDENDUM_START_RE = re.compile(r"^\s*부칙\s*[<\(]", re.MULTILINE)
_APPENDIX_INLINE_RE = re.compile(r"^\s*■\s*.*?\[별표\s*\d+", re.MULTILINE)
_PAGE_MARKER_RE = re.compile(r"\n*법제처\s+\d+\s+국가법령정보센터\n*")


def _strip_page_markers(text: str) -> str:
    """Remove '법제처 N 국가법령정보센터' page footers."""
    return _PAGE_MARKER_RE.sub("\n", text)


def _is_toc_entry(text: str) -> bool:
    """Return True if *text* is a TOC stub without substantive body."""
    body = re.sub(
        r"^(?:#{1,6}\s+)?(?:-?\s*)?제\d+조(?:의\d+)?\s*(?:\([^)]*\))?\s*",
        "",
        text,
        count=1,
    ).strip()
    if len(body) < 20:
        return True
    # No paragraph markers, no sentence endings, no numbered items → header/TOC
    has_para = any(c in body for c in "①②③④⑤⑥⑦⑧⑨⑩")
    has_sentence = bool(re.search(r"다\.", body))
    has_numbering = bool(re.search(r"^\s*-?\s*\d+\.", body, re.MULTILINE))
    if not has_para and not has_sentence and not has_numbering:
        return True
    return False


def _is_deleted_article(text: str) -> bool:
    """Detect deleted articles like '제83조 삭제 <1999. 4. 30.>'."""
    return "삭제" in text and len(text.strip()) < 80


def _extract_appendix_id(text: str) -> str:
    m = re.search(r"별표\s*(\d+)", text)
    return f"별표 {m.group(1)}" if m else "별표"


def chunk_law_text(source: str, text: str) -> List[Chunk]:
    """조문 단위 구조적 청킹 (Structural Chunking by Article).

    법규 마크다운 텍스트를 '제○조' 단위로 분할하고,
    삭제 조문·부칙·목차 항목을 필터링한다.
    """
    cleaned = _strip_page_markers(text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if not cleaned:
        return []

    # -- 별표 standalone file --------------------------------------------------
    if "별표" in source[:80] or "[별표" in source[:80]:
        return [
            Chunk(
                source=source,
                text=cleaned,
                metadata={
                    "article_id": _extract_appendix_id(source),
                    "article_title": "",
                    "section_type": "appendix",
                    "char_count": len(cleaned),
                },
            )
        ]

    # -- Collect split points --------------------------------------------------
    splits: List[tuple] = []  # (pos, article_id, title, section_type)

    for m in _ARTICLE_START_RE.finditer(cleaned):
        splits.append((m.start(), m.group(1), m.group(2) or "", "article"))

    for m in _ADDENDUM_START_RE.finditer(cleaned):
        splits.append((m.start(), "부칙", "", "addendum"))

    for m in _APPENDIX_INLINE_RE.finditer(cleaned):
        splits.append((m.start(), _extract_appendix_id(m.group(0)), "", "appendix"))

    splits.sort(key=lambda x: x[0])

    # De-duplicate positions within 5 chars
    if splits:
        deduped = [splits[0]]
        for s in splits[1:]:
            if s[0] - deduped[-1][0] > 5:
                deduped.append(s)
        splits = deduped

    # -- Extract chunks --------------------------------------------------------
    chunks: List[Chunk] = []

    for i, (pos, article_id, title, sec_type) in enumerate(splits):
        end = splits[i + 1][0] if i + 1 < len(splits) else len(cleaned)
        body = cleaned[pos:end].strip()
        if not body:
            continue

        # Filter: deleted articles
        if _is_deleted_article(body):
            continue

        # Filter: TOC stubs
        if sec_type == "article" and _is_toc_entry(body):
            continue

        # Filter: addendum (부칙) — historical amendment notes
        if sec_type == "addendum":
            continue

        chunks.append(
            Chunk(
                source=source,
                text=body,
                metadata={
                    "article_id": article_id,
                    "article_title": title,
                    "section_type": sec_type,
                    "char_count": len(body),
                },
            )
        )

    # -- Fallback: no chunks produced → single chunk --------------------------
    if not chunks and not splits:
        chunks.append(
            Chunk(
                source=source,
                text=cleaned,
                metadata={
                    "article_id": "",
                    "article_title": "",
                    "section_type": "unknown",
                    "char_count": len(cleaned),
                },
            )
        )

    return chunks


def build_index(data_dir: Path):
    docs = load_markdown_files(data_dir)
    all_chunks: List[Chunk] = []
    for source, text in docs:
        all_chunks.extend(chunk_law_text(source, text))

    if not all_chunks:
        raise ValueError("data/ 폴더에 .md 문서가 없습니다.")

    corpus = [chunk.text for chunk in all_chunks]
    chunk_text_no_space = [re.sub(r"\s+", "", chunk.text) for chunk in all_chunks]
    chunk_source_no_space = [re.sub(r"\s+", "", chunk.source) for chunk in all_chunks]

    vectorizer = TfidfVectorizer(ngram_range=(1, 2), min_df=1)
    matrix = vectorizer.fit_transform(corpus)

    n_features = matrix.shape[1]
    n_samples = matrix.shape[0]
    n_components = max(2, min(64, n_features - 1, n_samples - 1))
    dense_encoder = TruncatedSVD(n_components=n_components, n_iter=7, random_state=42)
    dense_matrix = dense_encoder.fit_transform(matrix).astype(np.float32)
    dense_matrix = np.nan_to_num(dense_matrix, nan=0.0, posinf=0.0, neginf=0.0)
    dense_norms = np.linalg.norm(dense_matrix, axis=1)
    dense_norms = np.maximum(dense_norms, 1e-6)

    count_vectorizer = CountVectorizer(ngram_range=(1, 2), min_df=1)
    tf_matrix = count_vectorizer.fit_transform(corpus).tocsr()
    doc_len = np.asarray(tf_matrix.sum(axis=1)).ravel().astype(np.float32)
    avgdl = float(doc_len.mean()) if len(doc_len) > 0 else 0.0
    df = np.asarray((tf_matrix > 0).sum(axis=0)).ravel().astype(np.float32)
    n_docs = tf_matrix.shape[0]
    idf = np.array(
        [math.log((n_docs - d + 0.5) / (d + 0.5) + 1.0) for d in df],
        dtype=np.float32,
    )

    payload = {
        "chunks": all_chunks,
        "tfidf_vectorizer": vectorizer,
        "tfidf_matrix": matrix,
        "bm25_vectorizer": count_vectorizer,
        "bm25_tf_matrix": tf_matrix,
        "bm25_doc_len": doc_len,
        "bm25_avgdl": avgdl,
        "bm25_idf": idf,
        "dense_encoder": dense_encoder,
        "dense_matrix": dense_matrix,
        "dense_norms": dense_norms,
        "chunk_text_no_space": chunk_text_no_space,
        "chunk_source_no_space": chunk_source_no_space,
    }
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CACHE_PATH.open("wb") as f:
        pickle.dump(payload, f)

    return payload


def load_or_build_index(data_dir: Path):
    if CACHE_PATH.exists():
        try:
            with CACHE_PATH.open("rb") as f:
                cached = pickle.load(f)
        except Exception:
            return build_index(data_dir)
        required_keys = {
            "chunks",
            "tfidf_vectorizer",
            "tfidf_matrix",
            "bm25_vectorizer",
            "bm25_tf_matrix",
            "bm25_doc_len",
            "bm25_avgdl",
            "bm25_idf",
            "dense_encoder",
            "dense_matrix",
            "dense_norms",
            "chunk_text_no_space",
            "chunk_source_no_space",
        }
        if required_keys.issubset(cached.keys()):
            return cached
    return build_index(data_dir)


def _bm25_scores(index, question: str, k1: float = 1.5, b: float = 0.75):
    q_vec = index["bm25_vectorizer"].transform([question])
    term_indices = q_vec.indices
    if len(term_indices) == 0:
        return np.zeros(index["bm25_tf_matrix"].shape[0], dtype=np.float32)

    tf_matrix = index["bm25_tf_matrix"]
    doc_len = index["bm25_doc_len"]
    avgdl = max(index["bm25_avgdl"], 1e-6)
    idf = index["bm25_idf"]

    scores = np.zeros(tf_matrix.shape[0], dtype=np.float32)
    norm = k1 * (1 - b + b * (doc_len / avgdl))
    for term_idx in term_indices:
        term_tf = tf_matrix[:, term_idx].toarray().ravel().astype(np.float32)
        numer = term_tf * (k1 + 1.0)
        denom = term_tf + norm
        scores += idf[term_idx] * (numer / np.maximum(denom, 1e-6))
    return scores


def _normalize(scores: np.ndarray) -> np.ndarray:
    if scores.size == 0:
        return scores
    s_min = float(scores.min())
    s_max = float(scores.max())
    if s_max - s_min < 1e-9:
        return np.zeros_like(scores)
    return (scores - s_min) / (s_max - s_min)


def _dense_scores(index, question: str) -> np.ndarray:
    q_tfidf = index["tfidf_vectorizer"].transform([question])
    q_dense = index["dense_encoder"].transform(q_tfidf).astype(np.float32)[0]
    q_norm = max(float(np.linalg.norm(q_dense)), 1e-6)
    sims = index["dense_matrix"].dot(q_dense) / (index["dense_norms"] * q_norm)
    return sims.astype(np.float32)


def _weighted_mix(score_a: np.ndarray, score_b: np.ndarray, alpha: float) -> np.ndarray:
    alpha = min(max(alpha, 0.0), 1.0)
    return alpha * _normalize(score_a) + (1 - alpha) * _normalize(score_b)


def _extract_query_signals(question: str) -> List[str]:
    signals = []
    article_tokens = re.findall(r"제\s*\d+\s*조", question)
    signals.extend([re.sub(r"\s+", "", token) for token in article_tokens])

    for keyword in ["일조", "채광", "정북", "인접대지경계선", "높이제한"]:
        if keyword in question:
            signals.append(keyword)

    unique = []
    for s in signals:
        if s not in unique:
            unique.append(s)
    return unique


def retrieve(
    index,
    question: str,
    top_k: int = 1,
    mode: str = "hybrid_tfidf_bm25",
    alpha: float = 0.5,
) -> List[Chunk]:
    q_vec = index["tfidf_vectorizer"].transform([question])
    tfidf_scores = cosine_similarity(q_vec, index["tfidf_matrix"])[0]
    bm25_scores = _bm25_scores(index, question)
    dense_scores = _dense_scores(index, question)

    if mode == "tfidf":
        scores = tfidf_scores
    elif mode == "bm25":
        scores = bm25_scores
    elif mode == "dense":
        scores = dense_scores
    elif mode in {"hybrid", "hybrid_tfidf_bm25"}:
        scores = _weighted_mix(tfidf_scores, bm25_scores, alpha)
    elif mode == "hybrid_bm25_dense":
        scores = _weighted_mix(bm25_scores, dense_scores, alpha)
    elif mode == "hybrid_tfidf_dense":
        scores = _weighted_mix(tfidf_scores, dense_scores, alpha)
    else:
        raise ValueError(f"지원하지 않는 검색 모드: {mode}")

    signals = _extract_query_signals(question)
    if signals:
        boosts = np.zeros_like(scores, dtype=np.float32)
        text_no_space_list = index["chunk_text_no_space"]
        source_no_space_list = index["chunk_source_no_space"]
        for i, chunk in enumerate(index["chunks"]):
            hit_count = sum(
                1
                for sig in signals
                if sig in chunk.text
                or sig in chunk.source
                or sig in text_no_space_list[i]
                or sig in source_no_space_list[i]
            )
            if hit_count > 0:
                boosts[i] = min(0.45, 0.15 * hit_count)
        scores = scores + boosts

    top_idx = scores.argsort()[::-1][:top_k]
    return [index["chunks"][i] for i in top_idx if scores[i] > 0]


def build_prompt(question: str, contexts: List[Chunk]) -> str:
    if not contexts:
        context_block = "(검색된 근거 없음)"
    else:
        parts = []
        for i, c in enumerate(contexts, start=1):
            parts.append(f"[근거 {i}] 파일: {c.source}\n{c.text}")
        context_block = "\n\n".join(parts)

    return f"""
너는 건축 법규 검토 도우미다.
반드시 아래 근거 문맥에 기반해서만 답해라.
근거가 충분하지 않으면 추측하지 말고 '제공된 자료에서 확답할 수 없습니다.'라고 답해라.
답변은 간결하게 작성하고, 불필요한 인용/장문 복붙을 금지한다.

출력 규칙:
- 전체 8줄 이내
- 핵심 답변 2~4개 불릿
- 근거는 최대 3개만 제시
- 각 근거는 '조문/항목 + 한 줄 요지'로만 작성
- 근거 원문을 길게 그대로 붙여넣지 말 것

답변 형식:
1) 요약
2) 근거
   - [법령/조문] 요지

[질문]
{question}

[근거 문맥]
{context_block}
""".strip()


def ask_ollama(prompt: str, model: str = MODEL_NAME, max_tokens: int = 320) -> str:
    response = requests.post(
        OLLAMA_URL,
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {"num_predict": max_tokens, "temperature": 0.2},
        },
        timeout=180,
    )
    response.raise_for_status()
    payload = response.json()
    answer = payload.get("response", "").strip()
    if answer:
        return answer

    thinking = payload.get("thinking", "").strip()
    if thinking:
        return thinking
    return ""


def _slugify_question(question: str, max_len: int = 40) -> str:
    cleaned = re.sub(r"\s+", "-", question.strip())
    cleaned = re.sub(r"[^0-9A-Za-z가-힣\-_]", "", cleaned)
    if not cleaned:
        return "question"
    return cleaned[:max_len]


def save_qa_log(args, contexts: List[Chunk], answer: str) -> Path:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify_question(args.question)
    filename = f"{timestamp}_{slug}.json"
    output_path = output_dir / filename

    payload = {
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
            "provider": "ollama",
            "model": args.model,
            "max_tokens": args.max_tokens,
        },
    }

    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return output_path


def cmd_index(args):
    payload = build_index(Path(args.data_dir))
    chunks = payload["chunks"]
    print(f"인덱싱 완료: {len(chunks)}개 청크")
    print(f"저장 파일: {CACHE_PATH}")

    # -- 청크 통계 ---------------------------------------------------------------
    if chunks:
        from collections import Counter

        lengths = [len(c.text) for c in chunks]
        print(f"\n=== 청크 통계 ===")
        print(f"총 청크 수          : {len(lengths)}")
        print(f"평균 길이 (자)       : {sum(lengths) / len(lengths):.0f}")
        print(f"최소 / 최대 길이     : {min(lengths)} / {max(lengths)}")

        type_counts = Counter(c.metadata.get("section_type", "?") for c in chunks)
        print(f"section_type 분포   : {dict(type_counts)}")

        key_articles = [
            "제86조",
            "제82조",
            "제80조의2",
            "제58조",
            "제30조",
            "제33조",
            "제35조",
        ]
        print(f"\n=== 핵심 조문 확인 ===")
        for art in key_articles:
            matched = [c for c in chunks if c.metadata.get("article_id") == art]
            total_chars = sum(len(c.text) for c in matched)
            sources = set(c.source for c in matched)
            print(
                f"{art}: {len(matched)}개 청크, "
                f"총 {total_chars}자, "
                f"출처 {sources if matched else '-'}"
            )


def cmd_ask(args):
    index = load_or_build_index(Path(args.data_dir))
    contexts = retrieve(
        index,
        args.question,
        top_k=args.top_k,
        mode=args.retrieval_mode,
        alpha=args.alpha,
    )

    print("\n=== 검색된 근거 ===")
    if not contexts:
        print("검색 결과가 없습니다.")
    else:
        for i, c in enumerate(contexts, start=1):
            snippet = c.text[:160].replace("\n", " ")
            print(f"[{i}] {c.source}: {snippet}...")

    prompt = build_prompt(args.question, contexts)
    answer = ask_ollama(prompt, model=args.model, max_tokens=args.max_tokens)

    print("\n=== 답변 ===")
    print(answer)

    if not args.no_save_json:
        saved_path = save_qa_log(args, contexts, answer)
        print(f"\nJSON 저장 완료: {saved_path}")


def main():
    parser = argparse.ArgumentParser(
        description="간단한 건축 법규 RAG (Markdown + Ollama)"
    )
    sub = parser.add_subparsers(required=True)

    p_index = sub.add_parser("index", help="data 폴더의 md 파일을 인덱싱")
    p_index.add_argument(
        "--data-dir", default=str(DATA_DIR), help="마크다운 데이터 폴더"
    )
    p_index.set_defaults(func=cmd_index)

    p_ask = sub.add_parser("ask", help="질문하고 근거 기반 답변 받기")
    p_ask.add_argument("question", help="질문 문장")
    p_ask.add_argument("--data-dir", default=str(DATA_DIR), help="마크다운 데이터 폴더")
    p_ask.add_argument("--top-k", type=int, default=1, help="검색할 근거 개수")
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
        help="검색 방식: 단일(tfidf/bm25/dense), 하이브리드(hybrid_*). hybrid는 tfidf+bm25 별칭",
    )
    p_ask.add_argument(
        "--alpha",
        type=float,
        default=0.5,
        help="하이브리드일 때 첫 번째 검색기의 가중치(0~1)",
    )
    p_ask.add_argument("--model", default=MODEL_NAME, help="Ollama 모델명")
    p_ask.add_argument(
        "--max-tokens", type=int, default=320, help="LLM 최대 생성 토큰 수"
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
    p_ask.set_defaults(func=cmd_ask)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
