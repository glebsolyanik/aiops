import matplotlib.pyplot as plt
import pandas as pd
from IPython.display import display

from .core import act, extract_export_path, read_csv


ERROR_RESPONSE_PATTERN = r"5[0-9]{2}|timeout|deadline|unavailable|refused|reset"


def _threshold_column_name(quantile):
    return f"p{int(quantile * 100)}_threshold"

def _duration_column_name(quantile):
    return f"p{int(quantile * 100)}_duration"



def _build_operation_stats(base):
    if base.empty:
        return pd.DataFrame(
            columns=["count", "mean", "p50", "p95", "p99", "max"]
        )

    stats = (
        base.groupby("operation_name")["duration"]
        .agg(
            count="count",
            mean="mean",
            p50=lambda s: s.quantile(0.50),
            p95=lambda s: s.quantile(0.95),
            p99=lambda s: s.quantile(0.99),
            max="max",
        )
        .sort_values("p95", ascending=False)
    )
    stats[["mean", "p50", "p95", "p99", "max"]] = stats[["mean", "p50", "p95", "p99", "max"]].round(2)
    return stats


def load_trace_dataframe(namespace, orch, lookback_minutes=5):
    trace_export = act(orch, "get_traces", namespace, lookback_minutes)
    trace_file = extract_export_path(trace_export)
    traces_df = read_csv(trace_file)
    return {
        "namespace": namespace,
        "lookback_minutes": lookback_minutes,
        "trace_export": trace_export,
        "trace_file": trace_file,
        "traces_df": traces_df,
    }


def _ensure_trace_context(trace_input, quantile=0.95):
    if isinstance(trace_input, dict) and "base" in trace_input:
        return trace_input
    return prepare_trace_context(trace_input, quantile=quantile)


def prepare_trace_context(namespace, orch, lookback_minutes=5, quantile=0.95):
    trace_export = act(orch, "get_traces", namespace, lookback_minutes)
    trace_file = extract_export_path(trace_export)
    traces_df = read_csv(trace_file)

    base = traces_df

    if {"trace_id", "span_id"}.issubset(base.columns):
        base = base.drop_duplicates(subset=["trace_id", "span_id"])

    base = base.dropna(subset=["trace_id", "operation_name", "duration"])
    base["duration"] = pd.to_numeric(base["duration"], errors="coerce") / 1000
    base = base.dropna(subset=["duration"])
    base = base[base["duration"] >= 0].copy()
    base["response"] = base.get("response", pd.Series(index=base.index, dtype="object")).fillna("").astype(str)
    base["has_error_flag"] = base.get("has_error", False).astype(str).str.lower().isin(["true", "1"])

    threshold_col = _threshold_column_name(quantile)
    operation_stats = _build_operation_stats(base)

    if "parent_span" in base.columns:
        roots = base[base["parent_span"] == "ROOT"].copy()
    else:
        roots = pd.DataFrame(columns=base.columns.tolist() + [threshold_col])

    if not roots.empty:
        roots[threshold_col] = roots.groupby("operation_name")["duration"].transform(lambda s: s.quantile(quantile))
        root_map = roots[["trace_id", "span_id"]].rename(columns={"span_id": "root_span_id"})
        root_children = base.merge(root_map, on="trace_id", how="inner")
        root_children = root_children[root_children["parent_span"] == root_children["root_span_id"]].copy()
    else:
        root_children = pd.DataFrame(columns=base.columns.tolist() + ["root_span_id"])

    return {
        "base": base,
        "roots": roots,
        "root_children": root_children,
        "operation_stats": operation_stats,
        "quantile": quantile,
        "threshold_col": threshold_col,
        "trace_export": trace_export,
        "trace_file": trace_file,
        "traces_df": traces_df,
    }


def summarize_operation_durations(trace_input, operations=None, top_n=10, sort_by="p95", quantile=0.95):
    trace_context = _ensure_trace_context(trace_input, quantile=quantile)
    stats = trace_context["operation_stats"].copy()
    if stats.empty:
        return stats

    stats = stats.sort_values(sort_by, ascending=False)
    if operations is None:
        return stats.head(top_n).copy()

    selected_ops = [op for op in operations if op in stats.index]
    if not selected_ops:
        return stats.iloc[0:0].copy()

    return stats.loc[selected_ops].copy()


def plot_operation_duration_distributions(trace_input, operations=None, top_n=4, sort_by="p95", bins=30, quantile=0.95):
    trace_context = _ensure_trace_context(trace_input, quantile=quantile)
    base = trace_context["base"]
    selected_stats = summarize_operation_durations(trace_context, operations=operations, top_n=top_n, sort_by=sort_by)

    if selected_stats.empty:
        print("Нет операций для построения графика.")
        return selected_stats

    selected_ops = selected_stats.index.tolist()
    fig, axes = plt.subplots(len(selected_ops), 2, figsize=(14, 4 * len(selected_ops)))
    if len(selected_ops) == 1:
        axes = [axes]

    for row_axes, op_name in zip(axes, selected_ops):
        hist_ax, box_ax = row_axes
        durations = base.loc[base["operation_name"] == op_name, "duration"]
        op_stats = selected_stats.loc[op_name]

        hist_ax.hist(durations, bins=bins, color="tab:blue", alpha=0.75)
        hist_ax.axvline(op_stats["p50"], color="tab:green", linestyle="--", label=f"p50={op_stats['p50']:.1f}")
        hist_ax.axvline(op_stats["p95"], color="tab:orange", linestyle="--", label=f"p95={op_stats['p95']:.1f}")
        hist_ax.axvline(op_stats["p99"], color="tab:red", linestyle="--", label=f"p99={op_stats['p99']:.1f}")
        hist_ax.set_title(op_name)
        hist_ax.set_xlabel("duration (sec)")
        hist_ax.set_ylabel("count")
        hist_ax.legend()

        box_ax.boxplot(durations, vert=False)
        box_ax.set_title(f"Boxplot: {op_name}")
        box_ax.set_xlabel("duration (sec)")
        box_ax.set_yticks([])

    plt.tight_layout()
    plt.show()
    return selected_stats


def select_bad_traces(trace_input, top_n=10, quantile=None):
    trace_context = _ensure_trace_context(
        trace_input,
        quantile=quantile if quantile is not None else 0.95,
    )
    threshold_col = trace_context["threshold_col"]
    quantile = trace_context["quantile"]
    roots = trace_context["roots"].copy()
    base = trace_context["base"]
    root_children = trace_context["root_children"]

    if roots.empty:
        return {
            "bad_roots": roots,
            "bad_trace_ids": [],
            "bad_spans": base.iloc[0:0].copy(),
            "bad_root_children": root_children.iloc[0:0].copy(),
            "root_summary": roots,
            "branch_summary": pd.DataFrame(),
        }

    roots["is_bad_root"] = (
        (roots["duration"] >= roots[threshold_col])
        | roots["has_error_flag"]
        | roots["response"].str.contains(ERROR_RESPONSE_PATTERN, case=False, regex=True, na=False)
    )

    bad_roots = roots[roots["is_bad_root"]].copy().sort_values("duration", ascending=False)
    bad_trace_ids = bad_roots["trace_id"].drop_duplicates().tolist()
    bad_spans = base[base["trace_id"].isin(bad_trace_ids)].copy()
    bad_root_children = root_children[root_children["trace_id"].isin(bad_trace_ids)].copy()

    duration_col = _duration_column_name(quantile)
    branch_summary = (
        bad_root_children.groupby(["service_name", "operation_name"])
        .agg(
            trace_count=("trace_id", "nunique"),
            span_count=("trace_id", "count"),
            mean_duration=("duration", "mean"),
            p_duration=("duration", lambda s: s.quantile(quantile)),
            max_duration=("duration", "max"),
        )
        .sort_values(["trace_count", "mean_duration"], ascending=False)
        .head(top_n)
        .reset_index()
    )
    if not branch_summary.empty:
        branch_summary.rename(columns={"p_duration": duration_col}, inplace=True)

    root_summary = bad_roots[
        ["trace_id", "service_name", "operation_name", "duration", threshold_col, "has_error", "response"]
    ].copy()

    return {
        "bad_roots": bad_roots,
        "bad_trace_ids": bad_trace_ids,
        "bad_spans": bad_spans,
        "bad_root_children": bad_root_children,
        "root_summary": root_summary,
        "branch_summary": branch_summary,
    }


def analyze_bad_traces(trace_input, top_n=10, plot=True, quantile=0.95):
    trace_context = _ensure_trace_context(trace_input, quantile=quantile)
    analysis = select_bad_traces(trace_context, top_n=top_n)
    branch_summary = analysis["branch_summary"]

    print(f"Всего trace: {trace_context['base']['trace_id'].nunique()}")
    print(f"Bad traces: {len(analysis['bad_trace_ids'])}")

    if plot and not branch_summary.empty:
        labels = branch_summary["service_name"] + " | " + branch_summary["operation_name"]
        plt.figure(figsize=(10, 5))
        plt.barh(labels, branch_summary["trace_count"], color="tab:blue")
        plt.xlabel("bad trace count")
        plt.ylabel("root child branch")
        plt.title("Most frequent root child branches in bad traces")
        plt.gca().invert_yaxis()
        plt.tight_layout()
        plt.show()

    return analysis


def build_trace_tree(trace_context, trace_id, max_depth=2):
    trace_spans = trace_context["base"][trace_context["base"]["trace_id"] == trace_id].copy()
    trace_spans = trace_spans.sort_values("start_time") if "start_time" in trace_spans.columns else trace_spans
    if trace_spans.empty:
        return trace_spans

    span_map = {row["span_id"]: row for _, row in trace_spans.iterrows()}
    children_map = {}
    for _, row in trace_spans.iterrows():
        children_map.setdefault(row["parent_span"], []).append(row["span_id"])

    roots = [span_id for span_id, row in span_map.items() if row["parent_span"] == "ROOT"]
    if not roots:
        return trace_spans

    root_span_id = roots[0]
    direct_children = children_map.get(root_span_id, [])
    longest_child_id = None
    if direct_children:
        longest_child_id = max(direct_children, key=lambda span_id: span_map[span_id]["duration"])

    rows = []

    def walk(span_id, depth):
        if depth > max_depth:
            return
        row = span_map[span_id]
        rows.append(
            {
                "trace_id": row["trace_id"],
                "depth": depth,
                "tree_label": f"{'  ' * depth}{row['service_name']} -> {row['operation_name']}",
                "service_name": row["service_name"],
                "operation_name": row["operation_name"],
                "duration": row["duration"],
                "has_error": row.get("has_error", False),
                "response": row.get("response", ""),
                "span_id": row["span_id"],
                "parent_span": row["parent_span"],
                "is_longest_root_child": row["span_id"] == longest_child_id,
            }
        )
        for child_id in children_map.get(span_id, []):
            walk(child_id, depth + 1)

    walk(root_span_id, 0)
    return pd.DataFrame(rows)


def inspect_top_bad_traces(trace_context, bad_trace_analysis, top_n=5, max_depth=2):
    threshold_col = trace_context["threshold_col"]
    top_bad_roots = bad_trace_analysis["bad_roots"].head(top_n).copy()
    trees = {}

    print(f"Топ {top_n} плохих трейсов:")
    if not top_bad_roots.empty:
        display(
            top_bad_roots[
                ["trace_id", "service_name", "operation_name", "duration", threshold_col, "has_error", "response"]
            ]
        )

    for trace_id in top_bad_roots["trace_id"]:
        tree_df = build_trace_tree(trace_context, trace_id, max_depth=max_depth)
        trees[trace_id] = tree_df
        print(f"Дерево трейса {trace_id}")
        if tree_df.empty:
            print("  Нет данных")
            continue
        display(
            tree_df[
                [
                    "depth",
                    "tree_label",
                    "duration",
                    "has_error",
                    "response",
                    "is_longest_root_child",
                ]
            ]
        )

    return trees