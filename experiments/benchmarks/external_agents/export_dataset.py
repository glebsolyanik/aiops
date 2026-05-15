"""
Export AIOpsLab problem data (traces, metrics) and ground truth for offline
comparison with RCLAgent, mABC, and SRE-agent.

Run from AIOpsLab root so that trace_output and metrics_output are created there:
  cd experiments/benchmarks/AIOpsLab && python -c "
  import sys
  sys.path.insert(0, '..')
  from external_agents.export_dataset import run_export
  run_export(['network_loss_hotel_res-detection-1', 'network_loss_hotel_res-localization-1'], export_root='../external_agents/export')
  "
Or from benchmarks dir with PYTHONPATH including AIOpsLab:
  cd experiments/benchmarks && python external_agents/export_dataset.py
"""
import atexit
import json
import os
import re
import shutil
import sys
from pathlib import Path


def _extract_trace_path(message: str):
    """Extract file path from get_traces return message."""
    if not message or not isinstance(message, str):
        return None
    m = re.search(r"exported to:\s*([^\n]+)", message, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"(/[^\s\n]+\.csv)", message)
    if m:
        return m.group(1).strip()
    return None


def _extract_metrics_path(message: str):
    """Return path from get_metrics (usually returned as-is)."""
    if not message or not isinstance(message, str):
        return None
    message = message.strip()
    if os.path.isdir(message) or os.path.isfile(message):
        return message
    if message.startswith("error") or "not found" in message.lower():
        return None
    return message


def _infer_task_type(problem_id: str) -> str:
    if "-detection-" in problem_id:
        return "detection"
    if "-localization-" in problem_id:
        return "localization"
    if "-analysis-" in problem_id:
        return "analysis"
    if "-mitigation-" in problem_id:
        return "mitigation"
    return "unknown"


def run_export(
    problem_ids: list[str],
    export_root: str | Path,
    lookback_minutes: int = 5,
    aiopslab_root: str | Path | None = None,
):
    """
    For each problem_id: init_problem, get_traces + get_metrics, copy to export/{problem_id}/, write ground_truth.json.
    Expects to be run with cwd = AIOpsLab root (or aiopslab_root set) so that trace_output/metrics_output exist.
    """
    export_root = Path(export_root)
    export_root.mkdir(parents=True, exist_ok=True)

    if aiopslab_root is not None:
        aiopslab_root = Path(aiopslab_root)
        if str(aiopslab_root) not in sys.path:
            sys.path.insert(0, str(aiopslab_root))
        os.chdir(aiopslab_root)

    from aiopslab.orchestrator import Orchestrator
    from aiopslab.orchestrator.problems.registry import ProblemRegistry
    from aiopslab.utils.critical_section import CriticalSection

    registry = ProblemRegistry()
    for problem_id in problem_ids:
        if registry.get_problem(problem_id) is None:
            print(f"Skip unknown problem_id: {problem_id}")
            continue

        out_dir = export_root / problem_id
        out_dir.mkdir(parents=True, exist_ok=True)

        orch = Orchestrator()
        orch.register_agent(_DummyAgent(), name="export")
        try:
            task_desc, instructions, actions = orch.init_problem(problem_id)
        except Exception as e:
            print(f"init_problem failed for {problem_id}: {e}")
            continue

        prob = orch.session.problem
        namespace = getattr(prob, "namespace", None) or getattr(prob.app, "namespace", "test-hotel-reservation")

        # Ground truth
        faulty_service = getattr(prob, "faulty_service", None)
        task_type = _infer_task_type(problem_id)
        ground_truth = {
            "problem_id": problem_id,
            "namespace": namespace,
            "task_type": task_type,
            "faulty_service": faulty_service,
            "expected_detection": "Yes" if faulty_service else "No",
        }
        with open(out_dir / "ground_truth.json", "w") as f:
            json.dump(ground_truth, f, indent=2)

        # Collect traces
        try:
            trace_msg = prob.perform_action("get_traces", namespace, lookback_minutes)
            trace_path = _extract_trace_path(trace_msg)
            if trace_path and os.path.isfile(trace_path):
                dest = out_dir / "traces.csv"
                shutil.copy2(trace_path, dest)
                ground_truth["traces_path"] = str(dest)
            elif trace_path and os.path.isdir(trace_path):
                dest_dir = out_dir / "trace_output"
                if dest_dir.exists():
                    shutil.rmtree(dest_dir)
                shutil.copytree(trace_path, dest_dir)
                ground_truth["traces_path"] = str(dest_dir)
        except Exception as e:
            print(f"get_traces failed for {problem_id}: {e}")
            ground_truth["traces_error"] = str(e)

        # Collect metrics
        try:
            metrics_msg = prob.perform_action("get_metrics", namespace, lookback_minutes)
            metrics_path = _extract_metrics_path(metrics_msg)
            if metrics_path and os.path.isdir(metrics_path):
                dest_dir = out_dir / "metrics_output"
                if dest_dir.exists():
                    shutil.rmtree(dest_dir)
                shutil.copytree(metrics_path, dest_dir)
                ground_truth["metrics_path"] = str(dest_dir)
            elif metrics_path and os.path.isfile(metrics_path):
                dest = out_dir / "metrics.csv"
                shutil.copy2(metrics_path, dest)
                ground_truth["metrics_path"] = str(dest)
        except Exception as e:
            print(f"get_metrics failed for {problem_id}: {e}")
            ground_truth["metrics_error"] = str(e)

        with open(out_dir / "ground_truth.json", "w") as f:
            json.dump(ground_truth, f, indent=2)

        # Recover fault and cleanup
        from aiopslab.utils.status import exit_cleanup_fault
        with CriticalSection():
            try:
                prob.recover_fault()
            except Exception:
                pass
            try:
                atexit.unregister(exit_cleanup_fault)
            except Exception:
                pass
            try:
                prob.app.cleanup()
            except Exception:
                pass

        print(f"Exported {problem_id} -> {out_dir}")
    print("Export done.")


class _DummyAgent:
    async def get_action(self, _):
        return ""

    def init_context(self, *_args, **_kwargs):
        pass


def main():
    import argparse
    p = argparse.ArgumentParser(description="Export AIOpsLab dataset for external agents")
    p.add_argument("--problems", nargs="+", default=["network_loss_hotel_res-detection-1", "network_loss_hotel_res-localization-1"],
                   help="Problem IDs to export")
    p.add_argument("--export-root", default=Path(__file__).resolve().parent / "export", type=Path)
    p.add_argument("--aiopslab", default=None, type=Path, help="AIOpsLab root (default: parent of benchmarks)")
    p.add_argument("--lookback", type=int, default=5)
    args = p.parse_args()
    aiopslab_root = args.aiopslab
    if aiopslab_root is None:
        aiopslab_root = Path(__file__).resolve().parent.parent / "AIOpsLab"
    run_export(args.problems, export_root=args.export_root, lookback_minutes=args.lookback, aiopslab_root=aiopslab_root)


if __name__ == "__main__":
    main()
