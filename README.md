# Enigma Core API

Backend da Enigma Core para ingestão de dados de futebol, inferência pré-jogo, decisão auditável, forward test, CLV, rating research e observabilidade operacional.

## Produção

- API: `https://enigma-core-api.onrender.com`
- Swagger: `https://enigma-core-api.onrender.com/docs`
- Deploy: GitHub `main` -> Render Auto-Deploy
- Banco: PostgreSQL / Supabase
- Fonte de dados: Sportmonks
- Wrapper de produção: `app.main_v017`
- Versão atual: **0.50.0**

## Arquitetura operacional atual

Fluxo principal:

`Sportmonks -> ingestão/odds -> J1 -> Prediction -> Decision Engine -> DecisionRecord/Ledger -> settlement/CLV -> Forward-Test Report`

O J1 tem um único entrypoint de cron em produção:

`python -m app.j1_scheduler`

Ele opera com `J1_EXECUTION_MODE=batch` como modo fail-safe. Após a ativação do Horizontal V1, o mesmo entrypoint usa `producer`, enfileira trabalho por fixture no PostgreSQL e os workers executam:

`python -m app.j1_claim_worker`

A implementação canônica do producer é `app.j1_work_producer`; ela é delegada pelo scheduler e não precisa de um segundo comando de cron.

## J1 Horizontal V1

Implementado no código e no Blueprint:

- work queue PostgreSQL por fixture + snapshot window;
- `FOR UPDATE SKIP LOCKED`;
- leases, claim tokens e retries limitados;
- três workers declarados no Render Blueprint;
- capacidade operacional 5 -> 10 -> 20, com `J1_MAX_FIXTURES=20`;
- secret wiring de `DATABASE_URL` e `SPORTMONKS_API_TOKEN` via `fromService.envVarKey`;
- Prediction/Decision/Ledger continuam imutáveis e sem duplicação de lógica de negócio.

O cutover operacional só é considerado concluído depois que o serviço real `enigma-j1-worker` existir no Render com três instâncias saudáveis e o cron for alterado de `batch` para `producer`.

## Enigma Rating V2

`enigma_rating_v2_research_v1` adiciona uma camada research-only com sinais explícitos de:

- Poisson 1X2;
- Dixon-Coles para placares baixos;
- Elo + Davidson para empate;
- xG e xGA históricos;
- forma exata de 10 jogos;
- impacto de lineup/desfalques somente quando houver valor auditável do XI/ausências;
- confiança calibrada e edge de mercado.

`GET /rating/context-v2/{sportmonks_fixture_id}` deriva automaticamente gols, xG/xGA, forma-10 e Elo usando somente dados anteriores ao kickoff da fixture. O contexto de lineup expõe o XI observado, mas não inventa valor para jogadores ausentes.

O Rating V2 **não substitui** o modelo promovido. `baseline_1x2_temporal_v1`, as 36 features STANDARD e os thresholds do Decision Engine permanecem inalterados até validação temporal por Brier, Log Loss, calibração e CLV.

## Forward Test e CLV

A API expõe Forward-Test Report V2/V3, métricas de qualidade probabilística, calibração, ROI diagnóstico e cobertura/distribuição de CLV. O sistema permanece **research-only** e não executa apostas reais automaticamente.

## Contratos e continuidade

Referências principais:

- `docs/j1-operations-v1.md` — contrato operacional canônico de J1, entrypoints, Render, cutover e rollback;
- `docs/enigma-rating-v2.md` — Poisson, Dixon-Coles, Elo, xG/xGA, forma-10, lineup e política de promoção da V2;
- `docs/daily-prediction-runner-v1.md` — semântica do fluxo Prediction/Decision/Ledger em J1;
- `docs/performance-scale-v1.md` — pooling, concorrência, capacidade 5/10/20 e Horizontal V1;
- `docs/forward-test-report-v3.md` — qualidade probabilística, calibração e CLV;
- `docs/PROGRESS.md` — checkpoint de engenharia atual.

## CI operacional

`.github/workflows/j1-hardening-checks.yml` usa descoberta automática de `tests/test_*.py` e valida também o contrato entre código, `render.yaml`, entrypoints e documentação com:

`python scripts/validate_j1_operational_contract.py`

Isso impede que um PR reintroduza entrypoints duplicados ou deixe Render/docs fora de sincronia com a arquitetura J1 vigente.
