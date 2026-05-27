from pathlib import Path

from evaluate_readiness import evaluate_mode, load_queries
from main import DATA_DIR, load_or_build_index

READINESS_THRESHOLD = 0.85
TOP_K = 5
PROJECT_ROOT = Path(__file__).resolve().parent.parent
QUERIES_PATH = PROJECT_ROOT / "config" / "eval_queries.json"


def rank_tuple(report):
    return (
        report["recall_at_k"],
        report["precision_at_k"],
        report["mrr"],
    )


def summarize(report):
    return (
        f"Recall@{TOP_K}={report['recall_at_k']:.3f}, "
        f"Precision@{TOP_K}={report['precision_at_k']:.3f}, "
        f"ReadinessAcc={report['readiness_accuracy']:.3f}, "
        f"MRR={report['mrr']:.3f}"
    )


def main():
    queries = load_queries(QUERIES_PATH)
    index = load_or_build_index(Path(DATA_DIR))

    single_modes = ["tfidf", "bm25", "dense"]
    pair_modes = [
        "hybrid_tfidf_bm25",
        "hybrid_bm25_dense",
        "hybrid_tfidf_dense",
    ]

    candidates = []

    print("=== SINGLE MODES ===")
    for mode in single_modes:
        report = evaluate_mode(index, queries, mode=mode, top_k=TOP_K, alpha=0.5)
        candidates.append((mode, "-", report))
        print(f"{mode}: {summarize(report)}")

    print("\n=== PAIR HYBRID BEST BY SWEEP ===")
    for mode in pair_modes:
        best_alpha = None
        best_report = None
        for i in range(1, 10):
            alpha = i / 10
            report = evaluate_mode(index, queries, mode=mode, top_k=TOP_K, alpha=alpha)
            if best_report is None or rank_tuple(report) > rank_tuple(best_report):
                best_report = report
                best_alpha = alpha
        candidates.append((mode, f"{best_alpha:.1f}", best_report))
        print(f"{mode} (best alpha={best_alpha:.1f}): {summarize(best_report)}")

    print("\n=== GATE + LEXICOGRAPHIC SELECTION ===")
    print(f"gate: ReadinessAcc >= {READINESS_THRESHOLD:.2f}")

    passed = [
        c for c in candidates if c[2]["readiness_accuracy"] >= READINESS_THRESHOLD
    ]
    if passed:
        selected = sorted(passed, key=lambda x: rank_tuple(x[2]), reverse=True)[0]
        print(f"passed candidates: {len(passed)} / {len(candidates)}")
    else:
        selected = sorted(
            candidates, key=lambda x: x[2]["readiness_accuracy"], reverse=True
        )[0]
        print("passed candidates: 0 (fallback to highest ReadinessAcc)")

    mode, alpha, report = selected
    print(f"selected mode={mode}, alpha={alpha}")
    print(f"selected metrics: {summarize(report)}")


if __name__ == "__main__":
    main()
