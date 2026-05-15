from aiopslab.service.kubectl import KubeCtl
import pandas as pd
from IPython.display import display

def display_pods_status(namespace, kubectl):
    pods_nl = kubectl.list_pods(namespace).items
    rows_nl = []

    for p in pods_nl:
        statuses = p.status.container_statuses or []
        ready_count = sum(1 for cs in statuses if cs.ready)
        total_count = len(statuses)
        restarts = sum(cs.restart_count for cs in statuses)
        status = p.status.phase
        for cs in statuses:
            waiting = getattr(cs.state, "waiting", None)
            if waiting and waiting.reason:
                status = waiting.reason
                break
        rows_nl.append(
            {
                "pod": p.metadata.name,
                "ready": f"{ready_count}/{total_count}",
                "status": status,
                "restarts": restarts,
                "pod_ip": p.status.pod_ip,
                "node": p.spec.node_name,
            }
        )

    pods_nl_df = pd.DataFrame(rows_nl).sort_values(["status", "pod"])
    display(pods_nl_df)