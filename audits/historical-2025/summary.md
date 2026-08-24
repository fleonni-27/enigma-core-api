# Auditoria Histórica 2025 — 8 competições

Gerado em: `2026-08-24T14:18:54.376580+00:00`

> Escopo: universo retornado/armazenado sob o token/API Sportmonks atual. Não equivale a certificação de cobertura integral da temporada real.

Status: **PARTIAL**

Total alvo observado: **1424 fixtures** · snapshots: **1424** · training eligible: **1663**

| Competição | Fixtures | Snapshots | Elegíveis | Elig.% | FULL_XG | STD_NO_XG | INCOMPLETE | NO_SNAPSHOT | Meses | Status |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| Serie A | 330 | 330 | 380 | 115.15% | 380 | 0 | 0 | 0 | 9/12 | PARTIAL_AUDIT |
| Serie B | 330 | 330 | 380 | 115.15% | 0 | 380 | 0 | 0 | 7/12 | PARTIAL_AUDIT |
| Copa do Brasil | 0 | 0 | 0 | 0.00% | 0 | 0 | 0 | 0 | 0/12 | ABSENT_IN_CURRENT_TOKEN_SCOPE |
| Copa Libertadores | 107 | 107 | 155 | 144.86% | 142 | 13 | 0 | 0 | 7/12 | PARTIAL_AUDIT |
| Copa Sudamericana | 0 | 0 | 0 | 0.00% | 0 | 0 | 0 | 0 | 0/12 | ABSENT_IN_CURRENT_TOKEN_SCOPE |
| Premier League | 328 | 328 | 378 | 115.24% | 378 | 0 | 0 | 0 | 9/12 | PARTIAL_AUDIT |
| La Liga | 329 | 329 | 370 | 112.46% | 370 | 0 | 0 | 0 | 9/12 | PARTIAL_AUDIT |
| Champions League | 0 | 0 | 0 | 0.00% | 0 | 0 | 0 | 0 | 0/12 | ABSENT_IN_CURRENT_TOKEN_SCOPE |

## Regras de interpretação

- `ABSENT_IN_CURRENT_TOKEN_SCOPE`: nenhuma fixture da competição foi observada no banco em 2025.
- `PRESENT_WITH_MISSING_SNAPSHOTS`: há fixtures observadas sem snapshot enriquecido.
- `PRESENT_AND_SNAPSHOT_COMPLETE_IN_OBSERVED_SCOPE`: todas as fixtures observadas têm snapshot; ainda assim não certifica o universo real da competição.
- `PARTIAL_AUDIT`: houve falha de chamada, limite ou divergência entre coverage e quality; revisar antes de concluir.

Erros registrados: **1**.
