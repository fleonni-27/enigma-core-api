# Dashboard Selection Clarity V1

## Problema

Os termos `BET` e `NO_BET` descrevem a ação da política, mas não identificam sozinhos qual resultado 1X2 está sendo avaliado.

## Semântica

- `1` = mandante
- `X` = empate
- `2` = visitante
- `BET` = a política aprovou entrada na seleção registrada
- `NO_BET` = a política rejeitou entrada na seleção registrada

`BET` e `NO_BET` nunca significam automaticamente mandante ou visitante.

## Dashboard

A apresentação passa a usar:

- `ENTRAR` para `BET`
- `NÃO ENTRAR` para `NO_BET`
- `Mandante (1) — <equipe>`
- `Empate (X)`
- `Visitante (2) — <equipe>`

Os códigos persistidos originais continuam visíveis como referência de auditoria.

A API `/dashboard/api/records` também expõe:

- `selection_side`: `HOME`, `DRAW`, `AWAY` ou `UNKNOWN`
- `selection_label`: rótulo humano da seleção
- `selected_team`: equipe selecionada quando aplicável

## Guardrails

Esta alteração é exclusivamente de apresentação/auditoria. Não altera Decision Engine, thresholds, probabilidades, odds, ledger, settlement, P&L ou registros históricos.
