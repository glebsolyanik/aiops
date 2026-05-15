"""
Run mABC on converted data and save predicted root cause to results/mabc/{problem_id}.json.

Expects mABC repo at external/mABC. Converts export to mABC label + data, runs main/main.py (or single-question),
parses stdout for "Root Cause Endpoint: XXX".

Перед запуском подгружается AIOpsLab/.env (OPENAI_*, OPENAI_COMPATIBLE_*), дочерний процесс наследует env.
MABC_VERBOSE=1 — потоковый вывод процесса; MABC_REASONING_LOG — блоки [mABC-Agent] (Thought/Action/…).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


def _load_aiopslab_env():
    """Подтянуть токен, URL и модель из AIOpsLab/.env в os.environ."""
    if not load_dotenv:
        return
    benchmarks = Path(__file__).resolve().parent.parent
    aiops_env = benchmarks / "AIOpsLab" / ".env"
    if aiops_env.is_file():
        load_dotenv(aiops_env, override=False)
    load_dotenv(override=False)


def _mabc_verbose() -> bool:
    return os.getenv("MABC_VERBOSE", "1").strip().lower() not in ("0", "false", "no", "off")


def _run_mabc_subprocess_live(
    cmd: list,
    cwd: str,
    env: dict,
    timeout_seconds: int,
) -> tuple[int, str, str]:
    """Запуск с потоковым выводом в консоль (как coordinator RCL: видно ход работы сразу)."""
    out_list: list[str] = []
    err_list: list[str] = []

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    def _drain(pipe, accum: list[str], prefix: str) -> None:
        try:
            for line in iter(pipe.readline, ""):
                accum.append(line)
                sys.stdout.write(prefix + line)
                sys.stdout.flush()
        except Exception:
            pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    t_out = threading.Thread(target=_drain, args=(proc.stdout, out_list, "[mABC] "))
    t_err = threading.Thread(target=_drain, args=(proc.stderr, err_list, "[mABC stderr] "))
    t_out.daemon = True
    t_err.daemon = True
    t_out.start()
    t_err.start()
    try:
        proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=15)
        except Exception:
            pass
        t_out.join(timeout=5)
        t_err.join(timeout=5)
        raise
    t_out.join(timeout=30)
    t_err.join(timeout=30)
    return proc.returncode or 0, "".join(out_list), "".join(err_list)


ENDPOINT_TO_SERVICE = {
    "frontend-service": "frontend",
    "user-service": "user",
    "search-service": "search",
    "recommendation-service": "recommendation",
    "reservation-service": "reservation",
    "profile-service": "profile",
    "rate-service": "rate",
    "geo-service": "geo",
}


def run_mabc_on_export(
    export_dir: Path,
    problem_id: str,
    results_dir: Path,
    mabc_data_root: Path,
    mabc_repo_root: Path | None = None,
    timeout_seconds: int = 600,
    verbose: bool | None = None,
) -> dict:
    """
    Convert export to mABC layout, run main, parse "Root Cause Endpoint: XXX".
    Saves results/mabc/{problem_id}.json with predicted_service, raw_answer, success.
    """
    from external_agents.convert_to_mabc import convert_export_to_mabc
    from external_agents import AGENTS_ROOT

    _load_aiopslab_env()
    show_output = _mabc_verbose() if verbose is None else verbose

    if mabc_repo_root is not None:
        mabc_repo_root = Path(mabc_repo_root)
    else:
        default_external = Path(__file__).resolve().parent / "external" / "mABC"
        mabc_repo_root = default_external if default_external.exists() else AGENTS_ROOT / "mABC"
    mabc_data_root = Path(mabc_data_root) if mabc_data_root else mabc_repo_root / "data"
    convert_export_to_mabc(Path(export_dir), mabc_data_root, problem_id)

    if not mabc_repo_root.exists():
        result = {"problem_id": problem_id, "predicted_service": None, "success": False, "error": "mABC repo not found at " + str(mabc_repo_root)}
        Path(results_dir).mkdir(parents=True, exist_ok=True)
        with open(Path(results_dir) / f"{problem_id}.json", "w") as f:
            json.dump(result, f, indent=2)
        return result

    env = os.environ.copy()
    env["PYTHONPATH"] = str(mabc_repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    # Явно прокинуть то, что читает mABC/settings.py (на случай урезанного окружения у дочернего процесса)
    for key in (
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "OPENAI_BASE_URL",
        "OPENAI_COMPATIBLE_API_KEY",
        "OPENAI_COMPATIBLE_BASE_URL",
        "OPENAI_COMPATIBLE_MODEL",
    ):
        v = os.environ.get(key)
        if v is not None:
            env[key] = v
    main_py_in_sub = mabc_repo_root / "main" / "main.py"
    main_py_at_root = mabc_repo_root / "main.py"

    # cwd должен быть КОРЕНЬ репозитория mABC: tool_path вида agents/tools/*.py и data/label относительно него
    repo_data = mabc_repo_root / "data"
    repo_data.mkdir(parents=True, exist_ok=True)
    (repo_data / "label").mkdir(parents=True, exist_ok=True)
    label_src = mabc_data_root / "label" / "label.json"
    if label_src.exists():
        shutil.copy2(label_src, repo_data / "label" / "label.json")
    traces_src = mabc_data_root / "traces"
    if traces_src.exists():
        dest_traces = repo_data / "traces"
        dest_traces.mkdir(parents=True, exist_ok=True)
        for f in traces_src.iterdir():
            if f.is_file():
                shutil.copy2(f, dest_traces / f.name)

    env["MABC_REPO_ROOT"] = str(mabc_repo_root.resolve())
    try:
        from external_agents.build_mabc_artifacts import build_from_export, read_alert_time_from_label

        _al = read_alert_time_from_label(repo_data / "label" / "label.json")
        build_from_export(Path(export_dir), mabc_repo_root / "data", alert_time=_al)
    except Exception as ex:
        if show_output:
            print(f"[mABC] предупреждение: build_mabc_artifacts: {ex}", flush=True)

    if main_py_in_sub.exists():
        cmd = [sys.executable, "main/main.py"]
    elif main_py_at_root.exists():
        cmd = [sys.executable, "main.py"]
    else:
        cmd = [sys.executable, "main/main.py"]
    cwdd = str(mabc_repo_root)

    if show_output:
        _base = (os.getenv("OPENAI_COMPATIBLE_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "").strip()
        _model = (
            os.getenv("OPENAI_COMPATIBLE_MODEL")
            or os.getenv("OPENAI_MODEL")
            or "(default)"
        )
        _key_ok = bool(
            os.getenv("OPENAI_COMPATIBLE_API_KEY") or os.getenv("OPENAI_API_KEY")
        )
        print(
            "[mABC subprocess] model=%r base_url=%r api_key_set=%s"
            % (_model, _base or "(OpenAI default)", _key_ok),
            flush=True,
        )

    if show_output:
        print(
            "[mABC] Потоковый вывод (как RCL). Отключить поток: MABC_VERBOSE=0\n",
            flush=True,
        )
        returncode, stdout, stderr = _run_mabc_subprocess_live(cmd, cwdd, env, timeout_seconds)
        print("=" * 60 + f" mABC завершён, exit={returncode} " + "=" * 60, flush=True)
    else:
        comp = subprocess.run(
            cmd,
            cwd=cwdd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        returncode = comp.returncode or 0
        stdout = comp.stdout or ""
        stderr = comp.stderr or ""

    # Parse "Root Cause Endpoint: XXX"
    match = re.search(r"Root Cause Endpoint:\s*([^\n,]+)", stdout, re.IGNORECASE)
    endpoint = match.group(1).strip() if match else None
    predicted_service = ENDPOINT_TO_SERVICE.get(endpoint, endpoint) if endpoint else None
    if predicted_service is None and endpoint:
        predicted_service = endpoint.replace("-service", "").strip()

    result = {
        "problem_id": problem_id,
        "predicted_service": predicted_service,
        "raw_answer": match.group(0) if match else None,
        "stdout_tail": stdout[-3000:] if len(stdout) > 3000 else stdout,
        "success": bool(predicted_service),
        "stderr_tail": stderr[-1000:] if stderr else None,
        "subprocess_returncode": returncode,
    }

    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    with open(results_dir / f"{problem_id}.json", "w") as f:
        json.dump(result, f, indent=2)
    return result
