# Enigma Core API

Backend inicial da Enigma Core para coleta auditável de fixtures, previsões e snapshots de odds.

## Arquitetura inicial

Sportmonks -> FastAPI -> PostgreSQL/Supabase

## Segurança

Nunca faça commit do arquivo `.env`. Use `.env.example` apenas como modelo.

## Configuração local

1. Crie `.env` a partir de `.env.example`.
2. Preencha `DATABASE_URL` com a connection string PostgreSQL do Supabase.
3. Preencha `SPORTMONKS_API_TOKEN` com o token da Sportmonks.
4. Instale dependências: `pip install -r requirements.txt`.
5. Execute `sql/001_initial_schema.sql` no SQL Editor do Supabase.
6. Inicie: `uvicorn app.main:app --reload`.
7. Teste `GET /health` e `GET /fixtures/today`.

## Regra de histórico

`odds_snapshots` é append-only por desenho operacional: cada captura recebe um novo `fetched_at`. Não atualizar snapshots antigos. Isso permitirá reconstruir J0, J1, PRE_CLOSE e Closing Line Value.

## Próximas etapas

- Normalizar e persistir fixtures da Sportmonks.
- Implementar coleta de odds pré-jogo.
- Classificar snapshots por janela J0/J1/PRE_CLOSE.
- Adicionar resultados e métricas Brier, Log Loss, CLV, EV, P/L e Yield.
- Adicionar testes e migrations versionadas.
