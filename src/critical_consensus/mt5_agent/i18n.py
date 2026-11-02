"""Russian / English / Vietnamese labels for every operator-facing MT5 agent string.

Two rules keep this file trustworthy:

* The journal stores machine codes, never translations. A reworded label must not
  change a historical audit trail, so nothing in this module is used when hashing.
* Lookup is total. Every code the agent can emit has three labels and every message
  key has three variants with the same placeholders; the tests enforce it, and an
  unknown code falls back to the code itself instead of crashing a run mid-cycle.
"""
from __future__ import annotations

import re
import string
from typing import Any

LANGUAGES = ("ru", "en", "vi")
DEFAULT_LANGUAGE = "ru"

LANGUAGE_NAMES = {"ru": "Русский", "en": "English", "vi": "Tiếng Việt"}

# policy.blackout_blocker() numbers its windows at run time, so the code is not a constant.
_BLACKOUT = re.compile(r"^BLACKOUT_WINDOW_(\d+)$")

_ALIASES = {
    "ru": "ru", "rus": "ru", "russian": "ru", "русский": "ru", "ru_ru": "ru",
    "en": "en", "eng": "en", "english": "en", "en_us": "en", "en_gb": "en",
    "vi": "vi", "vn": "vi", "vie": "vi", "vietnamese": "vi", "tieng viet": "vi",
    "tiếng việt": "vi", "viet": "vi", "vi_vn": "vi",
}


class UnsupportedLanguage(ValueError):
    """A language value the CLI must report in the operator's current language."""

    def __init__(self, value: Any) -> None:
        self.value = value
        super().__init__(f"unsupported language {value!r}; choose {'/'.join(LANGUAGES)}")


def normalize_language(value: Any) -> str:
    """Map any reasonable spelling to ``ru``/``en``/``vi``, or refuse the value."""
    if not isinstance(value, str):
        raise UnsupportedLanguage(value)
    key = value.strip().replace("-", "_").lower()
    # ``tieng viet``, ``tieng-viet`` and ``tieng_viet`` are the same name; check the spaced
    # form too so a hyphenated or underscored spelling does not become an unsupported value.
    normalized = (_ALIASES.get(key) or _ALIASES.get(key.replace("_", " "))
                  or _ALIASES.get(key.split("_")[0]))
    if normalized is None:
        raise UnsupportedLanguage(value)
    return normalized


# (ru, en, vi) in LANGUAGES order. Keys are the codes from mt5_agent.codes.
CODE_LABELS: dict[str, tuple[str, str, str]] = {
    # Cycle statuses.
    "BLOCKED": ("Заблокировано", "Blocked", "Bị chặn"),
    "NO_SIGNAL": ("Нет сигнала", "No signal", "Không có tín hiệu"),
    "OBSERVED": ("Наблюдение — позиция не открыта", "Observed — no position opened",
                 "Quan sát — không mở lệnh"),
    "PAPER_OPENED": ("Бумажная позиция открыта", "Paper position opened",
                     "Đã mở lệnh mô phỏng"),
    # Agent gates, in run_cycle() order.
    "CLOCK_NOT_UTC_AWARE": ("Внутренние часы не знают часового пояса",
                            "Agent clock is not timezone-aware",
                            "Đồng hồ agent không có múi giờ"),
    "KILL_SWITCH_ACTIVE": ("Аварийный выключатель активен", "Kill switch is active",
                           "Công tắc khẩn cấp đang bật"),
    "ACCOUNT_NOT_DEMO": ("Счёт не является демо-счётом", "Account is not a demo account",
                         "Tài khoản không phải demo"),
    "ACCOUNT_LOGIN_MISMATCH": ("Логин счёта не совпадает с политикой",
                               "Account login does not match the policy",
                               "Login tài khoản không khớp chính sách"),
    "ACCOUNT_SERVER_MISMATCH": ("Сервер брокера не совпадает с политикой",
                                "Broker server does not match the policy",
                                "Máy chủ broker không khớp chính sách"),
    "ACCOUNT_EQUITY_INVALID": ("Некорректные значения средств (equity) или баланса",
                               "Invalid equity or balance",
                               "Vốn (equity) hoặc số dư không hợp lệ"),
    "ACCOUNT_MARGIN_INVALID": ("Свободная маржа нулевая или отрицательная",
                               "Free margin is zero or negative",
                               "Margin tự do bằng 0 hoặc âm"),
    "ACCOUNT_LEVERAGE_INVALID": ("Некорректное кредитное плечо", "Invalid leverage",
                                 "Đòn bẩy không hợp lệ"),
    "ACCOUNT_READ_FAILED": ("Терминал не вернул данные счёта",
                            "Failed to read the account from the terminal",
                            "Không đọc được tài khoản từ terminal"),
    "EQUITY_BASELINES_UNAVAILABLE": ("Нет корректных базовых значений equity",
                                     "Equity baselines are unavailable",
                                     "Không có mức equity gốc"),
    "DAILY_LOSS_LIMIT_REACHED": ("Дневной лимит потерь достигнут", "Daily loss limit reached",
                                 "Đã tới hạn lỗ trong ngày"),
    "REALIZED_DAILY_LOSS_LIMIT_REACHED": ("Дневной лимит зафиксированных потерь достигнут",
                                          "Realized daily loss limit reached",
                                          "Đã tới hạn lỗ đã thực hiện trong ngày"),
    "DRAWDOWN_LIMIT_REACHED": ("Лимит просадки достигнут", "Drawdown limit reached",
                               "Đã tới hạn sụt giảm vốn"),
    "OUTSIDE_TRADING_WINDOW": ("Вне торгового окна", "Outside the trading window",
                               "Ngoài khung giờ giao dịch"),
    "POSITION_CAP_COUNT_UNKNOWN": ("Число открытых позиций неизвестно",
                                   "Open position count is unknown",
                                   "Không rõ số lệnh đang mở"),
    "POSITION_CAP_REACHED": ("Достигнут лимит открытых позиций", "Open position cap reached",
                             "Đã tới giới hạn lệnh mở"),
    "POSITION_CAP_EXCEEDED": ("Лимит открытых позиций превышен", "Open position cap exceeded",
                              "Đã vượt giới hạn lệnh mở"),
    "POSITION_READ_FAILED": ("Не удалось прочитать открытые позиции",
                             "Failed to read open positions",
                             "Không đọc được các lệnh đang mở"),
    "POSITION_STOP_UNSET": ("У открытой позиции нет корректного стоп-лосса",
                            "An open position has no usable stop loss",
                            "Lệnh đang mở không có stop loss hợp lệ"),
    "LOSS_FLOOR_BREACHED": ("Риск существующих стопов уже исчерпал дневной бюджет",
                            "Existing stop risk already exhausts the daily budget",
                            "Rủi ro stop hiện tại đã dùng hết ngân sách ngày"),
    "AGGREGATE_STOP_RISK_LIMIT_REACHED": ("Суммарный риск по стопам превысит дневной бюджет",
                                          "Aggregate stop risk would exceed the daily budget",
                                          "Tổng rủi ro stop sẽ vượt ngân sách ngày"),
    "SYMBOL_ALREADY_OPEN": ("По этому инструменту уже есть позиция",
                            "A position on this symbol is already open",
                            "Đã có lệnh trên mã này"),
    "MARKET_DATA_READ_FAILED": ("Не удалось прочитать котировки или бары",
                                "Failed to read quotes or bars",
                                "Không đọc được báo giá hoặc nến"),
    "INSUFFICIENT_BARS": ("Недостаточно баров истории", "Not enough historical bars",
                          "Không đủ dữ liệu nến"),
    "QUOTE_FROM_THE_FUTURE": ("Котировка датирована будущим", "Quote timestamp is in the future",
                              "Báo giá có thời gian thuộc tương lai"),
    "QUOTE_STALE": ("Котировка устарела", "Quote is stale", "Báo giá đã cũ"),
    "PRICE_MOVED_DURING_CONFIRMATION": (
        "Цена ушла от согласованного входа дальше допустимого",
        "The price moved too far from the approved entry",
        "Giá đã đi quá xa so với mức vào đã xác nhận"),
    "PROPOSAL_SYMBOL_MISMATCH": (
        "Предложение относится к другому инструменту и не может быть отправлено",
        "The proposal belongs to another instrument and cannot be sent",
        "Đề xuất thuộc về một mã khác và không thể được gửi"),
    "PROPOSAL_UNREADABLE": (
        "В предложении нет полей, из которых собирается ордер",
        "The proposal lacks the fields an order is built from",
        "Đề xuất thiếu các trường để dựng một lệnh"),
    "RISK_REWARD_BELOW_MINIMUM": ("Соотношение прибыль/риск ниже минимума",
                                  "Reward-to-risk is below the policy minimum",
                                  "Tỷ lệ lợi nhuận/rủi ro dưới mức tối thiểu"),
    "MINIMUM_LOT_EXCEEDS_RISK_BUDGET": ("Минимальный лот превышает риск-бюджет",
                                        "The minimum lot exceeds the risk budget",
                                        "Lô tối thiểu vượt ngân sách rủi ro"),
    "MARGIN_MINIMUM_LOT_UNFUNDED": ("Свободной маржи не хватает на минимальный лот",
                                    "Free margin cannot fund the minimum lot",
                                    "Margin tự do không đủ cho lô tối thiểu"),
    "VOLUME_CEILING_BELOW_BROKER_MIN": ("Потолок лота ниже минимального лота брокера",
                                        "The lot ceiling is below the broker's minimum lot",
                                        "Mức trần lô dưới lô tối thiểu của broker"),
    "LEG_CHURN_DETECTED": ("Позиции по инструменту закрываются через минуты",
                           "Legs on this symbol keep closing minutes after they open",
                           "Vị thế trên mã này liên tục bị đóng sau vài phút"),
    "PAPER_BROKER_REQUIRED_FOR_PAPER_MODE": ("Для бумажного режима нужен локальный брокер",
                                             "Paper mode requires a local paper broker",
                                             "Chế độ mô phỏng cần broker mô phỏng cục bộ"),
    "PAPER_OPEN_REJECTED": ("Бумажный брокер отклонил позицию",
                            "The paper broker refused the position",
                            "Broker mô phỏng từ chối lệnh"),
    "CONFIRMATION_UNAVAILABLE": ("Режим подтверждения — спрашивать некого",
                                 "Confirmation mode has no operator to ask",
                                 "Chế độ xác nhận — không có người để hỏi"),
    "OPERATOR_DECLINED": ("Оператор отклонил предложение", "The operator declined the proposal",
                          "Người vận hành đã từ chối đề xuất"),
    "OPERATOR_CONFIRMATION_FAILED": ("Опрос оператора завершился ошибкой",
                                     "Asking the operator failed",
                                     "Việc hỏi người vận hành bị lỗi"),
    # Strategy blockers.
    "ATR_NOT_MEANINGFUL": ("ATR нулевой или бессмысленный", "ATR is zero or meaningless",
                           "ATR bằng 0 hoặc vô nghĩa"),
    "PRIOR_CLOSE_NON_POSITIVE": ("Предыдущее закрытие неположительное",
                                 "Previous close is not positive",
                                 "Giá đóng trước đó không dương"),
    "SPREAD_ABOVE_LIMIT": ("Спред выше лимита политики", "Spread exceeds the policy limit",
                           "Chênh lệch vượt giới hạn chính sách"),
    "NO_TREND_SIGNAL": ("Трендового сигнала нет", "No trend signal", "Không có tín hiệu xu hướng"),
    "STOP_DISTANCE_INVALID": ("Некорректное расстояние до стопа", "Stop distance is invalid",
                              "Khoảng cách tới stop không hợp lệ"),
    "TAKE_PROFIT_PRICE_INVALID": ("Некорректная цена тейк-профита",
                                  "Take-profit price is invalid",
                                  "Giá take profit không hợp lệ"),
    "BUY_GEOMETRY_INVALID": ("Некорректная геометрия BUY", "BUY geometry is invalid",
                             "Cấu trúc giá của lệnh BUY không hợp lệ"),
    "SELL_GEOMETRY_INVALID": ("Некорректная геометрия SELL", "SELL geometry is invalid",
                              "Cấu trúc giá của lệnh SELL không hợp lệ"),
    "STOP_INSIDE_BROKER_STOPS_LEVEL": ("Стоп ближе минимального уровня брокера",
                                       "Stop is inside the broker's stops level",
                                       "Stop nằm trong mức stop tối thiểu của broker"),
    "TARGET_INSIDE_BROKER_STOPS_LEVEL": ("Тейк-профит ближе минимального уровня брокера",
                                         "Take-profit is inside the broker's stops level",
                                         "Take profit nằm trong mức stop tối thiểu của broker"),
    "BAR_SERIES_INVALID": ("Последовательность баров не прошла проверку",
                           "The bar series failed validation", "Chuỗi nến không hợp lệ"),
    "BAR_SYMBOL_MISMATCH": ("Спецификация, тик или бары относятся к другому инструменту",
                            "The symbol spec, tick or bars belong to a different symbol",
                            "Đặc tả, tick hoặc nến thuộc mã khác"),
    "BAR_TIMEFRAME_MISMATCH": ("Бары пришли с другим таймфреймом",
                               "Bars arrived on a different timeframe",
                               "Nến đến với khung thời gian khác"),
    "BAR_TIME_IN_FUTURE": ("Последний бар датирован будущим",
                           "The last bar is dated in the future",
                           "Nến cuối cùng thuộc về tương lai"),
    "BAR_TIME_TOO_OLD": ("Последний бар слишком старый для текущей котировки",
                         "The last bar is too old for the current quote",
                         "Nến cuối cùng quá cũ so với báo giá hiện tại"),
    # Why a simulated position closed. Journaled as codes, rendered only for the operator.
    "MANUAL": ("Закрыто вручную", "Closed manually", "Đóng thủ công"),
    "STOP_LOSS": ("Выход по стоп-лоссу", "Stop-loss exit", "Thoát bằng stop loss"),
    "STOP_LOSS_GAP": ("Стоп-лосс исполнен по гэпу", "Stop-loss filled on a gap",
                      "Stop loss khớp qua gap"),
    "TAKE_PROFIT": ("Выход по тейк-профиту", "Take-profit exit", "Thoát bằng take profit"),
    "TAKE_PROFIT_GAP": ("Тейк-профит исполнен по гэпу", "Take-profit filled on a gap",
                        "Take profit khớp qua gap"),
    "END_OF_RUN": ("Закрыто в конце прогона", "Closed at the end of the run",
                   "Đóng khi kết thúc lượt chạy"),
    # The cycle record names the absence of an order-sending surface as a code.
    "NO_BROKER_ORDER_API": ("В пакете нет API для отправки ордеров",
                            "No broker order API exists in this package",
                            "Gói này không có API gửi lệnh tới broker"),
    # Execution modes. No colon inside a label: ``out.mode_line`` already introduces it and
    # the operator would otherwise read "execution mode: confirmation: every position".
    "auto": ("автоматический — агент открывает позиции сам",
             "automatic — the agent opens positions by itself",
             "tự động — agent tự mở lệnh"),
    "confirm": ("с подтверждением — каждая позиция запрашивает согласие оператора",
                "confirmation — every position asks the operator first",
                "xác nhận — mỗi lệnh đều hỏi người vận hành trước"),
    "observe": ("наблюдение — сигналы записываются, позиции не открываются",
                "observe — signals are journaled, no position is opened",
                "quan sát — ghi tín hiệu, không mở lệnh"),
    "paper": ("бумажная торговля — сделки симулируются локально",
              "paper trading — fills are simulated locally",
              "giao dịch mô phỏng — lệnh được giả lập cục bộ"),
}

# Operator-facing prose. Placeholders must be identical in all three locales.
TEXT: dict[str, tuple[str, str, str]] = {
    "cli.description": (
        "Агент MetaTrader 5: только демо-счёт и локальная симуляция, ордера брокеру не"
        " отправляются",
        "MetaTrader 5 agent: demo account and local simulation only, no broker orders",
        "Agent MetaTrader 5: chỉ tài khoản demo và mô phỏng cục bộ, không gửi lệnh tới broker"),
    "cli.language": ("язык вывода: ru (Русский), en (English) или vi (Tiếng Việt)",
                     "output language: ru (Русский), en (English) or vi (Tiếng Việt)",
                     "ngôn ngữ đầu ra: ru (Русский), en (English) hoặc vi (Tiếng Việt)"),
    "cli.db": ("файл журнала решений", "decision journal file", "tệp nhật ký quyết định"),
    "cli.run": ("Прогнать агента по историческим барам", "Run the agent over historical bars",
                "Chạy agent trên dữ liệu nến lịch sử"),
    "cli.report": ("Показать журнал", "Show the journal", "Xem nhật ký"),
    "cli.reset": ("Очистить журнал", "Clear the journal", "Xóa nhật ký"),
    "cli.policy": ("файл политики YAML", "YAML policy file", "tệp chính sách YAML"),
    "cli.source": ("источник баров: csv или live (терминал MT5 без торговых вызовов)",
                   "bar source: csv or live (order-free MT5 terminal)",
                   "nguồn nến: csv hoặc live (terminal MT5 không gửi lệnh)"),
    "cli.csv": ("путь к экспорту MT5 CSV", "path to the MT5 CSV export",
                "đường dẫn tệp xuất MT5 CSV"),
    "cli.source_timezone": ("IANA-зона экспорта MT5, если метки в CSV наивные",
                            "IANA zone of the MT5 export when CSV stamps are naive",
                            "múi giờ IANA của tệp xuất khi mốc thời gian trong CSV không có"
                            " múi giờ"),
    "cli.terminal_path": ("путь к исполняемому файлу MetaTrader 5",
                          "MetaTrader 5 terminal executable path",
                          "đường dẫn tệp thực thi MetaTrader 5"),
    "cli.live_bars": ("сколько последних баров взять из терминала",
                      "how many recent bars to read from the terminal",
                      "số nến gần nhất cần đọc từ terminal"),
    "cli.balance": ("стартовый баланс для симуляции", "starting balance for the simulation",
                    "số dư ban đầu để mô phỏng"),
    "cli.currency": ("валюта счёта", "account currency", "tiền tệ tài khoản"),
    "cli.limit_cycles": ("максимум циклов за прогон", "maximum cycles per run",
                         "số vòng tối đa mỗi lần chạy"),
    "cli.runner_cycles": (
        "максимум проходов по списку инструментов; 0 — без ограничения",
        "maximum passes over the instrument list; 0 means unlimited",
        "số lượt tối đa qua danh sách mã; 0 là không giới hạn"),
    "cli.symbols": (
        "инструменты через запятую или all (EURUSD,XAUUSD,...)",
        "comma-separated instruments or all (EURUSD,XAUUSD,...)",
        "các mã cách nhau bằng dấu phẩy hoặc all (EURUSD,XAUUSD,...)"),
    "cli.run_id": ("идентификатор прогона для журнала; без него он выводится из файла состояния"
                   " и политики, так что перезапуск остаётся тем же прогоном",
                   "journal run identifier; without it one is derived from the state file and"
                   " policy, so a restart is still the same run",
                   "mã lượt chạy cho nhật ký; nếu không có, mã được suy ra từ tệp trạng thái và"
                   " chính sách, nên khởi động lại vẫn là cùng lượt chạy"),
    "cli.fresh": ("очистить весь общий журнал перед прогоном, включая прогоны других run-id"
                  " (требует --confirm)",
                  "clear the whole shared journal before running, including other run ids"
                  " (requires --confirm)",
                  "xóa toàn bộ nhật ký dùng chung trước khi chạy, kể cả các run-id khác"
                  " (cần --confirm)"),
    "cli.usage": ("использование:", "usage:", "cách dùng:"),
    "cli.help": ("показать эту справку и выйти", "show this help message and exit",
                 "hiển thị trợ giúp này và thoát"),
    "cli.positional": ("позиционные аргументы:", "positional arguments:",
                       "đối số vị trí:"),
    "cli.options": ("параметры:", "options:", "tùy chọn:"),
    "cli.confirm": ("подтверждение разрушительного действия",
                    "acknowledgement for a destructive action",
                    "xác nhận thao tác không thể hoàn tác"),
    "cli.execution": ("режим исполнения: auto (сам) или confirm (спрашивать оператора)",
                     "execution mode: auto (by itself) or confirm (ask the operator)",
                     "chế độ thực thi: auto (tự chạy) hoặc confirm (hỏi người vận hành)"),
    "cli.verbose": ("печатать каждый цикл", "print every cycle", "in mỗi vòng"),
    "cli.verify_anchor": ("хэш якоря из предыдущего прогона для проверки цепочки",
                          "anchor hash from a previous run to verify the chain",
                          "mã anchor từ lượt trước để kiểm tra chuỗi"),
    "cli.balance_report": ("баланс для расчёта доходности", "balance used for the return",
                           "số dư dùng để tính tỷ suất sinh lời"),
    "cli.bridge_port": ("порт локального моста (только 127.0.0.1)",
                        "localhost bridge port (127.0.0.1 only)",
                        "cổng cầu nối nội bộ (chỉ 127.0.0.1)"),
    "cli.tombstones": ("файл защищённых идентификаторов команд (относительный путь считается"
                       " от корня репозитория)",
                       "durable command-id tombstone file (a relative path is resolved from the"
                       " repository root)",
                       "tệp mã lệnh đã ghi nhận (đường dẫn tương đối tính từ gốc repository)"),
    "cli.interval_seconds": ("пауза между циклами, секунды", "pause between cycles, seconds",
                             "khoảng nghỉ giữa các vòng, giây"),
    "cli.fill_timeout": ("сколько ждать подтверждения сделки, секунды",
                         "seconds to wait for a fill before declaring the result unknown",
                         "số giây chờ khớp lệnh trước khi coi kết quả là chưa rõ"),
    "cli.ttl_seconds": ("время жизни команды, секунды (не больше 600)",
                        "command TTL in seconds (at most 600)",
                        "thời hạn lệnh, giây (tối đa 600)"),
    "cli.acknowledge_uncertain": (
        "продолжить, приняв неопределённые команды к ручной сверке",
        "continue while accepting uncertain earlier commands for manual reconciliation",
        "tiếp tục, chấp nhận các lệnh chưa rõ để đối soát thủ công"),
    "cli.release_kill_switch": (
        "снять аварийную остановку в файле состояния и выйти (нужен отдельный админ-токен)",
        "release the persisted emergency stop and exit (needs the separate admin token)",
        "tắt khẩn dừng đã lưu rồi thoát (cần token admin riêng)"),
    "cli.reconcile_window": ("сколько часов назад проверять отданные команды",
                             "how many hours back to reconcile served commands",
                             "đối soát lệnh đã giao trong bao nhiêu giờ gần đây"),
    "cli.max_volume": ("жёсткий предел объёма, лотов (не больше 0.20)",
                       "hard volume cap in lots (at most 0.20)",
                       "giới hạn khối lượng cứng, lot (tối đa 0.20)"),

    "out.disclaimer": (
        "Внимание: агент работает только с демо-счётом или в локальной симуляции. Ни один"
        " ордер не отправляется брокеру. Прошлые результаты не гарантируют будущую прибыль.",
        "Warning: only a demo account or a local simulation runs. No order is ever sent to a"
        " broker. Past results do not guarantee future profit.",
        "Cảnh báo: chỉ tài khoản demo hoặc mô phỏng cục bộ được chạy. Không lệnh nào được gửi"
        " tới broker. Kết quả quá khứ không bảo đảm lợi nhuận tương lai."),
    "out.fresh_needs_confirm": (
        "Для --fresh нужен также --confirm: вся история общего журнала, включая другие"
        " run-id, будет очищена.",
        "--fresh also requires --confirm: the whole shared journal, including other run ids,"
        " will be erased.",
        "--fresh cần kèm --confirm: toàn bộ nhật ký dùng chung, kể cả các run-id khác, sẽ bị"
        " xóa."),
    "out.need_csv": ("--csv обязателен при --source csv.",
                     "--csv is required with --source csv.",
                     "--csv bắt buộc khi dùng --source csv."),
    "out.not_enough_bars": ("Недостаточно баров: {bars} (минимум {minimum}).",
                            "Not enough bars: {bars} (minimum {minimum}).",
                            "Không đủ nến: {bars} (tối thiểu {minimum})."),
    "out.live_real_account": ("Терминал подписан на real-счёт; live-источник отклонён.",
                              "The terminal is signed into a real account; the live source is"
                              " refused.",
                              "Terminal đang đăng nhập tài khoản thật; nguồn live bị từ chối."),
    "out.live_login_mismatch": ("Логин терминала {actual} не совпадает с политикой {expected}; "
                                "источник отклонён.",
                                "Terminal login {actual} does not match the policy {expected};"
                                " source refused.",
                                "Login terminal {actual} không khớp chính sách {expected}; "
                                "nguồn bị từ chối."),
    "out.live_server_mismatch": ("Сервер терминала {actual} не совпадает с политикой {expected};"
                                 " источник отклонён.",
                                 "Terminal server {actual} does not match the policy {expected};"
                                 " source refused.",
                                 "Máy chủ terminal {actual} không khớp chính sách {expected}; "
                                 "nguồn bị từ chối."),
    "out.anchor_mismatch": ("Цепочка журнала не совпадает с заявленным якорем.",
                            "The journal chain does not match the supplied anchor.",
                            "Chuỗi nhật ký không khớp với anchor đã cung cấp."),
    "out.chain_invalid": ("Цепочка журнала не прошла внутреннюю проверку.",
                          "The journal chain failed its internal verification.",
                          "Chuỗi nhật ký không vượt qua kiểm tra nội bộ."),
    "report.note": ("Только бумажная симуляция. Ни один ордер не дошёл до брокера; прошлые"
                    " бумажные результаты не предсказывают реальную торговлю.",
                    "Paper simulation only. No order reached a broker; past paper results do not"
                    " predict live performance.",
                    "Chỉ mô phỏng; không lệnh nào tới broker; kết quả mô phỏng quá khứ không"
                    " dự đoán hiệu quả giao dịch thật."),
    "out.journal_cleared": ("Журнал очищен.", "Journal cleared.", "Đã xóa nhật ký."),
    "out.reset_needs_confirm": ("Добавьте --confirm, чтобы очистить журнал решений.",
                                "Add --confirm to clear the decision journal.",
                                "Thêm --confirm để xóa nhật ký quyết định."),
    "out.error": ("Ошибка: {reason}", "Error: {reason}", "Lỗi: {reason}"),
    "code.blackout_window": ("Действует окно запрета торговли {index}",
                             "Trading blackout window {index} is active",
                             "Đang áp dụng vùng cấm giao dịch {index}"),
    "out.yes": ("да", "yes", "có"),
    "out.no": ("нет", "no", "không"),
    "err.number": ("{value} должно быть числом", "{value} must be a number",
                   "{value} phải là một số"),
    "err.positive_number": ("{value} должно быть положительным и конечным",
                            "{value} must be positive and finite",
                            "{value} phải dương và hữu hạn"),
    "err.integer": ("{value} должно быть целым числом", "{value} must be an integer",
                    "{value} phải là số nguyên"),
    "err.at_least_one": ("{value} должно быть не меньше 1", "{value} must be at least 1",
                         "{value} phải ít nhất là 1"),
    "err.non_negative": ("{value} не может быть отрицательным", "{value} cannot be negative",
                         "{value} không được âm"),
    "err.symbols": (
        "инструмент «{value}» недопустим: укажите тикеры через запятую или all",
        "instrument \"{value}\" is not usable: give tickers separated by commas, or all",
        "mã \"{value}\" không hợp lệ: hãy nhập các mã cách nhau bằng dấu phẩy, hoặc all"),
    "err.empty_run_id": ("--run-id не может быть пустым", "--run-id cannot be empty",
                         "--run-id không được để trống"),
    "err.run_id_invalid": (
        "--run-id должен быть короче 65 символов и без переводов строки",
        "--run-id must be at most 64 characters and a single line",
        "--run-id phải dài tối đa 64 ký tự và trên một dòng"),
    "err.language": ("язык {value} не поддерживается; выберите ru, en или vi",
                     "language {value} is not supported; choose ru, en or vi",
                     "ngôn ngữ {value} không được hỗ trợ; chọn ru, en hoặc vi"),
    "out.summary": ("Итог {run_id}: циклов {cycles}, позиций открыто {opened}, сделок закрыто "
                    "{trades}, реализованный результат {pnl}, итоговые средства {equity}, "
                    "цепочка журнала корректна: {chain}.",
                    "Summary {run_id}: cycles {cycles}, positions opened {opened}, trades closed"
                    " {trades}, realized P&L {pnl}, final equity {equity}, journal chain valid:"
                    " {chain}.",
                    "Tóm tắt {run_id}: vòng {cycles}, lệnh đã mở {opened}, giao dịch đã đóng "
                    "{trades}, lãi/lỗ đã thực hiện {pnl}, vốn (equity) cuối {equity}, chuỗi nhật ký "
                    "hợp lệ: {chain}."),
    "out.mode_line": ("Режим исполнения: {mode}. Инструмент {symbol}, источник данных: {source}.",
                      "Execution mode: {mode}. Symbol {symbol}, data source: {source}.",
                      "Chế độ thực thi: {mode}. Mã {symbol}, nguồn dữ liệu: {source}."),
    "out.operator_stopped": ("Прогон остановлен оператором; итоговый отчёт ниже.",
                             "Run stopped by the operator; the report follows.",
                             "Lượt chạy bị người vận hành dừng; báo cáo bên dưới."),
    "out.non_interactive": ("Ввод с клавиатуры недоступен: все последующие предложения"
                            " отклоняются как неподтверждённые.",
                            "No keyboard input available: every later proposal is declined as"
                            " unconfirmed.",
                            "Không có dữ liệu nhập từ bàn phím: mọi đề xuất sau đó bị từ chối vì"
                            " chưa được xác nhận."),

    "confirm.title": ("Агент предлагает позицию по {symbol}:",
                      "The agent proposes a position on {symbol}:",
                      "Agent đề xuất một lệnh trên mã {symbol}:"),
    "confirm.direction": ("Направление", "Direction", "Hướng"),
    "confirm.volume": ("Объём, лотов", "Volume, lots", "Khối lượng, lot"),
    "confirm.entry": ("Цена входа", "Entry price", "Giá vào"),
    "confirm.stop": ("Стоп-лосс", "Stop loss", "Cắt lỗ"),
    "confirm.take_profit": ("Тейк-профит", "Take profit", "Chốt lời"),
    "confirm.risk": ("Риск в валюте счёта", "Risk in account currency",
                     "Rủi ro theo tiền tệ tài khoản"),
    "confirm.reward": ("Цель в валюте счёта", "Target in account currency",
                       "Mục tiêu theo tiền tệ tài khoản"),
    "confirm.equity": ("Средства (equity)", "Equity", "Vốn (equity)"),
    "confirm.question": ("Открыть позицию? y — да, n — нет, a — да для всех последующих,"
                         " q — остановить прогон. Ответ: ",
                         "Open this position? y — yes, n — no, a — yes for all later ones,"
                         " q — stop the run. Answer: ",
                         "Mở lệnh này? y — có, n — không, a — có cho tất cả lệnh sau,"
                         " q — dừng lượt chạy. Trả lời: "),
    "confirm.accepted": ("Оператор подтвердил открытие позиции.",
                         "The operator approved opening the position.",
                         "Người vận hành đã xác nhận mở lệnh."),
    "confirm.declined": ("Оператор отклонил предложение.", "The operator declined the proposal.",
                         "Người vận hành đã từ chối đề xuất."),
    "confirm.auto_accepted": ("Оператор выбрал автоматический режим до конца прогона.",
                              "The operator chose automatic execution for the rest of the run.",
                              "Người vận hành chọn chế độ tự động cho phần còn lại."),
    "confirm.stopped": ("Оператор остановил прогон.", "The operator stopped the run.",
                        "Người vận hành đã dừng lượt chạy."),

    # Demo execution runner (scripts/run_mt5_demo_trading.py). These strings describe a
    # demo-only EA path that is separate from the order-free package; the journal still
    # stores codes, not translations.
    "runner.description": (
        "Демо-исполнение: агент ищет сигнал, локальный мост отдаёт команду EA, EA отправляет"
        " только демо-ордер. Реальные деньги не используются.",
        "Demo execution: the agent finds a signal, a localhost bridge hands the command to the"
        " EA, and the EA sends a demo order only. No real money is used.",
        "Thực thi demo: agent tìm tín hiệu, cầu nối nội bộ chuyển lệnh cho EA, EA chỉ gửi lệnh"
        " demo. Không dùng tiền thật."),
    "runner.policy_gate": (
        "Демо-исполнение требует account.require_demo: true и режим observe или paper.",
        "Demo execution requires account.require_demo: true and mode observe or paper.",
        "Thực thi demo yêu cầu account.require_demo: true và chế độ observe hoặc paper."),
    "runner.not_demo": (
        "Терминал подключён не к демо-счёту; демо-исполнение отклонено.",
        "The terminal is not signed into a demo account; demo execution refused.",
        "Terminal không đăng nhập tài khoản demo; từ chối thực thi demo."),
    "runner.identity_mismatch": (
        "Счёт {login} на сервере {server} не совпадает с политикой; демо-исполнение отклонено.",
        "Account {login} on {server} does not match the policy; demo execution refused.",
        "Tài khoản {login} trên {server} không khớp chính sách; từ chối thực thi demo."),
    "runner.identity_unbound": (
        "В политике не заданы account.expected_login и account.expected_server; демо-исполнение"
        " отклонено: ордер ушёл бы на любой счёт, куда подключён терминал.",
        "The policy sets neither account.expected_login nor account.expected_server; demo"
        " execution refused: the order would go to whichever account the terminal holds.",
        "Chính sách không đặt account.expected_login hay account.expected_server; từ chối thực"
        " thi demo: lệnh sẽ gửi tới bất kỳ tài khoản nào terminal đang đăng nhập."),
    "runner.risk_baseline_seeded": (
        "Дневной бюджет риска для счёта {login} на {server} начат заново за день {day}: отсчёт"
        " от {equity}. Просадка предыдущего дня в него не входит.",
        "The daily risk budget for account {login} on {server} starts fresh for day {day},"
        " counting from {equity}; the previous day's drawdown is not part of it.",
        "Ngân sách rủi ro trong ngày của tài khoản {login} trên {server} bắt đầu lại cho ngày"
        " {day}, tính từ {equity}; mức sụt giảm của ngày trước không được tính."),
    "runner.ready": (
        "Демо-счёт {login} ({server}), баланс {balance}, плечо {leverage}. Мост {url}.",
        "Demo account {login} ({server}), balance {balance}, leverage {leverage}. Bridge {url}.",
        "Tài khoản demo {login} ({server}), số dư {balance}, đòn bẩy {leverage}. Cầu nối {url}."),
    "runner.ea_hint": (
        "Проверьте, что демо-EA прикреплён к графику, а URL моста добавлен в белый список"
        " WebRequest.",
        "Make sure the demo EA is attached to a chart and the bridge URL is in the WebRequest"
        " allowlist.",
        "Hãy chắc rằng EA demo đã gắn vào biểu đồ và URL cầu nối nằm trong danh sách cho phép"
        " WebRequest."),
    "runner.symbols_line": (
        "Инструменты: {active}. Недоступны у брокера: {unavailable}.",
        "Instruments: {active}. Not offered by the broker: {unavailable}.",
        "Các mã: {active}. Broker không cung cấp: {unavailable}."),
    "runner.symbol_unavailable": (
        "Инструмент {symbol} недоступен в терминале ({reason}); пропущен.",
        "Instrument {symbol} is unavailable in the terminal ({reason}); skipped.",
        "Mã {symbol} không có trong terminal ({reason}); bỏ qua."),
    "runner.symbol_aliased": (
        "Инструмент {requested} у этого брокера торгуется как {resolved}; используется"
        " {resolved}, спецификация берётся из терминала.",
        "This broker lists {requested} as {resolved}; trading {resolved} and reading its"
        " specification from the terminal.",
        "Broker này niêm yết {requested} dưới dạng {resolved}; dùng {resolved} và lấy đặc"
        " tả từ terminal."),
    "runner.no_symbols": (
        "Ни один запрошенный инструмент недоступен в терминале; запуск остановлен.",
        "None of the requested instruments is available in the terminal; the run is stopped.",
        "Không mã nào trong danh sách có trong terminal; lượt chạy bị dừng."),
    "runner.bridge_token_missing": (
        "Нужен MT5_BRIDGE_TOKEN: 24..256 символов [A-Za-z0-9._~-].",
        "MT5_BRIDGE_TOKEN is required: 24..256 characters of [A-Za-z0-9._~-].",
        "Cần MT5_BRIDGE_TOKEN: 24..256 ký tự thuộc [A-Za-z0-9._~-]."),
    "runner.kill_switch_armed": (
        "Аварийная остановка активна ({reason}); ни один новый ордер не будет отправлен,"
        " пока её не снимут.",
        "The emergency stop is armed ({reason}); no new order will be sent until it is"
        " released.",
        "Khẩn dừng đang bật ({reason}); không lệnh mới nào được gửi cho tới khi tắt lại."),
    "runner.kill_switch_seeded": (
        "В {file} не было ни одной записи, поэтому прогон создал её с активной аварийной"
        " остановкой ({reason}): отсутствие записи не является разрешением торговать. Снимите"
        " её через --release-kill-switch с MT5_KILL_SWITCH_ADMIN_TOKEN.",
        "{file} held no record at all, so this run created one with the emergency stop armed"
        " ({reason}): an absent record is not permission to trade. Lift it with"
        " --release-kill-switch and MT5_KILL_SWITCH_ADMIN_TOKEN.",
        "Trong {file} không có bản ghi nào, nên phiên chạy này tạo một bản ghi với khẩn dừng"
        " đang bật ({reason}): không có bản ghi không phải là giấy phép giao dịch. Tắt nó bằng"
        " --release-kill-switch cùng MT5_KILL_SWITCH_ADMIN_TOKEN."),
    "runner.kill_switch_state_lost": (
        "Состояние аварийной остановки не записано ({error}); она остаётся активной в этом"
        " процессе, но переживёт ли она перезапуск — неизвестно.",
        "The emergency-stop state could not be written ({error}); it stays armed in this"
        " process, but whether it survives a restart is unknown.",
        "Không ghi được trạng thái khẩn dừng ({error}); nó vẫn bật trong tiến trình này,"
        " nhưng có sống qua khởi động lại hay không thì chưa rõ."),
    "runner.kill_switch_released": (
        "Аварийная остановка снята; {file}, {updated_at}.",
        "The emergency stop is released; {file}, {updated_at}.",
        "Khẩn dừng đã tắt; {file}, {updated_at}."),
    "runner.kill_switch_release_refused": (
        "Снятие не принято: {reason}. Нужен отдельный MT5_KILL_SWITCH_ADMIN_TOKEN от 32"
        " символов, не равный MT5_BRIDGE_TOKEN.",
        "Release refused: {reason}. A separate MT5_KILL_SWITCH_ADMIN_TOKEN of at least 32"
        " characters, different from MT5_BRIDGE_TOKEN, is required.",
        "Từ chối tắt: {reason}. Cần MT5_KILL_SWITCH_ADMIN_TOKEN riêng từ 32 ký tự và khác"
        " MT5_BRIDGE_TOKEN."),
    "runner.queued": (
        "Команда {command_id} поставлена в очередь: {direction} {symbol}, объём {volume}.",
        "Command {command_id} queued: {direction} {symbol}, volume {volume}.",
        "Lệnh {command_id} đã vào hàng đợi: {direction} {symbol}, khối lượng {volume}."),
    "runner.filled": (
        "Демо-сделка подтверждена: тикет {ticket}, цена {price}, объём {volume}.",
        "Demo fill confirmed: ticket {ticket}, price {price}, volume {volume}.",
        "Đã xác nhận khớp lệnh demo: ticket {ticket}, giá {price}, khối lượng {volume}."),
    "runner.fill_mismatch": (
        "Сделка {ticket} не соответствует команде: {reasons}; она не записана как исполнение,"
        " нужна ручная сверка.",
        "Deal {ticket} does not match the command: {reasons}; it is not booked as a fill and"
        " manual reconciliation is required.",
        "Giao dịch {ticket} không khớp với lệnh: {reasons}; không được ghi nhận là khớp lệnh"
        " và cần đối soát thủ công."),
    "runner.expired": (
        "EA не забрал команду за отведённое время; ничего не отправлено.",
        "The EA did not pick up the command within its TTL; nothing was sent.",
        "EA không nhận lệnh trong thời hạn; chưa gửi gì."),
    "runner.unconfirmed": (
        "EA забрал команду, но сделка не наблюдалась за {seconds} с. Результат неизвестен:"
        " нужна ручная сверка, повтор запрещён.",
        "The EA picked up the command but no fill was observed within {seconds}s. The result is"
        " unknown: reconcile manually, do not retry.",
        "EA đã nhận lệnh nhưng không thấy khớp lệnh trong {seconds} giây. Kết quả chưa rõ: hãy"
        " đối soát thủ công, không thử lại."),
    "runner.unconfirmed_unreadable": (
        "EA забрал команду, но историю сделок не удалось прочитать ({error}) за {seconds} с."
        " Результат неизвестен: нужна ручная сверка, повтор запрещён.",
        "The EA picked up the command but the deal history stayed unreadable ({error}) for"
        " {seconds}s. The result is unknown: reconcile manually, do not retry.",
        "EA đã nhận lệnh nhưng không đọc được lịch sử giao dịch ({error}) trong {seconds} giây."
        " Kết quả chưa rõ: hãy đối soát thủ công, không thử lại."),
    "runner.refused": (
        "Предложение отклонено до отправки: {reason}.",
        "The proposal was refused before sending: {reason}.",
        "Đề xuất bị từ chối trước khi gửi: {reason}."),
    "runner.duplicate": (
        "Этот идентификатор ордера уже отдавался EA; команда не повторяется.",
        "This order identifier was already handed to the EA; the command is not repeated.",
        "Mã lệnh này đã được giao cho EA; không lặp lại lệnh."),
    "runner.stopped_uncertain": (
        "Остановка: у предыдущей команды неизвестный результат; нужна ручная сверка.",
        "Stopping: the previous command has an unknown result; manual reconciliation is"
        " required.",
        "Dừng: lệnh trước có kết quả chưa rõ; cần đối soát thủ công."),
    "runner.stopped": (
        "Оператор остановил демо-исполнение.",
        "The operator stopped demo execution.",
        "Người vận hành đã dừng thực thi demo."),
    "runner.interrupted_send": (
        "Прервано с командой {command_id} для {symbol}, уже переданной в мост: EA мог её"
        " взять, а сделка не прочитана; результат неизвестен.",
        "Interrupted while command {command_id} for {symbol} was already in the bridge: the EA"
        " may have taken it, and no deal was read, so the result is unknown.",
        "Bị gián đoạn khi lệnh {command_id} cho {symbol} đã nằm trong cầu nối: EA có thể đã"
        " nhận lệnh, và không có giao dịch nào được đọc, nên kết quả không rõ."),
    "runner.confirm_note": (
        "Внимание: подтверждённый ордер уйдёт на ДЕМО-счёт через EA.",
        "Note: an approved order will be sent to the DEMO account through the EA.",
        "Lưu ý: lệnh được duyệt sẽ gửi tới tài khoản DEMO qua EA."),
    "runner.summary": (
        "Итог демо-исполнения {run_id}: сигналов {signals}, команд {commands}, исполнено"
        " {filled}, истекло {expired}, неизвестно {unconfirmed}, отклонено {refused}; цепочка"
        " журнала корректна: {chain}.",
        "Demo execution summary {run_id}: signals {signals}, commands {commands}, filled"
        " {filled}, expired {expired}, unconfirmed {unconfirmed}, refused {refused}; journal"
        " chain valid: {chain}.",
        "Tóm tắt thực thi demo {run_id}: tín hiệu {signals}, lệnh {commands}, khớp {filled},"
        " hết hạn {expired}, chưa rõ {unconfirmed}, bị từ chối {refused}; chuỗi nhật ký hợp lệ:"
        " {chain}."),
    "runner.uncertain_startup": (
        "В журнале моста есть отданные команды без подтверждённой сделки: {ids}. Проверьте"
        " вручную или добавьте --acknowledge-uncertain.",
        "The bridge journal lists served commands without a confirmed deal: {ids}. Reconcile"
        " manually or add --acknowledge-uncertain.",
        "Nhật ký cầu nối có lệnh đã giao mà chưa xác nhận khớp: {ids}. Hãy đối soát thủ công"
        " hoặc thêm --acknowledge-uncertain."),
    "runner.acknowledged": (
        "Оператор принял неопределённые команды к ручной сверке: {ids}.",
        "The operator accepted the uncertain commands for manual reconciliation: {ids}.",
        "Người vận hành đã nhận các lệnh chưa rõ để đối soát thủ công: {ids}."),
    "runner.disclaimer": (
        "Демо-исполнение через EA: ордера уходят только на демо-счёт. Реальные деньги не"
        " используются; прошлые результаты не гарантируют прибыль.",
        "Demo execution through the EA: orders go to a demo account only. No real money is"
        " used; past results do not guarantee profit.",
        "Thực thi demo qua EA: lệnh chỉ gửi tới tài khoản demo. Không dùng tiền thật; kết quả"
        " quá khứ không bảo đảm lợi nhuận."),
    # Host setup: terminal configuration for the loopback demo bridge.
    "setup.not_windows": (
        "Этот скрипт настраивает терминал MetaTrader 5 на Windows.",
        "This script configures a MetaTrader 5 terminal on Windows.",
        "Tập lệnh này cấu hình terminal MetaTrader 5 trên Windows."),
    "setup.terminal_running": (
        "Терминал MetaTrader 5 запущен ({processes}). Закройте его и повторите.",
        "MetaTrader 5 is running ({processes}). Close it and retry.",
        "MetaTrader 5 đang chạy ({processes}). Hãy đóng nó rồi thử lại."),
    "setup.ini_missing": (
        "Файл конфигурации терминала не найден: {path}.",
        "Terminal configuration file not found: {path}.",
        "Không tìm thấy tệp cấu hình terminal: {path}."),
    "setup.ini_encoding": (
        "Файл {path} не в UTF-16 с BOM; скрипт не перезаписывает его вслепую.",
        "{path} is not UTF-16 with a BOM; the script will not rewrite it blindly.",
        "{path} không phải UTF-16 có BOM; tập lệnh sẽ không ghi đè một cách mù quáng."),
    "setup.ini_section": (
        "В common.ini должна быть ровно одна секция [Experts], найдено: {count}.",
        "common.ini must contain exactly one [Experts] section; found {count}.",
        "common.ini phải có đúng một mục [Experts]; tìm thấy {count}."),
    "setup.ini_key_missing": (
        "В секции [Experts] нет ключа {name}.",
        "The [Experts] section has no {name} key.",
        "Mục [Experts] không có khóa {name}."),
    "setup.ini_duplicate_key": (
        "Ключ {name} встречается в [Experts] более одного раза.",
        "The {name} key appears more than once in [Experts].",
        "Khóa {name} xuất hiện nhiều lần trong [Experts]."),
    "setup.url_not_loopback": (
        "Адрес моста должен быть http://127.0.0.1:<порт> или http://localhost:<порт>;"
        " получено {url}.",
        "The bridge URL must be http://127.0.0.1:<port> or http://localhost:<port>; got {url}.",
        "URL bridge phải là http://127.0.0.1:<cổng> hoặc http://localhost:<cổng>; nhận được {url}."),
    "setup.ea_missing": (
        "Нет собранного демо-EA: {path}. Запустите скрипт без --skip-build.",
        "No demo EA binary at {path}. Run the script without --skip-build.",
        "Không có EA demo tại {path}. Chạy lại không dùng --skip-build."),
    "setup.ea_built": (
        "EA собран: {errors} ошибок, {warnings} предупреждений; установлен в {experts}.",
        "EA compiled: {errors} errors, {warnings} warnings; installed into {experts}.",
        "EA đã biên dịch: {errors} lỗi, {warnings} cảnh báo; đã cài vào {experts}."),
    "setup.ea_present": (
        "Собранный EA уже на месте: {path}.",
        "The compiled EA is already in place: {path}.",
        "EA đã biên dịch đã có sẵn: {path}."),
    "setup.ini_unchanged": (
        "common.ini уже разрешает автоторговлю и WebRequest для {url}; файл не изменён.",
        "common.ini already allows automated trading and WebRequest for {url}; nothing"
        " changed.",
        "common.ini đã cho phép giao dịch tự động và WebRequest cho {url}; không thay đổi."),
    "setup.ini_changed": (
        "common.ini обновлён; резервная копия: {backup}.",
        "common.ini updated; backup: {backup}.",
        "Đã cập nhật common.ini; bản sao lưu: {backup}."),
    "setup.verify_failed": (
        "Запись common.ini не прошла проверку: {path}.",
        "common.ini did not verify after writing: {path}.",
        "common.ini không khớp sau khi ghi: {path}."),
    "setup.dry_run": (
        "Пробный запуск: файлы не изменены. Добавьте --apply.",
        "Dry run: no files changed. Add --apply.",
        "Chạy thử: chưa thay đổi tệp nào. Thêm --apply."),
    "setup.ready": (
        "Терминал готов: демо-счёт {login}, EA {ea}, мост {url}.",
        "Terminal ready: demo account {login}, EA {ea}, bridge {url}.",
        "Terminal đã sẵn sàng: tài khoản demo {login}, EA {ea}, bridge {url}."),
    "setup.next": (
        "Дальше: запустите демо-раннер (он поднимет мост), включите AutoTrading в терминале и"
        " прикрепите EA к графику; BridgeToken берите из MT5_BRIDGE_TOKEN.",
        "Next: start the demo runner (it raises the bridge), enable AutoTrading in the"
        " terminal and attach the EA to a chart; use MT5_BRIDGE_TOKEN as BridgeToken.",
        "Tiếp theo: chạy demo runner (nó mở bridge), bật AutoTrading trong terminal và gắn EA"
        " vào biểu đồ; dùng MT5_BRIDGE_TOKEN làm BridgeToken."),
    "setup.data_dir_missing": (
        "Не найден каталог данных терминала: {path}. Укажите --data-dir.",
        "Terminal data directory not found: {path}. Pass --data-dir.",
        "Không tìm thấy thư mục dữ liệu terminal: {path}. Hãy truyền --data-dir."),
    "setup.data_dir_ambiguous": (
        "Найдено несколько каталогов данных терминала ({count}); укажите --data-dir.",
        "Several terminal data directories found ({count}); pass --data-dir.",
        "Tìm thấy nhiều thư mục dữ liệu terminal ({count}); hãy truyền --data-dir."),
    "setup.process_check_failed": (
        "Не удалось проверить запущенные процессы ({error}); состояние неизвестно, скрипт"
        " остановлен.",
        "Could not check running processes ({error}); the state is unknown, so the script"
        " stopped.",
        "Không kiểm tra được tiến trình đang chạy ({error}); trạng thái không rõ nên tập lệnh"
        " dừng."),
}

_INDEX = {language: position for position, language in enumerate(LANGUAGES)}


def describe(code: str, language: str) -> str:
    """Human label for a status or blocker code; the raw code when there is no label."""
    key = str(code)
    entry = CODE_LABELS.get(key)
    position = _INDEX[normalize_language(language)]
    if entry is not None:
        return entry[position]
    blackout = _BLACKOUT.match(key)
    if blackout is not None:
        return text("code.blackout_window", language, index=blackout.group(1))
    return key


def describe_many(codes: Any, language: str) -> str:
    """One line of localized labels, for the verbose cycle trace and the operator prompt."""
    return ", ".join(describe(code, language) for code in codes) or "-"


def text(key: str, language: str, **params: Any) -> str:
    """Localized message formatted with ``params``. An unknown key is a bug, not a fallback."""
    entry = TEXT.get(key)
    if entry is None:
        raise KeyError(f"unknown message key: {key}")
    template = entry[_INDEX[normalize_language(language)]]
    return template.format(**params)


def placeholders(rendered: str) -> frozenset[str]:
    """Placeholder names in one already-selected string."""
    return frozenset(field for _, field, _, _ in string.Formatter().parse(rendered) if field)


def configure_console_output(*streams: Any, errors: str = "replace") -> None:
    """Make localized output survivable on a legacy Windows code page.

    A piped cp1252 stream raises ``UnicodeEncodeError`` on the first Cyrillic or
    Vietnamese character, which would kill ``--help`` and every report. UTF-8 keeps the
    text readable in a file or a ``chcp 65001`` console; ``replace`` keeps an exotic
    stream from aborting a run that is already trading. ``errors="surrogateescape"`` on
    stdin is what lets an operator type "да" on a cp1251 console without a decode crash.
    """
    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors=errors)
        except (OSError, ValueError):
            continue


__all__ = ["CODE_LABELS", "DEFAULT_LANGUAGE", "LANGUAGES", "LANGUAGE_NAMES", "TEXT",
           "UnsupportedLanguage", "configure_console_output", "describe", "describe_many",
           "normalize_language", "placeholders", "text"]
