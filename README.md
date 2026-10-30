# Intelligence → Critical Consensus → Execution (MT5)

**Статус: reference scaffold / demo-first, НЕ production-ready trading system.** Этот репозиторий переводит ТЗ в проверяемый каркас: схема сообщений, fail-closed gates, калибровочные утилиты, FastAPI, конфигурации, pytest, Neo4j DDL и MQL5 EA-заготовка.

## Безопасные значения по умолчанию

- Автономная торговля запрещена. Необходимо отдельное разрешение пользователя на каждую команду.
- Live-исполнение в MQL5-шаблоне заблокировано без возможности включения одной настройкой; сначала требуются аудит, paper/demo-forward и отдельное изменение политики.
- API-эндпоинт получения команд в каркасе отвечает `204 No Content`, пока очередь исполнения не реализована и не проверена. Поэтому включение EA само по себе не должно породить ордер.
- Любой недостающий verdict, открытый challenge, stale source, VETO, риск-флаг, провал калибровки, отсутствие risk snapshot/news state, запрещённое UTC-окно, истёкший TTL, недействительное подтверждение или расхождение risk snapshot с точными параметрами ордера и equity-базой блокирует исполнение.
- Упоминание `confidence >= 0.98` означает лишь порог допуска по политике. Для CertaintyGate дополнительно нужны калибровочная история и health-метрики: минимум 30 наблюдений для `dissent_rate` и `false_certainty_rate`; отсутствующие метрики блокируют PASS. Это **не** означает 98% вероятность прибыли и не гарантирует исход сделки.

## Состав

- `src/critical_consensus/gates.py` — self-adversarial checks, peer-review validation, challenge loop, fail-closed certainty/chief/permission/authorization gates.
- `src/critical_consensus/pipeline.py` — deterministic Certainty → nine-step Chief → Permission → human-review path with an immutable `signal_input` hash bound to every verdict; `scripts/evaluate_signal_bundle.py` evaluates trusted evidence packets without accepting client-supplied human approval; `--persist` additionally requires an inactive persisted kill switch and a fresh MT5 risk snapshot bound to the exact requested order parameters.
- `src/critical_consensus/calibration.py` — Brier score, shrinkage penalty, weighted consensus; `calibration_eval.py` — chronological walk-forward Platt calibration and sample-aware reliability report.
- `src/critical_consensus/api.py` — FastAPI policy endpoints; every policy evaluation requires a fresh HMAC-signed evidence wrapper and, for certainty, a separately signed calibration-service attestation. Authorization and order submission endpoints deliberately remain disabled until durable permission state and a real 2FA/signature verifier exist.
- `src/critical_consensus/evidence.py` — HMAC-SHA256 envelopes binding exact canonical payloads, issuer, nonce, TTL and payload hash, with distinct `evidence`, `governance` and `operator` signing domains; `governance.py` — signed historical dissent/false-certainty attestations; `authorization.py` — one-time nonce stores (in-memory for tests, insert-only PostgreSQL tombstones for durable use).
- `src/critical_consensus/market_data.py` — read-only MT5 history + CSV adapter and OHLC validation; `providers.py` / `agent_runtime.py` — configurable OpenAI-compatible research agents, exact input-hash binding, TTL checks, unique provider-origin checks and fail-closed collection. `scripts/run_agent_analysis.py` requires all nine mandatory policy-agent IDs, then runs provider-backed anonymized peer review (two reviewers per verdict) and up to three evidence-bound challenge-resolution rounds. `READY_FOR_CHIEF` only means this review ledger is complete; it does not run Certainty Gate or authorize trading.
- `src/critical_consensus/redis_streams.py` — idempotent event publishing, consumer groups, retry/DLQ and pending-message recovery; `scripts/run_worker.py` durably writes valid events to PostgreSQL before ACK, while poison/invalid events are written to the DLQ and only then ACKed; an event whose handler raises stays pending for retry. This worker archives events; it does not dispatch orders.
- `schemas/` — JSON Schema Draft 2020-12 для 11 контрактов.
- `config/` — deny-by-default policy thresholds и permissions.
- `mql5/CriticalConsensusEA.mq5` — демонстрационный EA: demo account only, disabled by default; строгая проверка loopback-URL (точный host `127.0.0.1`/`localhost`, числовой порт, без userinfo/query), header-safe `BridgeToken`, TTL/SL/TP/slippage checks и flushed idempotency-заглушка перед `OrderSend`.
- `src/critical_consensus/persistence.py` — PostgreSQL repository for immutable market bars, pre-outcome forecasts, one-time labels, kill-switch/risk evidence, Redis inbox events and full pipeline decision/audit persistence. `db/postgres_schema.sql` + `db/migrations/` — base schema plus ordered, forward-only migrations (immutable evidence tables and insert-only nonce tombstones); `neo4j/schema.cypher` — graph schema.
- `tests/` — adversarial unit tests.
- `.github/workflows/security-audit.yml`, `tests.yml` and `validation-suite.yml` — dependency/source security scans, unit contracts and PostgreSQL/Redis integration checks.
- `docs/` — архитектура, формулы, пример trace и недельный roadmap.

## Быстрый запуск (Linux, Python 3.11+)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
export MT5_BRIDGE_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export MT5_EVIDENCE_HMAC_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export MT5_GOVERNANCE_HMAC_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export MT5_OPERATOR_HMAC_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvicorn critical_consensus.api:app --host 127.0.0.1 --port 8000
```

`MT5_EVIDENCE_HMAC_KEY`, `MT5_GOVERNANCE_HMAC_KEY` и `MT5_OPERATOR_HMAC_KEY` должны быть тремя разными ключами длиной 32–256 байт; соответствующие issuer (`MT5_EVIDENCE_ISSUER`, `MT5_GOVERNANCE_ISSUER`, `MT5_OPERATOR_ISSUER`) также должны различаться. Первый подписывает evidence-пакеты на доверенном ingress, второй — исторические метрики калибровочного сервиса, третий — решение оператора (approval) в отдельном домене `operator`. Без operator-ключа и issuer бандл с решением человека блокируется, а не принимается без подписи. `MT5_BRIDGE_TOKEN` должен содержать не менее 24 символов `[A-Za-z0-9._~-]`. Ключи провайдеров задаются только через переменные вида `MT5_AGENT_KEY_*`, на которые ссылается `api_key_env`; произвольные имена переменных и inline-секреты отклоняются.

Для Windows VPS с MT5: Python bridge должен слушать только `127.0.0.1`; в MT5 добавьте localhost в список разрешённых адресов для `WebRequest`. Не публикуйте bridge на `0.0.0.0` без отдельного сетевого и криптографического контроля.


## Опциональный локальный слой данных

`docker-compose.yml` поднимает PostgreSQL 16 (образ TimescaleDB; hypertables в схеме не создаются), Redis и Neo4j только на loopback. В репозитории есть PostgreSQL repository adapter (`persistence.py`), миграции и worker-скрипт (`scripts/run_worker.py`); они покрыты интеграционными тестами, которые запускаются только при настроенных `TEST_DATABASE_URL`/`TEST_REDIS_URL`. Развёрнутый и эксплуатируемый worker service и реальная broker/database инсталляция не проверялись.

```bash
cp .env.example .env
python -c 'import secrets; print(secrets.token_urlsafe(32))'  # сгенерировать отдельный POSTGRES_PASSWORD
python -c 'import secrets; print(secrets.token_urlsafe(32))'  # сгенерировать отдельный NEO4J_PASSWORD
docker compose up -d
```

Впишите два разных сгенерированных значения в `.env` и замените пароль в `DATABASE_URL` на значение `POSTGRES_PASSWORD` до подключения DB-клиента. Не используйте примерные значения в production. В этой версии приложение не открывает DB-соединение само по себе.

## Сквозной demo evaluation flow

`evaluate_signal_bundle(...)` принимает уже сформированные verdicts, peer reviews, challenge ledger, source reports и ChiefReasoning. Он пересчитывает coverage peer-review, использует калибровочные метаданные для consensus, связывает PermissionRequest с хэшем ChiefReasoning, пересчитанным consensus и точными сводками источников, затем применяет certainty gate, ChiefAgent и PermissionGate. Если пользователь не передал решение, возвращается `REQUIRE_HUMAN` вместе с `human_review_packet`: все peer reviews, dissenting opinions, challenge ledger, ChiefReasoning, источники, риск-снимок и tail-risk. Решение человека принимается только как отдельно подписанный operator-envelope, привязанный к `signal_id`, хэшу ChiefReasoning и `permission_request_sha256` (точным параметрам ордера), и только при `require_persisted_risk=True`. Валидное решение приводит только к `AUTHORIZED_ONCE`; команда ордера не создаётся.

```python
from critical_consensus.evidence import sign_bundle
from critical_consensus.pipeline import evaluate_signal_bundle

signed = sign_bundle(bundle, key=evidence_key, issuer="trusted-ingress")
result = evaluate_signal_bundle(
    signed, certainty_policy, permission_matrix,
    evidence_key=evidence_key, governance_key=governance_key,
)
print(result["status"], result["blockers"])
assert result["trade_allowed"] is False
```

Бандл обязан нести свежий `evidence_envelope`; исторические `dissent_rate`/`false_certainty_rate` принимаются только из подписанной атестации калибровочного сервиса (отдельный ключ, отдельный issuer). `mandatory_agents` и `compliance_agent_id` берутся только из политики. `compliance_status` вычисляется по verdict `ComplianceAgent`, а не по полю бандла. PermissionRequest обязан совпадать с `signal_input` по `instrument` и `direction`. Persisted MT5 risk snapshot должен совпадать с заказом по symbol и request-binding hash, а `risk_value`/`risk_fraction` пересчитываются из `size × |entry − SL|`, tick size/tick value и equity и сравниваются с допуском 1%; daily loss и drawdown пересчитываются из equity-баз. Разрешение человека требует `decision_id` и одноразовый `nonce`, атомарно потребляемый через `InMemoryNonceStore` (тесты/демо) или `PostgresNonceStore`; повторное использование отклоняется. `PostgresNonceStore` хранит insert-only tombstones: приложение не удаляет строки в обработке запроса, ручная очистка допускается только после `expires_at` и согласованного retention-окна.

Защищённые policy API endpoints требуют `X-Bridge-Key` и тело `{"payload": ..., "envelope": ...}` с валидной HMAC-подписью. Если `MT5_BRIDGE_TOKEN`, `MT5_EVIDENCE_HMAC_KEY` или (для certainty) `MT5_GOVERNANCE_HMAC_KEY` не настроены, endpoint отвечает HTTP 503; отсутствующий или неверный `X-Bridge-Key` — HTTP 401; неверная или изменённая подпись — HTTP 403; неверная схема тела — HTTP 422. API пересчитывает `reviewer_count`, consensus и `open_challenges` из raw-записей и игнорирует заявленные клиентом агрегаты; `/v1/permission/check` намеренно отвечает HTTP 423 и требует persisted-risk эндпоинт `/v1/risk/permission/check`. `/health` не содержит секретов и честно показывает, какие проверки сконфигурированы, но всегда сообщает execution disabled. Ключ bridge — защита localhost-транспорта, а не 2FA/цифровая подпись пользователя. `/v1/authorization/check` намеренно отвечает HTTP 423: он не принимает переданный клиентом объект PermissionResult как доказательство выданного разрешения, поскольку долговременного server-side permission store и настоящего 2FA/signature verifier пока нет.

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
9. Неподписанный, просроченный или изменённый evidence bundle не доходит ни до одного gate.
10. Исторические health-метрики без подписанной атестации калибровочного сервиса блокируют certainty PASS.
11. Одно и то же человеческое подтверждение не может быть использовано дважды.

## Проверка текущего каркаса

- Локальная проверка ветки `harden/10of10` на Python 3.12 (Windows): `pytest -q` — **251 passed, 10 skipped**; `python scripts/check_mql5_safety.py` — **219 static invariants verified, 34 mutations rejected** (компиляция EA и demo-исполнение по-прежнему NOT PROVEN). Все пропуски требуют `TEST_DATABASE_URL` и/или `TEST_REDIS_URL` и здесь не запускались. Результаты GitHub Actions относятся к предыдущим коммитам и для этой ветки должны быть подтверждены отдельным run после push; они не наследуются автоматически.
- Проверяются fail-closed gates, HMAC evidence/governance envelopes, одноразовые authorization nonce, integrity binding между ChiefReasoning/consensus/source summary и PermissionRequest, выдача полного human review packet, hash chain, API key gate, строгая каноническая сериализация и Redis Streams helpers.
- Проверка зелёных тестов доказывает поведение локальных функций на синтетических входах, но не проверяет брокер, MQL5-компиляцию, реальную очередь, рыночные данные или доходность.
- ChiefReasoning hash вычисляется по каноническому JSON без поля `reasoning_hash`; изменение любого шага делает hash невалидным.

## Официальные точки интеграции

- Python integration MetaTrader 5: https://www.mql5.com/en/docs/python_metatrader5
- MQL5 `OrderSend`: https://www.mql5.com/en/docs/trading/ordersend
- MQL5 network / `WebRequest`: https://www.mql5.com/en/docs/network
- Redis Streams: https://redis.io/docs/latest/develop/data-types/streams/
