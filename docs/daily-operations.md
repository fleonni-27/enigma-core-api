# Enigma Core — Operação diária

## Objetivo

Eliminar a necessidade de localizar manualmente `sportmonks_fixture_id` e alimentar odds jogo a jogo.

## Fluxo automático

O workflow `.github/workflows/daily-operations-sync.yml` executa a sincronização do dia no fuso `America/Sao_Paulo`:

- 00:00 local: bootstrap diário de fixtures + primeira coleta de odds;
- 12:00 local: atualização das odds;
- 17:00 local: nova atualização das odds.

O agendamento do GitHub Actions tem precisão de minuto e pode sofrer atraso de fila. Portanto, 00:00 é o gatilho operacional; não há garantia de execução exatamente em 00:00:01.

## O que a sincronização faz

`POST /operations/daily-sync`

1. busca no Sportmonks todas as fixtures da data;
2. grava/atualiza as fixtures no banco;
3. filtra as ligas-alvo da Enigma Core;
4. retorna `fixture_id` interno e `sportmonks_fixture_id` para cada jogo;
5. busca as odds prematch de cada fixture-alvo;
6. grava snapshots de odds com `snapshot_window=daily_YYYYMMDD`.

A sincronização não gera prediction, decisão ou aposta. Ela é apenas a camada automática de alimentação das fontes.

## Onde ver os jogos e IDs do dia

`GET /operations/today`

Opcionalmente:

`GET /operations/today?target_date=YYYY-MM-DD`

A resposta mostra, para cada fixture-alvo:

- `fixture_id`;
- `sportmonks_fixture_id`;
- liga;
- mandante e visitante;
- horário;
- quantidade de odds já gravadas;
- horário da última coleta de odds;
- quantidade de predictions persistidas.

## Reprocessamento manual

Caso seja necessário repetir a alimentação do dia:

`POST /operations/daily-sync?target_date=YYYY-MM-DD&refresh_odds=true`

Não é necessário informar IDs de fixtures individualmente.
