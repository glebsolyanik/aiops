"""
Run RCLAgent on converted data and save predicted root cause to results/rclagent/{problem_id}.json.

Expects RCLAgent repo at external/RCLAgent (or RCL_AGENT_ROOT). Converts export to RCL format,
starts tool_server in subprocess, runs coordinator (or invokes their pipeline), parses result JSON.
Loads AIOpsLab/.env so subprocess gets OPENAI_API_KEY, OPENAI_COMPATIBLE_*, etc.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


_AIOPS_ENV = Path(__file__).resolve().parent.parent / "AIOpsLab" / ".env"


def _load_aiopslab_env():
    """Load AIOpsLab/.env so RCL subprocess has API keys and base URLs."""
    if load_dotenv is None:
        return
    if _AIOPS_ENV.exists():
        load_dotenv(_AIOPS_ENV)


def _mask_key(val: str) -> str:
    v = (val or "").strip()
    if not v:
        return "<пусто>"
    if len(v) <= 10:
        return f"<len={len(v)}>"
    return f"{v[:6]}…{v[-4:]} (len={len(v)})"


def _print_rcl_llm_env(env: dict) -> None:
    """В консоль: какие ключи/URL реально видит coordinator (секреты замаскированы)."""
    if (os.environ.get("RCL_PRINT_KEYS") or "1").strip().lower() in ("0", "false", "no"):
        return
    print("\n" + "=" * 60 + "\n[RCL] Окружение LLM для subprocess coordinator:\n" + "=" * 60, flush=True)
    for name in (
        "OPENAI_COMPATIBLE_BASE_URL",
        "OPENAI_COMPATIBLE_API_KEY",
        "OPENAI_COMPATIBLE_MODEL",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DASHSCOPE_API_KEY",
    ):
        raw = (env.get(name) or "").strip()
        if "BASE_URL" in name or "MODEL" in name:
            print(f"  {name}: {raw or '<пусто>'}", flush=True)
        else:
            print(f"  {name}: {_mask_key(raw)}", flush=True)
    base = (env.get("OPENAI_COMPATIBLE_BASE_URL") or "").strip()
    if base:
        k = (env.get("OPENAI_COMPATIBLE_API_KEY") or "").strip()
        pick = "OPENAI_COMPATIBLE_API_KEY" if k else "fallback OPENAI_API_KEY / …"
        print(f"\n  → как в llm.py при заданном BASE_URL: ключ {pick}", flush=True)
        em = (env.get("OPENAI_COMPATIBLE_MODEL") or "").strip() or "<глобальный claude в llm.py>"
        print(f"  → модель в запросе к роутеру: OPENAI_COMPATIBLE_MODEL = {em}", flush=True)
    else:
        print("\n  → как в llm.py: OPENAI_API_KEY / ANTHROPIC / COMPATIBLE / DASHSCOPE по порядку", flush=True)
    print("  (отключить вывод: RCL_PRINT_KEYS=0)\n" + "=" * 60 + "\n", flush=True)


def run_rcl_on_export(
    export_dir: Path,
    problem_id: str,
    results_dir: Path,
    rcl_data_root: Path,
    rcl_repo_root: Path | None = None,
    sub_path: str | None = None,
    timeout_seconds: int = 300,
) -> dict:
    """
    Convert export to RCL layout, run RCL tool_server + coordinator, parse result.
    Saves results/rclagent/{problem_id}.json with predicted_service, raw_result, success.

    Чтобы сохранить рассуждения и шаги RCL на диск (conversation_trace_*.txt, result_*.json):
      export RCL_AGENT_LOG_DIR=/path/to/logs
    Тогда копируется data/.../result/* в $RCL_AGENT_LOG_DIR/{problem_id}/.
    """
    from external_agents.convert_to_rcl import convert_export_to_rcl
    from external_agents import AGENTS_ROOT

    if rcl_repo_root is not None:
        rcl_repo_root = Path(rcl_repo_root)
    else:
        default_external = Path(__file__).resolve().parent / "external" / "RCLAgent"
        rcl_repo_root = default_external if default_external.exists() else AGENTS_ROOT / "RCLAgent"
    if not rcl_repo_root.exists():
        return {
            "problem_id": problem_id,
            "predicted_service": None,
            "raw_result": None,
            "success": False,
            "error": "RCLAgent repo not found at " + str(rcl_repo_root),
        }

    # RCL expects data/{sub_path}/ inside repo
    rcl_data_root = Path(rcl_data_root) if rcl_data_root else rcl_repo_root / "data"
    sub_path = sub_path or problem_id.replace("-", "_")
    convert_export_to_rcl(Path(export_dir), rcl_data_root, sub_path)

    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    out_file = results_dir / f"{problem_id}.json"

    data_dir = rcl_data_root / sub_path
    if not (data_dir / "trace" / "all" / "trace_jaeger-span.csv").exists():
        result = {"problem_id": problem_id, "predicted_service": None, "success": False, "error": "No trace data after convert"}
        with open(out_file, "w") as f:
            json.dump(result, f, indent=2)
        return result

    # Coordinator и tool_server работают с cwd=rcl_repo_root и ищут data/{sub_path}/.
    # Если данные лежат во временной папке (rcl_data_root != repo/data), копируем их в репо.
    repo_data = rcl_repo_root / "data"
    if rcl_data_root.resolve() != repo_data.resolve():
        if repo_data.exists():
            shutil.rmtree(repo_data)
        shutil.copytree(rcl_data_root, repo_data)

    _load_aiopslab_env()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(rcl_repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    cwd = str(rcl_repo_root)
    _print_rcl_llm_env(env)

    # Start tool_server in subprocess (it expects argv[1] = sub_path)
    proc_server = subprocess.Popen(
        [sys.executable, "tool_server.py", sub_path],
        cwd=cwd,
        env={**env, "FLASK_APP": "tool_server.py"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(2)
        # Run coordinator; it reads from data/{sub_path}/hipstershop.Frontend/Recv._durations.txt
        # and calls http://127.0.0.1:5000/... We need to run from rcl_repo_root with data/ = rcl_data_root
        orig_cwd = os.getcwd()
        os.chdir(rcl_repo_root)
        try:
            coord_path = rcl_repo_root / "coordinator.py"
            if not coord_path.exists():
                result = {"problem_id": problem_id, "predicted_service": None, "success": False, "error": "coordinator.py not found"}
            else:
                # Coordinator: stdout/stderr в родительский процесс — видно рассуждения RCL-Agent
                comp = subprocess.run(
                    [sys.executable, "coordinator.py", sub_path],
                    cwd=cwd,
                    env={**env},
                    stdout=None,
                    stderr=None,
                    timeout=timeout_seconds,
                )
                result_path = repo_data / sub_path / "result"
                result_jsons = list(result_path.glob("result_*.json")) if result_path.is_dir() else []
                predicted = None
                raw = None
                if result_jsons:
                    with open(result_jsons[-1]) as f:
                        raw = json.load(f)
                    predicted = (
                        raw.get("service")
                        or raw.get("root_cause_service")
                        or raw.get("service_name")
                        or (raw.get("result") if isinstance(raw.get("result"), str) else None)
                    )
                result = {
                    "problem_id": problem_id,
                    "predicted_service": predicted,
                    "raw_result": raw,
                    "success": bool(predicted),
                    "coordinator_returncode": comp.returncode,
                }
                log_root = os.environ.get("RCL_AGENT_LOG_DIR", "").strip()
                if log_root and result_path.is_dir():
                    dest = Path(log_root) / problem_id
                    dest.mkdir(parents=True, exist_ok=True)
                    for f in result_path.iterdir():
                        if f.is_file():
                            shutil.copy2(f, dest / f.name)
                    result["rcl_saved_logs_dir"] = str(dest.resolve())
        finally:
            os.chdir(orig_cwd)
    finally:
        proc_server.terminate()
        proc_server.wait(timeout=5)

    with open(out_file, "w") as f:
        json.dump(result, f, indent=2)
    return result
