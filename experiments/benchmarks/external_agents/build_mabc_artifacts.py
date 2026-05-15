"""
Строит data/metric/endpoint_stats.json и data/topology/endpoint_maps.json из артефактов AIOpsLab
(traces.csv, опционально metrics_output/) чтобы тулы mABC (MetricExplorer, TraceExplorer) читали реальные данные.
"""
import csv
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


def _service_key(name: str) -> str:
    if not name:
        return "unknown-service"
    n = name.strip()
    if n.endswith("-service"):
        return n
    return f"{n}-service"


def _span_ts_us(row: dict) -> float:
    try:
        st = int(float(row.get("start_time") or 0))
    except (TypeError, ValueError):
        return 0.0
    if st <= 0:
        return 0.0
    # Jaeger / AIOpsLab: обычно Unix в микросекундах (~1.7e15)
    if st >= 1e15:
        return float(st)
    if st >= 1e12:
        return float(st) * 1000.0
    if st >= 1e9:
        return float(st) * 1e6
    return float(st)


def _minute_key(ts_us: float) -> str:
    sec = ts_us / 1e6
    return datetime.utcfromtimestamp(sec).strftime("%Y-%m-%d %H:%M:%S")


def _minute_floor(ts_us: float) -> str:
    sec = int(ts_us / 1e6)
    dt = datetime.utcfromtimestamp(sec).replace(second=0, microsecond=0)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def build_from_export(export_dir: Path, mabc_repo_data: Path, alert_time: str | None = None) -> None:
    """
    export_dir: traces.csv, metrics_output/ (опционально).
    mabc_repo_data: корень data/ в репозитории mABC (data/metric, data/topology).
    """
    export_dir = Path(export_dir)
    mabc_repo_data = Path(mabc_repo_data)
    traces_path = export_dir / "traces.csv"
    if not traces_path.exists():
        for p in export_dir.glob("**/*.csv"):
            if "trace" in p.name.lower() or "span" in p.name.lower():
                traces_path = p
                break

    metric_dir = mabc_repo_data / "metric"
    topo_dir = mabc_repo_data / "topology"
    metric_dir.mkdir(parents=True, exist_ok=True)
    topo_dir.mkdir(parents=True, exist_ok=True)

    aggregated_stats: dict = defaultdict(lambda: defaultdict(lambda: {
        "_calls": 0, "_err": 0, "_dur_sum": 0.0, "_timeout": 0,
    }))

    # endpoint -> minute -> set of downstream
    downstream: dict = defaultdict(lambda: defaultdict(set))
    upstream: dict = defaultdict(lambda: defaultdict(set))

    if traces_path.exists():
        with open(traces_path, newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        by_trace: dict = defaultdict(dict)
        for row in rows:
            tid = row.get("trace_id") or row.get("traceID")
            sid = row.get("span_id") or row.get("spanID")
            if tid and sid:
                by_trace[tid][sid] = row

        for tid, spans in by_trace.items():
            for sid, row in spans.items():
                svc = _service_key(row.get("service_name") or row.get("serviceName") or "")
                ts_us = _span_ts_us(row)
                mk = _minute_floor(ts_us)
                dur_ms = 0.0
                try:
                    dur_ms = float(row.get("duration") or 0) / 1000.0
                except (TypeError, ValueError):
                    pass
                err = str(row.get("has_error", "")).lower() in ("true", "1", "yes")
                aggregated_stats[svc][mk]["_calls"] += 1
                aggregated_stats[svc][mk]["_dur_sum"] += dur_ms
                if err:
                    aggregated_stats[svc][mk]["_err"] += 1

                parent = row.get("parent_span") or row.get("references") or ""
                if not parent or parent == "ROOT":
                    continue
                parent_row = spans.get(parent)
                if not parent_row:
                    continue
                caller = _service_key(parent_row.get("service_name") or "")
                callee = svc
                if caller != callee:
                    pmk = _minute_floor(_span_ts_us(parent_row))
                    downstream[caller][pmk].add(callee)
                    upstream[callee][pmk].add(caller)

    # финальный JSON для MetricExplorer
    out_stats: dict = {}
    for ep, minutes in aggregated_stats.items():
        out_stats[ep] = {}
        for minute, acc in minutes.items():
            c = max(acc["_calls"], 1)
            err = acc["_err"]
            out_stats[ep][minute] = {
                "calls": acc["_calls"],
                "success_rate": round((1 - err / c) * 100, 2),
                "error_rate": round(err / c * 100, 2),
                "average_duration": round(acc["_dur_sum"] / c, 3),
                "timeout_rate": round(acc["_timeout"] / c * 100, 2),
            }

    # Добавить сигнал CPU по подам из metrics_output (container)
    metrics_root = export_dir / "metrics_output"
    if metrics_root.is_dir():
        cpu_files = list(metrics_root.glob("**/kpi_container_cpu_usage_seconds_total.csv"))
        if cpu_files:
            _merge_container_cpu(out_stats, cpu_files[0], alert_time)

    with open(metric_dir / "endpoint_stats.json", "w", encoding="utf-8") as f:
        json.dump(out_stats, f, indent=2)

    # topology: endpoint -> { minute: [downstream...] }
    endpoint_maps: dict = {}
    for ep, mins in downstream.items():
        endpoint_maps[ep] = {m: sorted(list(s)) for m, s in mins.items()}

    with open(topo_dir / "endpoint_maps.json", "w", encoding="utf-8") as f:
        json.dump(endpoint_maps, f, indent=2)

    # upstream maps для TraceExplorer
    up_maps: dict = {}
    for ep, mins in upstream.items():
        up_maps[ep] = {m: sorted(list(s)) for m, s in mins.items()}
    with open(topo_dir / "endpoint_upstream_maps.json", "w", encoding="utf-8") as f:
        json.dump(up_maps, f, indent=2)


def _merge_container_cpu(out_stats: dict, cpu_csv: Path, alert_time: str | None) -> None:
    """Грубая прокси: прирост CPU по окну вокруг алерта для подов hotel-reservation."""
    try:
        rows = []
        with open(cpu_csv, newline="", encoding="utf-8", errors="replace") as f:
            for row in csv.DictReader(f):
                rows.append(row)
    except OSError:
        return
    if not rows:
        return

    pods: dict = defaultdict(list)  # service_prefix -> [(ts, val)]
    for row in rows:
        cid = (row.get("cmdb_id") or "").lower()
        try:
            v = float(row.get("value") or 0)
            ts = int(float(row.get("timestamp") or 0))
        except (TypeError, ValueError):
            continue
        for prefix in (
            "user", "frontend", "recommendation", "reservation", "search",
            "profile", "rate", "geo",
        ):
            if f".{prefix}-" in cid or f"/{prefix}-" in cid or cid.endswith(f".{prefix}"):
                pods[f"{prefix}-service"].append((ts, v))
                break

    base_time = None
    if alert_time:
        try:
            base_time = datetime.strptime(alert_time[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    if base_time is None and rows:
        base_time = datetime.utcfromtimestamp(int(float(rows[-1]["timestamp"])))

    if not base_time:
        return

    tkey = base_time.strftime("%Y-%m-%d %H:%M:%S")
    for svc, pts in pods.items():
        if len(pts) < 2:
            continue
        pts.sort(key=lambda x: x[0])
        v0, v1 = pts[0][1], pts[-1][1]
        delta = abs(v1 - v0)
        if svc not in out_stats:
            out_stats[svc] = {}
        if tkey not in out_stats[svc]:
            out_stats[svc][tkey] = {
                "calls": 0, "success_rate": 100.0, "error_rate": 0.0,
                "average_duration": 0.0, "timeout_rate": 0.0,
            }
        out_stats[svc][tkey]["cpu_usage_delta"] = round(delta, 4)
        out_stats[svc][tkey]["note"] = "container_cpu_usage_seconds_total delta over export window"


def read_alert_time_from_label(label_path: Path) -> str | None:
    try:
        with open(label_path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d:
            return next(iter(d.keys()))
    except (OSError, json.JSONDecodeError, StopIteration):
        pass
    return None
