# Roadmap по неделям и Definition of Done

## Недели 1–2: контракты и безопасный data path
- JSON Schema согласованы и версионируются; migrations и append-only audit проверены.
- Доступны health endpoint, политика fail-closed и replay на синтетических событиях.
- DoD: unit/integration tests зелёные; ни один endpoint не отправляет ордера.

## Недели 3–4: MVP demo/paper
- 3–4 агента: PlatformScout, GlobalNews, TechnicalAnalyst, RiskManager; ingestion только из разрешённых API/RSS.
- Risk gates, source TTL, dedupe, idempotency store; оператор вручную подтверждает каждый demo order.
- DoD: тесты stale data, duplicate event, kill switch, missing SL, rejected broker request; журнал воспроизводим.

## Недели 5–8: KnowledgeHub + challenge resolution
- PostgreSQL, TimescaleDB, Redis Streams consumer groups, DLQ; SourceRanking, Contradiction, Rumor/Compliance.
- DoD: повторная доставка безопасна, poison message уходит в DLQ, MNPI-SUSPECT блокируется, открытые challenge блокируют CertaintyGate.

## Недели 9–12: peer review + calibrated certainty
- N×N anonymous review, минимум два независимых reviewer, Devil's Advocate, максимум 3 раунда, Brier/reliability reports.
- DoD: golden replay suite, report показывает sample size и confidence intervals; нет claims про near certainty без истории.

## Недели 13–16: Chief / Permission / user authorization / audit
- Chief steps a–i, PermissionGate, UI с dissent и tail risk, TOTP/signature provider, per-order approve, immutable audit export.
- DoD: live-путь выключен; негативные тесты подтверждают отказ при любой пропущенной проверке.

## После 16 недель: demo-forward и независимый review
- Минимум 4 недели demo-forward — только первичный пилотный критерий, не доказательство надёжности; продолжать до достаточной выборки по заранее зафиксированному протоколу.
- 8 недель demo-forward не создают автоматического права на автономию. Нужно письменное решение оператора, юридическая проверка, отдельный risk acceptance и live pilot с подтверждением каждой сделки.
- DoD для live-пилота: независимый code/security review, проверенная сверка позиций, стресс/chaos testing, broker constraints, alarm drill, kill-switch drill, signed policy version и rollback plan.
