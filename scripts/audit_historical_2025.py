from __future__ import annotations

import calendar
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE = os.getenv("ENIGMA_API_BASE", "https://enigma-core-api.onrender.com").rstrip("/")
YEAR = 2025
TARGET_LEAGUES = [
    "Serie A",
    "Serie B",
    "Copa do Brasil",
    "Copa Libertadores",
    "Copa Sudamericana",
    "Premier League",
    "La Liga",
    "Champions League",
]
OUT_DIR = Path("audits/historical-2025")
OUT_JSON = OUT_DIR / "audit.json"
OUT_MD = OUT_DIR / "summary.md"


def get_json(path: str, params: list[tuple[str, str]] | dict[str, str], *, timeout: int = 900, attempts: int = 4) -> dict:
    query = urlencode(params, doseq=True)
    url = f"{API_BASE}{path}?{query}" if query else f"{API_BASE}{path}"
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = Request(url, headers={"User-Agent": "enigma-core-audit/2025"})
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("status") != "ok":
                raise RuntimeError(f"non-ok payload: {payload.get('status')}")
            return payload
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < attempts:
                time.sleep(10 * attempt)
    raise RuntimeError(f"GET failed after {attempts} attempts: {url}: {last_error}")


def month_bounds(month: int) -> tuple[str, str]:
    last_day = calendar.monthrange(YEAR, month)[1]
    return f"{YEAR}-{month:02d}-01", f"{YEAR}-{month:02d}-{last_day:02d}"


def pct(numerator: int, denominator: int) -> float:
    return round((numerator / denominator) * 100.0, 2) if denominator else 0.0


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat()

    totals: dict[str, dict] = {
        league: {
            "league": league,
            "fixtures": 0,
            "snapshots": 0,
            "lineups_fixtures": 0,
            "statistics_fixtures": 0,
            "xg_fixtures": 0,
            "FULL_XG": 0,
            "STANDARD_NO_XG": 0,
            "INCOMPLETE": 0,
            "NO_SNAPSHOT": 0,
            "training_eligible": 0,
            "months_present": 0,
            "months_audited": 0,
            "quality_scope_mismatches": 0,
            "errors": [],
        }
        for league in TARGET_LEAGUES
    }
    monthly: list[dict] = []
    global_errors: list[dict] = []

    for month in range(1, 13):
        start_date, end_date = month_bounds(month)
        month_entry = {
            "month": f"{YEAR}-{month:02d}",
            "start_date": start_date,
            "end_date": end_date,
            "coverage_status": "pending",
            "leagues": {},
            "errors": [],
        }

        try:
            coverage = get_json(
                "/coverage/data",
                {"start_date": start_date, "end_date": end_date},
            )
            month_entry["coverage_status"] = "ok"
        except Exception as exc:  # noqa: BLE001
            coverage = {"leagues": []}
            month_entry["coverage_status"] = "failed"
            month_entry["errors"].append({"stage": "coverage", "error": str(exc)})
            global_errors.append({"month": month_entry["month"], "stage": "coverage", "error": str(exc)})

        coverage_by_league = {
            row.get("league"): row
            for row in coverage.get("leagues", [])
            if row.get("target") and row.get("league") in TARGET_LEAGUES
        }

        for league in TARGET_LEAGUES:
            c = coverage_by_league.get(league) or {}
            league_month = {
                "league": league,
                "fixtures": int(c.get("fixtures") or 0),
                "snapshots": int(c.get("snapshots") or 0),
                "lineups_fixtures": int(c.get("lineups_fixtures") or 0),
                "statistics_fixtures": int(c.get("statistics_fixtures") or 0),
                "xg_fixtures": int(c.get("xg_fixtures") or 0),
                "quality_status": "pending",
                "profiles": {"FULL_XG": 0, "STANDARD_NO_XG": 0, "INCOMPLETE": 0, "NO_SNAPSHOT": 0},
                "training_eligible": 0,
                "quality_selected_fixtures": 0,
                "scope_matches_coverage": None,
                "errors": [],
            }

            try:
                quality = get_json(
                    "/quality/features",
                    [
                        ("start_date", start_date),
                        ("end_date", end_date),
                        ("leagues", league),
                        ("limit", "200"),
                    ],
                )
                summary = quality.get("summary") or {}
                profiles = summary.get("profiles") or {}
                selected = int(quality.get("selected_fixtures") or 0)
                league_month["quality_status"] = "ok"
                league_month["profiles"] = {
                    "FULL_XG": int(profiles.get("FULL_XG") or 0),
                    "STANDARD_NO_XG": int(profiles.get("STANDARD_NO_XG") or 0),
                    "INCOMPLETE": int(profiles.get("INCOMPLETE") or 0),
                    "NO_SNAPSHOT": int(profiles.get("NO_SNAPSHOT") or 0),
                }
                league_month["training_eligible"] = int(summary.get("training_eligible") or 0)
                league_month["quality_selected_fixtures"] = selected
                league_month["scope_matches_coverage"] = selected == league_month["fixtures"]
                if selected >= 200:
                    league_month["errors"].append("QUALITY_LIMIT_REACHED_200_REVIEW_REQUIRED")
                if not league_month["scope_matches_coverage"]:
                    league_month["errors"].append("QUALITY_SCOPE_DIFFERS_FROM_COVERAGE")
            except Exception as exc:  # noqa: BLE001
                league_month["quality_status"] = "failed"
                league_month["errors"].append(str(exc))
                global_errors.append({"month": month_entry["month"], "league": league, "stage": "quality", "error": str(exc)})

            month_entry["leagues"][league] = league_month
            t = totals[league]
            t["fixtures"] += league_month["fixtures"]
            t["snapshots"] += league_month["snapshots"]
            t["lineups_fixtures"] += league_month["lineups_fixtures"]
            t["statistics_fixtures"] += league_month["statistics_fixtures"]
            t["xg_fixtures"] += league_month["xg_fixtures"]
            t["FULL_XG"] += league_month["profiles"]["FULL_XG"]
            t["STANDARD_NO_XG"] += league_month["profiles"]["STANDARD_NO_XG"]
            t["INCOMPLETE"] += league_month["profiles"]["INCOMPLETE"]
            t["NO_SNAPSHOT"] += league_month["profiles"]["NO_SNAPSHOT"]
            t["training_eligible"] += league_month["training_eligible"]
            if league_month["fixtures"] > 0:
                t["months_present"] += 1
            if league_month["quality_status"] == "ok":
                t["months_audited"] += 1
            if league_month["scope_matches_coverage"] is False:
                t["quality_scope_mismatches"] += 1
            t["errors"].extend(league_month["errors"])

        monthly.append(month_entry)

    by_league = []
    for league in TARGET_LEAGUES:
        row = totals[league]
        row["snapshot_coverage_pct"] = pct(row["snapshots"], row["fixtures"])
        row["lineups_pct"] = pct(row["lineups_fixtures"], row["fixtures"])
        row["statistics_pct"] = pct(row["statistics_fixtures"], row["fixtures"])
        row["xg_pct"] = pct(row["xg_fixtures"], row["fixtures"])
        row["training_eligibility_pct_of_fixtures"] = pct(row["training_eligible"], row["fixtures"])
        row["training_eligibility_pct_of_snapshots"] = pct(row["training_eligible"], row["snapshots"])
        if row["fixtures"] == 0:
            row["observed_coverage_status"] = "ABSENT_IN_CURRENT_TOKEN_SCOPE"
        elif row["months_audited"] < 12 or row["quality_scope_mismatches"]:
            row["observed_coverage_status"] = "PARTIAL_AUDIT"
        elif row["snapshots"] < row["fixtures"]:
            row["observed_coverage_status"] = "PRESENT_WITH_MISSING_SNAPSHOTS"
        else:
            row["observed_coverage_status"] = "PRESENT_AND_SNAPSHOT_COMPLETE_IN_OBSERVED_SCOPE"
        by_league.append(row)

    total_fixtures = sum(row["fixtures"] for row in by_league)
    total_snapshots = sum(row["snapshots"] for row in by_league)
    total_eligible = sum(row["training_eligible"] for row in by_league)
    status = "ok" if not global_errors and all(row["months_audited"] == 12 for row in by_league) else "partial"

    audit = {
        "status": status,
        "version": "historical_2025_audit_v1",
        "generated_at": generated_at,
        "source": API_BASE,
        "year": YEAR,
        "target_leagues": TARGET_LEAGUES,
        "scope_note": "Audit describes the universe returned/stored under the current Sportmonks token/API. It does not prove complete real-world season coverage.",
        "totals": {
            "fixtures": total_fixtures,
            "snapshots": total_snapshots,
            "training_eligible": total_eligible,
            "snapshot_coverage_pct": pct(total_snapshots, total_fixtures),
            "training_eligibility_pct_of_fixtures": pct(total_eligible, total_fixtures),
            "errors": len(global_errors),
        },
        "by_league": by_league,
        "monthly": monthly,
        "errors": global_errors,
        "policy": {
            "read_only": True,
            "no_backfill_triggered": True,
            "no_model_retraining_triggered": True,
            "quality_endpoint": "/quality/features",
            "coverage_endpoint": "/coverage/data",
            "quality_limit_per_league_month": 200,
        },
    }
    OUT_JSON.write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        "# Auditoria Histórica 2025 — 8 competições",
        "",
        f"Gerado em: `{generated_at}`",
        "",
        "> Escopo: universo retornado/armazenado sob o token/API Sportmonks atual. Não equivale a certificação de cobertura integral da temporada real.",
        "",
        f"Status: **{status.upper()}**",
        "",
        f"Total alvo observado: **{total_fixtures} fixtures** · snapshots: **{total_snapshots}** · training eligible: **{total_eligible}**",
        "",
        "| Competição | Fixtures | Snapshots | Elegíveis | Elig.% | FULL_XG | STD_NO_XG | INCOMPLETE | NO_SNAPSHOT | Meses | Status |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in by_league:
        lines.append(
            f"| {row['league']} | {row['fixtures']} | {row['snapshots']} | {row['training_eligible']} | "
            f"{row['training_eligibility_pct_of_fixtures']:.2f}% | {row['FULL_XG']} | {row['STANDARD_NO_XG']} | "
            f"{row['INCOMPLETE']} | {row['NO_SNAPSHOT']} | {row['months_present']}/12 | {row['observed_coverage_status']} |"
        )
    lines.extend([
        "",
        "## Regras de interpretação",
        "",
        "- `ABSENT_IN_CURRENT_TOKEN_SCOPE`: nenhuma fixture da competição foi observada no banco em 2025.",
        "- `PRESENT_WITH_MISSING_SNAPSHOTS`: há fixtures observadas sem snapshot enriquecido.",
        "- `PRESENT_AND_SNAPSHOT_COMPLETE_IN_OBSERVED_SCOPE`: todas as fixtures observadas têm snapshot; ainda assim não certifica o universo real da competição.",
        "- `PARTIAL_AUDIT`: houve falha de chamada, limite ou divergência entre coverage e quality; revisar antes de concluir.",
        "",
        f"Erros registrados: **{len(global_errors)}**.",
    ])
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps(audit["totals"], ensure_ascii=False))
    print(f"Wrote {OUT_JSON} and {OUT_MD}")


if __name__ == "__main__":
    main()
