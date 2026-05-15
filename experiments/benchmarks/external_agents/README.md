# External agents integration (RCLAgent, mABC, SRE-agent)

Offline comparison and optional AIOpsLab adapters for [RCLAgent](https://github.com/LLMLog/RCLAgent), [mABC](https://github.com/zwpride/mABC), and [AWS SRE-agent](https://github.com/awslabs/amazon-bedrock-agentcore-samples/tree/main/02-use-cases/SRE-agent).

## 1. Export dataset (AIOpsLab)

From `experiments/benchmarks` (or AIOpsLab root if you set `--aiopslab`):

```bash
python external_agents/export_dataset.py --problems network_loss_hotel_res-detection-1 network_loss_hotel_res-localization-1 --export-root external_agents/export
```

Requires a live Kubernetes cluster and AIOpsLab env. Exports `export/{problem_id}/` with `traces.csv` (or `trace_output/`), `metrics_output/`, and `ground_truth.json`.

## 2. Convert and run external agents

Репо агентов ищутся в двух местах (по порядку):
1. `external_agents/external/RCLAgent`, `external_agents/external/mABC`
2. **`experiments/agents/RCLAgent`**, **`experiments/agents/mABC`** — папка с вашими клонами

Если репо лежит в `experiments/agents/`, ничего дополнительно настраивать не нужно. Иначе укажите корень: `--agents-root /path/to/folder` (внутри должны быть подпапки `RCLAgent`, `mABC`).

- **RCLAgent**: клонируйте в `external_agents/external/RCLAgent` или в `experiments/agents/RCLAgent`. Запуск: `python external_agents/run_rcl.py` или `run_all.py`.
- **mABC**: клонируйте в `external_agents/external/mABC` или в `experiments/agents/mABC`. Запуск: `python external_agents/run_mabc.py` или `run_all.py`. Перед запуском подгружается **`AIOpsLab/.env`**: те же переменные, что и у React/OpenAI-compatible (`OPENAI_COMPATIBLE_API_KEY`, `OPENAI_COMPATIBLE_BASE_URL`, `OPENAI_COMPATIBLE_MODEL` или `OPENAI_API_KEY` / `OPENAI_MODEL`). **`MABC_VERBOSE=1`** (по умолчанию) — потоковый вывод stdout/stderr в консоль, как у RCL. **`MABC_REASONING_LOG=1`** — блоки `[mABC-Agent]` (Thought / Action / Observation / фазы main.py). Отключить структурный лог: `MABC_REASONING_LOG=0`. Полный дамп промптов в консоль: `MABC_LLM_RAW_DUMP=1`. Объект ответа API: `MABC_PRINT_COMPLETION_OBJECT=1`.
- **SRE-agent**: установите CLI `sre-agent` и запускайте: `python external_agents/run_sre_agent.py` или `run_all.py`.

## 3. Compare results

```bash
python external_agents/compare_results.py --export-root external_agents/export --results-dir external_agents/results --output external_agents/results/comparison.json
```

Prints a table (problem_id, agent, success, accuracy) and summary by agent.

## 4. Run all and compare

```bash
python external_agents/run_all.py --export-root external_agents/export --results-dir external_agents/results
```

Если агенты в `experiments/agents/`:
```bash
python external_agents/run_all.py --agents-root ../agents
```
(относительно `external_agents/`; или полный путь к папке с RCLAgent и mABC).

## 5. Запуск в бенчмарке (как React)

Агенты `rcl`, `mabc`, `sre_agent` и `react` можно запускать в том же цикле, что и React: оркестратор вызывает `get_traces`/`get_metrics`, затем агент возвращает `submit(...)`.

**Из командной строки** (из каталога `AIOpsLab`):

```bash
cd experiments/benchmarks/AIOpsLab
python -m clients.run_agent --list
python -m clients.run_agent rcl network_loss_hotel_res-detection-1 30
python -m clients.run_agent mabc network_loss_hotel_res-localization-1
python -m clients.run_agent sre_agent network_loss_hotel_res-detection-1 20
```

**В ноутбуке** (тот же сценарий, что и для React):

```python
from aiopslab.orchestrator import Orchestrator
from clients.registry import AgentRegistry

reg = AgentRegistry()
agent = reg.get_agent("rcl")()   # или "mabc", "sre_agent", "react"
orch = Orchestrator()
orch.register_agent(agent, name="rcl-agent")

problem_desc, instructs, apis = orch.init_problem("network_loss_hotel_res-detection-1")
agent.init_context(problem_desc, instructs, apis)

output = await orch.start_problem(max_steps=30)
```

**Через API**: `POST /simulate` с `agent_name: "rcl"` (или `mabc`, `sre_agent`). Репо RCL/mABC ищутся в `external/` или в `experiments/agents/`.
