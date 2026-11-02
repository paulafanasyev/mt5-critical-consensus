# Agent MT5: vòng lặp observe/paper bên trên terminal

Tài liệu này mô tả tầng **agent MetaTrader 5**, nằm cạnh pipeline
«thăm dò → đồng thuận phản biện → cấp phép» (xem [architecture.md](architecture.md)). Agent
đọc trạng thái terminal, áp dụng các cổng rủi ro fail-closed và ghi mọi quyết định vào nhật ký.
Agent **không gửi lệnh**: trong package `critical_consensus.mt5_agent` không có bất kỳ lời gọi
hàm giao dịch nào, và điều này được kiểm bằng test chứ không phải bằng lời hứa.

Gửi lệnh chỉ tồn tại bên ngoài package. Đường demo (`scripts/mt5_demo_bridge.py`,
`scripts/run_mt5_demo_trading.py`, `scripts/build_demo_ea.py`, `scripts/setup_demo_terminal.py` cùng
một expert advisor đã biên dịch) được mô tả trong mục «Thực thi demo qua cầu nối loopback». Nó chạy
**chỉ với tài khoản demo** và vẫn không đưa hệ thống lại gần tiền thật hơn chút nào.

Ngôn ngữ khác: [English](MT5_AGENT.en.md) · [Русский](MT5_AGENT.md).

## Trạng thái bằng chứng (đọc trước khi dùng)

| Tuyên bố | Trạng thái | Bằng chứng |
|---|---|---|
| Chính sách chỉ nhận khoá đã biết, chế độ live bị từ chối | **ĐÃ XÁC MINH** | `test_live_mode_and_unknown_keys_are_rejected` |
| Một vòng lặp đi qua các cổng theo thứ tự cố định và bị chặn khi có bất kỳ lỗi nào | **ĐÃ XÁC MINH** | 62 test trong `tests/test_mt5_agent.py`, 59 trong `tests/test_mt5_agent_invariants.py`, 37 trong `tests/test_mt5_agent_i18n.py` |
| Mọi lý do chặn mà agent thực sự phát ra đều nằm trong danh sách mã và có nhãn ở cả ba ngôn ngữ | **ĐÃ XÁC MINH** | `test_every_emitted_blocker_is_a_declared_code`, `test_every_journaled_code_has_a_non_empty_label_in_all_three_languages` |
| Paper broker không nhìn thấy nến tương lai; trong cùng một nến, stop được khớp trước take-profit | **ĐÃ XÁC MINH** | `test_paper_broker_hides_future_bars`, `test_paper_broker_fills_stop_before_target_in_the_same_bar` |
| Khối lượng tính từ khoảng cách stop và đặc tả symbol, bị giới hạn bởi cả rủi ro **và** margin | **ĐÃ XÁC MINH** | `test_sizing_never_exceeds_the_risk_budget`, `test_sizing_caps_volume_by_margin_and_not_only_by_risk` |
| Sửa một bản ghi nhật ký bị phát hiện; phần đầu bị xoá chỉ bị phát hiện nhờ một mốc neo giữ ngoài tệp | **ĐÃ XÁC MINH** | `test_journal_detects_tampering`, `test_journal_anchor_detects_a_deleted_run_prefix`, `test_the_run_id_is_part_of_the_signed_body` |
| Không bản ghi nhật ký nào báo đã gửi lệnh cho broker | **ĐÃ XÁC MINH** | `test_agent_never_reports_a_broker_order`, `test_package_source_contains_no_order_sending_surface` |
| Chế độ `confirm` chỉ mở vị thế khi người vận hành trả lời "có" rõ ràng, còn `auto` từ chối chính sách dùng tài khoản không phải demo | **ĐÃ XÁC MINH** | `test_confirm_mode_without_an_approver_never_opens_a_position`, `test_automatic_execution_requires_a_demo_account_even_in_the_library` |
| `LiveMT5Backend` ánh xạ đúng phản hồi terminal và trừ offset giờ của server | **ĐÃ XÁC MINH bằng package giả lập** | `test_live_backend_*`, `test_cli_live_source_subtracts_the_server_offset` |
| Đọc terminal MT5 thật trên máy này | **ĐÃ XÁC MINH** | 2026-10-31: `MetaTrader5` 5.0.6231, `terminal64.exe` build 6296, tài khoản demo 10013404855 trên `MetaQuotes-Demo` (số dư 10.00 USD, đòn bẩy 100), các đặc tả thật và 120 nến H1 cho EURUSD/GBPUSD/AUDUSD/NZDUSD/XAUUSD/XAGUSD; offset server 7200 s |
| EA demo biên dịch và cài được vào terminal | **ĐÃ XÁC MINH** | 2026-10-31: `scripts/build_demo_ea.py` lật đúng một dòng cổng trong một bản sao được sinh ra, MetaEditor báo 0 lỗi và 0 cảnh báo, và `MQL5/Experts/CriticalConsensusEA_Demo.ex5` (37780 byte) đã được cài |
| Cấu hình terminal cho phép giao dịch thuật toán và WebRequest loopback | **ĐÃ XÁC MINH, nhưng không durable** | 2026-10-31: `Config/common.ini` mang `Enabled=1`, `WebRequest=1`, `WebRequestUrl=http://127.0.0.1:8000`, vẫn là UTF-16 LE kèm BOM và CRLF, đã xác minh theo byte và có một bản sao lưu nằm cạnh. Việc một terminal sống ghi đè tệp này không phải là giả thuyết: ngày 2026-11-01 lúc 00:21, MT5 đã đặt `Enabled=0` và xóa trắng `WebRequestUrl` sau khi `setup_demo_terminal.py --apply` áp các khóa lúc 00:11. Trạng thái hiện tại lại được kiểm ngày 2026-11-01: `Enabled=1`, `WebRequest=1`, `WebRequestUrl=http://127.0.0.1:8000;http://127.0.0.1;http://localhost:8000;http://localhost`. Sự cho phép cần được giữ trong một terminal đang chạy, chứ không phải một lần ghi vào tệp |
| EA thực sự thăm dò cầu nối (một `WebRequest` thật từ một biểu đồ) | **ĐÃ XÁC MINH** | 2026-11-01 13:52–13:55 UTC, tài khoản demo 114929502 trên `MetaQuotes-Demo`, `CriticalConsensusEA_Demo` nằm trên biểu đồ XAUUSD,H1. Phép kiểm được mô tả trong mục «Cách kiểm tra transport khi thị trường đóng cửa»: việc bật công tắc ngừng qua cầu nối đã tạo trong `MQL5/Logs/20261101.log` (UTF-16) các dòng `BLOCKED: kill switch is active or state is invalid.` đúng theo nhịp poll 3 s, và việc gỡ công tắc đã dừng chúng lại — nghĩa là advisor đã nhận HTTP 200 trên `/v1/risk/kill-switch` và 204 trên `/v1/execution/next` với cùng `BridgeToken` đã được nhập vào các trường của biểu đồ. Trước khi nhập token, chính tệp log đó cho thấy các dòng `BLOCKED: kill-switch state unavailable; HTTP 401` |
| Tên công cụ đến từ broker, không được giả định | **ĐÃ XÁC MINH** | `resolve_broker_symbol` + `LiveMT5Backend.symbols()`/`use_broker_names` trong `src/critical_consensus/mt5_agent/backend.py` và `scripts/run_mt5_demo_trading.py`; các test `tests/test_broker_symbol_alias.py` (11 test). `XAUUSDm`/`XAUUSDc`/`xauusd` được phân giải về đúng cách viết của chính server, nhiều kết quả khớp là một sự từ chối chứ không phải một phép đoán |
| Số học cổng của advisor và runner trùng nhau | **ĐÃ XÁC MINH** | `scripts/ea_logic_mirror.py` — bản phản ánh từng dòng của `CriticalConsensusEA.mq5` sang Python, `tests/test_ea_logic_parity.py` — 109 phép đối chiếu (các trường bắt buộc, ngữ pháp số và định danh, token, URL loopback, tombstone, khối lượng, lưới 200 giá SL/TP so với `stops_level`). Chính test này đã tìm ra một lệch làm tròn thật trong `price_gates` và nó đã được sửa |
| Một lệnh demo được broker thực thi | **CHƯA XÁC MINH** | vận chuyển chạy, nhưng không có hàng đợi nào: ngày 2026-11-01 thị trường đóng từ 23:59 Thứ Sáu và chính sách chặn Chủ nhật, nên mọi vòng lặp cho ra `OUTSIDE_TRADING_WINDOW` với `broker_order_sent: false` trong tất cả các bản ghi (1386 bản ghi tính đến 14:27:34 UTC, chuỗi hợp lệ; con số là ảnh chụp — một lượt chạy đang sống thêm 12 bản ghi mỗi phút). Chu kỳ đầu tiên có khả năng dựng một lệnh là Thứ Hai 2026-11-02 00:00 UTC |
| Lợi nhuận của chiến lược và tỉ lệ lệnh thắng | **CHƯA XÁC MINH** | backtest trên chuỗi tổng hợp; `confidence` là điểm heuristic, không phải xác suất đã hiệu chuẩn. Tỉ lệ thắng 98% không được đo ở đây và mã này không thể đạt được. Các lượt chạy đã đo trên nến thật thì lỗ ([DEMO_RESULTS.md](DEMO_RESULTS.md)) |
| Thực thi trên tài khoản thật | **CHƯA XÁC MINH và không được triển khai** | package Python và CLI không chứa lời gọi gửi lệnh nào, theo thiết kế; đường demo từ chối mọi thứ không phải tài khoản demo (`AllowLiveExecution=false`, `require_demo`, bước kiểm `ACCOUNT_TRADE_MODE`), và một chính sách có `mode: live` bị từ chối khi tải |

Bản xem trước cục bộ dùng khi viết tài liệu này phát lại chuỗi EURUSD H1 **tổng hợp** (random walk
có trend, có seed); nó không được commit như một artifact, và các test tạo cùng chuỗi đó bằng helper
`make_bars()` của riêng chúng. Các test chạy nhật ký và các cổng trên chuỗi đó; mọi con số chúng in
ra chỉ minh hoạ cơ chế, **không phải** ước tính lợi nhuận. Không có gì ở đây quyết định khi nào được
dùng tiền thật: đó là một quyết định riêng, rõ ràng, nằm ngoài mã này, và package hiện tại không thể
thực thi quyết định đó theo cả hai chiều.

Tình trạng kiểm tra tính đến ngày 2026-11-01: `.venv\Scripts\python.exe -m pytest -q` — **971 test
đạt, 10 test bỏ qua** (các
bước bỏ qua cần `TEST_DATABASE_URL` và/hoặc `TEST_REDIS_URL`; interpreter từ `.venv` ở đây là bắt
buộc chứ không phải lịch sự: một phần suite spawn `sys.executable` như một tiến trình con, nên một
`python` hệ thống không có package đã cài sẽ báo 36 lỗi `ModuleNotFoundError` thay vì một kết quả);
`python scripts/check_mql5_safety.py`
— **473 bất biến tĩnh của advisor và 105 phép làm suy yếu bị từ chối**. Bước kiểm tĩnh không biên
dịch mã nguồn advisor: MetaEditor là thứ xác nhận việc biên dịch, còn việc thực thi trên tài khoản
thì chỉ broker mới xác nhận được.

## Thành phần package

| Module | Vai trò |
|---|---|
| `mt5_agent/backend.py` | truy cập terminal không gửi lệnh: `AccountInfo`, `SymbolSpec`, `Tick`, `PositionSnapshot`, protocol `MT5Backend`, adapter `LiveMT5Backend` |
| `mt5_agent/policy.py` | `AgentPolicy`/`RiskLimits`/`StrategyConfig`/`TradingWindow`, tải YAML, từ chối khoá lạ, `execution_mode`, `ui_language` |
| `mt5_agent/strategy.py` | tín hiệu tất định SMA/ATR + momentum, hình học stop/take, các lý do chặn |
| `mt5_agent/sizing.py` | khối lượng từ `cash_per_price_unit` (kích thước hợp đồng, nếu không thì cặp tick) và `volume_step` trong ngân sách rủi ro và margin khả dụng |
| `mt5_agent/paper.py` | `PaperBroker`: phát lại theo nến, không nhìn trước, khớp theo bid/ask, gap, mốc equity theo ngày |
| `mt5_agent/journal.py` | một chuỗi băm SQLite (`audit_events`, điểm equity nằm trong đó), `verify_chain`, `report` |
| `mt5_agent/agent.py` | `MT5Agent.run_cycle()` — thứ tự cổng, cổng xác nhận, ghi quyết định |
| `mt5_agent/i18n.py` | nhãn tiếng Nga/Anh/Việt cho mọi mã và mọi thông báo cho người vận hành |
| `mt5_agent/codes.py` | danh sách mã trạng thái và lý do chặn duy nhất |
| `scripts/run_mt5_agent.py` | CLI: `run` (csv/live, auto/confirm, ru/en/vi), `report`, `reset --confirm` |
| `config/mt5_agent.example.yaml` | ví dụ chính sách có chú thích |

Mọi thứ có thể đặt một lệnh ra thế giới đều nằm tách khỏi package đó, và chỉ dành cho demo:

| Thành phần | Vai trò |
|---|---|
| `scripts/mt5_demo_bridge.py` | cầu nối HTTP chỉ-loopback: hai GET (`/v1/risk/kill-switch`, `/v1/execution/next`) và đúng một POST — chỉ trên đường dẫn kill-switch, kiểm tra `X-Bridge-Key`, tệp `kill_switch.json` bền mà cả runner lẫn advisor cùng đọc, một lệnh đang chờ, TTL, các tombstone idempotency đã fsync, không bao giờ ghi log token hay nội dung lệnh |
| `scripts/run_mt5_demo_trading.py` | thành phần duy nhất có thể dựng một lệnh: lấy quyết định của agent, từ chối khi công tắc ngừng đang bật, kiểm lại mọi cổng, xếp hàng nó trong cầu nối và đối chiếu với chính lịch giao dịch của broker; không bao giờ thử lại một lần gửi không chắc chắn |
| `scripts/build_demo_ea.py` | sinh ra `CriticalConsensusEA_Demo.mq5` bằng cách lật đúng một dòng cổng compile-time, tùy chọn biên dịch nó bằng MetaEditor và cài `.ex5` |
| `scripts/setup_demo_terminal.py` | mặc định là dry run: từ chối khi terminal đang chạy, vá `[Experts]` trong `Config/common.ini`, cài EA đã build; không quyết định điều gì về một lệnh |
| `scripts/operator_answers.py` | phân tích câu trả lời cả dòng (`да`/`vâng`/`y`) dùng chung cho CLI và runner demo |
| `mql5/CriticalConsensusEA.mq5` | mã nguồn advisor; trong repository này cổng luôn là `false`, và bộ kiểm tĩnh cố định giá trị mặc định đó |

## Thứ tự một vòng lặp

Mỗi cổng gặp vấn đề kết thúc vòng lặp với `BLOCKED`; thị trường dùng được mà không có tín hiệu là
`NO_SIGNAL`, còn chế độ observe là `OBSERVED`. Nhật ký chỉ lưu mã, không bao giờ lưu văn bản đã dịch.

1. `CLOCK_NOT_UTC_AWARE` — đồng hồ của agent phải trả thời gian có timezone.
2. `KILL_SWITCH_ACTIVE` — công tắc ngừng lệnh đang bật chặn trước mọi lần đọc thị trường. Section
   `kill_switch` bị thiếu sẽ được tải ở trạng thái bật với lý do `POLICY_NOT_PROVIDED`; một công
   tắc đang bật với lý do rỗng là lỗi tải.
3. Tài khoản: `ACCOUNT_READ_FAILED`, rồi `ACCOUNT_NOT_DEMO`, `ACCOUNT_LOGIN_MISMATCH`,
   `ACCOUNT_SERVER_MISMATCH`, `ACCOUNT_EQUITY_INVALID`, `ACCOUNT_MARGIN_INVALID`,
   `ACCOUNT_LEVERAGE_INVALID`.
4. Mốc equity: `EQUITY_BASELINES_UNAVAILABLE` (thiếu, không hữu hạn, giá trị đầu ngày hoặc đỉnh
   ≤ 0, đỉnh thấp hơn equity hiện tại/đầu ngày, hoặc mốc lớn hơn equity hiện tại quá 1000 lần),
   sau đó `DAILY_LOSS_LIMIT_REACHED`, `REALIZED_DAILY_LOSS_LIMIT_REACHED`,
   `DRAWDOWN_LIMIT_REACHED` (ngưỡng tính cả khi bằng). Backend phải cung cấp
   `realized_daily_pnl`; thiếu thuộc tính, getter báo lỗi, hoặc giá trị `None`/chuỗi/bool/không
   hữu hạn là dữ liệu không dùng được cho cổng ngày: chu kỳ bị chặn với
   `EQUITY_BASELINES_UNAVAILABLE` thay vì coi ngày chưa biết là không lỗ.
5. Lịch: `OUTSIDE_TRADING_WINDOW`, `BLACKOUT_WINDOW_<n>`.
6. Vị thế: `POSITION_CAP_COUNT_UNKNOWN` khi terminal không trả lời, `POSITION_READ_FAILED`,
   `POSITION_CAP_REACHED`/`POSITION_CAP_EXCEEDED`, `SYMBOL_ALREADY_OPEN`.
7. Thị trường: `MARKET_DATA_READ_FAILED`, `BAR_SYMBOL_MISMATCH`, `QUOTE_FROM_THE_FUTURE`, `QUOTE_STALE`.
   Đặc tả, tick và nến mới nhất đều phải mang đúng mã trong chính sách; backend bỏ qua tham số
   mã và trả lời cho một công cụ khác sẽ bị chặn bằng `BAR_SYMBOL_MISMATCH` trước khi tính khối
   lượng hoặc chạm sổ paper.
8. Sổ đang mở: `MARKET_DATA_READ_FAILED` (không đọc được đặc tả của một vị thế trên mã khác),
   `POSITION_STOP_UNSET` (vị thế mở có stop không đo được), `LOSS_FLOOR_BREACHED` (rủi ro của các
   stop đang đặt cộng lỗ đã chốt trong ngày đã dùng hết ngân sách ngày, hoặc khoản lỗ mà các stop
   bảo vệ sẽ chạm hoặc vượt equity tài khoản).
9. Tín hiệu: các chặn của chiến lược (`INSUFFICIENT_BARS`, `NO_TREND_SIGNAL`, `SPREAD_ABOVE_LIMIT`,
   `PRIOR_CLOSE_NON_POSITIVE`, `ATR_NOT_MEANINGFUL`, `STOP_DISTANCE_INVALID`,
   `TAKE_PROFIT_PRICE_INVALID`, `STOP_INSIDE_BROKER_STOPS_LEVEL`,
   `TARGET_INSIDE_BROKER_STOPS_LEVEL`, `BUY_GEOMETRY_INVALID`, `SELL_GEOMETRY_INVALID`,
   `BAR_SERIES_INVALID`, `BAR_SYMBOL_MISMATCH`, `BAR_TIMEFRAME_MISMATCH`, `BAR_TIME_IN_FUTURE`,
   `BAR_TIME_TOO_OLD`) →
   `NO_SIGNAL`, hoặc `BLOCKED` khi chính đầu vào bị hỏng. `BAR_TIME_TOO_OLD` kích hoạt khi nến
   đóng mới nhất trễ hơn báo giá quá hai chu kỳ, tức là thiếu mất cả một nến.
   `BAR_TIMEFRAME_MISMATCH` nghĩa là backend trả khung thời gian khác với chính sách.
10. `RISK_REWARD_BELOW_MINIMUM`, rồi tính khối lượng: `MINIMUM_LOT_EXCEEDS_RISK_BUDGET` hoặc
    `MARGIN_MINIMUM_LOT_UNFUNDED`.
11. `AGGREGATE_STOP_RISK_LIMIT_REACHED` — lỗ đã chốt trong ngày cộng rủi ro của các stop đang đặt
    cộng rủi ro của vị thế mới sẽ vượt ngân sách ngày.
12. Chế độ: `observe` → `OBSERVED`. Ở `paper` với `execution_mode: confirm`, người vận hành được
    hỏi: `CONFIRMATION_UNAVAILABLE` (chưa nối approver), `OPERATOR_DECLINED`,
    `OPERATOR_CONFIRMATION_FAILED`.
13. Báo giá lại sau khi xác nhận: trước khi khớp, báo giá được đọc lại (`MARKET_DATA_READ_FAILED`,
    `BAR_SYMBOL_MISMATCH`, `QUOTE_FROM_THE_FUTURE`, `QUOTE_STALE`, `SPREAD_ABOVE_LIMIT`). Giá khớp
    chỉ được lệch tối đa 10% khoảng cách stop đã duyệt so với mức vào đã duyệt
    (`PRICE_MOVED_DURING_CONFIRMATION`); tỉ lệ lời/rủi ro được tính lại tại giá khớp
    (`RISK_REWARD_BELOW_MINIMUM`) và khối lượng được tính lại theo stop tại giá khớp, không bao giờ
    vượt lot đã duyệt (`MINIMUM_LOT_EXCEEDS_RISK_BUDGET`, `MARGIN_MINIMUM_LOT_UNFUNDED`,
    `AGGREGATE_STOP_RISK_LIMIT_REACHED`). Chỉ báo giá mới và trong biên cho phép tới được
    `PAPER_OPENED`; sổ mô phỏng từ chối lot là `PAPER_OPEN_REJECTED`, còn backend không có sổ mô
    phỏng là `PAPER_BROKER_REQUIRED_FOR_PAPER_MODE`.

Mỗi bản ghi mà agent ghi đều mang `"broker_order_sent": false` và `"execution_mode"`; bản ghi vòng
lặp còn nêu `"execution_policy": "NO_BROKER_ORDER_API"` và việc điểm equity của nó có được ghi hay
không (`"equity_recorded"`, `"equity_record_error"`). Đối tượng kết quả có thêm `"cycle_recorded"`:
một nhật ký từ chối dòng của vòng lặp cho kết quả `false` thay vì làm hỏng lượt chạy.

## Chế độ thực thi: tự động hay hỏi trước

`execution_mode` là công tắc mà người vận hành yêu cầu:

| Giá trị | Hành vi | Ràng buộc |
|---|---|---|
| `confirm` (mặc định) | mỗi vị thế mới được hiển thị cho người vận hành, và chỉ câu "có" rõ ràng mới mở | cần approver; không có thì vòng lặp bị chặn, không bao giờ mở |
| `auto` | agent tự mở vị thế | bị từ chối ngay khi tải chính sách trừ khi `account.require_demo: true`, nên không thể điều khiển tài khoản thật |

Trong CLI, `confirm` hỏi bằng ngôn ngữ đã chọn và nhận `y`/`n`/`a`/`q` (cùng các từ bản địa đầy
đủ, ví dụ `да`/`нет`/`все`/`стоп`, `vâng`/`không`/`tất cả`/`dừng`). Chỉ khớp cả dòng: `y` mở lệnh
này, `n` bỏ qua, `a` thôi hỏi cho phần còn lại của lượt chạy, `q` kết thúc lượt chạy nhưng báo cáo
vẫn được ghi. Một mảnh như `в` (tiếng Nga) hay `tất` (tiếng Việt) không phải câu trả lời, nên không
bao giờ bị hiểu nhầm thành "đồng ý tất cả". Không có bàn phím thì mọi đề xuất bị từ chối vì chưa xác
nhận, và lượt chạy vẫn tiếp tục để nhật ký không bị thiếu. `--execution auto|confirm` ghi đè chính
sách cho một lượt chạy và được kiểm lại, nên quy tắc "chỉ demo" vẫn đúng trong cả hai trường hợp.
Vị thế được mô phỏng cục bộ ở cả hai chế độ; không có lệnh broker nào trong cả hai.

## Thực thi demo qua cầu nối loopback

Chuỗi liên kết đi theo một đường duy nhất và thứ tự của nó là cố định: package `mt5_agent` quyết
định và tính khối lượng (nó không thể gửi), `run_mt5_demo_trading.py` kiểm lại quyết định đó và xếp
đúng một lệnh vào `mt5_demo_bridge.py`, advisor thăm dò cầu nối qua `WebRequest` trên `127.0.0.1`
và gọi `OrderSend` trên một tài khoản demo, còn runner đối chiếu kết quả với chính lịch giao dịch
của broker theo magic number `8172026` và chú thích `CC:<id>`.

Các cổng phải đồng thời thỏa trước khi một lệnh tồn tại:

1. Tài khoản terminal phải là demo (`ACCOUNT_TRADE_MODE`), chính sách phải yêu cầu điều đó
   (`account.require_demo: true`), và `expected_login`/`expected_server` phải khớp.
2. Chế độ chính sách phải là `paper` (hoặc `observe`), và `auto` là bất khả thi nếu không có yêu
   cầu demo.
3. Báo giá được đọc lại hai lần. Trước khi hỏi người vận hành, điểm vào, hình học stop/take,
   `stops_level` của broker, khối lượng và margin khả dụng được kiểm trên dữ liệu tươi mới. Sau khi
   có trả lời, `MT5Agent.post_approval_gates` đọc lại terminal và áp dụng toàn bộ bộ cổng mà một
   lệnh khớp paper nhận được — công tắc ngừng, danh tính tài khoản, mốc equity và lỗ trong
   ngày, cửa sổ giao dịch và ngày bị chặn, giới hạn vị thế và sổ đang mở, tuổi tick, spread, ngưỡng trượt giá so với điểm
   vào đã phê duyệt, risk/reward tại giá khớp, lot được tính lại, margin và stops level — và lệnh
   được dựng từ giá cùng lot mà bộ cổng trả về, không phải từ báo giá trước xác nhận.
4. Khối lượng phải nằm trong `volume_min`/`volume_step`/`volume_max` của broker, dưới `--max-volume`
   (tối đa 0.20) và trong margin khả dụng; tham số `MaxVolume` của chính advisor cũng là 0.20.
5. Cầu nối giữ một lệnh đang chờ, đòi `X-Bridge-Key` (24–256 ký tự), áp TTL 5–600 s, và không bao
   giờ phục vụ cùng một `command_id` hai lần. `client_order_id` tất định, nên khởi động lại runner
   không tạo ra lệnh thứ hai.
6. Ở chế độ `confirm` cần cả một dòng trả lời của người vận hành (`да`/`vâng`/`y`); một mảnh không
   phải câu trả lời.
7. Advisor độc lập kiểm lại tất cả điều trên, cộng công tắc ngừng lệnh qua một GET mới, TTL, cổng
   compile-time `ENABLE_DEMO_EXECUTION` của nó, `AllowLiveExecution`, symbol, khối lượng và các
   stop, và nó ghi các tombstone idempotency trước khi gửi. Nó cũng kiểm các giới hạn của chính mình
   ngay từ lúc khởi động và từ chối khởi tạo khi `PollSeconds` nằm ngoài 1–60, `MaxVolume` nằm ngoài
   (0; 0.20], `MaxSlippagePoints` nằm ngoài 1–20, `MaxCommandTtlSeconds` nằm ngoài 1–600 hoặc
   `MagicNumber` không phải số magic: mọi giá trị đó đến từ các trường của biểu đồ, và một advisor
   chấp nhận `MaxVolume = 100` sẽ thực thi một khối lượng mà không ai thương lượng từ bất kỳ phía
   nào.
8. Một kết quả không chắc chắn (lệnh đã được phục vụ, không thấy khớp) dừng lượt chạy và cần
   `--acknowledge-uncertain` rõ ràng; không có đường mã nào gửi lại.

### Cùng một số học cho advisor và runner

Khoảng lệch giữa «Python kiểm tra gì» và «MQL5 kiểm tra gì» rẻ đóng lại bằng một phép tính hơn là
bằng sự chú ý. `scripts/ea_logic_mirror.py` là bản phản ánh từng dòng của advisor sang Python: cùng
các `JsonString`/`JsonNumber`/`JsonTrue`/`JsonFalse` đặt trên đúng bộ phân tích một đối tượng JSON
phẳng đó, cùng ngữ pháp cho số và cho định danh, cùng FNV-1a (nó đi theo từng byte của UTF-16LE, nên
`"a"` cho `089BE207B544F1E4` chứ không đi theo ASCII), cùng các khoá tombstone, cùng bộ phân tích URL
loopback và cùng hàng đợi kiểm tra của `ExecuteDemoCommand` — theo đúng thứ tự đó và với đúng các văn
bản từ chối đó. Các test trong `tests/test_ea_logic_parity.py` (109 phép đối chiếu) so bản phản ánh
với cầu nối đang sống (thân thật của lệnh arm/disarm lấy trực tiếp từ socket), với `json.loads` kèm
từ chối nghiêm ngặt `NaN`/`Infinity`, với các biểu thức chính quy của cầu nối, với chính văn bản
`.mq5` (các trường bắt buộc, giá trị mặc định, giới hạn và số magic được đối chiếu bằng ba cách), và
với một lưới 200 giá mà điểm vào và stop được làm tròn theo các hướng khác nhau so với `stops_level`.

Lưới đầu tiên đó đã tìm ra một lỗi thật nằm phía chúng tôi. MT5 gửi cho broker những con số đã được
làm tròn tới `digits`, còn `price_gates` lại so với `stops_level` những con số chưa làm tròn; tại giá
4000.005, với một stop 1.02 point và mức stops là 2 point, Python chặn lệnh còn advisor thì sẽ chấp
nhận nó. Một cổng đo một con số mà broker không bao giờ nhận thì không phải là một cổng. Giờ
`broker_level()` làm tròn điểm vào, stop và take-profit đúng như `NormalizeDouble` (làm tròn nửa ra
xa giá trị không), và mọi phép kiểm hình học, trượt giá cũng như `stops_level` đều đi theo các mức đã
làm tròn; test parity đòi tập hợp các lần từ chối phải trùng nhau trên toàn bộ lưới.

### Công tắc ngừng khẩn cấp trên đường demo

Công tắc ngừng là một tệp, không phải một biến: `kill_switch.json` nằm cạnh các tombstone. Một bản
ghi lưu thay đổi cuối cùng của công tắc (`kill_switch_active`, `kill_switch_reason`, `actor_id`,
`updated_at`, `event_hash`), được viết lại toàn bộ qua một tệp tạm cộng `os.replace` và fsync. Advisor
đọc bản ghi đó trong mỗi lần poll, còn runner đọc nó hai lần cho mỗi offer — trước mọi lần đọc thị
trường và ngay trước khi dựng một lệnh. Cả hai đều đọc lại tệp chứ không giữ một bản sao trong bộ
nhớ, nên người vận hành bật hoặc tắt công tắc ngay trong lúc một lượt chạy đang sống, và lần đọc kế
tiếp đã là câu trả lời mới. Vì vậy `scripts/activate_kill_switch.ps1` đã được tài liệu hóa cũng dừng
được đường này, và một lần bật trước khi khởi động lại vẫn còn hiệu lực sau khi khởi động lại.

- Bật chỉ cần khóa của cầu nối: `POST /v1/risk/kill-switch` với thân
  `{"active": true, "reason": …}` và header `X-Bridge-Key`. Câu trả lời mang `status: "PERSISTED"`,
  `updated_at` và `event_hash` — đúng những gì đoạn PowerShell đó kiểm tra; ngoài ra nó còn đòi một
  hash 64 ký tự và rồi tự thực hiện đúng phép GET mà advisor thực hiện, nên một lần bật mà trạng thái
  được poll không đồng tình sẽ bị báo là thất bại. Một yêu cầu mà thân không
  đọc được thành đối tượng JSON, có `active` không phải boolean hoặc có các trường ghi chú không phải
  chuỗi bị từ chối với 400 và không thay đổi gì; thân dài hơn 4096 byte được rút hết ra khỏi socket
  (tối đa 1 MiB) rồi bị từ chối với 413, để khách hàng nhận được phán quyết thay vì một kết nối bị
  đứt. Một khách hàng im lặng giữa chừng yêu cầu bị ngắt sau 10 giây, thay vì giữ một luồng xử lý.
- Tắt cần bí mật thứ hai: `MT5_KILL_SWITCH_ADMIN_TOKEN` dài ít nhất 32 ký tự, khác `MT5_BRIDGE_TOKEN`,
  gửi trong `X-Kill-Switch-Admin`. Thiếu admin token hoặc trùng với khóa cầu nối là 503, sai là 403;
  cả hai trường hợp công tắc vẫn bật. Khi không có runner nào đang sống,
  `python scripts/run_mt5_demo_trading.py --policy <policy.yaml> --db <journal.db>
  --release-kill-switch` đọc đúng tệp đó, lấy admin token từ môi trường và ghi trạng thái tắt;
  `--policy` và `--db` bắt buộc với mọi lần gọi, kể cả lần này.
- Không có gì mặc định là an toàn. Một trạng thái không đọc được không bao giờ được coi là đã tắt: nếu
  không có bản ghi nào, câu trả lời là `{"kill_switch_active": true, "kill_switch_reason":
  "NOT_SEEDED"}`; `kill_switch.json` hỏng hoặc không đọc được khiến runner từ chối ngay trước khi bắt
  đầu và khiến GET của advisor trả 503; một bản ghi từng tồn tại rồi biến mất — tức là tệp bị xóa —
  cũng là 503 chứ không phải một khởi đầu sạch. Vì vậy xóa tệp trạng thái không thể tắt công tắc.
- Lượt chạy đầu tiên không tìm thấy bản ghi nào sẽ tạo bản ghi đó ở trạng thái **bật**
  (`kill_switch_reason: "SEEDED_ARMED"`), chứ không lấy từ section `kill_switch` của chính sách. Một
  chính sách mô tả cách giao dịch, không phải là quyền được giao dịch; sự im lặng trong tệp cấu hình
  không thể gỡ một lệnh dừng. Một bản ghi do người vận hành để lại qua HTTP, hay do một lượt chạy bị
  dừng, không bao giờ bị ghi đè. Trong bộ nhớ chỉ giữ một điều duy nhất: một lần bật không ghi xuống
  đĩa được thì vẫn tiếp tục chặn (câu trả lời nói `MEMORY_ONLY`), còn một lần tắt không ghi được để
  công tắc ở trạng thái bật.
- Mọi kết quả dừng một lượt chạy — "stop" ở bước xác nhận, một lần gửi không xác nhận, một id lệnh bị
  trùng — bật đúng bản ghi bền đó, nên kết thúc tiến trình không kết thúc lệnh dừng. Một Ctrl-C tới
  sau khi lệnh đã được đưa vào cầu nối không được coi riêng là một kết thúc sạch: lượt chạy đánh dấu
  lệnh là chưa xác nhận, bật đúng tệp đó và thoát với mã `4` (`interrupted_with_send_in_flight`), vì
  chỉ lịch giao dịch của broker mới biết lệnh có thực sự đi hay không.

### Cách kiểm tra transport khi thị trường đóng cửa

Transport được kiểm mà không cần một lệnh nào, và đó chính xác là cách phải làm, thay vì «chờ tới
Thứ Hai». Advisor gọi cầu nối trong mỗi nhịp timer, nên trạng thái công tắc ngừng hiện ra ngay trong
log của nó:

1. Khởi động runner và chắc chắn rằng `terminal64.exe` đang chạy, advisor đang gắn trên một biểu đồ,
   và nút AutoTrading đang bật.
2. Chụp một bản log: `MQL5/Logs/<YYYYMMDD>.log` là UTF-16, đọc bằng
   `iconv -f UTF-16LE -t UTF-8`.
3. Bật công tắc ngừng qua cầu nối: `POST /v1/risk/kill-switch` với `X-Bridge-Key` và thân
   `{"active": true, "reason": "…"}`; câu trả lời bắt buộc là 200 kèm `status: "PERSISTED"`,
   `updated_at` và `event_hash`.
4. Chờ không ít hơn một chu kỳ poll. Dấu hiệu của sự lành mạnh là các dòng `BLOCKED: kill switch is
   active or state is invalid.` cách đều nhau; chúng nghĩa là advisor đã nhận và phân tích được câu
   trả lời của cầu nối, tức `BridgeToken` trong các trường của biểu đồ trùng với khóa của cầu nối.
5. Gỡ công tắc (`--release-kill-switch`, xem ở trên) và chắc chắn các dòng đó dừng lại, đồng thời
   không có dòng `Bridge returned HTTP` hay `fail closed` nào cả: im lặng sau khi gỡ nghĩa là một 200
   trên đường dẫn của công tắc và một 204 trên đường dẫn lệnh, tức cả hai tuyến đều được xác thực.

Ba kết luận sai ở đây đã một lần làm mất thời gian: **một log rỗng không có nghĩa là advisor đã
dừng** (một EA lành mạnh với công tắc đã gỡ và hàng đợi rỗng thì không viết gì cả); **MT5 ghi log của
nó với một độ trễ**, nên «không có dòng mới» phải được kiểm lại sau khoảng ba mươi giây thay vì tuyên
bố ngay; và `BLOCKED: kill-switch state unavailable; HTTP 401` không phải là thiếu thị trường cũng
chẳng phải thiếu lệnh, mà đúng một điều: khóa đã không khớp.

### Tên công cụ đến từ broker

`MetaQuotes-Demo` không phải chuẩn chính tả của tên công cụ, và runner không còn giả vờ rằng nó là
chuẩn đó. Tên được yêu cầu được đối chiếu với những gì server thực sự trả về: khớp chính xác thì thắng;
nếu không, phần cơ sở sáu ký tự (`XAUUSD`, `EURUSD`) được thử với các hậu tố dài một tới ba ký tự —
broker nào có dạng gì là câu hỏi cho server của họ, không phải cho mã. Cùng một yêu cầu mà khớp với
nhiều cách viết được phân giải thành một sự từ chối (`MT5 offers several variants of …; refusing to
guess which one this account trades`), chứ không phải dòng đầu tiên của danh sách: `XAUUSD` và
`XAUUSDc` là hai công cụ khác nhau, và đoán nghĩa là giao dịch bằng lot cent thay vì lot thường. Từ đó
trở đi cả lượt chạy dùng cách viết của server, sự kiện đổi tên được ghi vào nhật ký
(`demo_symbol_alias`), và một tên khoá nội bộ không cho hai cách viết chạm nhau trong cùng một bảng.
Nhờ vậy `EURUSDm` của nhóm giao dịch Exness, `XAUUSDc` của tài khoản cent và dạng chữ thường `xauusd`
không còn là «symbol không khả dụng».

### Quan sát ngày 2026-11-01 (bốn công cụ, terminal thật, thị trường đóng cửa)

Tài khoản demo `114929502` trên `MetaQuotes-Demo`, số dư và equity 100 000 USD, đòn bẩy 100,
`trade_allowed`. Chính sách `agent_data/live_gold_ema_policy.yaml`: `mode: paper`,
`execution_mode: auto`, cửa sổ 00:00–22:00 UTC, `blocked_weekdays_utc: [6]`. Lượt chạy: 4 công cụ ×
một chu kỳ mỗi 20 s, và nhật ký của tiến trình đó dừng ở **1386 bản ghi `agent_cycle` lúc
14:27:34 UTC**, mọi bản ghi đều mang lý do chặn `OUTSIDE_TRADING_WINDOW` và
`broker_order_sent: false`, chuỗi hợp lệ. Con số này là một ảnh chụp chứ không phải bất biến: một
lượt chạy đang sống ghi 12 bản ghi mỗi phút (4 công cụ × 3 chu kỳ), và vào lúc chỉnh sửa tài liệu
này, cầu nối đã thuộc về một runner khác với cùng `run_id` — vì định danh lượt chạy, tệp công tắc
ngừng và lịch sử các id đã phục vụ đều suy ra từ đường dẫn tombstone, chứ không từ số tiến trình.
`BTCUSD` vắng mặt trong
danh sách này là có chủ ý: broker không cung cấp nó (`MetaQuotes-Demo` không giao dịch tiền mã hóa),
và runner ghi sự không khả dụng thành một dòng riêng, chứ không âm thầm loại công cụ.

Trong nhật ký đó không có lấy một blocker `KILL_SWITCH_ACTIVE`, và lý do không phải là một công tắc
chết: trên đường demo, stop có hai vật mang khác nhau. Cổng số 2 của thứ tự chu kỳ đọc section
`kill_switch` của **chính sách** (`agent_data/live_gold_ema_policy.yaml` khai `active: false`), còn
tệp `kill_switch.json` bền vững được `DemoExecutor.offer()` kiểm tra — nơi duy nhất một lệnh được
dựng ra. Khi thị trường đóng cửa, chu kỳ chết ở cổng cửa sổ và không bao giờ tới `offer()`, nên công
tắc bật từ 13:52 đến 14:27:59 UTC không để lại dấu vết nào trong nhật ký quyết định; nó để lại dấu
vết trong log của advisor, và chính vì vậy mà transport được kiểm qua log EA chứ không qua bộ đếm
blocker. Cũng chính tệp log đó cho thấy sự chặn đến từ cửa sổ chứ không từ transport hỏng: trước
13:52 UTC các dòng là `HTTP 401`, sau khi nhập token là `kill switch is active`, và sau
khi gỡ là im lặng.

### Quan sát trực tiếp ngày 2026-10-31 (`--symbols all`, terminal thật)

13 công cụ đã được yêu cầu; broker cung cấp 7. Kết quả của một vòng lặp: **0 lệnh được xếp hàng,
0 lệnh được khớp, `broker_order_sent: false` trong mọi bản ghi, chuỗi nhật ký hợp lệ**.

| Công cụ | Kết quả | Vì sao đúng |
|---|---|---|
| EURUSD, GBPUSD, AUDUSD, NZDUSD, XAUUSD, XAGUSD | `BLOCKED` → `QUOTE_STALE` | tick cuối cùng 2026-10-30T21:00:00Z; thị trường đóng cửa suốt cuối tuần |
| US500 | `BLOCKED` → `MARKET_DATA_READ_FAILED` | `MT5 returned no tick for US500` — đặc tả đọc được, không có báo giá nào |
| USDJPY, USDCAD, USDCHF, EURGBP | bỏ qua trước vòng lặp | `pays profit in JPY/CAD/CHF/GBP, not in the account currency USD` — tính khối lượng theo tiền tệ tài khoản sẽ sai |
| NAS100, BTCUSD | bỏ qua trước vòng lặp | `MT5 could not select symbol …` — broker này không cung cấp |

Không thể mặc cả để bỏ qua tuổi báo giá: `max_quote_age_seconds` chỉ được chấp nhận trong khoảng từ
1 tới 300, nên một chuỗi ngày Thứ Bảy luôn cho `QUOTE_STALE`. Đó là chủ ý.

Lý do tài khoản này vẫn sẽ không thực thi ngay cả khi thị trường mở là phép tính trên các đặc tả
thật và các nến H1 thật (một phép tính, không phải một sự từ chối đã đo): ngân sách rủi ro
`0.005 × min(equity, balance) = 0.05 USD` ở số dư 10.00 USD, trong khi rủi ro của lot tối thiểu theo
broker là 3.27 USD trên EURUSD, 3.35 trên GBPUSD, 1.93 trên AUDUSD, 1.98 trên NZDUSD, 3.97 trên
XAUUSD và 4.24 trên XAGUSD. Mọi symbol đều dừng ở `MINIMUM_LOT_EXCEEDS_RISK_BUDGET`. Một tài khoản
demo 10 USD không chứa nổi một lệnh đơn lẻ đúng chính sách; ở 10 000 USD thì cùng 0.5% cho ngân sách
50 USD và những con số đó lọt qua. Từ 2026-11-01 lượt chạy đi trên tài khoản 114929502 với số dư
100 000 USD — cũng chính vì vậy mà câu hỏi «vì sao không có gì được thực thi» không còn là một câu
hỏi về tính khối lượng nữa: nó chỉ còn là thị trường đóng cửa và một ngày trong tuần bị chặn.

### Cách chạy nó

Các bí mật chỉ được lấy từ môi trường. Không tồn tại `--bridge-token`, cũng không có admin token trên
dòng tham số: dòng tham số do những người dùng khác của cùng máy nhìn thấy và xuất hiện trong một ảnh
chụp `Win32_Process`, còn tệp trạng thái của cầu nối lẫn nhật ký đều không ghi khóa hay thân câu trả
lời.

```powershell
# 1. terminal phải ở trạng thái ĐÓNG: MT5 ghi lại common.ini khi nó thoát
python scripts/setup_demo_terminal.py                       # dry run: điều gì sẽ thay đổi
python scripts/setup_demo_terminal.py --apply               # build, cài, cho phép WebRequest loopback
# 2. khởi động terminal, đăng nhập tài khoản demo, gắn CriticalConsensusEA_Demo vào một biểu đồ bằng tay
# 3. bật nút AutoTrading và giữ cho cầu nối mở
$env:MT5_BRIDGE_TOKEN = "<24-256 ký tự>"
$env:MT5_KILL_SWITCH_ADMIN_TOKEN = "<không ngắn hơn 32 ký tự, khác khóa cầu nối>"
python scripts/run_mt5_demo_trading.py --policy agent_data/live_demo_policy.yaml `
  --db agent_data/mt5_demo.db --symbols all --cycles 0 --execution confirm --verbose
```

`--cycles 0` nghĩa là «cứ chạy cho tới khi tiến trình bị dừng». Nếu advisor không được gắn, các
lệnh đơn giản hết hạn dưới TTL — nhật ký báo chúng một cách trung thực là `expired`, và không lệnh
nào tới được broker. Nút AutoTrading và `[Experts] Enabled=1` là hai công tắc khác nhau: nút GUI tắt
mọi thứ, nên phải chạy lại `setup_demo_terminal.py` sau bất kỳ lần bật/tắt nào thực hiện bên trong
terminal; phép kiểm transport ở mục trên chính là thứ phân biệt «không có thị trường» với «không có
sự cho phép».

Đường dẫn trạng thái và định danh lượt chạy không phụ thuộc vào nơi nó được khởi động. `--tombstones`
được phân giải so với gốc repository, chứ không phải thư mục hiện tại, nên một lệnh `cd` không thể di
dời được tệp công tắc ngừng lẫn lịch sử các id đã phục vụ; định danh lượt chạy mặc định được suy ra
từ đường dẫn đó cộng với tệp chính sách, chứ không phải một giá trị ngẫu nhiên. Điều này quan trọng vì
`client_order_id` được dựng từ nó: một id ngẫu nhiên cho mỗi tiến trình sẽ biến mọi lần khởi động lại
thành «lệnh đầu tiên trong đời», và lớp chống trùng tín hiệu sẽ chẳng còn bảo vệ được gì.

Mã thoát của runner: `0` — lượt chạy kết thúc theo `--cycles` hoặc bị người vận hành dừng, và không có
lệnh nào còn chưa được xác nhận; `2` — cấu hình bị từ chối trước khi ra tới thị trường (chính sách,
ràng buộc tài khoản, admin token thiếu hoặc quá ngắn khi dùng `--release-kill-switch`); `3` — lượt
chạy không thể bắt đầu một cách trung thực: không công cụ nào được broker cho phép, cầu nối không
mở được socket trên `127.0.0.1`, lịch sử giao dịch của terminal không đọc được khi đã có lệnh được
phục vụ, hoặc trong tombstone vẫn còn những lệnh đã phục vụ mà chưa xác nhận được khớp,
điều đòi `--acknowledge-uncertain` rõ ràng; `4` — Ctrl-C tới sau khi lệnh đã được đưa vào cầu nối
(`interrupted_with_send_in_flight`): đúng tệp công tắc ngừng bền đó đang bật, và chỉ được chạm lại id
ấy sau khi đối chiếu với các giao dịch của broker. Một công tắc ngừng đang bật không phải là một mã
thoát và cũng không phải một tạm dừng: lượt chạy in lý do một lần lúc khởi động và tiếp tục đếm chu
kỳ, vì stop bền vững nằm trong `offer()`, còn `offer()` chỉ chạy khi tín hiệu đã tồn tại (hai vật
mang của stop được mô tả ở phần Quan sát 2026-11-01).

Gỡ công tắc ngừng khi không có runner nào đang sống chỉ được thực hiện qua một bí mật riêng:

```powershell
python scripts/run_mt5_demo_trading.py --policy agent_data/live_demo_policy.yaml `
  --db agent_data/mt5_demo.db --release-kill-switch
```

## Ngôn ngữ

Toàn bộ văn bản dành cho người vận hành — trạng thái, lý do chặn, câu hỏi, bản tóm tắt, lỗi và
tuyên bố miễn trừ — có bằng **tiếng Nga**, **tiếng Anh** và **tiếng Việt**. Chọn ngôn ngữ theo thứ tự:

```bash
MT5_AGENT_LANGUAGE=vi python scripts/run_mt5_agent.py --language en run ...   # cờ thắng biến môi trường
```

rồi tới `ui_language` trong chính sách, rồi `ru`. Cờ được chấp nhận cả trước lẫn sau lệnh con, và
nếu lặp lại thì lần xuất hiện cuối cùng thắng, nhưng mọi lần xuất hiện đều phải là ngôn ngữ được hỗ trợ.
`report` dùng ngôn ngữ mà `--run-id` của chính nó đã dùng (ngôn ngữ của lượt chạy mới nhất khi
không có id); `reset` dùng ngôn ngữ của lượt chạy mới nhất, kể cả khi thiếu `--confirm` và lệnh bị
từ chối. `--help` in dòng cách dùng, tiêu đề các mục và dòng trợ giúp đã dịch; nó được dựng trước
khi đọc chính sách nên chỉ theo cờ/biến môi trường/ngôn ngữ mặc định. Mã máy trong nhật ký và trong đầu ra
JSON không đổi theo ngôn ngữ (`blocker_histogram` vẫn theo mã), còn `blocker_labels` hiển thị đúng
các khoá đó bằng ngôn ngữ đã chọn. JSON tóm tắt của `run` còn có thêm `exit_reason_histogram` (các mã như
`STOP_LOSS`, `TAKE_PROFIT_GAP`, `END_OF_RUN`) và `exit_reason_labels`; hai khoá này không có trong
JSON của `report`. `run` và `report` ghi đúng
một đối tượng JSON ra stdout; `reset` ghi một câu đã dịch; câu hỏi, vết chạy chi tiết, bản tóm tắt
và tuyên bố miễn trừ đi ra stderr. Lỗi đối số in tiền tố cách dùng và dòng `Lỗi:` đã dịch, nhưng
chi tiết của argparse vẫn bằng tiếng Anh; `ui_language` bị từ chối trong chính sách được báo bằng
ngôn ngữ đã chọn, còn văn bản lỗi kiểm tra chính sách khác cùng lý do lỗi terminal, CSV và backend
vẫn bằng tiếng Anh.

## Tín hiệu và khối lượng

- `gap = (SMA_fast − SMA_slow) / ATR`; BUY cần `gap ≥ min_sma_gap_atr` **và** momentum >
  `min_momentum_abs`; SELL đối xứng. Ngược lại là `NO_TREND_SIGNAL`.
- Stop: `entry ∓ ATR · stop_atr_multiple`; take-profit: `entry ± rủi ro · take_profit_risk_multiple`.
  Cả hai mức phải tôn trọng `stops_level` của broker.
- `confidence = 0.5 + 0.5·strength`, bị chặn bởi `max_confidence` và gắn nhãn
  `heuristic_score_not_calibrated_probability`. Đây là điểm xếp hạng tình huống, không phải xác suất.
- Khối lượng: `risk_per_lot = stop_distance · cash_per_price_unit`, trong đó `cash_per_price_unit`
  là giá trị một bước giá của một lot tính theo tiền tệ tài khoản: `trade_contract_size` khi
  `currency_profit` trùng tiền tệ tài khoản, ngược lại là `trade_tick_value / trade_tick_size`.
  Chọn bậc `volume_step` lớn nhất sao cho lỗ khi chạm stop vẫn nằm trong
  `min(equity, balance) · risk_per_trade_fraction` và margin cần thiết —
  `khối lượng × cash_per_price_unit × giá vào / đòn bẩy` — vừa với margin khả dụng. Nếu ngay cả lot
  tối thiểu đã vượt ngân sách thì từ chối — stop không bao giờ bị nới rộng. Chỉ dùng cặp tick sẽ
  đánh giá thấp kim loại: trên MetaQuotes-Demo nó cho XAUUSD 10 $ mỗi bước giá mỗi lot, trong khi
  hợp đồng, `order_calc_profit` và margin thực sự bị phong toả đều nói 100 $; lệnh tính theo tick
  vượt ngân sách gấp mười lần (`tests/test_mt5_contract_sizing.py`).

## Paper broker

`PaperBroker` cài đúng interface không gửi lệnh của terminal, nên agent không phân biệt nguồn dữ liệu:

- `seek(moment)` di chuyển cửa sổ nến nhìn thấy; `bars()` và `tick()` không bao giờ trả dữ liệu sau
  con trỏ — look-ahead không thể xảy ra về mặt vật lý.
- Đồng hồ mô phỏng đọc **giá đóng** của nến nhìn thấy cuối cùng, không phải giá mở: OHLC của một nến
  chỉ biết được khi nến đóng, và gắn báo giá vào lúc mở sẽ coi nó là cũ tới một nến. Exit vẫn chỉ
  được xử lý trên các nến sau nến mà vị thế được mở.
- Trong một nến, stop được kiểm trước take-profit: nến chạm cả hai mức tính là lỗ. Gap vượt qua
  một mức được khớp tại giá mở nến (`STOP_LOSS_GAP`/`TAKE_PROFIT_GAP`).
- BUY vào theo ask và ra theo bid, SELL ngược lại; spread lấy từ `spread_points` của từng nến, dùng
  `SymbolSpec.spread_points` khi nến không khai báo hoặc báo spread không dương.
- Mốc ngày (`day_start_equity`, `peak_equity`, `realized_daily_pnl` tính theo ngày server) điều khiển
  các hạn mức ngày; nếu backend không cung cấp, vòng lặp bị chặn `EQUITY_BASELINES_UNAVAILABLE`.

## Nhật ký quyết định

Một bảng SQLite duy nhất, `audit_events`, là toàn bộ audit trail: các sự kiện `agent_cycle`,
`paper_trade_closed` và `equity_point`, nối bằng `make_audit_event`; mỗi sự kiện được gắn `run_id`
nằm trong phần được băm, và chính nó là thứ `--run-id` dùng để giới hạn báo cáo. Equity cũng nằm
trong chuỗi như mọi thứ khác, nên một dòng sụt giảm bị sửa là một băm hỏng, không phải một con số.
Nếu lượt chạy đóng ít nhất một lệnh, đóng sổ ghi thêm một điểm equity cuối cùng (best-effort: việc
ghi thành công hay lỗi được đưa vào bản tóm tắt lượt chạy qua
`closing_equity_recorded`/`closing_equity_error`, và lượt chạy không bị hỏng). `report()` trả về số
vòng, biểu đồ lý do chặn, mức sụt giảm, `broker_order_sent: false` và `verified_chain`;
`initial_equity` có thể được truyền tường minh (lệnh `run` của CLI làm vậy), nếu không thì dùng
điểm được ghi đầu tiên. `verify_chain()` báo lỗi khi nội dung bị sửa và khi một dòng bị xoá ở giữa
chuỗi: trường hợp đầu là `EVENT_CONTENT_HASH_MISMATCH`, trường hợp sau là `PREVIOUS_HASH_MISMATCH`
vì dòng kế tiếp trỏ tới một băm đã biến mất. Phần **đầu** bị xoá chỉ bị bắt nhờ một mốc neo giữ
ngoài tệp: `report()` tự neo vào `previous_hash` của sự kiện đầu tiên còn lại, nên muốn kiểm tra
trung thực cần `--verify-anchor <hash>` từ bên ngoài. Khi đó báo cáo vẫn được ghi ra stdout dưới
dạng một đối tượng JSON duy nhất, mã thoát là 3, và lời giải thích đã dịch đi ra stderr. Chỉ mốc neo
bên ngoài mới làm thay đổi mã thoát: khi `verified_chain` tự neo không hợp lệ, `report` vẫn thoát
với mã 0, in JSON với `verified_chain.valid: false` và ghi cảnh báo đã dịch (`out.chain_invalid`) ra
stderr. Trường `anchor_check` trong báo cáo của CLI mang kết quả kiểm tra bên ngoài, còn `verified_chain`
luôn kiểm tra lượt chạy từ chính dòng đầu của nó. Một mốc neo đưa vào chuỗi rỗng bị từ chối
(`PREVIOUS_HASH_MISMATCH`), nên xoá toàn bộ dòng chỉ bị phát hiện khi có mốc neo đã lưu được cung
cấp; nếu không có mốc neo, nhật ký rỗng vẫn báo chuỗi nhất quán nội bộ với `events: 0`. Chỉ chuỗi
không rỗng mới là mốc neo: `--verify-anchor ""` được coi như không có mốc neo.
`verified_chain.anchor_source` nêu nguồn: `provided`
cho mốc neo bên ngoài và `first_event_previous_hash` cho tự neo. Hãy giữ `anchor_hash` (chính là
`previous_hash` của dòng đầu) làm mốc neo bên ngoài; `last_hash` là đỉnh chuỗi và cho
`PREVIOUS_HASH_MISMATCH` khi dùng làm mốc neo. Dòng đầu tiên của nhật ký mới hoàn toàn không có
`anchor_hash` (`null`), nên không có gì phía trước để neo. Mốc neo thuộc về một lượt chạy: hãy
truyền cùng `--run-id` cho lệnh `report` về sau; nếu thiếu, một nhật ký có các dòng sớm hơn thuộc
lượt chạy khác sẽ bắt đầu từ dòng sớm hơn và trả `PREVIOUS_HASH_MISMATCH` (mã 3).

## Cách chạy

```bash
cp config/mt5_agent.example.yaml config/mt5_agent.yaml   # rồi sửa theo tài khoản của bạn
# Bản mẫu cố ý giữ kill_switch.active: true. Hãy tắt nó (false) kèm lý do của bạn trước lần
# chạy đầu, nếu không mọi vòng lặp bị chặn với KILL_SWITCH_ACTIVE.
python scripts/run_mt5_agent.py --db agent_data/mt5_agent.db run \
  --policy config/mt5_agent.yaml --source csv --csv export_eurusd_h1.csv \
  --source-timezone Europe/London --balance 10000 --language vi
python scripts/run_mt5_agent.py --db agent_data/mt5_agent.db report --run-id <id> --verify-anchor <hash>
python scripts/run_mt5_agent.py --db agent_data/mt5_agent.db reset --confirm
```

Lệnh không thể hoàn tác cần `--confirm` rõ ràng: `run --fresh` (xoá nhật ký trước khi chạy) và
`reset` (xoá nhật ký). Thiếu nó, CLI thoát với mã 2 mà không tạo và không sửa nhật ký. `reset` vẫn
có thể mở nhật ký đã có ở chế độ chỉ đọc để chọn ngôn ngữ cho lời từ chối của nó; không dòng nào bị
tạo, sửa hay xoá. `run` thường không có `--fresh`, và `report`, chạy được mà không cần.
`--source live` đọc lịch sử của terminal đã cài qua `LiveMT5Backend` (cần terminal đang chạy hoặc
`terminal_path`, cùng package `MetaTrader5`) và trừ `server_utc_offset_seconds` bắt buộc. Nó không
gọi lệnh giao dịch nào. `symbol_info()` chỉ đọc, nhưng khi symbol chưa có trong Market Watch,
`symbol_spec()` dùng `symbol_select()` — cách đăng ký được tài liệu hoá — và việc này thật sự thêm
symbol vào danh sách symbol của terminal. Nó từ chối terminal đăng nhập vào tài khoản thật khi
`account.require_demo: true`
(mặc định), và từ chối khi login hoặc server không khớp lúc `account.expected_login` /
`account.expected_server` được cố định. Agent kiểm tra cố định theo danh tính mà backend trả về: với
`--source live` đó là login và server của terminal, nên giá trị cố định phải khớp; lượt chạy paper
trên CSV dùng danh tính paper cố định (login `1000000000`, server `PAPER-LOCAL`). Bất kỳ giá trị cố
định nào khác danh tính paper đó đều chặn cả các vòng lặp CSV, vì vậy hãy để trống khi dùng CSV.
Login và mật khẩu không bao giờ được truyền qua CLI và không vào nhật ký.

## Chính sách: giá trị mặc định

Fail-closed: `mode: paper`, `execution_mode: confirm`, `ui_language: ru`, `require_demo: true`,
`max_open_positions: 1`, `risk_per_trade_fraction: 0.005`, `max_daily_loss_fraction: 0.02`,
`max_drawdown_fraction: 0.06`, `max_quote_age_seconds: 15`, cửa sổ `00:00–22:00 UTC`, chặn Chủ nhật,
`kill_switch.active: true` khi thiếu section, và thiếu `server_utc_offset_seconds` là lỗi tải chứ
không phải mặc định UTC ngầm. Khoá lạ trong chính sách hoặc trong bất kỳ section nào là lỗi tải.
`strategy.take_profit_risk_multiple` thấp hơn `risk.min_risk_reward` bị từ chối như một chính sách
bất khả thi. `symbol_spec` trong chính sách cung cấp đặc tả symbol cho việc mô phỏng và tính khối
lượng ở mọi nguồn; `--source live` cung cấp nến và danh tính tài khoản, không cung cấp đặc tả
symbol. Vòng chạy demo-execution (`scripts/run_mt5_demo_trading.py`) đi xa hơn: nó bỏ luôn
`symbol_spec` đã khai cho mọi công cụ và chỉ tính khối lượng theo dữ liệu terminal báo về, vì một
khai báo chỉ là bản ghi của một phép đo tại một broker, còn terminal mới là phép đo.

## Cách kiểm tra trên Windows VPS của bạn

1. Cài terminal MT5 và package `pip install MetaTrader5` (chỉ Windows x64).
2. Đăng nhập **tài khoản demo** trong terminal và giữ terminal chạy.
3. Cố định `account.expected_login` và `account.expected_server` trong chính sách. Với
   `--source live` chúng phải khớp terminal; lượt chạy paper trên CSV dùng danh tính `PAPER-LOCAL`
   cố định, nên hãy để trống ở đó (nếu không mọi vòng lặp bị chặn vì không khớp).
4. Tắt công tắc ngừng lệnh một cách có chủ đích (`kill_switch.active: false` cùng lý do riêng):
   bản mẫu giữ nó bật, và công tắc bật sẽ chặn mọi vòng lặp.
5. Đặt `server_utc_offset_seconds` đúng offset thật của server broker (số phút chẵn, ±14 giờ).
6. Bắt đầu với `mode: observe` — vòng lặp ghi quyết định nhưng không mở gì.
7. Sau đó `mode: paper` với `--source live` hoặc file CSV.
8. Xem `report`: `verified_chain.valid` phải là `true` và `broker_order_sent` phải là `false`.
9. Số dư demo quá nhỏ thì không bao giờ chạm tới bước thực thi: ở 10 USD với
   `risk_per_trade_fraction: 0.005` ngân sách rủi ro là 0.05 USD, trong khi lot tối thiểu của bất kỳ
   symbol nào ở trên đều rủi ro 1.93–4.24 USD (xem «Thực thi demo qua cầu nối loopback»), nên vòng
   lặp dừng ở `MINIMUM_LOT_EXCEEDS_RISK_BUDGET`. Hãy mở một tài khoản demo 10 000 USD hoặc nâng tỉ
   lệ rủi ro một cách có ý thức và kiểm lại ngân sách. Trên tài khoản đang được dùng hiện nay
   (114929502, 100 000 USD), cổng này được đi qua với một biên dư, và lệnh chặn chỉ còn đến từ thị
   trường đóng cửa và ngày trong tuần bị chặn.
10. Chỉ khi đó mới đáng chuẩn bị terminal cho việc thực thi demo:
    `python scripts/setup_demo_terminal.py` (dry run), rồi `--apply` khi terminal đã đóng, rồi gắn
    `CriticalConsensusEA_Demo` vào một biểu đồ bằng tay và bật AutoTrading.

## Cố ý không mô hình hoá

- Swap, hoa hồng và chi phí qua đêm; trượt giá trong nến (lệnh mô phỏng khớp theo báo giá sau khi
  duyệt, bị giới hạn ở 10% khoảng cách stop); khớp một phần và requote vượt biên đó; trôi
  `tick_value` khi đồng tiền định giá biến động; broker đổi DST giữa lượt chạy (offset là hằng số
  bạn khai báo). Các mốc trong ngày được tính trong phạm vi tiến trình `PaperBroker` và không sống
  qua một lần khởi động lại của lượt chạy paper; còn mốc rủi ro của demo-runner là ngoại lệ, cả theo
  thiết kế lẫn theo test: `demo_risk_baseline` được ghi vào cùng nhật ký append-only và được khôi phục
  theo ràng buộc `(login, server, ngày của broker)`, nên một lần khởi động lại tiến trình không xoá
  sạch mốc đầu ngày và không kéo đỉnh equity lùi lại
  (`test_risk_baselines_survive_runner_restart_and_are_scoped_to_the_broker_account`).
- Các công cụ trả lợi nhuận bằng tiền tệ khác tiền tệ tài khoản bị loại khỏi phiên live: trên tài
  khoản USD, `SymbolSpec.from_terminal` từ chối USDJPY, USDCAD, USDCHF và EURGBP, và vòng
  all-universe đang chạy ghi nhật ký chúng là `demo_symbol_unavailable` — nghĩa là chỉ 7 trong 13
  công cụ đi tới được phiên giao dịch (EURUSD, GBPUSD, AUDUSD, NZDUSD, XAUUSD, XAGUSD, US500, mỗi
  mã có vòng riêng trong nhật ký; terminal này không cung cấp BTCUSD và NAS100). Lời từ chối không
  phải là thái quá: kích thước lot của một cặp cross là `contract_size` quy đổi qua tỷ giá của tiền tệ
  yết giá, nên nó thay đổi theo cặp. Đo trên MetaQuotes-Demo ngày 2026-11-02: cặp tick cho USDJPY
  633,60 $ mỗi bước giá, còn `order_calc_profit` trên một tick cho 0,63 $ (630,00 mỗi bước giá),
  USDCAD 71.567,62 so với 72.000 và USDCHF 121.228,29 so với 121.000 — với cặp cross, các trường
  tick của broker thật sự nói bằng tiền tệ tài khoản. Phần còn thiếu là vế margin: với cặp cross,
  `khối lượng × cash_per_price_unit × giá / đòn bẩy` cho ra đơn vị tiền tệ cơ sở chứ không phải
  tiền tệ tài khoản, nên cần cách tính riêng theo đồng tiền cơ sở trước khi bỏ lời từ chối.
- Package Python không có `order_send` hay bất kỳ đường ghi giao dịch nào; bước quét mã nguồn
  `test_package_source_contains_no_order_sending_surface` chỉ bao phủ package `mt5_agent`, còn các
  test subprocess riêng của CLI kiểm tra JSON của nó không bao giờ báo đã gửi lệnh. Template MQL5
  **có** lời gọi `OrderSend`, nhưng nó nằm sau hằng số compile-time `ENABLE_DEMO_EXECUTION = false`
  (và sau tham số `AllowLiveExecution` buộc phải là false), nên không thể tới được nếu không sửa
  mã nguồn và biên dịch lại; agent Python không bao giờ gọi template đó.
- Đường demo trong `scripts/` không phải ngoại lệ của quy tắc đó: cả `run_mt5_demo_trading.py` lẫn
  `mt5_demo_bridge.py` đều không chứa một lời gọi giao dịch MT5 nào (grep cho
  `order_send|trade_|OrderSend` không tìm thấy gì). Python chỉ giữ một lệnh HTTP sống tới hết TTL
  của nó; advisor bên trong terminal mới là thứ có thể biến nó thành một lệnh, và đó là lý do
  "Python đã gửi một lệnh" là bất khả thi trong hệ thống này, kể cả với demo.
- Không mô hình hoá và không đo: khớp một phần, requote sau `Done`, trượt giá so với điểm vào đã
  duyệt, và mất kết nối giữa advisor và cầu nối. Tất cả những gì có cho các trường hợp đó là các
  tombstone, bản ghi đúng-ID của cầu nối và một bước đối chiếu thủ công với lịch giao dịch.
- Không có `mode: live`: chính sách từ chối ngay khi tải.
- Agent không phải một phần của cơ chế cấp phép: `pipeline.evaluate_signal_bundle`, Certainty Gate
  và phê duyệt con người vẫn là những con đường duy nhất (và vẫn đang đóng) tới một lệnh thật.
