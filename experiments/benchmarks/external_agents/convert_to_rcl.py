"""
Convert AIOpsLab export (export/{problem_id}/) to RCLAgent data/{sub_path}/ layout.

RCLAgent expects:
  data/{sub_path}/trace/all/trace_jaeger-span.csv
  data/{sub_path}/metric/all/metrics.csv
  data/{sub_path}/metric/node_service_map.pkl, service_node_map.pkl
  data/{sub_path}/hipstershop.Frontend/Recv._durations.txt  (TSV: trace_id, service_name, operation_name, duration)
"""
import pickle
import shutil
from pathlib import Path

import pandas as pd


def _normalize_parent_col(df: pd.DataFrame) -> pd.DataFrame:
    if "parent_span" not in df.columns and "parent_span_id" in df.columns:
        df = df.rename(columns={"parent_span_id": "parent_span"})
    return df


def _is_root_parent(val) -> bool:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return True
    s = str(val).strip().upper()
    return s in ("", "ROOT", "NONE", "NAN", "NULL", "0")


def _root_spans_for_recv(df: pd.DataFrame) -> pd.DataFrame:
    """Одна строка на trace_id для корневого span (AIOpsLab: NaN/ROOT/пусто; иначе первый span в trace)."""
    df = _normalize_parent_col(df)
    if "trace_id" not in df.columns:
        return df.head(50)

    if "parent_span" in df.columns:
        mask = df["parent_span"].apply(_is_root_parent)
        roots = df[mask]
        if not roots.empty:
            return roots.drop_duplicates(subset=["trace_id"], keep="first")

    # Fallback: первый span по каждому trace_id
    sort_cols = [c for c in ("start_time", "timestamp", "span_id") if c in df.columns]
    if sort_cols:
        df = df.sort_values(sort_cols)
    return df.groupby("trace_id", as_index=False).first()


def _rows_from_metrics_frame(m: pd.DataFrame, kpi_default: str) -> list[dict]:
    rows = []
    if "timestamp" not in m.columns:
        return rows

    num_cols = m.select_dtypes(include=["number"]).columns.tolist()
    skip = {"timestamp"}
    value_cols = [c for c in num_cols if c not in skip]

    if "value" in m.columns:
        for _, r in m.iterrows():
            cmdb = r.get("cmdb_id", "")
            svc = r.get("service_name", "")
            node = r.get("node_id", "")
            if isinstance(cmdb, str) and "." in cmdb and not svc:
                parts = cmdb.split(".", 1)
                node, svc = parts[0], parts[1]
            elif not svc and cmdb:
                svc = str(cmdb)
            rows.append(
                {
                    "service_name": str(svc or "unknown"),
                    "node_id": str(node or ""),
                    "timestamp": int(float(r["timestamp"])) if pd.notna(r["timestamp"]) else 0,
                    "kpi_name": kpi_default,
                    "value": float(r["value"]) if pd.notna(r["value"]) else 0.0,
                }
            )
        return rows

    for vc in value_cols:
        for _, r in m.iterrows():
            if pd.isna(r.get(vc)):
                continue
            cmdb = r.get("cmdb_id", "")
            svc = r.get("service_name", "")
            node = r.get("node_id", "")
            if isinstance(cmdb, str) and "." in cmdb and not svc:
                parts = cmdb.split(".", 1)
                node, svc = parts[0], parts[1]
            elif not svc and cmdb:
                svc = str(cmdb)
            rows.append(
                {
                    "service_name": str(svc or "unknown"),
                    "node_id": str(node or ""),
                    "timestamp": int(float(r["timestamp"])) if pd.notna(r["timestamp"]) else 0,
                    "kpi_name": vc,
                    "value": float(r[vc]),
                }
            )
    return rows


def convert_export_to_rcl(export_dir: Path, rcl_data_root: Path, sub_path: str) -> Path:
    sub_root = rcl_data_root / sub_path
    sub_root.mkdir(parents=True, exist_ok=True)
    trace_src = export_dir / "traces.csv"

    if not trace_src.exists():
        trace_dir = export_dir / "trace_output"
        if trace_dir.exists():
            csvs = [p for p in trace_dir.rglob("*.csv") if "metric" not in str(p).lower()]
            if csvs:
                trace_src = csvs[0]

    if trace_src.exists():
        df = pd.read_csv(trace_src)
        df = _normalize_parent_col(df)
        trace_dest_dir = sub_root / "trace" / "all"
        trace_dest_dir.mkdir(parents=True, exist_ok=True)
        out_csv = trace_dest_dir / "trace_jaeger-span.csv"
        df.to_csv(out_csv, index=False)

        roots = _root_spans_for_recv(df)
        if roots.empty and len(df) > 0:
            roots = df.head(min(100, len(df)))
        frontend_dir = sub_root / "frontend.Frontend"
        frontend_dir.mkdir(parents=True, exist_ok=True)
        recv_file = frontend_dir / "Recv._durations.txt"
        with open(recv_file, "w") as f:
            f.write("trace_id\tservice_name\toperation_name\tduration\n")
            for _, row in roots.iterrows():
                tid = row.get("trace_id", row.get("traceID", ""))
                svc = row.get("service_name", row.get("serviceName", ""))
                op = row.get("operation_name", row.get("operationName", ""))
                dur = row.get("duration", 0)
                try:
                    dur = int(float(dur))
                except (TypeError, ValueError):
                    dur = 0
                f.write(f"{tid}\t{svc}\t{op}\t{dur}\n")

        hipster_dir = sub_root / "hipstershop.Frontend"
        if hipster_dir.exists():
            shutil.rmtree(hipster_dir)
        shutil.copytree(frontend_dir, hipster_dir)

    metric_src_dir = export_dir / "metrics_output"
    metric_dest_dir = sub_root / "metric" / "all"
    metric_dest_dir.mkdir(parents=True, exist_ok=True)
    rows = []

    if metric_src_dir.exists():
        seen = set()
        for csv_path in sorted(metric_src_dir.rglob("*.csv")):
            rp = csv_path.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            low = csv_path.name.lower()
            if "trace" in low and "jaeger" in low:
                continue
            try:
                m = pd.read_csv(csv_path)
            except Exception:
                continue
            rows.extend(_rows_from_metrics_frame(m, csv_path.stem))

    cols = ["service_name", "node_id", "timestamp", "kpi_name", "value"]
    if rows:
        pd.DataFrame(rows)[cols].to_csv(sub_root / "metric" / "all" / "metrics.csv", index=False)
    else:
        pd.DataFrame(columns=cols).to_csv(sub_root / "metric" / "all" / "metrics.csv", index=False)

    node_service_map = {}
    service_node_map = {}
    if trace_src.exists():
        df = pd.read_csv(trace_src)
        df = _normalize_parent_col(df)
        if "service_name" in df.columns:
            for i, svc in enumerate(df["service_name"].dropna().unique()):
                node_id = f"node_{i}"
                node_service_map[node_id] = {str(svc)}
                service_node_map[str(svc)] = node_id

    metric_dir = sub_root / "metric"
    metric_dir.mkdir(parents=True, exist_ok=True)
    with open(metric_dir / "node_service_map.pkl", "wb") as f:
        pickle.dump(node_service_map, f)
    with open(metric_dir / "service_node_map.pkl", "wb") as f:
        pickle.dump(service_node_map, f)

    (sub_root / "result").mkdir(parents=True, exist_ok=True)

    return sub_root
