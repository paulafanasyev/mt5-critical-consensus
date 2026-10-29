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
- `src/critical_consensus/pipeline.py` — orchestration of existing agent outputs through Certainty → Chief → Permission → human approval; it never creates an order.
- `src/critical_consensus/calibration.py` — Brier score, shrinkage penalty, weighted consensus; явные ограничения эвристики.
- `src/critical_consensus/api.py` — FastAPI policy endpoints, including protected read-only market-snapshot validation; authorization and order submission remain deliberately disabled until a real 2FA/signature verifier and a separately audited execution boundary exist.
- `src/critical_consensus/market_data.py`, `src/critical_consensus/mt5_readonly.py` — fail-closed snapshot checks and a quote-only adapter for an external MT5 host; its CI test uses a fake terminal client.
- `src/critical_consensus/authorization_store.py` — PostgreSQL one-time decision registry with atomic consume/replay prevention; not connected to public authorization API and does not authorize trades.
- `src/critical_consensus/audit_store.py` — append-only PostgreSQL audit trace storage with post-write verification and idempotent retries. Pass it explicitly as `audit_store=` to `evaluate_signal_bundle`; persistence failure forces `DENY`.
- `src/critical_consensus/redis_streams.py` — idempotent event publishing, consumer-group helpers, retry handling and DLQ (at-least-once; worker deployment is not included).
- `schemas/` — JSON Schema Draft 2020-12 для 11 контрактов.
- `config/` — deny-by-default policy thresholds и permissions.
- `mql5/CriticalConsensusEA.mq5` — демонстрационный EA: demo account only, disabled by default, TTL/SL/TP/slippage/idempotency checks.
- `db/postgres_schema.sql`, `db/migrations/20261029_permission_decision_consumed.sql`, `neo4j/schema.cypher` — схемы хранения/графа и миграция статуса решения.
- `tests/` — adversarial unit tests.
- `.github/workflows/security-audit.yml` — checks installed Python dependencies for known advisories on push, pull request, and weekly schedule.
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

`evaluate_signal_bundle(...)` принимает уже сформированные verdicts, peer reviews, challenge ledger, source reports и ChiefReasoning. Он пересчитывает coverage peer-review, использует калибровочные метаданные для consensus, связывает PermissionRequest с хэшем ChiefReasoning, пересчитанным consensus и точными сводками источников, затем применяет certainty gate, ChiefAgent и PermissionGate. Если пользователь не передал решение, возвращается `REQUIRE_HUMAN` вместе с `human_review_packet`: все peer reviews, dissenting opinions, challenge ledger, ChiefReasoning, источники, риск-снимок и tail-risk. Даже валидные метаданные подтверждения не дают `AUTHORIZED_ONCE`: без постоянного хранилища `decision_id`, защиты от повторного воспроизведения и серверного состояния авторизации pipeline возвращает `DENY`. Команда ордера не создаётся.

```python
from critical_consensus.pipeline import evaluate_signal_bundle
result = evaluate_signal_bundle(bundle, certainty_policy, permission_matrix)
print(result["status"], result["blockers"])
assert result["trade_allowed"] is False
```

Защищённые policy API endpoints требуют `X-Bridge-Key`. Если `MT5_BRIDGE_TOKEN` не настроен или короче 24 символов, они отключаются с HTTP 503. `/health` не содержит секретов и показывает execution disabled. Этот ключ — защита localhost bridge, а не механизм 2FA/цифровой подписи пользователя. `/v1/authorization/check` намеренно отвечает HTTP 423: он не принимает переданный клиентом объект PermissionResult как доказательство выданного разрешения, поскольку долговременного server-side permission store и настоящего 2FA/signature verifier пока нет.

## Где ещё нет production-функций

Это не готовая торговая система: не реализованы реальные коннекторы новостей/бирж, запущенный worker service для Redis Streams, production DB repository/persistence adapter, TOTP-провайдер, проверка санкций, калибровка на размеченных out-of-sample результатах, проверенный парсер JSON для MQL5 и реальная диспетчеризация ордеров. Redis helpers используют at-least-once модель; реальный handler обязан фиксировать `processed_events` вместе со своими эффектами в одной транзакции. Они представлены интерфейсами/схемами, но не объявляются готовыми. Не подключайте реальный счёт к этому каркасу. MQL5-файл не проверен компиляцией в MetaEditor в этой среде; встроенный парсер рассчитан на строго ограниченный flat JSON и перед использованием должен быть заменён на проверенный JSON-парсер и подписанный payload.

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

### Актуальная интеграционная ветка

- Ветка: `integration/safety-gates-merged`, интеграционный PR: [#7](https://github.com/paulafanasyev/mt5-critical-consensus/pull/7). Проверенный кодовый коммит: `05650e7b7c4dee7dd99526b6efaa4be22b12d644`.
- GitHub Actions: **161 passed in 1.75s** на Python 3.12 с PostgreSQL 16 service container. [Тесты](https://github.com/paulafanasyev/mt5-critical-consensus/actions/runs/40906531330).
- [Аудит безопасности](https://github.com/paulafanasyev/mt5-critical-consensus/actions/runs/40906531313): `pip-audit` сообщил `No known vulnerabilities found`, Bandit — `No issues identified`.
- В тесты входят strict API/policy gates, защищённый `POST /v1/market/snapshot/validate`, структурная и семантическая проверка котировок, PostgreSQL-реестр и миграция одноразового решения, а также read-only MT5 adapter с fake-клиентом.
- PostgreSQL-реестр решений не подключён к публичному endpoint авторизации. Реальный provider аутентификации/MFA/signature не реализован; запись или потребление решения само по себе не разрешает торговлю.
- `/v1/authorization/check` и `/v1/execution/submit` остаются HTTP 423. `execution_enabled=false`, `live_enabled=false`; ордер не создаётся и не отправляется.
- **NOT PROVEN / pending:** Vercel Preview для текущей интеграционной ветки. Создание деплоя было отклонено ограничением `api-deployments-free-per-day` (более 100 деплоев за сутки, повторная попытка через 24 часа); production не менялся и остаётся на `main` commit `1774971700fed478479ed657082f9300989505df`. Также не проверены запущенный Windows MT5 terminal/брокер, компиляция MQL5 в MetaEditor, подлинность рыночного feed, прибыльность и готовность к production.

### Исторический baseline ветки `main`

Предыдущие замеры относятся к более раннему состоянию репозитория, а не к интеграционной ветке: локально 78 тестов и один CI-прогон с 84 тестами ([старый прогон](https://github.com/paulafanasyev/mt5-critical-consensus/actions/runs/40849840096), [старый аудит](https://github.com/paulafanasyev/mt5-critical-consensus/actions/runs/40849840095)). Не используйте эти числа как текущую оценку интегрированного кода.

- Зелёные синтетические тесты проверяют поведение функций и интеграцию с тестовой PostgreSQL, но не доказывают работу брокера, реального MT5 terminal, production-хранилища или доходность.
- ChiefReasoning hash вычисляется по каноническому JSON без поля `reasoning_hash`; изменение любого шага делает hash невалидным.

## Официальные точки интеграции

- Python integration MetaTrader 5: https://www.mql5.com/en/docs/python_metatrader5
- MQL5 `OrderSend`: https://www.mql5.com/en/docs/trading/ordersend
- MQL5 network / `WebRequest`: https://www.mql5.com/en/docs/network
- Redis Streams: https://redis.io/docs/latest/develop/data-types/streams/
