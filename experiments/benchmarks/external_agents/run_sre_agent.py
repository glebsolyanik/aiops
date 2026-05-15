"""
Run AWS SRE-agent on export and save predicted root cause to results/sre_agent/{problem_id}.json.

SRE-agent is prompt-based: sre-agent --prompt "...". We build a prompt from problem description
and (optionally) paste get_traces/get_metrics output from export. Parse report for root cause / service name.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def run_sre_agent_on_export(
    export_dir: Path,
    problem_id: str,
    results_dir: Path,
    ground_truth_path: Path | None = None,
    sre_agent_cmd: str = "sre-agent",
    timeout_seconds: int = 120,
) -> dict:
    """
    Build prompt from ground_truth + optional trace/metric snippets, run sre-agent --prompt "...", parse report.
    Saves results/sre_agent/{problem_id}.json. If sre_agent_cmd not in PATH, returns error in result.
    """
    export_dir = Path(export_dir)
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    out_file = results_dir / f"{problem_id}.json"

    gt_path = ground_truth_path or export_dir / "ground_truth.json"
    gt = {}
    if gt_path.exists():
        with open(gt_path) as f:
            gt = json.load(f)

    namespace = gt.get("namespace", "test-hotel-reservation")
    task_type = gt.get("task_type", "localization")
    prompt = (
        f"Problem: {problem_id}. Namespace: {namespace}. Task: {task_type}. "
        "Identify the faulty service (root cause) for this incident. "
        "Respond with the service name only, or a short report ending with 'Root cause service: <name>'."
    )
    # Optionally append trace/metric summary from export
    traces_csv = export_dir / "traces.csv"
    if traces_csv.exists():
        try:
            import pandas as pd
            df = pd.read_csv(traces_csv, nrows=50)
            prompt += "\n\nTraces sample (first 50 rows):\n" + df.to_string(index=False)[:2000]
        except Exception:
            pass

    try:
        comp = subprocess.run(
            [sre_agent_cmd, "--prompt", prompt],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        result = {
            "problem_id": problem_id,
            "predicted_service": None,
            "success": False,
            "error": f"{sre_agent_cmd} not found in PATH (SRE-agent may not be installed)",
        }
        with open(out_file, "w") as f:
            json.dump(result, f, indent=2)
        return result

    stdout = comp.stdout or ""
    # Heuristic: look for "root cause service: X" or "faulty service: X" or last line as service name
    match = re.search(r"(?:root cause|faulty)\s+service:\s*([a-zA-Z0-9_-]+)", stdout, re.IGNORECASE)
    predicted = match.group(1).strip() if match else None
    if not predicted and stdout.strip():
        lines = [l.strip() for l in stdout.strip().splitlines() if l.strip()]
        if lines:
            last = lines[-1]
            if len(last) < 50 and re.match(r"^[a-zA-Z0-9_-]+$", last):
                predicted = last

    result = {
        "problem_id": problem_id,
        "predicted_service": predicted,
        "stdout_tail": stdout[-4000:] if len(stdout) > 4000 else stdout,
        "success": bool(predicted),
        "stderr_tail": (comp.stderr or "")[-1000:],
    }
    with open(out_file, "w") as f:
        json.dump(result, f, indent=2)
    return result
