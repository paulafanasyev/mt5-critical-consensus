# Agent MT5: vòng lặp observe/paper bên trên terminal

Tài liệu này mô tả tầng **agent MetaTrader 5**, nằm cạnh pipeline
«thăm dò → đồng thuận phản biện → cấp phép» (xem [architecture.md](architecture.md)). Agent
đọc trạng thái terminal, áp dụng các cổng rủi ro fail-closed và ghi mọi quyết định vào nhật ký.
Agent **không gửi lệnh**: trong package `critical_consensus.mt5_agent` không có bất kỳ lời gọi
hàm giao dịch nào, và điều này được kiểm bằng test chứ không phải bằng lời hứa.

Ngôn ngữ khác: [English](MT5_AGENT.en.md) · [Русский](MT5_AGENT.md).

## Trạng thái bằng chứng (đọc trước khi dùng)

| Tuyên bố | Trạng thái | Bằng chứng |
|---|---|---|
| Chính sách chỉ nhận khoá đã biết, chế độ live bị từ chối | **ĐÃ XÁC MINH** | `test_live_mode_and_unknown_keys_are_rejected` |
| Một vòng lặp đi qua các cổng theo thứ tự cố định và bị chặn khi có bất kỳ lỗi nào | **ĐÃ XÁC MINH** | 62 test trong `tests/test_mt5_agent.py`, 59 trong `tests/test_mt5_agent_invariants.py`, 32 trong `tests/test_mt5_agent_i18n.py` |
| Mọi lý do chặn mà agent thực sự phát ra đều nằm trong danh sách mã và có nhãn ở cả ba ngôn ngữ | **ĐÃ XÁC MINH** | `test_every_emitted_blocker_is_a_declared_code`, `test_every_journaled_code_has_a_non_empty_label_in_all_three_languages` |
| Paper broker không nhìn thấy nến tương lai; trong cùng một nến, stop được khớp trước take-profit | **ĐÃ XÁC MINH** | `test_paper_broker_hides_future_bars`, `test_paper_broker_fills_stop_before_target_in_the_same_bar` |
| Khối lượng tính từ khoảng cách stop và đặc tả symbol, bị giới hạn bởi cả rủi ro **và** margin | **ĐÃ XÁC MINH** | `test_sizing_never_exceeds_the_risk_budget`, `test_sizing_caps_volume_by_margin_and_not_only_by_risk` |
| Sửa một bản ghi nhật ký bị phát hiện; phần đầu bị xoá chỉ bị phát hiện nhờ một mốc neo giữ ngoài tệp | **ĐÃ XÁC MINH** | `test_journal_detects_tampering`, `test_journal_anchor_detects_a_deleted_run_prefix`, `test_the_run_id_is_part_of_the_signed_body` |
| Không bản ghi nhật ký nào báo đã gửi lệnh cho broker | **ĐÃ XÁC MINH** | `test_agent_never_reports_a_broker_order`, `test_package_source_contains_no_order_sending_surface` |
| Chế độ `confirm` chỉ mở vị thế khi người vận hành trả lời "có" rõ ràng, còn `auto` từ chối chính sách dùng tài khoản không phải demo | **ĐÃ XÁC MINH** | `test_confirm_mode_without_an_approver_never_opens_a_position`, `test_automatic_execution_requires_a_demo_account_even_in_the_library` |
| `LiveMT5Backend` ánh xạ đúng phản hồi terminal và trừ offset giờ của server | **ĐÃ XÁC MINH bằng package giả lập** | `test_live_backend_*`, `test_cli_live_source_subtracts_the_server_offset` |
| Đọc terminal MT5 thật trên máy này | **CHƯA XÁC MINH** | máy này không cài package `MetaTrader5` và không có `terminal64.exe`, nên không đọc được gì từ terminal thật |
| Lợi nhuận của chiến lược và tỉ lệ lệnh thắng | **CHƯA XÁC MINH** | backtest trên chuỗi tổng hợp; `confidence` là điểm heuristic, không phải xác suất đã hiệu chuẩn. Tỉ lệ thắng 98% không được đo ở đây và mã này không thể đạt được |
| Thực thi trên tài khoản thật hoặc tài khoản demo của broker | **CHƯA XÁC MINH và không được triển khai** | package Python và CLI không có lời gọi gửi lệnh nào, theo thiết kế; template tham chiếu MQL5 có `OrderSend` nằm sau cổng (xem «Cố ý không mô hình hoá») |

Bản xem trước cục bộ dùng khi viết tài liệu này phát lại chuỗi EURUSD H1 **tổng hợp** (random walk
có trend, có seed); nó không được commit như một artifact, và các test tạo cùng chuỗi đó bằng helper
`make_bars()` của riêng chúng. Các test chạy nhật ký và các cổng trên chuỗi đó; mọi con số chúng in
ra chỉ minh hoạ cơ chế, **không phải** ước tính lợi nhuận. Không có gì ở đây quyết định khi nào được
dùng tiền thật: đó là một quyết định riêng, rõ ràng, nằm ngoài mã này, và package hiện tại không thể
thực thi quyết định đó theo cả hai chiều.

## Thành phần package

| Module | Vai trò |
|---|---|
| `mt5_agent/backend.py` | truy cập terminal không gửi lệnh: `AccountInfo`, `SymbolSpec`, `Tick`, `PositionSnapshot`, protocol `MT5Backend`, adapter `LiveMT5Backend` |
| `mt5_agent/policy.py` | `AgentPolicy`/`RiskLimits`/`StrategyConfig`/`TradingWindow`, tải YAML, từ chối khoá lạ, `execution_mode`, `ui_language` |
| `mt5_agent/strategy.py` | tín hiệu tất định SMA/ATR + momentum, hình học stop/take, các lý do chặn |
| `mt5_agent/sizing.py` | khối lượng từ `tick_size`/`tick_value`/`volume_step` trong ngân sách rủi ro và margin khả dụng |
| `mt5_agent/paper.py` | `PaperBroker`: phát lại theo nến, không nhìn trước, khớp theo bid/ask, gap, mốc equity theo ngày |
| `mt5_agent/journal.py` | một chuỗi băm SQLite (`audit_events`, điểm equity nằm trong đó), `verify_chain`, `report` |
| `mt5_agent/agent.py` | `MT5Agent.run_cycle()` — thứ tự cổng, cổng xác nhận, ghi quyết định |
| `mt5_agent/i18n.py` | nhãn tiếng Nga/Anh/Việt cho mọi mã và mọi thông báo cho người vận hành |
| `mt5_agent/codes.py` | danh sách mã trạng thái và lý do chặn duy nhất |
| `scripts/run_mt5_agent.py` | CLI: `run` (csv/live, auto/confirm, ru/en/vi), `report`, `reset --confirm` |
| `config/mt5_agent.example.yaml` | ví dụ chính sách có chú thích |

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
- Khối lượng: `risk_per_lot = stop_distance / tick_size · tick_value`; chọn bậc `volume_step` lớn
  nhất sao cho lỗ khi chạm stop vẫn nằm trong `min(equity, balance) · risk_per_trade_fraction` và
  margin cần thiết — `khối lượng × (tick_value / tick_size) × giá vào / đòn bẩy` tại giá vào — vừa
  với margin khả dụng. Nếu ngay cả lot tối thiểu đã vượt ngân sách thì từ chối — stop không bao giờ
  bị nới rộng.

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
symbol.

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

## Cố ý không mô hình hoá

- Swap, hoa hồng và chi phí qua đêm; trượt giá trong nến (lệnh mô phỏng khớp theo báo giá sau khi
  duyệt, bị giới hạn ở 10% khoảng cách stop); khớp một phần và requote vượt biên đó; trôi
  `tick_value` khi đồng tiền định giá biến động; broker đổi DST giữa lượt chạy (offset là hằng số
  bạn khai báo); các mốc equity chỉ sống trong một tiến trình, không lưu qua lần khởi động lại.
- Package Python không có `order_send` hay bất kỳ đường ghi giao dịch nào; bước quét mã nguồn
  `test_package_source_contains_no_order_sending_surface` chỉ bao phủ package `mt5_agent`, còn các
  test subprocess riêng của CLI kiểm tra JSON của nó không bao giờ báo đã gửi lệnh. Template MQL5
  **có** lời gọi `OrderSend`, nhưng nó nằm sau hằng số compile-time `ENABLE_DEMO_EXECUTION = false`
  (và sau tham số `AllowLiveExecution` buộc phải là false), nên không thể tới được nếu không sửa
  mã nguồn và biên dịch lại; agent Python không bao giờ gọi template đó.
- Không có `mode: live`: chính sách từ chối ngay khi tải.
- Agent không phải một phần của cơ chế cấp phép: `pipeline.evaluate_signal_bundle`, Certainty Gate
  và phê duyệt con người vẫn là những con đường duy nhất (và vẫn đang đóng) tới một lệnh thật.
