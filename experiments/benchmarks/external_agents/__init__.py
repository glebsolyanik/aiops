# External agents integration: export, convert, run, compare

from pathlib import Path

# Default repo locations: first external_agents/external/{RCLAgent,mABC}, then experiments/agents/
_EXTERNAL_AGENTS_DIR = Path(__file__).resolve().parent
_EXTERNAL_REPO = _EXTERNAL_AGENTS_DIR / "external"
# experiments/agents/ — папка с вашими клонами RCLAgent, mABC и т.д.
AGENTS_ROOT = _EXTERNAL_AGENTS_DIR.resolve().parent.parent / "agents"
