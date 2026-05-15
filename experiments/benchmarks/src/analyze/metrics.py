import pandas as pd
import matplotlib.pyplot as plt
from IPython.display import display

from .core import extract_export_path, get_pod, read_csv, act

def prepare_metric_context(namespace, services, kubectl, orch, lookback_minutes=5):
    metrics_export = act(orch, "get_metrics", namespace, lookback_minutes)
    metric_dir = extract_export_path(metrics_export)
    container_dir = metric_dir / "container"
    pod_map = {service: get_pod(namespace, service, kubectl) for service in services}

    return {
        "namespace": namespace,
        "lookback_minutes": lookback_minutes,
        "metric_dir": metric_dir,
        "container_dir": container_dir,
        "metric_files": sorted(path.name for path in container_dir.glob("*.csv")),
        "pod_map": pod_map,
        "metric_cache": {},
    }


def load_metric_file(metric_context, metric_filename):
    if metric_filename in metric_context["metric_cache"]:
        return metric_context["metric_cache"][metric_filename]

    path = metric_context["container_dir"] / metric_filename
    df = read_csv(path)
    if df.empty:
        metric_context["metric_cache"][metric_filename] = df
        return df

    df = df.copy()
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["timestamp", "value", "cmdb_id"])
    df["timestamp_dt"] = pd.to_datetime(df["timestamp"], unit="s")
    metric_context["metric_cache"][metric_filename] = df
    return df


def get_service_metric_timeseries(metric_context, service, metric_filename):
    df = load_metric_file(metric_context, metric_filename)
    if df.empty:
        return pd.DataFrame()

    pod_name = metric_context["pod_map"].get(service, service)
    subset = df[df["cmdb_id"].str.contains(pod_name, na=False)].copy()
    if subset.empty:
        return pd.DataFrame()

    grouped = (
        subset.groupby("timestamp", as_index=False)
        .agg(value=("value", "sum"))
        .sort_values("timestamp")
    )
    grouped["timestamp_dt"] = pd.to_datetime(grouped["timestamp"], unit="s")
    grouped["service"] = service
    grouped["metric"] = metric_filename
    grouped["pod_name"] = pod_name
    grouped["delta_value"] = grouped["value"].diff().fillna(0).clip(lower=0)
    grouped["signal_value"] = grouped["delta_value"] if metric_filename.endswith("_total.csv") else grouped["value"]
    grouped["signal_kind"] = "delta" if metric_filename.endswith("_total.csv") else "value"
    return grouped


def summarize_service_metrics(metric_context, services, metric_filenames):
    rows = []

    for service in services:
        for metric_filename in metric_filenames:
            ts = get_service_metric_timeseries(metric_context, service, metric_filename)
            if ts.empty:
                continue

            signal = ts["signal_value"]
            rows.append(
                {
                    "service": service,
                    "metric": metric_filename,
                    "signal_kind": ts["signal_kind"].iloc[0],
                    "points": len(ts),
                    "latest": signal.iloc[-1],
                    "mean": signal.mean(),
                    "p95": signal.quantile(0.95),
                    "max": signal.max(),
                }
            )

    return pd.DataFrame(rows).sort_values(["metric", "service"]).reset_index(drop=True)


def plot_service_metric_grid(metric_context, services, metric_filenames, last_n=30):
    fig, axes = plt.subplots(len(metric_filenames), 1, figsize=(12, 3.5 * len(metric_filenames)), sharex=False)
    if len(metric_filenames) == 1:
        axes = [axes]

    for ax, metric_filename in zip(axes, metric_filenames):
        plotted = False
        signal_kind = None

        for service in services:
            ts = get_service_metric_timeseries(metric_context, service, metric_filename)
            if ts.empty:
                continue
            ts = ts.tail(last_n)
            signal_kind = ts["signal_kind"].iloc[0]
            ax.plot(ts["timestamp_dt"], ts["signal_value"], marker="o", linewidth=1.5, label=service)
            plotted = True

        title_suffix = f" ({signal_kind})" if signal_kind else ""
        ax.set_title(metric_filename.replace("kpi_", "").replace(".csv", "") + title_suffix)
        ax.set_ylabel("value")
        ax.tick_params(axis="x", rotation=45)
        if plotted:
            ax.legend()
        else:
            ax.text(0.5, 0.5, "no data", ha="center", va="center")
            ax.axis("off")

    plt.tight_layout()
    plt.show()


def inspect_service_metrics(metric_context, services, metric_filenames, last_n=30):
    summary_df = summarize_service_metrics(metric_context, services, metric_filenames)
    plot_service_metric_grid(metric_context, services, metric_filenames, last_n=last_n)
    return summary_df