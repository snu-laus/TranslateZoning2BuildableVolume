import argparse
import json
from pathlib import Path
from typing import Dict, List

from main import DATA_DIR, load_or_build_index, retrieve


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUERIES_PATH = PROJECT_ROOT / "config" / "eval_queries.json"


def load_queries(path: Path) -> List[Dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("질의셋 파일은 리스트(JSON array)여야 합니다.")
    return payload


def _normalize(text: str) -> str:
    return "".join(text.split())


def _contains_any(chunk_text: str, chunk_source: str, tokens: List[str]) -> bool:
    text_norm = _normalize(chunk_text)
    source_norm = _normalize(chunk_source)
    for token in tokens:
        t = _normalize(token)
        if t and (t in text_norm or t in source_norm):
            return True
    return False


def _available_attachments(index) -> List[str]:
    names = []
    for chunk in index["chunks"]:
        if "별첨" in chunk.source or "별첨" in chunk.text:
            names.append(chunk.source)
            names.append(chunk.text)
    return names


def evaluate_mode(index, queries: List[Dict], mode: str, top_k: int, alpha: float):
    recall_sum = 0.0
    mrr_sum = 0.0
    readiness_correct = 0
    precision_sum = 0.0

    attachment_corpus = "\n".join(_available_attachments(index))
    attachment_corpus_norm = _normalize(attachment_corpus)

    for item in queries:
        required_items = item.get("required_items", [])
        required_attachments = item.get("required_attachments", [])
        gold_ready = item.get("gold_ready", True)
        gold_missing = item.get("gold_missing_items", [])

        results = retrieve(
            index,
            item["question"],
            top_k=top_k,
            mode=mode,
            alpha=alpha,
        )

        found_items = []
        first_hit_rank = 0
        for token in required_items:
            token_found = False
            for rank, chunk in enumerate(results, start=1):
                if _contains_any(chunk.text, chunk.source, [token]):
                    token_found = True
                    if first_hit_rank == 0:
                        first_hit_rank = rank
                    break
            if token_found:
                found_items.append(token)

        required_count = max(len(required_items), 1)
        recall_sum += len(found_items) / required_count
        if first_hit_rank > 0:
            mrr_sum += 1.0 / first_hit_rank

        missing_items = [token for token in required_items if token not in found_items]

        missing_attachments = []
        for attachment in required_attachments:
            if _normalize(attachment) not in attachment_corpus_norm:
                missing_attachments.append(attachment)

        pred_missing = missing_items + missing_attachments
        pred_ready = len(pred_missing) == 0

        if pred_ready == gold_ready:
            readiness_correct += 1

        supported_chunks = 0
        for chunk in results:
            if _contains_any(chunk.text, chunk.source, required_items):
                supported_chunks += 1
        denom = max(len(results), 1)
        precision_sum += supported_chunks / denom

    n = max(len(queries), 1)

    return {
        "mode": mode,
        "queries": len(queries),
        "recall_at_k": recall_sum / n,
        "precision_at_k": precision_sum / n,
        "mrr": mrr_sum / n,
        "readiness_accuracy": readiness_correct / n,
    }


def print_report(report: Dict, top_k: int):
    print(f"\n=== {report['mode']} ===")
    print(f"queries                    : {report['queries']}")
    print(f"Recall@{top_k}                 : {report['recall_at_k']:.3f}")
    print(f"Precision@{top_k}              : {report['precision_at_k']:.3f}")
    print(f"MRR                        : {report['mrr']:.3f}")
    print(f"Readiness Accuracy         : {report['readiness_accuracy']:.3f}")


def main():
    parser = argparse.ArgumentParser(description="법규 검토 준비도 중심 검색 평가")
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES_PATH))
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--modes",
        nargs="+",
        default=["tfidf", "bm25", "dense"],
        choices=[
            "tfidf",
            "bm25",
            "dense",
            "hybrid_tfidf_bm25",
            "hybrid_bm25_dense",
            "hybrid_tfidf_dense",
        ],
    )
    parser.add_argument("--alpha", type=float, default=0.5)
    args = parser.parse_args()

    queries = load_queries(Path(args.queries))
    index = load_or_build_index(Path(args.data_dir))

    print("준비도 중심 평가 시작")
    print(f"queries file               : {args.queries}")
    print(f"top-k                      : {args.top_k}")
    print(f"modes                      : {', '.join(args.modes)}")

    for mode in args.modes:
        report = evaluate_mode(index, queries, mode, args.top_k, args.alpha)
        print_report(report, args.top_k)


if __name__ == "__main__":
    main()
