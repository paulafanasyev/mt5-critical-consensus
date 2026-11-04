# Intelligence → Critical Consensus → Execution (MT5)

**Русский** | [Tiếng Việt](README.vi.md)

**Статус: reference scaffold / demo-first, НЕ production-ready trading system.** Этот репозиторий переводит ТЗ в проверяемый каркас: схема сообщений, fail-closed gates, калибровочные утилиты, FastAPI, конфигурации, pytest, Neo4j DDL и MQL5 EA-заготовка.

## Безопасные значения по умолчанию

- Автономная торговля запрещена. Необходимо отдельное разрешение пользователя на каждую команду.
- Live-исполнение в MQL5-шаблоне заблокировано без возможности включения одной настройкой; сначала требуются аудит, paper/demo-forward и отдельное изменение политики.
- API-эндпоинт получения команд в каркасе отвечает `204 No Content`, пока очередь исполнения не реализована и не проверена. Поэтому включение EA само по себе не должно породить ордер.
- Любой недостающий verdict, открытый challenge, stale source, VETO, риск-флаг, провал калибровки, отсутствие risk snapshot/news state, запрещённое UTC-окно, истёкший TTL, недействительное подтверждение или расхождение risk snapshot с точными параметрами ордера и equity-базой блокирует исполнение.
- Упоминание `confidence >= 0.98` означает лишь порог допуска по политике. Для CertaintyGate дополнительно нужны калибровочная история и health-метрики: минимум 30 наблюдений для `dissent_rate` и `false_certainty_rate`; отсутствующие метрики блокируют PASS. Это **не** означает 98% вероятность прибыли и не гарантирует исход сделки. Уровень успеха 98% этим кодом не измеряется и недостижим для него.

## Состав

- `src/critical_consensus/gates.py` — self-adversarial checks, peer-review validation, challenge loop, fail-closed certainty/chief/permission/authorization gates.
- `src/critical_consensus/pipeline.py` — deterministic Certainty → nine-step Chief → Permission → human-review path with an immutable `signal_input` hash bound to every verdict; `scripts/evaluate_signal_bundle.py` evaluates trusted evidence packets without accepting client-supplied human approval; `--persist` additionally requires an inactive persisted kill switch and a fresh MT5 risk snapshot bound to the exact requested order parameters.
- `src/critical_consensus/calibration.py` — Brier score, shrinkage penalty, weighted consensus; `calibration_eval.py` — chronological walk-forward Platt calibration and sample-aware reliability report.
- `src/critical_consensus/api.py` — FastAPI policy endpoints; every policy evaluation requires a fresh HMAC-signed evidence wrapper and, for certainty, a separately signed calibration-service attestation. Authorization and order submission endpoints deliberately remain disabled until durable permission state and a real 2FA/signature verifier exist.
- `src/critical_consensus/evidence.py` — HMAC-SHA256 envelopes binding exact canonical payloads, issuer, nonce, TTL and payload hash, with distinct `evidence`, `governance`, `operator` and `review` signing domains; `governance.py` — signed historical dissent/false-certainty attestations; `authorization.py` — one-time per-operator nonce stores plus once-per-permission tombstones (in-memory for tests, insert-only PostgreSQL rows for durable use, migration 006).
- `src/critical_consensus/market_data.py` — order-free MT5 history + CSV adapter and OHLC validation (the MT5 path calls `symbol_select` to add the symbol to Market Watch); `providers.py` / `agent_runtime.py` — configurable OpenAI-compatible research agents, exact input-hash binding, TTL checks, unique provider-origin checks and fail-closed collection. `scripts/run_agent_analysis.py` requires all nine mandatory policy-agent IDs, then runs provider-backed anonymized peer review (two reviewers per verdict) and up to three evidence-bound challenge-resolution rounds. `READY_FOR_CHIEF` only means this review ledger is complete; it does not run Certainty Gate or authorize trading.
- `src/critical_consensus/redis_streams.py` — idempotent event publishing, consumer groups, retry/DLQ and pending-message recovery; `scripts/run_worker.py` durably writes valid events to PostgreSQL before ACK. Parse-invalid events are written to the DLQ on first delivery and only then ACKed; an event whose handler raises stays pending and is reclaimed (`XAUTOCLAIM`) on each worker loop, so it is retried up to `max_attempts=3` deliveries before it is dead-lettered and ACKed. This worker archives events; it does not dispatch orders.
- `src/critical_consensus/mt5_agent/` — слой агента без отправки ордеров поверх терминала (`backend.py`, `LiveMT5Backend` без единой торговой функции; `symbol_spec()` вызывает `symbol_select()`, только когда символа нет в Market Watch, и это добавляет его в список символов терминала), fail-closed `policy.py`, детерминированный сигнал `strategy.py`, объём из спецификации символа `sizing.py`, бумажный брокер без look-ahead `paper.py`, SQLite hash-журнал `journal.py` и цикл `agent.py`; `scripts/run_mt5_agent.py` запускает их на CSV-экспорте или истории терминала. Ни один режим не отправляет ордер.
- `schemas/` — JSON Schema Draft 2020-12 для 11 контрактов.
- `config/` — deny-by-default policy thresholds и permissions.
- `mql5/CriticalConsensusEA.mq5` — демонстрационный EA: demo account only, disabled by default; строгая проверка loopback-URL (точный host `127.0.0.1`/`localhost`, числовой порт, без userinfo/query), непустой header-safe `BridgeToken`, TTL/SL/TP/slippage checks и idempotency-заглушка с проверкой результата `GlobalVariablesFlush()` перед `OrderSend`.
- `src/critical_consensus/persistence.py` — PostgreSQL repository for immutable market bars, pre-outcome forecasts, one-time labels, kill-switch/risk evidence, Redis inbox events and full pipeline decision/audit persistence. `db/postgres_schema.sql` + `db/migrations/` — base schema plus ordered, forward-only migrations (immutable evidence tables and insert-only nonce tombstones); `neo4j/schema.cypher` — graph schema.
- `tests/` — adversarial unit tests.
- `.github/workflows/security-audit.yml`, `tests.yml` and `validation-suite.yml` — dependency/source security scans, unit contracts and PostgreSQL/Redis integration checks.
- `docs/` — архитектура, формулы, пример trace, недельный roadmap, `MT5_AGENT.md` в трёх языках (`MT5_AGENT.md`, `MT5_AGENT.en.md`, `MT5_AGENT.vi.md`) и `DEMO_RESULTS.md` в трёх языках с графиками прогона на реальных барах (`docs/charts/`).

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
export MT5_REVIEW_HMAC_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export MT5_EVIDENCE_ISSUER="trusted-ingress"
export MT5_GOVERNANCE_ISSUER="calibration-service"
export MT5_OPERATOR_ISSUER="operator-console"
export MT5_REVIEW_ISSUER="review-service"
uvicorn critical_consensus.api:app --host 127.0.0.1 --port 8000
```

`MT5_EVIDENCE_HMAC_KEY`, `MT5_GOVERNANCE_HMAC_KEY`, `MT5_OPERATOR_HMAC_KEY` и `MT5_REVIEW_HMAC_KEY` должны быть четырьмя разными ключами длиной 32–256 байт; соответствующие issuer (`MT5_EVIDENCE_ISSUER`, `MT5_GOVERNANCE_ISSUER`, `MT5_OPERATOR_ISSUER`, `MT5_REVIEW_ISSUER`) также должны различаться. Первый подписывает evidence-пакеты на доверенном ingress, второй — исторические метрики калибровочного сервиса, третий — решение оператора (approval) в домене `operator`, четвёртый — peer reviews, challenge ledger и ChiefReasoning в домене `review`. Без operator-ключа и issuer бандл с решением человека блокируется, а не принимается без подписи. Реестр ревью принимается только с валидной `review_attestation`, покрывающей `{signal_id, verdicts, peer_reviews, challenge_records, chief_reasoning}`; ingress-ключ не может её подписать. `MT5_BRIDGE_TOKEN` проверяется при каждом защищённом запросе (а не при старте процесса): он должен содержать 24–256 символов `[A-Za-z0-9._~-]` и не совпадать ни с одним signing-ключом — иначе endpoint отвечает HTTP 503, а `/health` показывает `bridge_token_distinct_from_signing_keys: false`. Отдельный `MT5_KILL_SWITCH_ADMIN_TOKEN` (≥32 символа) нужен только для выключения kill switch и тоже не должен совпадать с bridge-токеном или signing-ключами. Ключи провайдеров задаются только через переменные вида `MT5_AGENT_KEY_*`, на которые ссылается `api_key_env`; произвольные имена переменных и inline-секреты отклоняются.

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

`evaluate_signal_bundle(...)` принимает уже сформированные verdicts, peer reviews, challenge ledger, source reports и ChiefReasoning. Он пересчитывает coverage peer-review, использует калибровочные метаданные для consensus, проверяет review-домен, связывает PermissionRequest с хэшем ChiefReasoning, пересчитанным consensus и точными сводками источников, затем применяет certainty gate, ChiefAgent и PermissionGate. Если пользователь не передал решение, возвращается `REQUIRE_HUMAN` вместе с `human_review_packet`: все peer reviews, dissenting opinions, challenge ledger, ChiefReasoning, источники, риск-снимок и tail-risk. Пакет показывает именно persisted terminal snapshot (с его validation/reconciliation), а не клиентскую сводку риска. Решение человека принимается только как отдельно подписанный operator-envelope, привязанный к `signal_id`, хэшу ChiefReasoning, `permission_request_sha256` (точным параметрам ордера) и — на persisted-risk пути — `risk_snapshot_sha256` (снимку, который проверил risk gate), и только при `require_persisted_risk=True`. Валидное решение приводит только к `AUTHORIZED_ONCE`; команда ордера не создаётся.

```python
from critical_consensus.evidence import sign_bundle, sign_review
from critical_consensus.pipeline import evaluate_signal_bundle

bundle["review_attestation"] = sign_review(
    {"signal_id": bundle.get("signal_id"),
     "verdicts": bundle.get("verdicts"),
     "peer_reviews": bundle.get("peer_reviews"),
     "challenge_records": bundle.get("challenge_records"),
     "chief_reasoning": bundle.get("chief_reasoning")},
    key=review_key, issuer="review-service",
)
signed = sign_bundle(bundle, key=evidence_key, issuer="trusted-ingress")
result = evaluate_signal_bundle(
    signed, certainty_policy, permission_matrix,
    evidence_key=evidence_key, governance_key=governance_key,
    expected_evidence_issuer="trusted-ingress",
    expected_governance_issuer="calibration-service",
    review_key=review_key, expected_review_issuer="review-service",
)
print(result["status"], result["blockers"])
assert result["trade_allowed"] is False
```

Бандл обязан нести свежий `evidence_envelope`; verdicts, peer reviews, challenge ledger и ChiefReasoning принимаются только из отдельной `review_attestation` review-сервиса (четвёртый ключ и issuer), поэтому ingress-ключ не может «дописать» независимые ревью или Chief CONFIRM. Исторические `dissent_rate`/`false_certainty_rate` принимаются только из подписанной атестации калибровочного сервиса (отдельный ключ, отдельный issuer). `mandatory_agents` и `compliance_agent_id` берутся только из политики. `compliance_status` вычисляется по verdict `ComplianceAgent`, а не по полю бандла. PermissionRequest обязан совпадать с `signal_input` по `instrument` и `direction`. Persisted MT5 risk snapshot должен совпадать с заказом по symbol и request-binding hash, а `risk_value`/`risk_fraction` пересчитываются из `size × |худший вход − SL|` (худший из reviewed и reference price), tick size/tick value и `min(account_equity, account_balance)` и сравниваются с допуском 1%; daily loss и drawdown пересчитываются из equity-баз, day-start equity должна согласовываться с `balance − daily_pnl` в обе стороны (1%), а high-water mark не может быть ниже day-start. Снимок обязан нести идентичность счёта `account_login` (целое `>= 1`) и `account_server` (непустая строка); политика может закрепить ожидаемые значения, и тогда несовпадение блокирует. Позиции должны быть согласованы: `position_symbols` длиной ровно `open_positions`, `correlated_open_positions <= open_positions`, `open_stop_risk_fraction > 0` запрещён при нуле позиций, а каждая открытая позиция требует свежего тика своего символа — иначе риск по её стопу неизвестен и снимок блокируется. Slippage больше 10% дистанции до стопа или reference price за SL/TP блокируют снимок; RR дополнительно проверяется по худшей цене входа против `min_expected_rr`. Снимок обязан содержать `open_stop_risk_fraction` числом `[0, 1]` (риск незакрытых стопов считается от текущей цены; отсутствующее, строковое или `null` значение — DENY), и проспективные лимиты складывают его с реализованными daily-loss/drawdown и риском новой сделки. Разрешение человека требует `decision_id` и одноразовый `nonce`, атомарно потребляемый через `InMemoryNonceStore` (тесты/демо) или `PostgresNonceStore`; nonce одноразовый на оператора, а отдельная insert-only запись в `authorization_permissions` (миграция 006) привязана к серверному хэшу самого ордера (signal/instrument/direction/size/entry/SL/TP/mode), а не к клиентскому `permission_id`, поэтому ни повтор nonce, ни переименование разрешения не позволяют подтвердить тот же ордер дважды. `PostgresNonceStore` хранит insert-only tombstones: приложение не удаляет строки в обработке запроса, ручная очистка допускается только после `expires_at` и согласованного retention-окна. Evidence-envelope не одноразовый: его повтор ограничен `min(max_age_seconds, ttl_seconds)` (по умолчанию 300 с, потолок конверта 3600 с), а идемпотентность обеспечивают nonce/permission stores.

Защищённые policy API endpoints требуют `X-Bridge-Key` и тело `{"payload": ..., "envelope": ...}` с валидной HMAC-подписью. Если `MT5_BRIDGE_TOKEN`, evidence-ключ/issuer, governance-ключ/issuer или review-ключ/issuer не настроены либо signing-домены пересекаются, endpoint отвечает HTTP 503; отсутствующий или неверный `X-Bridge-Key` — HTTP 401; неверная или изменённая evidence-подпись, а также отсутствующая/чужая `review_attestation` (`review_attestation_invalid`) — HTTP 403; неверная схема тела — HTTP 422. API пересчитывает `reviewer_count`, consensus и `open_challenges` из raw-записей и игнорирует заявленные клиентом агрегаты; verdict-метаданные (signal_id, `input_hash` против канонического `signal_input`, `created_at`/TTL) перепроверяются так же, как в pipeline. `/v1/permission/check` намеренно отвечает HTTP 423 и требует persisted-risk эндпоинт `/v1/risk/permission/check`. `/health` не содержит секретов и честно показывает, какие проверки сконфигурированы (`evidence`/`governance`/`operator`/`review` verification, отдельность bridge-токена, kill-switch admin и signing-ключей/issuer), но всегда сообщает execution disabled. Ключ bridge — защита localhost-транспорта, а не 2FA/цифровая подпись пользователя. `/v1/authorization/check` намеренно отвечает HTTP 423: он не принимает переданный клиентом объект PermissionResult как доказательство выданного разрешения, поскольку долговременного server-side permission store и настоящего 2FA/signature verifier пока нет.

## MT5-агент: observe/paper поверх терминала

Отдельный пакет `src/critical_consensus/mt5_agent/` (не часть конвейера разрешений) читает
состояние терминала без торговых вызовов, применяет fail-closed гейты и пишет каждое решение в
SQLite hash-журнал. `symbol_spec()` вызывает `symbol_select()`, только когда символа нет в
Market Watch, и это добавляет его в список символов терминала.
**Ордерного API в пакете нет**: `mode: live` отвергается политикой, каждая запись журнала содержит
`broker_order_sent: false`, а тест `test_agent_never_reports_a_broker_order`, скан исходников
`mt5_agent` с allowlist вызовов терминала без отправки ордеров и проверка `dir(MT5Agent)`
закрепляют это.

```bash
cp config/mt5_agent.example.yaml config/mt5_agent.yaml
python scripts/run_mt5_agent.py --db agent_data/mt5_agent.db run \
  --policy config/mt5_agent.yaml --source csv --csv export_eurusd_h1.csv \
  --source-timezone Europe/London --balance 10000 --language ru --execution confirm
python scripts/run_mt5_agent.py --db agent_data/mt5_agent.db report
```

Гейты одного цикла: часы UTC → kill switch → демо-счёт и закреплённые login/server → базы equity
и дневные лимиты → торговое окно и blackout → лимит позиций → свежесть котировки → риск по стопам
уже открытых позиций и убыток, зафиксированный за день → сигнал SMA/ATR (включая лимит спреда) →
риск/награда и объём из стоп-расстояния и `volume_step` → суммарный риск по стопам с дневным
убытком → режим исполнения → реквота после
подтверждения (дрейф не более 10 % стоп-расстояния, пересчёт RR и объёма, не выше одобренного
лота). `paper` исполняет сделку локально на исторических барах без
подглядывания вперёд (стоп проверяется раньше тейка внутри одного бара); `observe` только
фиксирует предложение. `--execution confirm` (по умолчанию) спрашивает `y/n/a/q` перед каждой
позицией, `--execution auto` разрешён только на демо-счёте; `--language ru|en|vi` переключает весь
интерфейс (русский, английский, вьетнамский), а журнал хранит только машинные коды.

Чтение реального терминала на этом хосте **PROVEN с 2026-10-31**: `MetaTrader5` 5.0.6231,
`terminal64.exe` build 6296, демо-счёт 10013404855 на `MetaQuotes-Demo` (баланс 10.00 USD),
живые спецификации и H1-бары по EURUSD/GBPUSD/AUDUSD/NZDUSD/XAUUSD/XAGUSD. Печатные цифры в
тестах по-прежнему синтетические и доходность не оценивают.

Ордер в терминал уходит только из демо-пути вне пакета: `scripts/run_mt5_demo_trading.py`
складывает одну команду в loopback-мост `scripts/mt5_demo_bridge.py`, а `OrderSend` вызывает
советник внутри терминала; в Python-скриптах торговой функции MT5 нет вообще. Демо-EA собирается
(`scripts/build_demo_ea.py`: MetaEditor — 0 ошибок, 0 предупреждений) и устанавливается, терминал
готовит к этому `scripts/setup_demo_terminal.py` (по умолчанию dry-run, требует закрытого
терминала, патчит `[Experts]` в `Config/common.ini` с бэкапом). **Не доказано и требует руками
сделанного шага:** привязка советника к графику и сам брокерский демо-филл — 2026-10-31 рынок был
закрыт, поэтому все циклы заблокированы на `QUOTE_STALE`, а 10 USD на балансе не вмещают ни одной
сделки по политике 0.5% риска (минимальный лот стоит 1.93–4.24 USD риска). Подробные инструкции,
гейты и живое наблюдение: [docs/MT5_AGENT.md](docs/MT5_AGENT.md) /
[docs/MT5_AGENT.en.md](docs/MT5_AGENT.en.md) / [docs/MT5_AGENT.vi.md](docs/MT5_AGENT.vi.md).

## Где ещё нет production-функций

Это не готовая торговая система. Нет настроенных реальных API-ключей/моделей для девяти обязательных агентов и подтверждённых независимых market/news источников. Peer-review/challenge сервис и provider callbacks подключены, но реальная работа не проверена до задания ключей и запуска на реальных сигналах. Нет подтверждённых dissent/false-certainty метрик и размеченной out-of-sample калибровки. Walk-forward evaluator и хранение прогнозов реализованы, но реальные калибровочные результаты не получены. CLI полного Chief Gate принимает только внутренний evidence packet, требует отдельно подписанную review-атестацию, не создаёт reviews/challenges, не подделывает source credibility и не принимает human approval. Отдельный TOTP/signature verifier, production-развёртывание worker/API и реальная диспетчеризация ордеров отсутствуют. Не подключайте реальный счёт. Демо-EA скомпилирован MetaEditor'ом без ошибок и предупреждений и установлен в терминал (2026-10-31), но ни один его `OnInit` не появлялся в логе советников, то есть привязка к графику и реальный демо-филл не проверены; статические проверки компиляцию и исполнение не заменяют.

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
11. Peer reviews, challenge ledger и ChiefReasoning без подписи review-домена блокируют evaluation; ingress-ключ не может их подписать.
12. Одно и то же человеческое подтверждение не может быть использовано дважды — ни по nonce, ни по ордеру: tombstone привязан к серверному хэшу параметров ордера, а не к `permission_id`.

## Проверка текущего каркаса

- Локальная проверка на Python 3.12 (Windows), ветка `feat/demo-live-run-2026-11-01`, 2026-11-04: `.venv\Scripts\python.exe -m pytest -q` — **1274 passed, 10 skipped in 101.44 s** (108 из них — пять `tests/test_fleet*.py` файлов парка) (интерпретатор важен: subprocess-тесты поднимают `sys.executable`, и запуск системным `python` без установленного пакета даёт 36 падений `ModuleNotFoundError: No module named 'critical_consensus'` — это артефакт окружения, а не регрессия); `python scripts/check_mql5_safety.py` — **473 checks (including 105 mutation rejections)**. Все пропуски требуют `TEST_DATABASE_URL` и/или `TEST_REDIS_URL` и здесь не запускались. По сборке есть отдельное доказательство: `scripts/build_demo_ea.py` собрал `CriticalConsensusEA_Demo.ex5` с 0 ошибок и 0 предупреждений, а 2026-11-01 советник на графике XAUUSD,H1 реально опрашивает мост (лог `MQL5/Logs`, `HTTP 200`/`204` с тем же `BridgeToken`) — см. таблицу статусов в [docs/MT5_AGENT.md](docs/MT5_AGENT.md). Демо-ордер исполнен брокером трижды за 2026-11-02 — 00:00:09, 00:07:36 и 05:43:15 UTC; история счёта 114929502 читает три deal'а с magic `8172026` и комментарием `CC:`, а журнал `agent_data/demo_live_20261102.db` на последнее исполнение закрыл команду как `FILLED` с `broker_order_sent: true` и `requires_manual_reconciliation: false` (deal `10938325361`, позиция `11189096387`, EURUSD SELL 0,09 по 1,11823); формулировка «ни одна команда никогда не ставилась в очередь» относилась к бумажным прогонам 2026-10-31 и 2026-11-01 и снята. Устойчивость при этом NOT PROVEN: между исполнениями цепочка стояла (356 отказов `error=4014` с 03:51:48 по 04:11:03 UTC, затем `HTTP 1001, error=5203` в пустой порт до 05:33:22, 13 команд истекли как `COMMAND_EXPIRED` между 02:22:59 и 02:36:46), а после 05:43:15 очередь не проверялась ни разу — 321 последующий цикл упёрлись в `POSITION_CAP_REACHED` при `max_open_positions: 1`. Состояние транспорта измеряет `scripts/check_ea_transport.py`, и с этого замера оно опирается на счётчик аутентифицированных опросов самого моста: советник в порядке не печатает ничего, а прежняя вердикт-логика объявляла такую связку `ea_silent (BROKEN)`. И по-прежнему NOT PROVEN: прибыльность стратегии — измеренные прогоны на настоящих барах убыточны ([docs/DEMO_RESULTS.md](docs/DEMO_RESULTS.md)). Результаты GitHub Actions относятся к предыдущим коммитам и для этой ветки должны быть подтверждены отдельным run после push; они не наследуются автоматически.
- Проверяются fail-closed gates, HMAC evidence/governance envelopes, одноразовые authorization nonce, integrity binding между ChiefReasoning/consensus/source summary и PermissionRequest, выдача полного human review packet, hash chain, API key gate, строгая каноническая сериализация и Redis Streams helpers.
- Проверка зелёных тестов доказывает поведение локальных функций на синтетических входах, но не проверяет брокер, MQL5-компиляцию, реальную очередь, рыночные данные или доходность.
- ChiefReasoning hash вычисляется по каноническому JSON без поля `reasoning_hash`; изменение любого шага делает hash невалидным.

## Официальные точки интеграции

- Python integration MetaTrader 5: https://www.mql5.com/en/docs/python_metatrader5
- MQL5 `OrderSend`: https://www.mql5.com/en/docs/trading/ordersend
- MQL5 network / `WebRequest`: https://www.mql5.com/en/docs/network
- Redis Streams: https://redis.io/docs/latest/develop/data-types/streams/
