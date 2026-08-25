# Enigma Core API

Backend da Enigma Core para ingestão de dados de futebol, inferência pré-jogo, decisão auditável, forward test, settlement, CLV, rating research e observabilidade operacional.

## Produção

- API: `https://enigma-core-api.onrender.com`
- Swagger: `https://enigma-core-api.onrender.com/docs`
- Deploy: GitHub `main` -> Render Auto-Deploy
- Banco: PostgreSQL / Supabase
- Fonte de dados: Sportmonks
- Wrapper de produção: `app.main_v017`
- Versão desta entrega: **0.52.0**

## Arquitetura operacional atual

Fluxo principal:

`Sportmonks -> ingestão/odds -> J1 producer -> PostgreSQL work queue -> J1 workers -> Prediction -> Decision Engine -> DecisionRecord/Ledger -> settlement/CLV -> Daily Analysis / Forward-Test Report`

O J1 tem um único entrypoint de cron:

`python -m app.j1_scheduler`

Em produção ele opera com `J1_EXECUTION_MODE=producer`, enfileira trabalho por fixture no PostgreSQL e três instâncias do worker executam:

`python -m app.j1_claim_worker`

A implementação canônica do producer é `app.j1_work_producer`; ela é delegada pelo scheduler e não precisa de um segundo comando de cron.

O cutover de infraestrutura do Horizontal V1 está concluído. A prova final `enqueue -> claim -> Prediction -> Decision -> Ledger` depende de uma fixture realmente devida na janela J1; nenhum ciclo é fabricado para fechar esse marco.

## J1 Horizontal V1

Operacional:

- work queue PostgreSQL por fixture + snapshot window;
- `FOR UPDATE SKIP LOCKED`;
- leases, claim tokens e retries limitados;
- três workers Starter em Virginia;
- capacidade operacional 5 -> 10 -> 20, com `J1_MAX_FIXTURES=20`;
- secret wiring de `DATABASE_URL` e `SPORTMONKS_API_TOKEN` via `fromService.envVarKey`;
- Prediction/Decision/Ledger continuam imutáveis e sem duplicação de lógica de negócio.

## Settlement e Daily Analysis

Outcome Settlement V1 é idempotente, liquida apenas fixtures finalizadas e preserva a semântica 1X2 de tempo regulamentar.

O settlement agendado continua disponível via GitHub Actions. Além disso, `enigma-daily-analysis-report` roda diariamente às **01:30 BRT** (`30 4 * * *` UTC) no Render e executa settlement antes de gerar/persistir o Forward-Test Report V3 do dia anterior.

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

O Rating V2 **não substitui** o modelo promovido. `baseline_1x2_temporal_v1`, as 36 features STANDARD e os thresholds do Decision Engine permanecem inalterados.

## Enigma Rating V2 Evaluation V1

`GET /research/enigma-rating-v2/evaluation-v1` compara temporalmente o STANDARD contra:

- Poisson goals-only;
- Poisson com blend xG/xGA;
- Dixon-Coles goals-only;
- Dixon-Coles com blend xG/xGA;
- Elo-Davidson.

O relatório separa validation e test, usa **test** como holdout primário e mede cobertura, Brier multiclass, Log Loss, accuracy, probabilidade média do resultado real, skill vs uniforme/climatologia, ECE/MCE e estabilidade por liga/mês.

A ablação xG/xGA é pareada nos mesmos jogos. Forma-10 permanece diagnóstico até existir uma especificação preditiva aprendida com/sem esse sinal; nenhuma transformação probabilística arbitrária é introduzida.

Documentação canônica: `docs/enigma-rating-v2-evaluation-v1.md`.

## Forward Test e CLV

A API expõe Forward-Test Report V2/V3, métricas de qualidade probabilística, calibração, ROI diagnóstico e cobertura/distribuição de CLV. O sistema permanece **research-only** e não executa apostas reais automaticamente.

## Contratos e continuidade

Referências principais:

- `docs/j1-operations-v1.md` — contrato operacional canônico de J1, entrypoints, Render, cutover e rollback;
- `docs/enigma-rating-v2.md` — Poisson, Dixon-Coles, Elo, xG/xGA, forma-10, lineup e política de promoção da V2;
- `docs/enigma-rating-v2-evaluation-v1.md` — avaliação temporal e métricas challenger vs STANDARD;
- `docs/daily-prediction-runner-v1.md` — semântica do fluxo Prediction/Decision/Ledger em J1;
- `docs/performance-scale-v1.md` — pooling, concorrência, capacidade 5/10/20 e Horizontal V1;
- `docs/forward-test-report-v3.md` — qualidade probabilística, calibração e CLV;
- `docs/PROGRESS.md` — checkpoint de engenharia atual.

## CI operacional

`.github/workflows/j1-hardening-checks.yml` usa descoberta automática de `tests/test_*.py` e valida também o contrato entre código, `render.yaml`, entrypoints e documentação com:

`python scripts/validate_j1_operational_contract.py`

Isso impede que um PR reintroduza entrypoints duplicados ou deixe Render/docs fora de sincronia com a arquitetura J1 vigente.
