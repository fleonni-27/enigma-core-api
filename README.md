# Enigma Core API

Backend da Enigma Core para ingestão, auditoria, controle de qualidade e preparação de dados históricos de futebol.

## Produção

- API: `https://enigma-core-api.onrender.com`
- Swagger: `https://enigma-core-api.onrender.com/docs`
- Deploy: GitHub `main` -> Render Auto-Deploy
- Banco: PostgreSQL / Supabase
- Fonte de dados: Sportmonks
- Versão atual: **0.12.0**

## Pipeline validado

`Sportmonks -> FastAPI -> Supabase/PostgreSQL -> RAW Snapshot -> Audit -> Quality Gate -> Quality Batch v2 -> Feature Profile v1 -> Training Eligibility`

## Estado atual

A fase de validação estrutural foi concluída com sucesso em Serie A, Serie B, La Liga, Premier League e Copa Libertadores.

Foram testados **130 snapshots enriquecidos**, com **130/130 elegíveis para treinamento e 0 rejeições estruturais após snapshot**.

Feature Profile v1:

- Serie A, La Liga, Premier League e Libertadores: amostras enriquecidas classificadas como `FULL_XG`.
- Serie B: amostra enriquecida classificada como `STANDARD_NO_XG`.
- xG ausente nunca é interpretado como zero.

## Próximo marco

**Backfill Historical Controller v1**: execução mês a mês e liga a liga com checkpoints, retomada segura, idempotência, limites de API e Quality/Feature checks automáticos após cada lote.

## Documentação de continuidade

O estado detalhado do projeto está registrado em:

`docs/PROJECT_STATE_2026-08-21.md`

Esse arquivo deve ser tratado como referência operacional do estado atual do projeto.
