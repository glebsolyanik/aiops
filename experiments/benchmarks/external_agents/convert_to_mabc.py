"""
Convert AIOpsLab export to mABC data layout.

mABC expects:
  data/label/label.json  -> { "T": { "endpoint": "path" }, ... }  (time -> endpoint -> path)
  (and possibly trace/metric data under data/ for the agents)

We produce label.json with one entry per problem: alert time T, endpoint (we map service to endpoint string), path.
"""
import json
import os
import random
import shutil
from pathlib import Path
from datetime import datetime, timezone


# Map AIOpsLab hotel-reservation service names to endpoint-like names for mABC
HOTEL_SERVICE_TO_ENDPOINT = {
    "frontend": "frontend",
    "user": "user",
    "search": "search",
    "recommendation": "recommendation",
    "reservation": "reservation",
    "profile": "profile",
    "rate": "rate",
    "geo": "geo",
}


def convert_export_to_mabc(
    export_dir: Path,
    mabc_data_root: Path,
    problem_id: str,
    alert_time: str | None = None,
    alerting_service: str | None = None,
) -> Path:
    """
    export_dir: path to export/{problem_id}/.
    mabc_data_root: path to mABC data/ directory.
    problem_id: used as key in label.json.
    alert_time: optional "YYYY-MM-DD HH:MM:SS" (default: now).
    alerting_service: имя сервиса из HOTEL_SERVICE_TO_ENDPOINT (frontend, user, ...).
        Если None — случайный сервис из списка (seed: MABC_ALERT_SEED).
    faulty_service в ground_truth.json не подменяет alerting endpoint.
    Returns path to mabc_data_root.
    """
    mabc_data_root = Path(mabc_data_root)
    mabc_data_root.mkdir(parents=True, exist_ok=True)
    label_dir = mabc_data_root / "label"
    label_dir.mkdir(parents=True, exist_ok=True)
    label_file = label_dir / "label.json"

    gt = {}
    gt_path = export_dir / "ground_truth.json"
    if gt_path.exists():
        with open(gt_path) as f:
            gt = json.load(f)

    available = list(HOTEL_SERVICE_TO_ENDPOINT.keys())
    if alerting_service and alerting_service in HOTEL_SERVICE_TO_ENDPOINT:
        svc = alerting_service
    elif alerting_service and alerting_service in HOTEL_SERVICE_TO_ENDPOINT.values():
        svc = next(k for k, v in HOTEL_SERVICE_TO_ENDPOINT.items() if v == alerting_service)
    else:
        seed_s = os.getenv("MABC_ALERT_SEED", "").strip()
        if seed_s:
            random.seed(int(seed_s))
        svc = random.choice(available)
    # Короткое имя как в traces/topology (frontend, profile, …), не *-service
    endpoint_key = HOTEL_SERVICE_TO_ENDPOINT[svc]
    if alert_time is None:
        alert_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    path_val = None
    # mABC main: for t, v in data.items(): for endpoint, path in v.items() -> data is { "T": { "endpoint": "path" } }
    labels = {}
    if label_file.exists():
        with open(label_file) as f:
            labels = json.load(f)
    labels[alert_time] = {endpoint_key: path_val}
    with open(label_file, "w") as f:
        json.dump(labels, f, indent=2)
    meta = {
        "problem_id": problem_id,
        "alerting_endpoint": endpoint_key,
        "note": "Случайный alerting; faulty_service в ground_truth — для оценки, не совпадает с alerting.",
        "alerting_service_key": svc,
        "faulty_service_ground_truth": gt.get("faulty_service"),
    }
    with open(mabc_data_root / "label" / "alerting_meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    # Copy traces/metrics to a path mABC might expect (e.g. data/traces/{problem_id}/)
    traces_src = export_dir / "traces.csv"
    if not traces_src.exists():
        trace_out = export_dir / "trace_output"
        if trace_out.exists():
            for c in trace_out.glob("**/*.csv"):
                traces_src = c
                break
    if traces_src.exists():
        trace_dest_dir = mabc_data_root / "traces"
        trace_dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(traces_src, trace_dest_dir / f"{problem_id}_traces.csv")

    return mabc_data_root
