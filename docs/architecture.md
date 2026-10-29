# Архитектура и sequence

```mermaid
flowchart TB
  subgraph I[1. INTELLIGENCE]
    PS[PlatformScout] --> ING[Ingestion adapters]
    GN[GlobalNews] --> ING
    RL[RumorLeakWatcher] --> COMP[Compliance pre-filter]
    OC[OnChain / Macro / AlternativeData] --> ING
    ING --> HUB[KnowledgeHub + dedupe + TTL]
  end
  subgraph K[2. KNOWLEDGE]
    HUB --> KG[(Neo4j Knowledge Graph)]
    HUB --> MEM[(PostgreSQL / vector memory)]
    HUB --> SR[SourceRanking]
    HUB --> CT[ContradictionAgent]
    SR --> VER[Agent verdicts + calibration metadata]
    CT --> VER
    VER --> PR[Anonymous peer review N×N]
    PR --> DA[Devil's Advocate: >=3 counter-hypotheses]
    DA --> CR[Challenge resolution <=3 rounds]
  end
  subgraph A[3. ANALYTICS / GOVERNANCE / EXECUTION]
    HUB --> TA[Technical / Features / Quant]
    TA --> RM[Portfolio + Risk]
    CR --> CG[Certainty Gate: all predicates]
    RM --> CG
    CG -->|FAIL| GM[GATHER_MORE -> Intelligence]
    CG -->|DEADLOCK| ESC[ESCALATE user, no trade]
    CG -->|PASS| CH[ChiefAgent: steps a-i]
    CH --> PG[Permission Gate]
    PG -->|DENY| STOP[No order]
    PG -->|REQUIRE_HUMAN| UA[Human approval + live 2FA/signature]
    UA -->|reject/expired| STOP
    UA -->|approval metadata valid, replay guard absent| STOP
    EX --> RECON[Fill + positions reconciliation]
    RECON --> AUD[(Append-only audit + immutable object storage)]
    CG --> AUD
    CH --> AUD
    PG --> AUD
    UA --> AUD
  end
  BUS[(Redis Streams / NATS)] <--> HUB
  BUS <--> PR
  BUS <--> EX
  TS[(TimescaleDB market data)] --> ING
  PGDB[(PostgreSQL orders / decisions / audit)] <--> AUD
  MT5[MT5 terminal / broker] <--> EX
```

## Реализация в текущем каркасе

- Реализованы Python policy gates, `evaluate_signal_bundle`, HTTP bridge-key authentication, Redis Streams helper functions and hash-chain verification.
- `PermissionRequest` привязывается к сигналу, exact `ChiefReasoning` hash, рассчитанному consensus и сводкам проверенных источников; расхождение блокирует переход к human gate.
- Human review packet содержит все peer reviews, dissenting opinions, полный challenge ledger, ChiefReasoning, source reports, risk snapshot и tail-risk fields.
- API endpoint проверки authorization отключён (HTTP 423), пока отсутствуют persistent server-side permission state и настоящий криптографический 2FA/signature verifier.
- Подключение ingestion agents, permanent DB repository, actual worker service, TOTP/signature provider, order dispatcher and MQL5 compilation are not yet implemented/verified.
- Execution-related endpoints intentionally cannot publish, fetch or send an order command. The diagram includes the future production boundary, not an enabled path.

## Invariants

- Любое решение вычисляется как conjunction обязательных условий, не по большинству голосов.
- `PASS` у CertaintyGate означает только передачу на ChiefAgent; `CONFIRM` ChiefAgent означает только разрешение перейти к PermissionGate.
- PermissionGate возвращает `REQUIRE_HUMAN`; без durable permission state и persistent replay/idempotency store approval остаётся метаданными и завершается `DENY`. Одноразовое разрешение пока не реализовано.
- Исполнительный мост перепроверяет TTL, client_order_id, slippage, SL/TP, состояние счёта и текущие позиции непосредственно перед операцией.
- При неопределённом результате вызова брокера не выполняется слепой retry; запускается reconcile.

## Область MVP

1. Первая итерация: данные read-only, demo/paper, 3–4 агента, ручной approve на каждый ордер.
2. Никакого live, пока не проверены контракты, брокерские ограничения, отказоустойчивость, forward-тесты и юридические условия.
3. Перенос bridge на отдельный Windows VPS; агенты/очереди/БД — Linux. Слушать localhost, внешний доступ закрыть firewall/IP allowlist.


## Новые safety adapters (интеграционный этап)

- `PostgresDecisionStore` реализует серверный реестр одноразовых решений с атомарным потреблением и ограничением TTL. Это только хранилище: оно не доказывает личность и пока не подключено к `user_authorization_gate` или публичному API.
- `MT5ReadOnlyAdapter` читает allow-listed ticks через `symbol_info_tick` и прогоняет снимки через валидатор freshness/spread. Он не вызывает `order_send`; CI покрывает адаптер поддельным клиентом, но не реальный MT5 terminal.
- Веб-авторизация остаётся отключённой (HTTP 423), а торговля запрещена. Для завершения нужны реальный проверенный auth/MFA/signature provider, серверный permission issuance flow, подключение decision store к проверенному server-side boundary, отдельный read-only Windows demo smoke test и независимый аудит до изменения любого execution пути.


## Долговременный audit trace storage

- `PostgresAuditStore` сохраняет hash-linked trace целиком одной транзакцией, проверяет её после записи и поддерживает идемпотентный повтор того же trace. Время события хранится одновременно в UTC timestamp и исходной строковой форме, поскольку именно строка включена в canonical hash.
- Заголовок trace и отдельные события защищены от UPDATE/DELETE триггерами и правами для PUBLIC; в production роль приложения должна иметь минимальные права, а выгрузки следует защищать WORM/object-lock.
- Хранилище не выдаёт разрешение на ордер. В текущей версии оно вызывается явно; автоматическое сохранение каждого результата `evaluate_signal_bundle` ещё не подключено и требует отдельной интеграции с runtime.
