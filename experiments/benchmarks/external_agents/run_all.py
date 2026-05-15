"""
Run all external agents on exported problems and then compare.

Usage:
  cd experiments/benchmarks && python external_agents/run_all.py --export-root external_agents/export

Agents are looked up in order: external_agents/external/{RCLAgent,mABC}, then experiments/agents/{RCLAgent,mABC}.
Override with --agents-root to point to a folder containing RCLAgent and mABC subdirs.
"""
import argparse
from pathlib import Path

from external_agents.run_rcl import run_rcl_on_export
from external_agents.run_mabc import run_mabc_on_export
from external_agents.run_sre_agent import run_sre_agent_on_export
from external_agents.compare_results import run_comparison
from external_agents import AGENTS_ROOT


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--export-root", type=Path, default=Path(__file__).resolve().parent / "export")
    p.add_argument("--results-dir", type=Path, default=Path(__file__).resolve().parent / "results")
    p.add_argument("--agents-root", type=Path, default=None,
                   help="Root folder for agent repos (default: external_agents/external, then experiments/agents)")
    p.add_argument("--agents", nargs="+", default=["rcl", "mabc", "sre_agent"], help="Which to run: rcl, mabc, sre_agent")
    p.add_argument("--skip-run", action="store_true", help="Only run comparison on existing results")
    args = p.parse_args()

    export_root = args.export_root
    results_dir = args.results_dir
    agents_root = args.agents_root
    if not export_root.exists():
        print(f"Export root not found: {export_root}. Run export_dataset.py first.")
        return

    problem_dirs = [d for d in export_root.iterdir() if d.is_dir() and (d / "ground_truth.json").exists()]
    if not problem_dirs:
        print("No problem exports found.")
        return

    if not args.skip_run:
        rcl_root = (agents_root / "RCLAgent") if agents_root else None
        mabc_root = (agents_root / "mABC") if agents_root else None
        for export_dir in problem_dirs:
            problem_id = export_dir.name
            if "rcl" in args.agents:
                run_rcl_on_export(export_dir, problem_id, results_dir / "rclagent", rcl_data_root=None, rcl_repo_root=rcl_root)
            if "mabc" in args.agents:
                run_mabc_on_export(export_dir, problem_id, results_dir / "mabc", mabc_data_root=None, mabc_repo_root=mabc_root)
            if "sre_agent" in args.agents:
                run_sre_agent_on_export(export_dir, problem_id, results_dir / "sre_agent")

    run_comparison(export_root, {
        "rclagent": results_dir / "rclagent",
        "mabc": results_dir / "mabc",
        "sre_agent": results_dir / "sre_agent",
    }, output_json=results_dir / "comparison.json", output_table=True)


if __name__ == "__main__":
    main()
