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
- `src/critical_consensus/api.py` — FastAPI policy endpoints; authorization and order submission endpoints deliberately disabled until durable permission state and a real 2FA/signature verifier exist.
- `src/critical_consensus/redis_streams.py` — idempotent event publishing, consumer-group helpers, retry handling and DLQ (at-least-once; worker deployment is not included).
- `schemas/` — JSON Schema Draft 2020-12 для 11 контрактов.
- `config/` — deny-by-default policy thresholds и permissions.
- `mql5/CriticalConsensusEA.mq5` — демонстрационный EA: demo account only, disabled by default, TTL/SL/TP/slippage/idempotency checks.
- `db/postgres_schema.sql`, `neo4j/schema.cypher` — минимальные схемы хранения и графа.
- `tests/` — adversarial unit tests.
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

- Локальный исходный архив: `pytest -q` — **77 тестов PASS**. Последний GitHub Actions run на `main` — **84 passed in 0.88s**, без предупреждений pytest: [workflow run](https://github.com/paulafanasyev/mt5-critical-consensus/actions/runs/40849397176). CI закреплён на `ubuntu-22.04`, использует `actions/checkout@v7`, `actions/setup-python@v7` и `httpx2` для TestClient. Это синтетические тесты функций/API, а не брокерные испытания.
- Проверяются fail-closed gates, integrity binding между ChiefReasoning/consensus/source summary и PermissionRequest, выдача полного human review packet, hash chain, API key gate и Redis Streams helpers.
- Проверка зелёных тестов доказывает поведение локальных функций на синтетических входах, но не проверяет брокер, MQL5-компиляцию, реальную очередь, рыночные данные или доходность.
- ChiefReasoning hash вычисляется по каноническому JSON без поля `reasoning_hash`; изменение любого шага делает hash невалидным.

## Официальные точки интеграции

- Python integration MetaTrader 5: https://www.mql5.com/en/docs/python_metatrader5
- MQL5 `OrderSend`: https://www.mql5.com/en/docs/trading/ordersend
- MQL5 network / `WebRequest`: https://www.mql5.com/en/docs/network
- Redis Streams: https://redis.io/docs/latest/develop/data-types/streams/
