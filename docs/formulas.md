# Метрики и формулы (начальные, требуют эмпирической валидации)

## Brier score

Для прогнозов `p_i` и бинарных исходов `y_i ∈ {0,1}`:

`BS = (1/N) * Σ (p_i - y_i)^2`; меньше — лучше. Оценивать out-of-sample, по отдельным режимам рынка и с доверительными интервалами. При маленьком N рейтинг агента помечать `INSUFFICIENT_SAMPLE`, а не считать точным.

## Штраф за плохую калибровку

`excess = max(0, BS - BS_threshold)`

`factor = clamp(0, 1, 1 - excess / penalty_scale)`

`p_shrunk = base_rate + (p_raw - base_rate) * factor`

При `p_raw >= 0.95` и empirical accuracy в этой confidence-bin `< 0.70` применяется дополнительный shrinkage. Это **защитная эвристика**, а не статистически корректная калибровка. Production должен использовать обученную Platt/isotonic calibration на разделённых во времени данных, reliability diagram, минимум наблюдений и мониторинг drift. Brier score сам по себе не определяет калиброванную вероятность.

## Самокритика и минимальный confidence

`APPROVE` допустим по первичной политике только при confidence `>= 0.75`; `< 0.50` приводит к `REJECT`, а диапазон `[0.50, 0.75)` — к `ABSTAIN` с блокирующей причиной. Это только локальный порог первичной оценки: он не отменяет более строгий `near_certainty_threshold: 0.98`, перекрёстную проверку, калибровку или остальные gates.

## Weighted consensus (описательный индикатор, не voting)

`w_i = max(0.01, 1 - BS_i)`

`consensus_score = Σ(w_i * p_shrunk_i) / Σ(w_i)`

Score не заменяет обязательные проверки каждого агента: все обязательные verdicts должны пройти индивидуальные условия, а один veto/reject/challenge блокирует gate. Для production лучше учесть uncertainty weights и временную стабильность качества.

## Credibility источника

Все входы нормированы `[0,1]`:

`base = .30*verified_accuracy + .20*authenticity + .15*independence + .15*freshness + .10*transparency + .10*lead_time`

`credibility = clamp(0,1, base * (1 - .50*fake_rate) * (1 - .25*bias_penalty))`

`freshness = exp(-age_seconds / ttl_seconds)` можно вычислять на ingestion. Точные веса должны подбираться только на сохранённых исторических оценках источников. Независимость источников проверяется по общему первоисточнику/синдикации, а не по количеству URL.

## Риск позиции (не завершён в MVP)

Денежный риск по стопу: `risk_cash = equity * risk_fraction`, `volume = risk_cash / loss_per_lot_at_SL`, с нормализацией на `volume_min`, `volume_step`, `volume_max`, валюту счёта, tick size/value, комиссии и гэп-риск. Не вычисляйте объём только по расстоянию в пунктах. Нужны брокерские `order_calc_profit`/`order_check` или эквивалентная проверка.

## False certainty rate

Долю ложных высокоуверенных предсказаний считать только на outcome-верифицированных прогнозах и фиксированном горизонте. Не смешивать недоступные/неразрешённые наблюдения с проигрышами; показывать числитель, знаменатель и confidence interval.

## Governance health gates

`dissent_rate` рассчитывается на фиксированном окне валидированных peer-review; если он ниже `dissent_rate_floor` (по умолчанию 0.10) или исторических наблюдений меньше `min_dissent_samples`, CertaintyGate не проходит. `false_certainty_rate` считается только по разрешённым outcome-verified прогнозам; значение выше `false_certainty_rate_max` (0.05) или недостаточная выборка блокируют PASS. В текущей политике обе метрики требуют минимум 30 наблюдений. Это исходные защитные настройки, а не статистическая гарантия; размер окна, неопределённость и regime slicing надо проверить на фактическом out-of-sample наборе.
