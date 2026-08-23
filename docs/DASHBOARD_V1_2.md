# Dashboard V1.2 — Diagnostics & Sample Health

## Purpose

Dashboard V1.2 adds operational diagnostics to the Enigma Core forward-test dashboard without changing model, decision, settlement, or persistence behavior.

## New read-only metrics

- sample health guardrail
- settlement coverage
- BET and NO_BET rates
- NO_BET reason-code frequency
- NO_BET blocker signatures
- dominant rejection reason
- single vs multi-blocker NO_BET counts

## Sample-health policy

The thresholds are operational guardrails only:

- minimum 30 settled decision records
- minimum 10 settled BET records

Reaching them permits descriptive review only. It does not claim statistical significance, profitability, or production readiness.

## Safety

- RESEARCH_ONLY
- read-only dashboard
- no changes to Decision Engine
- no changes to Outcome Settlement
- no changes to forward-test ledger persistence
- real-money execution remains disabled
