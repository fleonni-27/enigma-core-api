from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]

WEB_NAME = "enigma-core-api"
CRON_NAME = "enigma-j1-runner"
WORKER_NAME = "enigma-j1-worker"
WEB_START = "uvicorn app.main_v017:app --host 0.0.0.0 --port $PORT"
CRON_START = "python -m app.j1_scheduler"
WORKER_START = "python -m app.j1_claim_worker"
CANONICAL_DOC = "docs/j1-operations-v1.md"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"J1 operational contract violation: {message}")


def _load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    _require(isinstance(payload, dict), f"{path.relative_to(ROOT)} must contain a mapping")
    return payload


def _service_by_name(config: dict[str, Any], name: str) -> dict[str, Any]:
    services = config.get("services") or []
    _require(isinstance(services, list), "render.yaml services must be a list")
    matches = [service for service in services if service.get("name") == name]
    _require(len(matches) == 1, f"render.yaml must define exactly one {name}")
    return matches[0]


def _env_by_key(service: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = service.get("envVars") or []
    _require(isinstance(rows, list), f"{service.get('name')} envVars must be a list")
    return {row.get("key"): row for row in rows if isinstance(row, dict) and row.get("key")}


def _require_secret_ref(service: dict[str, Any], key: str) -> None:
    env = _env_by_key(service)
    _require(key in env, f"{service.get('name')} must define {key}")
    ref = env[key].get("fromService")
    expected = {"type": "web", "name": WEB_NAME, "envVarKey": key}
    _require(ref == expected, f"{service.get('name')} {key} must reference {WEB_NAME}.{key}")


def _require_text(path: str, snippets: list[str]) -> None:
    target = ROOT / path
    _require(target.exists(), f"missing {path}")
    text = target.read_text(encoding="utf-8")
    for snippet in snippets:
        _require(snippet in text, f"{path} must contain {snippet!r}")


def validate_render_contract() -> None:
    config = _load_yaml(ROOT / "render.yaml")
    web = _service_by_name(config, WEB_NAME)
    cron = _service_by_name(config, CRON_NAME)
    worker = _service_by_name(config, WORKER_NAME)

    _require(web.get("type") == "web", f"{WEB_NAME} must remain a web service")
    _require(web.get("startCommand") == WEB_START, f"{WEB_NAME} start command drifted")

    _require(cron.get("type") == "cron", f"{CRON_NAME} must remain a cron service")
    _require(cron.get("startCommand") == CRON_START, f"{CRON_NAME} must use the canonical entrypoint")
    _require(cron.get("schedule") == "* * * * *", f"{CRON_NAME} must run every minute")
    cron_env = _env_by_key(cron)
    _require(
        (cron_env.get("J1_EXECUTION_MODE") or {}).get("value") == "batch",
        "Blueprint cutover default must remain fail-safe batch",
    )
    _require_secret_ref(cron, "DATABASE_URL")
    _require_secret_ref(cron, "SPORTMONKS_API_TOKEN")

    _require(worker.get("type") == "worker", f"{WORKER_NAME} must remain a background worker")
    _require(worker.get("startCommand") == WORKER_START, f"{WORKER_NAME} start command drifted")
    _require(worker.get("numInstances") == 3, f"{WORKER_NAME} must declare exactly three instances for Horizontal V1")
    _require(
        int(worker.get("maxShutdownDelaySeconds") or 0) >= 180,
        f"{WORKER_NAME} must preserve enough drain time for an active claim",
    )
    _require(worker.get("region") == web.get("region") == cron.get("region"), "J1 services must stay in one Render region")
    _require_secret_ref(worker, "DATABASE_URL")
    _require_secret_ref(worker, "SPORTMONKS_API_TOKEN")


def validate_entrypoint_contract() -> None:
    _require((ROOT / "app/j1_scheduler.py").exists(), "canonical cron entrypoint is missing")
    _require((ROOT / "app/j1_work_producer.py").exists(), "canonical producer implementation is missing")
    _require((ROOT / "app/j1_claim_worker.py").exists(), "canonical worker entrypoint is missing")
    _require(not (ROOT / "app/j1_scheduler_v2.py").exists(), "deprecated duplicate app/j1_scheduler_v2.py must not return")


def validate_docs_contract() -> None:
    _require_text(
        CANONICAL_DOC,
        [
            CRON_START,
            WORKER_START,
            "J1_EXECUTION_MODE=batch",
            "J1_EXECUTION_MODE=producer",
            "app.j1_work_producer",
            "GET /operations/j1-work/status",
        ],
    )
    _require_text("README.md", [CANONICAL_DOC, CRON_START, WORKER_START])
    _require_text("docs/daily-prediction-runner-v1.md", [CANONICAL_DOC, "Render cron", "GitHub Actions"])
    _require_text("docs/performance-scale-v1.md", ["J1_MAX_FIXTURES=20", "three horizontal claim workers"])


def validate_ci_contract() -> None:
    workflow = ".github/workflows/j1-hardening-checks.yml"
    _require_text(
        workflow,
        [
            "python scripts/validate_j1_operational_contract.py",
            "python -m unittest discover -s tests -p 'test_*.py' -v",
            '"scripts/**"',
            '"docs/**"',
            '"tests/**"',
        ],
    )


def main() -> None:
    validate_render_contract()
    validate_entrypoint_contract()
    validate_docs_contract()
    validate_ci_contract()
    print("J1 operational contract: OK")


if __name__ == "__main__":
    main()
