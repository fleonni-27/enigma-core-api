# Auditoria Histórica 2025 — 8 competições

Gerado em: `2026-08-24T11:12:40.187274+00:00`

> Escopo: universo retornado/armazenado sob o token/API Sportmonks atual. Não equivale a certificação de cobertura integral da temporada real.

Status: **OK**

Total alvo observado: **617 fixtures** · snapshots: **617** · training eligible: **617**

| Competição | Fixtures | Snapshots | Elegíveis | Elig.% | FULL_XG | STD_NO_XG | INCOMPLETE | NO_SNAPSHOT | Meses | Status |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| Serie A | 171 | 171 | 171 | 100.00% | 171 | 0 | 0 | 0 | 4/12 | PRESENT_AND_SNAPSHOT_COMPLETE_IN_OBSERVED_SCOPE |
| Serie B | 137 | 137 | 137 | 100.00% | 0 | 137 | 0 | 0 | 3/12 | PRESENT_AND_SNAPSHOT_COMPLETE_IN_OBSERVED_SCOPE |
| Copa do Brasil | 0 | 0 | 0 | 0.00% | 0 | 0 | 0 | 0 | 0/12 | ABSENT_IN_CURRENT_TOKEN_SCOPE |
| Copa Libertadores | 13 | 13 | 13 | 100.00% | 13 | 0 | 0 | 0 | 3/12 | PRESENT_AND_SNAPSHOT_COMPLETE_IN_OBSERVED_SCOPE |
| Copa Sudamericana | 0 | 0 | 0 | 0.00% | 0 | 0 | 0 | 0 | 0/12 | ABSENT_IN_CURRENT_TOKEN_SCOPE |
| Premier League | 156 | 156 | 156 | 100.00% | 156 | 0 | 0 | 0 | 4/12 | PRESENT_AND_SNAPSHOT_COMPLETE_IN_OBSERVED_SCOPE |
| La Liga | 140 | 140 | 140 | 100.00% | 140 | 0 | 0 | 0 | 4/12 | PRESENT_AND_SNAPSHOT_COMPLETE_IN_OBSERVED_SCOPE |
| Champions League | 0 | 0 | 0 | 0.00% | 0 | 0 | 0 | 0 | 0/12 | ABSENT_IN_CURRENT_TOKEN_SCOPE |

## Regras de interpretação

- `ABSENT_IN_CURRENT_TOKEN_SCOPE`: nenhuma fixture da competição foi observada no banco em 2025.
- `PRESENT_WITH_MISSING_SNAPSHOTS`: há fixtures observadas sem snapshot enriquecido.
- `PRESENT_AND_SNAPSHOT_COMPLETE_IN_OBSERVED_SCOPE`: todas as fixtures observadas têm snapshot; ainda assim não certifica o universo real da competição.
- `PARTIAL_AUDIT`: houve falha de chamada, limite ou divergência entre coverage e quality; revisar antes de concluir.

Erros registrados: **0**.
