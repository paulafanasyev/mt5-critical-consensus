# Intelligence → Critical Consensus → Execution (MT5)

**Статус: reference scaffold / demo-first, НЕ production-ready trading system.** Этот репозиторий переводит ТЗ в проверяемый каркас: схема сообщений, fail-closed gates, калибровочные утилиты, FastAPI, конфигурации, pytest, Neo4j DDL и MQL5 EA-заготовка.

## Безопасные значения по умолчанию

- Автономная торговля запрещена. Необходимо отдельное разрешение пользователя на каждую команду.
- Live-исполнение в MQL5-шаблоне заблокировано без возможности включения одной настройкой; сначала требуются аудит, paper/demo-forward и отдельное изменение политики.
- API-эндпоинт получения команд в каркасе отвечает `204 No Content`, пока очередь исполнения не реализована и не проверена. Поэтому включение EA само по себе не должно породить ордер.
- Любой недостающий verdict, открытый challenge, stale source, VETO, риск-флаг, провал калибровки, отсутствие risk snapshot/news state, запрещённое UTC-окно, истёкший TTL или недействительное подтверждение блокирует исполнение.
- Упоминание `confidence >= 0.98` означает лишь порог допуска по политике. Для CertaintyGate дополнительно нужны калибровочная история и health-метрики: минимум 30 наблюдений для `dissent_rate` и `false_certainty_rate`; отсутствующие метрики блокируют PASS. Это **не** означает 98% вероятность прибыли и не гарантирует исход сделки.

## Состав

- `src/critical_consensus/gates.py` — self-adversarial checks, peer-review validation, challenge loop, fail-closed certainty/chief/permission/authorization gates.
- `src/critical_consensus/pipeline.py` — deterministic Certainty → nine-step Chief → Permission → human-review path with an immutable `signal_input` hash bound to every verdict; `scripts/evaluate_signal_bundle.py` evaluates trusted evidence packets without accepting client-supplied human approval; `--persist` additionally requires an inactive persisted kill switch and a fresh MT5 risk snapshot bound to the exact requested order parameters.
- `src/critical_consensus/calibration.py` — Brier score, shrinkage penalty, weighted consensus; `calibration_eval.py` — chronological walk-forward Platt calibration and sample-aware reliability report.
- `src/critical_consensus/api.py` — FastAPI policy endpoints; authorization and order submission endpoints deliberately disabled until durable permission state and a real 2FA/signature verifier exist.
- `src/critical_consensus/market_data.py` — read-only MT5 history + CSV adapter and OHLC validation; `providers.py` / `agent_runtime.py` — configurable OpenAI-compatible research agents, exact input-hash binding, TTL checks, unique provider-origin checks and fail-closed collection. `scripts/run_agent_analysis.py` requires all nine mandatory policy-agent IDs, then runs provider-backed anonymized peer review (two reviewers per verdict) and up to three evidence-bound challenge-resolution rounds. `READY_FOR_CHIEF` only means this review ledger is complete; it does not run Certainty Gate or authorize trading.
- `src/critical_consensus/redis_streams.py` — idempotent event publishing, consumer groups, retry/DLQ and pending-message recovery; `scripts/run_worker.py` durably writes events to PostgreSQL before ACK. This worker archives events; it does not dispatch orders.
- `schemas/` — JSON Schema Draft 2020-12 для 11 контрактов.
- `config/` — deny-by-default policy thresholds и permissions.
- `mql5/CriticalConsensusEA.mq5` — демонстрационный EA: demo account only, disabled by default, TTL/SL/TP/slippage/idempotency checks.
- `src/critical_consensus/persistence.py` — PostgreSQL repository for immutable market bars, pre-outcome forecasts, one-time labels, kill-switch/risk evidence, Redis inbox events and full pipeline decision/audit persistence. `db/postgres_schema.sql` + `db/migrations/` — base and append-only evidence schemas; `neo4j/schema.cypher` — graph schema.
- `tests/` — adversarial unit tests.
- `.github/workflows/security-audit.yml` and `.github/workflows/validation-suite.yml` — dependency/source security scans, unit contracts and PostgreSQL/Redis integration checks.
- `docs/` — архитектура, формулы, пример trace и недельный roadmap.

## Быстрый запуск (Linux, Python 3.11+)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
export MT5_BRIDGE_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvicorn critical_consensus.api:app --host 127.0.0.1 --port 8000
```

Для Windows VPS с MT5: Python bridge должен слушать только `127.0.0.1`; в MT5 добавьте localhost в список разрешённых адресов для `WebRequest`. Не публикуйте bridge на `0.0.0.0` без отдельного сетевого и криптографического контроля.


## Опциональный локальный слой данных

`docker-compose.yml` поднимает PostgreSQL/TimescaleDB, Redis и Neo4j только на loopback. Схемы и helpers есть, но полноценный repository adapter и worker service пока не подключены.

```bash
cp .env.example .env
python -c 'import secrets; print(secrets.token_urlsafe(32))'  # сгенерировать отдельный POSTGRES_PASSWORD
python -c 'import secrets; print(secrets.token_urlsafe(32))'  # сгенерировать отдельный NEO4J_PASSWORD
docker compose up -d
```

Впишите два разных сгенерированных значения в `.env` и замените пароль в `DATABASE_URL` на значение `POSTGRES_PASSWORD` до подключения DB-клиента. Не используйте примерные значения в production. В этой версии приложение не открывает DB-соединение само по себе.

## Сквозной demo evaluation flow

`evaluate_signal_bundle(...)` принимает уже сформированные verdicts, peer reviews, challenge ledger, source reports и ChiefReasoning. Он пересчитывает coverage peer-review, использует калибровочные метаданные для consensus, связывает PermissionRequest с хэшем ChiefReasoning, пересчитанным consensus и точными сводками источников, затем применяет certainty gate, ChiefAgent и PermissionGate. Если пользователь не передал решение, возвращается `REQUIRE_HUMAN` вместе с `human_review_packet`: все peer reviews, dissenting opinions, challenge ledger, ChiefReasoning, источники, риск-снимок и tail-risk. Валидное решение приводит только к `AUTHORIZED_ONCE`; команда ордера не создаётся.

```python
from critical_consensus.pipeline import evaluate_signal_bundle
result = evaluate_signal_bundle(bundle, certainty_policy, permission_matrix)
print(result["status"], result["blockers"])
assert result["trade_allowed"] is False
```

Защищённые policy API endpoints требуют `X-Bridge-Key`. Если `MT5_BRIDGE_TOKEN` не настроен или короче 24 символов, они отключаются с HTTP 503. `/health` не содержит секретов и показывает execution disabled. Этот ключ — защита localhost bridge, а не механизм 2FA/цифровой подписи пользователя. `/v1/authorization/check` намеренно отвечает HTTP 423: он не принимает переданный клиентом объект PermissionResult как доказательство выданного разрешения, поскольку долговременного server-side permission store и настоящего 2FA/signature verifier пока нет.

## Где ещё нет production-функций

Это не готовая торговая система. Нет настроенных реальных API-ключей/моделей для девяти обязательных агентов и подтверждённых независимых market/news источников. Peer-review/challenge сервис и provider callbacks подключены, но реальная работа не проверена до задания ключей и запуска на реальных сигналах. Нет подтверждённых dissent/false-certainty метрик и размеченной out-of-sample калибровки. Walk-forward evaluator и хранение прогнозов реализованы, но реальные калибровочные результаты не получены. CLI полного Chief Gate принимает только внутренний evidence packet; он не создаёт reviews/challenges, не подделывает source credibility и не принимает human approval. Отдельный TOTP/signature verifier, production-развёртывание worker/API и реальная диспетчеризация ордеров отсутствуют. Не подключайте реальный счёт. MQL5 не подтверждён компиляцией MetaEditor или демосчётом; статические проверки не заменяют эти испытания.

## Нормативные invariants

1. `VETO` всегда блокирует.
2. Один `REJECT`, `ABSTAIN` без причины или открытый challenge не пропускаются через certainty gate.
3. `FAIL` -> `GATHER_MORE`; deadlock -> `ESCALATE`; в обоих случаях без ордера.
4. `ChiefAgent CONFIRM` невозможен, пока не выполнены шаги `a`–`i`.
5. `PermissionGate` не заменяет user authorization.
6. `UserAuthorizationGate` не создаёт ордер, а лишь возвращает допуск для отдельного исполнительного слоя.
7. `OrderCommand` одноразовый и идемпотентный; неопределённый исход после отправки требует reconcile, а не слепого повторения.
8. Публичный слух не считается подтверждённым фактом; `MNPI-SUSPECT` всегда приводит к VETO.

## Проверка текущего каркаса

- До этого расширения локальная проверка исходников: `pytest -q` — **78 тестов PASS**; последний известный GitHub Actions прогон: **84 passed in 0.81s**, Python 3.12: [тесты](https://github.com/paulafanasyev/mt5-critical-consensus/actions/runs/40849840096). Исторический аудит зависимостей: [PASS](https://github.com/paulafanasyev/mt5-critical-consensus/actions/runs/40849840095). Для новых ingestion/storage/calibration изменений добавлен отдельный `validation-suite.yml`; его результаты должны быть подтверждены по конкретному PR/run, а не наследуются от старого CI. Эти проверки не заменяют broker/demo испытания.
- Проверяются fail-closed gates, integrity binding между ChiefReasoning/consensus/source summary и PermissionRequest, выдача полного human review packet, hash chain, API key gate и Redis Streams helpers.
- Проверка зелёных тестов доказывает поведение локальных функций на синтетических входах, но не проверяет брокер, MQL5-компиляцию, реальную очередь, рыночные данные или доходность.
- ChiefReasoning hash вычисляется по каноническому JSON без поля `reasoning_hash`; изменение любого шага делает hash невалидным.

## Официальные точки интеграции

- Python integration MetaTrader 5: https://www.mql5.com/en/docs/python_metatrader5
- MQL5 `OrderSend`: https://www.mql5.com/en/docs/trading/ordersend
- MQL5 network / `WebRequest`: https://www.mql5.com/en/docs/network
- Redis Streams: https://redis.io/docs/latest/develop/data-types/streams/
