import re
import pandas as pd
from pathlib import Path

def act(orch, name, *args):
    return orch.session.problem.perform_action(name, *args)


def get_pod(namespace, service, kubectl, pattern="io.kompose.service="):
    return kubectl.get_pod_name(namespace, f"{pattern}={service}")


def extract_export_path(text):
    if not isinstance(text, str):
        return None
    match = re.search(r"(/[^\n]+)", text)
    return Path(match.group(1).strip()) if match else None


def read_csv(path):
    path = Path(path)
    return pd.read_csv(path) if path.exists() else pd.DataFrame()