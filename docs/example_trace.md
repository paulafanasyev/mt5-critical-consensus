# Пример trace: сигнал заблокирован

Все данные ниже синтетические.

1. `IntelReport`: XAUUSD, гипотеза BUY, timestamp `T0`, источники `S1/S2`, оба помечены публичными.
2. `GlobalNewsAgent`: `APPROVE`, confidence `0.991`, Brier `0.08`; `TechnicalAnalystAgent`: `APPROVE`, `0.989`, Brier `0.09`; `RiskManagerAgent`: `APPROVE`, `0.990`, Brier `0.07`.
3. `DevilAdvocateAgent` публикует три альтернативные гипотезы, одну с новым противоречащим источником: challenge `C-17` остаётся `OPEN`.
4. CertaintyGate возвращает `GATHER_MORE`, блокер `OPEN_CHALLENGES_PRESENT`. Ордер не создаётся; задание на сбор данных содержит `challenge_id=C-17`.
5. После трёх неудачных раундов challenge resolution: `ESCALATE`; ChiefAgent и PermissionGate не пропускают сигнал дальше.
6. Audit chain: `IntelReport → Verdicts → PeerReviews → Challenge(C-17) → GATHER_MORE/ESCALATE`. Каждое событие хэшируется и ссылается на предыдущий hash.

Это корректное завершение процесса, а не ошибка системы: неопределённость должна блокировать сделку.
