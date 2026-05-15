"""
Load ground truth from export/{problem_id}/ground_truth.json and predictions from
results/{rclagent,mabc,sre_agent}/{problem_id}.json. Compute success and accuracy (for localization);
output a summary table and optional plots.
"""
import json
from pathlib import Path


def _is_subset(ground_list: list, pred_list: list) -> bool:
    if not ground_list:
        return True
    return all(g in pred_list for g in ground_list)


def _is_exact_match(ground: str | list, pred: str | list) -> bool:
    if isinstance(ground, list):
        if not isinstance(pred, list):
            return False
        return set(ground) == set(pred)
    return str(ground).strip().lower() == str(pred).strip().lower()


def evaluate_one(ground_truth: dict, prediction: dict, task_type: str) -> dict:
    """
    prediction: dict with predicted_service (str or list) or predicted_solution.
    Returns dict with success, accuracy (for localization), match_type.
    """
    faulty = ground_truth.get("faulty_service")
    expected_detection = ground_truth.get("expected_detection", "Yes" if faulty else "No")

    pred_svc = prediction.get("predicted_service") or prediction.get("predicted_solution")
    if pred_svc is None:
        return {"success": False, "accuracy": 0.0, "match_type": "no_prediction"}

    if task_type == "detection":
        ok = _is_exact_match(expected_detection, str(pred_svc))
        return {"success": ok, "accuracy": 100.0 if ok else 0.0, "match_type": "exact" if ok else "wrong"}

    if task_type == "localization":
        if isinstance(pred_svc, str):
            pred_list = [s.strip() for s in pred_svc.replace(",", " ").split() if s.strip()]
        elif isinstance(pred_svc, list):
            pred_list = list(pred_svc)
        else:
            pred_list = [str(pred_svc)]
        ground_list = [faulty] if faulty else []
        exact = _is_exact_match(ground_list, pred_list)
        subset = _is_subset(ground_list, pred_list)
        if exact:
            acc = 100.0
            match_type = "exact"
        elif subset and pred_list:
            acc = (len(ground_list) / len(pred_list)) * 100.0
            match_type = "subset"
        else:
            acc = 0.0
            match_type = "wrong"
        success = exact or (subset and len(pred_list) == 1)
        return {"success": success, "accuracy": acc, "match_type": match_type}

    return {"success": bool(pred_svc), "accuracy": 0.0, "match_type": "unknown"}


def run_comparison(
    export_root: Path,
    results_roots: dict[str, Path],
    output_json: Path | None = None,
    output_table: bool = True,
) -> list[dict]:
    """
    export_root: path to export/ (contains problem_id/ground_truth.json).
    results_roots: {"rclagent": Path, "mabc": Path, "sre_agent": Path} (each contains {problem_id}.json).
    Returns list of rows for table; optionally writes output_json and prints table.
    """
    export_root = Path(export_root)
    rows = []
    for problem_dir in sorted(export_root.iterdir()):
        if not problem_dir.is_dir():
            continue
        problem_id = problem_dir.name
        gt_path = problem_dir / "ground_truth.json"
        if not gt_path.exists():
            continue
        with open(gt_path) as f:
            gt = json.load(f)
        task_type = gt.get("task_type", "unknown")

        for agent_name, res_root in results_roots.items():
            res_path = Path(res_root) / f"{problem_id}.json"
            if not res_path.exists():
                rows.append({
                    "problem_id": problem_id,
                    "agent": agent_name,
                    "task_type": task_type,
                    "success": False,
                    "accuracy": 0.0,
                    "match_type": "no_result",
                })
                continue
            with open(res_path) as f:
                pred = json.load(f)
            ev = evaluate_one(gt, pred, task_type)
            rows.append({
                "problem_id": problem_id,
                "agent": agent_name,
                "task_type": task_type,
                "success": ev["success"],
                "accuracy": ev["accuracy"],
                "match_type": ev["match_type"],
            })

    if output_table and rows:
        # Print table
        print(f"{'problem_id':<45} {'agent':<12} {'task_type':<12} {'success':<8} {'accuracy':<8} {'match_type':<10}")
        print("-" * 100)
        for r in rows:
            print(f"{r['problem_id']:<45} {r['agent']:<12} {r['task_type']:<12} {str(r['success']):<8} {r['accuracy']:<8.1f} {r['match_type']:<10}")
        # Summary by agent
        from collections import defaultdict
        by_agent = defaultdict(lambda: {"success": 0, "total": 0, "accuracy_sum": 0.0})
        for r in rows:
            by_agent[r["agent"]]["total"] += 1
            if r["success"]:
                by_agent[r["agent"]]["success"] += 1
            by_agent[r["agent"]]["accuracy_sum"] += r["accuracy"]
        print("\nSummary by agent:")
        for agent, v in sorted(by_agent.items()):
            n = v["total"]
            print(f"  {agent}: success={v['success']}/{n}, accuracy_avg={v['accuracy_sum']/n:.1f}%")

    if output_json:
        with open(output_json, "w") as f:
            json.dump(rows, f, indent=2)
    return rows


def main():
    import argparse
    p = argparse.ArgumentParser(description="Compare external agent results to ground truth")
    p.add_argument("--export-root", type=Path, default=Path(__file__).resolve().parent / "export")
    p.add_argument("--results-dir", type=Path, default=Path(__file__).resolve().parent / "results",
                   help="Parent dir containing rclagent/, mabc/, sre_agent/")
    p.add_argument("--output", type=Path, default=None)
    args = p.parse_args()
    results_roots = {
        "rclagent": args.results_dir / "rclagent",
        "mabc": args.results_dir / "mabc",
        "sre_agent": args.results_dir / "sre_agent",
    }
    run_comparison(args.export_root, results_roots, output_json=args.output, output_table=True)


if __name__ == "__main__":
    main()
