# Kết quả chạy demo trên dữ liệu thật

Tiếng Nga là bản gốc. English: [DEMO_RESULTS.en.md](DEMO_RESULTS.en.md) · Русский:
[DEMO_RESULTS.md](DEMO_RESULTS.md).

Phiên chạy thực hiện ngày 2026-11-01 trên máy có cài MetaTrader 5 (build 6296), tài khoản demo
`114929502` tại `MetaQuotes-Demo`, tiền tệ tài khoản USD, vốn mở đầu 100.000. Nến là nến H1 thật của
nhà môi giới, tải từ terminal qua `copy_rates_range`; không một bước nào của phiên chạy chứa lệnh
giao dịch, và mọi bản ghi nhật ký đều mang `broker_order_sent: false`.

## Đã chạy những gì

```powershell
PYTHONPATH=src python scripts/run_mt5_agent.py run `
  --policy agent_data/demo_eurusd_real.yaml --source live --live-bars 600 `
  --balance 100000 --currency USD --db agent_data/demo_eurusd.db --language vi --execution auto --verbose
PYTHONPATH=src python scripts/run_mt5_agent.py run `
  --policy agent_data/demo_xauusd_real.yaml --source live --live-bars 600 `
  --balance 100000 --currency USD --db agent_data/demo_xauusd.db --language vi --execution auto --verbose
```

Chính sách nằm trong `agent_data/` (không thuộc repository): `risk_per_trade_fraction: 0.0002`,
`min_risk_reward: 1.5`, `max_open_positions: 1`, `execution_mode: auto`, `require_demo: true`, cùng
`expected_login` / `expected_server` được ghim. Thông số công cụ đọc từ terminal: EURUSD — point
0.00001, giá trị tick 1.0, khối lượng 0.01/0.01/500; XAUUSD — point 0.01, giá trị tick 0.1, khối
lượng 0.01/0.01/100.

## Kết quả phiên chạy

| Công cụ | Số chu kỳ | Lệnh đã đóng | Lệnh thắng | Tỷ lệ thắng | P/L thực hiện | Vốn | Vốn min / max | Chuỗi nhật ký |
|---|---|---|---|---|---|---|---|---|
| XAUUSD | 540 | 23 | 5 | 0,217 | −159,96 | 100.000 → 99.840,04 | 99.756,24 / 100.018,21 | `valid: true`, 1104 sự kiện |
| EURUSD | 540 | 24 | 4 | 0,167 | −203,39 | 100.000 → 99.796,61 | 99.791,41 / 100.044,31 | `valid: true` |

Không chu kỳ nào gửi lệnh tới nhà môi giới; 501 (XAUUSD) và 475 (EURUSD) chu kỳ bị chặn bởi giới hạn
một vị thế mở, 11 và 19 chu kỳ vì không có tín hiệu, 5 và 22 chu kỳ vì ngoài khung giờ giao dịch.

![Đường vốn](charts/demo_equity_curves.svg)

![Hệ số R theo từng lệnh đã đóng](charts/demo_trade_r_multiples.svg)

## Vì sao kết quả lại như vậy

Stop là 2 × ATR và target là 2R. Với bước ngẫu nhiên không trôi, xác suất đạt +2R trước khi chạm −1R
bằng đúng 1/3, và kỳ vọng trước chi phí bằng 0. Tỷ lệ thắng quan sát được (0,217 và 0,167) **thấp
hơn** ngưỡng đó, nên lỗ trên mỗi lệnh xuất hiện dù rủi ro rất nhỏ: −6,95 USD mỗi lệnh với vàng và
−8,47 USD với EURUSD.

Một phép đo riêng trên một năm nến thật của cùng nhà môi giới xác nhận điều này: tự tương quan bậc
một của lợi nhuận theo giờ là âm (XAUUSD −0,019, EURUSD −0,0067), nghĩa là sau một chuỗi nến tăng thì
nến kế tiếp có xu hướng hồi lại, trong khi SMA(5/20) vào lệnh đúng lúc đường nhanh đã nằm trên đường
chậm. Với 23 và 24 lệnh, các con số chưa chứng minh chiến lược thua lỗ (t = −1,57 và −1,95), nhưng
cũng không cho phép gọi nó là có lãi.

## Nghiên cứu lợi thế trên nến thật

`scripts/render_demo_evidence.py` tính lại cả hai họ chiến lược trên cùng các nến thật, không bao giờ
mềm tham số theo cửa sổ đang kiểm thử, và lặp lại phép quét ở nhiều mức chi phí mỗi chiều:

![Phụ thuộc chi phí](charts/real_bar_cost_sweep.svg)

| Công cụ | Chi phí mỗi chiều | Bollinger(20, 2.0) | SMA(5/20) |
|---|---|---|---|
| XAUUSD | 0 bps | −33,5% (219 lệnh) | +54,9% (344) |
| XAUUSD | 5 bps | −46,6% | +9,9% |
| XAUUSD | 20 bps | −72,4% | −60,9% |
| EURUSD | 0 bps | +2,1% (275 lệnh) | −0,3% (382) |
| EURUSD | 5 bps | −22,4% | −32,0% |
| EURUSD | 20 bps | −66,1% | −78,4% |

Chi phí bằng 0 là trường hợp không tồn tại về mặt vật lý, không phải cận trên: nó cho thấy cả hai họ
đều không có biên an toàn. Tín hiệu đo được duy nhất là phân bố theo giờ:

![t-thống kê theo giờ](charts/real_bar_hourly_tstats.svg)

Vàng cho t = +3,25 lúc 09 UTC và +2,51 lúc 03 UTC trên 5742 quan sát; ở EURUSD giờ dương lớn nhất là
+1,65, còn giờ lệch mạnh nhất lại âm: t = −2,00 lúc 04 UTC. Đây là
giả thuyết, chưa phải lợi thế: sau khi hiệu chỉnh cho 24 phép so sánh, giờ vàng chỉ còn ý nghĩa ranh
giới, và nó phải được kiểm tra trên một giai đoạn riêng.

## Đối chiếu với thư mục làm việc song song

`C:\Users\Administrator\.zcode\workspace\default` chứa công trình độc lập cùng ý tưởng dùng nến công
khai của Binance (không phải MT5): walk-forward 12 fold, 32 cấu hình chiến lược vàng, và các phiên
"live" ảo. Kết luận của họ trùng với chúng ta: các họ theo xu hướng đều âm, mẫu thắng in-sample đổ vỡ
out-of-sample (PF 1,4 → 0,48), vàng có tính hồi quy (lag-1 của họ −0,015 so với −0,019 của ta trên nến
nhà môi giới), và họ duy nhất sống sót ở đó — Bollinger(20, 2.0) trên EURUSDT, +7,1% — **không tái
lập** trên nến thật của nhà môi giới (−22,4% chỉ với 5 bps mỗi chiều). Số dư "live" của họ:
1000 → 1004, 1000 → 996, 996,70, 998,33, +0,08, −0,57.

## Đường truyền còn sống, thị trường đóng cửa: trạng thái ngày 2026-11-01

Thay đổi lớn nhất trong ngày: đường "runner → bridge → EA" hoạt động, và đây là điều đo được chứ
không phải giả định. EA `CriticalConsensusEA_Demo` trên đồ thị XAUUSD,H1 hỏi bridge mỗi 3 giây; việc
bật stop bền vững qua `POST /v1/risk/kill-switch` đã tạo trong `MQL5/Logs/20261101.log` đúng những
dòng mà một `GET` đã xác thực thành công phải tạo ra (`BLOCKED: kill switch is active or state is
invalid.` theo nhịp hỏi), còn việc nhả stop bằng `--release-kill-switch` đã khiến chúng dừng lại.
Trước khi dán token vào các ô tham số của đồ thị, cũng chính nhật ký đó báo `HTTP 401` ở mỗi tick.
Phương pháp cùng ba kết luận sai mà nó loại trừ được mô tả trong [MT5_AGENT.md](MT5_AGENT.md).

Những gì vẫn chưa khớp lệnh — và vì sao:

| Phiên chạy | Lệnh | Trạng thái |
|---|---|---|
| Trình chạy demo MT5 | `python scripts/run_mt5_demo_trading.py --policy agent_data/live_gold_ema_policy.yaml --db agent_data/demo_live_20261101.db --symbols XAUUSD,XAGUSD,EURUSD,GBPUSD,BTCUSD --cycles 0 --interval-seconds 20 --execution auto` | nhật ký của tiến trình đó dừng ở 1386 bản ghi `agent_cycle` lúc 14:27:34 UTC, tất cả `OUTSIDE_TRADING_WINDOW` (luồng giá đóng băng từ thứ Sáu 23:59 và chính sách chặn Chủ nhật), `broker_order_sent: false`, vốn 100.000 không đổi, chuỗi hợp lệ; tệp lệnh đã giao `agent_data/demo_bridge/served.jsonl` không tồn tại — hàng đợi chưa giao gì cả |
| Phiên chạy giấy đa sàn | `python standalone/scripts/live_multi_venue.py --interval-sec 60` | Bybit, OKX và Kraken, 6 ô (BTC và ETH trên mỗi sàn), EMA 50/200 + stop 2×ATR + target 2R, 0,5% của 10.000. Đang mở 6 vị thế (BTC 77.838,70–77.949,50; ETH 2.264,11–2.267,07), 0 lệnh đóng, số dư vẫn là 10.000,00: stop 2×ATR rộng hơn những gì cửa sổ quan sát được cho phép |
| Danh mục live | `python standalone/live_portfolio.py --minutes 0 --interval-sec 30 --balance 10000` | 5 ô, chế độ `evidence`, 2 vị thế mở (gold SELL từ 3.898,90, EUR SELL), 0 lệnh đóng, vốn 9.996,17 lúc 14:45 UTC |

Các con số trong bảng là ảnh chụp, không phải bất biến. Nhật ký ở dòng đầu dừng lại vì tiến trình đã
ghi nó không còn giữ cầu nối: cổng `127.0.0.1:8000` hiện thuộc về một runner khác chạy cùng chính
sách và cùng đường dẫn tombstone (nên cũng cùng `run_id`), và tiến trình đó ghi vào
`agent_data/demo_live_mine.db`. Một lượt chạy đang sống thêm 12 bản ghi `agent_cycle` mỗi phút
(4 công cụ × 3 chu kỳ), nên một con số như vậy chỉ có nghĩa kèm theo mốc thời gian của nó.

Hai điểm cần nói thẳng sau bảng. Phiên đầu chạy trên tài khoản 114929502 (100.000 USD) — nên câu hỏi
"vì sao không có gì mở" không còn là chuyện tính khối lượng, mà chỉ là thị trường đóng cửa cộng với
một ngày trong tuần bị chặn. Hai phiên sau là các lệnh **giấy** trên giá công khai thật của các sàn
crypto — chúng không đi qua MT5, không đi qua các cổng của agent, không chứng minh điều gì về việc
khớp lệnh tại nhà môi giới; chúng chỉ chứng minh rằng quy tắc kịp theo một chuỗi sống. Tệp
`agent_data/multi_venue.jsonl` trong ngày đã bị ba lần khởi động khác nhau ghi vào (nó không có cơ
chế chống instance thứ hai): 3 × 6 = 18 dòng `OPEN` và không một dòng `CLOSE`, nên "18 lệnh mở" là số
tổng hợp qua nhiều phiên chứ không phải lịch sử của một tiến trình; còn bộ đếm "6 mở, 0 đóng" trong
các dòng tick là trạng thái của tiến trình hiện tại.

## Lệnh đã tới nhà môi giới: trạng thái lúc 2026-11-02 00:41 UTC

Thị trường mở lúc 00:00 UTC và câu chốt của mục trước — "chỉ còn chờ một lệnh có magic `8172026`
kèm bình luận `CC:` phía nhà môi giới" — đã được giải quyết: những lệnh như vậy tồn tại. Dưới đây là
điều thực sự xảy ra, kể cả chỗ mà tự kế toán của agent đã sai.

Những dấu vết bền vững trước đó chưa từng có: `agent_data/demo_bridge/served.jsonl` tồn tại lần đầu
(2 dòng, 00:00:10 và 00:07:37 UTC — đúng một dòng cho mỗi lệnh của mỗi công cụ), và
`kill_switch.json` giờ đọc `kill_switch_active: false` sau khi người vận hành nhả stop lúc 00:07:05
UTC. Dòng bảng ngày 2026-11-01 "`served.jsonl` không tồn tại — hàng đợi chưa giao gì" được mục này
rút lại.

Hai lệnh đã khớp, cả hai được nhà môi giới chấp nhận (`retcode=10009` trong
`MQL5/Logs/20261102.log`):

| Công cụ | Lệnh | Kỳ vọng của agent | Lệnh của nhà môi giới | Thời gian (UTC) | Kết cục |
|---|---|---|---|---|---|
| EURUSD | `0e83540c…` SELL 0,06 (`cc-3f62effe965d7ad5111db40b5070`, run `demo-94c9fe799215`) | vào 1,11564, SL 1,1187071, TP 1,1095057, rủi ro 18,40 USD | deal `10934940848`, giá 1,11563, vị thế `11185843968` | 00:00:09 | agent ghi `UNCONFIRMED` / `NO_FILL_OBSERVED`; vị thế bị đóng ngoài EA lúc 00:07:14 (deal `10935020451`, magic 0, bình luận trống) với −1,56 USD |
| XAUUSD | `f62fcc82…` SELL 0,04 (`cc-63dee1b6fd2debd42ec87d81540e`, run `demo-93b10275ab25`) | vào 3902,18, SL 3947,0657, TP 3812,4086, rủi ro 17,95 USD | deal `10935023434`, giá 3902,27, vị thế `11185923231` | 00:07:36 | `FILLED`, `broker_order_sent: true`; vị thế vẫn mở, SL/TP do nhà môi giới giữ |

Thời gian là UTC sau khi trừ `server_utc_offset_seconds: 7200` khỏi mốc của terminal
(`src/critical_consensus/mt5_agent/backend.py:425-442`): mốc thô của deal đầu là
`2026-11-02T02:00:09`, khớp với `served.jsonl` (00:00:10) và dòng EA `07:00:10.556` giờ địa phương
(UTC+7).

Vì sao lệnh đầu không bao giờ được chính agent xác nhận — và không phải do thị trường:
`entry_deals()` trong `scripts/run_mt5_demo_trading.py:557-570` đọc lịch sử qua cửa sổ "−1 giờ /
+1 giờ" cho tới 00:06:25 UTC (diff trong working tree: `timedelta(hours=1)` → `days=7` / `days=3`),
trong khi máy chủ MetaQuotes-Demo chạy UTC+2 và terminal cắt khoảng ở mép phải: đo trên terminal
sống lúc 04:58:51 UTC với mép trái cố định −7 ngày, now+4h trả 1 dòng và không deal nào mang magic
`8172026`, còn now+8h trả 5 dòng và cả hai lệnh khớp. Một khoảng bị cắt đọc ra như sổ trống, nên lệnh mà nhà môi giới thực sự khớp
hoá thành `NO_FILL_OBSERVED`, phiên chạy tự dừng vì cổng của chính nó
(`kill_switch_reason: "EURUSD:UNCONFIRMED"`), và các lần khởi động lại (rc=3, sáu lần từ 00:00:58 tới
00:07:04) bị chính kết cục chưa giải quyết đó từ chối. Bản sửa nằm trong working tree và **chưa commit**;
phiên vàng khởi động lúc 00:06:33 đã mang theo nó, nên lệnh thứ hai được ghi đúng. Không cổng thực
thi nào bị đi vòng ở đây: stop được người vận hành nhả bằng `--release-kill-switch` lúc 00:07:05 UTC
(`kill_switch.json` ghi `actor_id: Administrator`, `kill_switch_active: false`), và phiên vàng khởi động
lại đã truyền `--acknowledge-uncertain`, tức người vận hành công nhận rõ ràng kết cục EURUSD chưa được giải quyết.

Nhật ký cho thấy gì (ảnh chụp lúc 00:41 UTC, không phải bất biến):

| Nhật ký | `agent_cycle` mỗi công cụ | `demo_order_command` | `demo_order_served` | `demo_order_outcome` | `demo_symbol_alias` | `demo_symbol_unavailable` | `demo_risk_baseline` |
|---|---|---|---|---|---|---|---|
| `agent_data/demo_live_all_universe.db` (4523 chu kỳ) | EURUSD 647, GBPUSD/AUDUSD/NZDUSD/XAUUSD/XAGUSD/US500 mỗi cái 646 | 1 | 1 | 1 (`UNCONFIRMED`) | 0 | 18 (6 công cụ × 3 lần khởi động) | 2 |
| `agent_data/demo_gold_live.db` (104 chu kỳ lúc 00:40:59 UTC) | XAUUSD 104 | 1 | 1 | 1 (`FILLED`) | 0 | 0 | 23 |

Các blocker. Trong nhật ký all-universe, 646 trong 647 chu kỳ EURUSD và đúng 646 chu kỳ của mỗi sáu
công cụ còn lại là `OUTSIDE_TRADING_WINDOW`; chu kỳ duy nhất không bị chặn là `OBSERVED` lúc 00:00:07
(cửa sổ trong `config/forward/06_ema_all_universe_h1.yaml:54-56` mở lúc 00:00 UTC, Chủ nhật bị chặn).
Sáu công cụ không có tín hiệu không phải vì cửa sổ của họ đóng mà vì lượt chạy dừng ở ký hiệu đầu
tiên: trình chạy đã bị dừng, và từ 00:07:06 UTC supervisor của nó đứng ngoài — "port 8000 held by
foreign pid=12464; standing down, not killing" (`agent_data/supervisor_all_universe.log`), trong đó
pid 12464 là cầu nối của phiên vàng. Ngược lại, phiên vàng có giao dịch và giờ bị chặn bởi chính giới
hạn của nó chứ không bởi cửa sổ: 100 chu kỳ `POSITION_CAP_REACHED,SYMBOL_ALREADY_OPEN`, 3
`POSITION_CAP_REACHED`, 1 `OBSERVED` (bộ đếm này tăng 3 chu kỳ mỗi phút, nên nó cũng chỉ là ảnh chụp).

Cổng của EA. `MQL5/Logs/20261102.log` (UTF-16LE, 142 dòng, mốc giờ địa phương UTC+7) chia thành năm
khoảng, không khoảng nào là một từ chối thực thi: 06:20:23–07:00:10 và 07:00:44–07:06:35 — 129 ×
`BLOCKED: kill-switch state unavailable; HTTP 1001, error=5203`, ở đó EA không đọc được trạng thái
stop nên chu kỳ không bao giờ tới lệnh; 07:00:10.556 — `Order accepted` cho `command_id=0e83540c…`,
`deal=10934940848`, `retcode=10009`; 07:06:35–07:07:04 — 11 × `BLOCKED: kill switch is active or state
is invalid.` (stop `EURUSD:UNCONFIRMED` đang bật); 07:07:37.560 — `Order accepted` cho
`command_id=f62fcc82…`, `deal=10935023434`, 32 giây sau khi nhả. Không có dòng nào về cổng trượt giá,
stops level, ràng buộc khối lượng, hình học sai hay `SymbolSelect failed`: tính đến 00:41 UTC chưa có
lệnh nào tới EA và bị một cổng của EA từ chối. Sự kiện `demo_symbol_alias` bằng 0 trong cả hai nhật
ký — việc đối chiếu cách viết tên của nhà môi giới chưa bao giờ cần dùng, cả hai lệnh đi ra dưới tên
mà chính agent tạo.

Trả lời câu "bao nhiêu công cụ thực sự giao dịch": một — XAUUSD. Tài khoản 114929502
(MetaQuotes-Demo, demo) lúc 00:43:45 UTC: số dư 99.998,44, vốn 100.026,12, `open_positions: 1`, vị thế
`11185923231` SELL 0,04 từ 3902,27 với stop 3947,07 và target 3812,41,lại chưa thực hiện +27,68 USD, bình luận
`CC:cc-63dee1b6fd2debd42`. Sáu công cụ all-universe còn lại không giao dịch: trình chạy của chúng bị
dừng bởi chính cổng “kết cục chưa giải quyết” của nó, và kết cục đó — như đã chỉ ra ở trên — là một lỗi đọc lịch sử deal,
không phải sự từ chối của nhà môi giới và không phải thiếu tín hiệu.

## Hai lệnh khớp đầu tiên: mô hình rủi ro sai ở đâu (đo lúc 01:59 UTC)

Tính đến 02:00 UTC tài khoản có hai deal đã thực hiện và một vị thế mở — và cả hai deal đều được
đóng KHÔNG phải bởi EA:

| Thời gian (UTC) | Deal | Chuyện gì | USD |
|---|---|---|---|
| 00:07:14 | `10935020451` | EURUSD BUY OUT 0,06 tại 1,11589, magic 0, bình luận trống | −1,56 |
| 01:21:53 | `10935754921` | XAUUSD BUY OUT 0,03 tại 3910,57, magic 0 (đóng một phần vị thế `11185923231`) | −24,90 |
| hiện tại | vị thế `11185923231` | XAUUSD SHORT 0,01 từ 3902,27, SL 3947,07, TP 3812,41, bình luận bị nhà môi giới xoá sau khi đóng một phần | chưa thực hiện ≈ −6,75 |

Số dư 99.973,54, vốn ≈ 99.967. Đã thực hiện −26,46 USD lớn hơn ngân sách ngày 20 USD, trong khi 
kill switch lẫn cổng ngân sách không kích hoạt. Lý do được đo, không phải đoán: **mô hình rủi ro đánh
thấp kim loại đúng 10 lần.**

Giá của vấn đề, theo thực nghiệm. Đóng 0,03 lot vàng ở mức cao hơn giá mở 8,30 làm mất 24,90, tức
24,90 / 8,30 / 0,03 = 100,00 $ cho mỗi 1 $ biến động giá trên mỗi lot; một phép đọc độc lập trên vị
thế sống cho cùng số: −6,75 / (ask 3909,02 − 3902,27) / 0,01 = 100,00. `trade_contract_size` của
chính nhà môi giới cho XAUUSD là 100,0, khớp với nhau. Nhưng `trade_tick_value / trade_tick_size`
= 0,1 / 0,01 = 10,0 — terminal báo giá trị tick thấp hơn chính contract size của nó và thấp hơn P&L
đã thực hiện tới mười lần. Lệnh vàng trong nhật ký ghi `final_risk_value` 17,9542 =
0,04 × 44,8857 × 10, tức phép tính khối lượng đã dùng con số bị đánh thấp đó. Rủi ro thật tới stop ở
3947,07 là 0,04 × 44,886 × 100 = **179,54 USD**, không phải 17,95.

Kiểm tra trên cả bảy công cụ (chỉ đọc, `symbol_info`):

| Công cụ | $/lot/$1 từ tick_value | $/lot/$1 từ contract_size | Kết luận |
|---|---|---|---|
| EURUSD, GBPUSD, AUDUSD, NZDUSD | 100.000 | 100.000 | khớp |
| US500 | 1,00 | 1,0 | khớp |
| XAUUSD | 10,00 | 100,0 | **thấp hơn 10 lần** |
| XAGUSD | 500,00 | 5000,0 | **thấp hơn 10 lần** |

Ghi chú trong `config/forward/06_ema_all_universe_h1.yaml:26-29` ("trả 100 $ cho một đơn vị giá trên
một lot, đó là contract size chứ không phải trường này; một lượt chạy live vẫn tính từ terminal") nêu
đúng giá trị nhưng không cứu được lượt chạy: trong cặp trường này terminal tự mâu thuẫn, và lượt chạy
đã lấy cái sai. FX không bị ảnh hưởng — `final_risk_value` 18,40 của EURUSD khớp với −1,56 đã thực
hiện (một bước 0,00026 trên 6.000 đơn vị) tới từng cent.

Trạng thái liên kết lúc 01:58 UTC: phiên vàng đã chết (trình chạy biến mất, nhật ký im từ
01:39:24.730 — 279 chu kỳ, 1 tín hiệu, 25 `demo_risk_baseline`); supervisor dựng lại trình chạy
all-universe lúc 01:40:50 (pid 10928, run_id `demo-8199be303a70`, khi port 8000 đã trống), và bây giờ
cả bảy công cụ bị chặn bởi `POSITION_CAP_REACHED` — phần còn lại 0,01 XAUUSD chiếm ô duy nhất, còn
XAUUSD thêm `SYMBOL_ALREADY_OPEN`. Không một tín hiệu nào xuất hiện từ khi khởi động lại, nên số công
cụ thực sự giao dịch bằng không: agent có một vị thế, nó không có một lệnh khớp mới nào.

## Giá rủi ro đã được sửa, và đường truyền đứt lần thứ hai (đo lúc 04:47–04:51 UTC)

Việc tính khối lượng giờ chạy trên `contract_size` (`SymbolSpec.cash_per_price_unit` ưu tiên contract
còn tỉ lệ `tick_value/tick_size` chỉ là dự phòng), và điều đó được kiểm với terminal thật chứ không
phải fixture: `from_terminal` trả 100.000,0 cho EURUSD/GBPUSD/AUDUSD/NZDUSD (độ lệch với cặp tick
~0), 1,0 cho US500 (0,0), 100,0 so với 10,0 cho XAUUSD (lệch 0,9) và 5000,0 so với 500,0 cho XAGUSD
(0,9). Phát lại `size_position` ngoại tuyến ở giá đã sửa: với fractions 0,0002 vàng bị từ chối bằng
`MINIMUM_LOT_EXCEEDS_RISK_BUDGET` (phép tính theo tick sẽ gửi 0,04 lot như "18,0 $"), với 0,0005 thì
0,01 lot và 44,9 $; bạc chỉ qua khi stop ≲ 1,0.

Trình chạy universe đã thực sự giao dịch ở giá đã sửa và xếp hàng các lệnh thật: 02:22:59–02:36:46
UTC — EURUSD SELL 0,16 (rủi ro 48,34 $), GBPUSD BUY 0,18 (49,53 $), XAGUSD SELL 0,01 (46,09 $).
`agent_data/demo_live_all_universe.db` lúc 04:47 UTC: 14 `demo_order_command`, 26
`demo_order_outcome`, **1** `demo_order_served`. 26 kết quả tách đúng như sau: 13 `COMMAND_EXPIRED`
(mọi lệnh mới, `broker_order_sent: false`), 12 `VOLUME_ABOVE_DEMO_CAP` (AUDUSD, NZDUSD và US500, mỗi
công cụ bốn vòng — khối lượng đề xuất vượt trần demo 0,20 lot) và một `NO_FILL_OBSERVED` lúc 00:00:40
cho lệnh duy nhất lúc 00:00:07. `served.jsonl` không tăng từ 00:07:37. Nhà môi giới không lấy lệnh nào
trong các lệnh được xây trên giá đúng, và nguyên nhân do chính nhật ký của advisor nêu ra chứ không
phải suy đoán.

Diễn biến sau đó: terminal được khởi động lại với `/config` lúc 03:51:44 UTC,
`CriticalConsensusEA_Demo (XAUUSD,H1)` khởi tạo lúc 03:51:45 với token hợp lệ rồi ghi
`HTTP -1, error=4014` trong **356 nhịp hỏi liên tiếp** tới 04:11:03, sau đó không viết thêm dòng nào.
Suốt thời gian đó cầu nối vẫn listen trên `127.0.0.1:8000` ngay trong tiến trình của trình chạy — nên
chặn nằm ở chính quyền `WebRequest` của terminal, không phải mạng và không phải token. Trình chạy
universe đã chết sớm hơn, lúc 03:09:36, với những chu kỳ cuối là
`BLOCKED ['ACCOUNT_READ_FAILED']` trên cả bảy công cụ. Từ 00:07:36 UTC không có thêm một lệnh khớp
nào: số công cụ thực sự giao dịch là **không**.

Phần công cụ quan sát có đoạn riêng vì chính nó đã che điều này nhiều giờ:
`scripts/system_status.ps1` đọc nhật ký của advisor theo tên cố định `20261101.log`, tức nhật ký của
**hôm qua**, nên nó cho người vận hành thấy một đoạn đuôi sạch trong khi 356 nhịp hỏi hôm nay là từ
chối; còn `scripts/system_start.ps1` ghi nhật ký vào `demo_live_20261101.db` với ngày đóng cứng trong
tên, nên mọi ngày sau đó lại viết tiếp vào trail của ngày hôm trước và đọc lại ngân sách rủi ro của
ngày cũ. Cả hai đã sửa; trạng thái đường truyền giờ là một lệnh — `scripts/check_ea_transport.py`
(0 lành, 1 gãy, 2 không có log) — và một test ghim rằng cả 43 chuỗi từ chối mà advisor có thể in đều
map tới một trạng thái có tên.

Trạng thái lúc 04:51 UTC: nhật ký phiên vàng có 942 bản ghi / 457 chu kỳ, dòng cuối
`2026-11-02T04:51:19,899`, 300 chu kỳ cuối là 100 %
`['POSITION_CAP_REACHED', 'SYMBOL_ALREADY_OPEN']` (phần còn lại chiếm ô duy nhất vì
`max_open_positions: 1`), và cả phiên có 1 lệnh / 1 served / 1 kết cục; tài khoản 114929502, số dư
99.973,54, vốn 99.975,18, margin tự do 99.936,16 (đo 04:47:52); vị thế XAUUSD SHORT 0,01 từ 3902,27 (SL 3947,07,
TP 3812,41) ở **+1,64** chưa thực hiện — 1,64 $ cho 1,64 điểm trên 0,01 lot lại là 100 $ mỗi lot cho mỗi 1 $; kill
switch đã nhả từ 00:07:05.295; các deal có magic `8172026` kèm `CC:` vẫn đúng hai cái, đã thực hiện
−26,46.

Trạng thái lúc 05:31–05:35 UTC, lần đọc tiếp theo trong cùng ngày, và nó đổi luôn kết luận. Advisor đã
quay lại bridge — `error=4014` không còn, thay bằng `HTTP 1001, error=5203` trên 639 dòng BLOCKED liên
tiếp, dòng mới nhất 12:33:22 theo đồng hồ máy (= 05:33:22 UTC), và `check_ea_transport.py` trên trạng thái
đó in `bridge_unreachable` với exit 1. Không có gì để nói chuyện: `Get-NetTCPConnection -LocalPort 8000
-State Listen` trả về rỗng, `Win32_Process` không còn `run_mt5_demo_trading.py` nào, nhật ký vàng dừng ở
seq 1096 (`2026-11-02T05:17:01.263800+00:00`) và `.err` của nó chỉ có banner ba dòng. Không thể dựng bridge
từ phiên này: `MT5_BRIDGE_TOKEN` và `MT5_KILL_SWITCH_ADMIN_TOKEN` vắng ở cả Process, User lẫn Machine
scope (so bằng độ dài, giá trị không bao giờ được in), và một token mới thì cũng chỉ mua thêm
`token_rejected`. Nghĩa là quyền terminal vẫn được coi là phần việc của operator cả ngày đã thực sự được
cấp, và thứ duy nhất còn ngăn hàng đợi tới broker là một runner đang chạy.

## "Crash" trong log thực ra là gì (đọc lúc 05:05–05:25 UTC)

Từ "rơi" trong journal này che bốn thứ khác nhau, và chỉ một trong số đó là code.

**Sáu lần `rc=3`, 00:01:58–00:07:04 UTC, cách nhau đúng 60 giây.** Mỗi lần cùng một dòng: `В журнале
моста есть отданные команды без подтверждённой сделки: cc-3f62effe965d7ad5111db40b5070`. Đó không phải
lỗi mà là lời chối bắt đầu một cách trung thực: lệnh được giao lúc 00:00:40, kết quả của nó bị ghi là
`NO_FILL_OBSERVED`, và một tiến trình mới không được phép khởi động đè lên một lệnh chưa đối soát. Nguyên
nhân nằm ở phía đọc, không phải broker — deal đó **có** trong lịch sử tài khoản, comment của nó là `CC:`
cộng hai mươi ký tự đầu của đúng id ấy, tức broker đã nhận lệnh và chỉ cửa sổ đọc là bỏ sót.

**Một giờ hai mươi phút tạm dừng có chủ đích, 00:07:06–01:28:33 UTC.** Sáu dòng `port 8000 held by
foreign pid=12464; standing down, not killing` — supervisor tự tránh khỏi tiến trình của người khác đang
giữ cổng, đúng như yêu cầu. Đây là tạm dừng theo quy tắc, không phải sự cố.

**Hai lần bị kết thúc từ bên ngoài.** `rc=4294967295` = `-1` của pid 10928 lúc 02:22:57 và
`rc=3221225786` = `0xC000013A` (interrupt console) của pid 14392 lúc 2026-11-01T23:20:55. Log runner
**không có một traceback nào từ `run_mt5_demo_trading.py`**, nên không tiến trình nào chết vì ngoại lệ
Python; cả hai bị gỡ từ ngoài và supervisor khởi động lại trong cùng một giây. Dòng cuối của chính
supervisor là `03:09:23Z heartbeat journal_rows=11116` — nó ngừng ghi, và đó là lý do runner im lặng từ
03:09:36. Trong khoảng 02:22:59 đến 02:36:46 runner này vẫn xếp hàng 13 lệnh, và cả 13 hết hạn TTL
(`COMMAND_EXPIRED`) vì advisor không với tới bridge trong những phút đó. Nửa giờ cuối trước sự im lặng là
một chế độ khác hẳn: từ seq 10503 (`02:37:47 UTC`) đến hết, cả 600 dòng `agent_cycle` gần nhất mang đúng
một blocker `ACCOUNT_READ_FAILED` — tiến trình vẫn quay nhưng không đọc được tài khoản. Chính blocker đó
đã xuất hiện từ 02:20:05 đến 02:22:57, ngay quanh lần `rc=-1` từ bên ngoài.

**Sáu traceback của bridge — thứ duy nhất thực sự cần sửa.** `Exception occurred during processing of
request from ('127.0.0.1', 5075x)` kèm `ConnectionAbortedError: [WinError 10053]` trông như bridge đang
chết, nhưng đó là một ngắt kết nối bình thường: terminal hủy chính `WebRequest` ngay khi nó từ chối lượt
thăm dò, socket loopback đứt giữa lúc đang trả lời, và `socketserver` in một traceback đầy đủ cho mỗi
trường hợp — 57 dòng cho ba lần ngắt — trong khi các dòng trước và sau chúng vẫn là vòng đời lệnh bình
thường và hàng đợi vẫn phục vụ. `scripts/mt5_demo_bridge.py` giờ ghi đè `handle_error`: ba dạng ngắt
mong đợi (`ConnectionAbortedError`, `ConnectionResetError`, `BrokenPipeError`) chỉ tốn **một** dòng, mọi
ngoại lệ khác vẫn nhận báo cáo đầy đủ. Đo trên một bridge tạm ở cổng tạm (không phải `:8000`, thuộc agent
khác): ba lần reset cưỡng bức cho **57 dòng stderr có traceback** khi tắt bộ lọc và **3 dòng, không
traceback** khi bật, và trong cả hai lần bridge đó trả 200 cho `GET /v1/risk/kill-switch` ngay sau đó.
Được khoá bởi 4 test mới trong `tests/test_mt5_demo_bridge.py`.

**Và điều thứ năm không có trong mấy log đó: lúc 05:31–05:33 UTC thì chính runner đã biến mất.**
`Get-NetTCPConnection -LocalPort 8000 -State Listen` trả về rỗng và `Win32_Process` không còn
`run_mt5_demo_trading.py` nào; nhật ký vàng dừng ở seq 1096, `2026-11-02T05:17:01.263800+00:00`, còn `.err`
của nó chỉ có banner ba dòng. Ở đây không có traceback để tìm — tiến trình không crash, nó không chạy.
Advisor trong lúc đó lại thăm bridge và nhận `HTTP 1001, error=5203`, tức quyền terminal đã được mở, và
hành động bên ngoài còn lại duy nhất là khởi động runner kèm token mà hiện vắng mặt ở mọi scope.

## Lệnh khớp thứ ba tại nhà môi giới: trạng thái 05:43–06:10 UTC

`scripts/system_start.ps1` được khởi động lúc 05:43:12 UTC, và từ giây đó hàng đợi lại tới được nhà môi
giới. `agent_data/demo_live_20261102.db` giữ **657 sự kiện / 324 chu trình** từ `05:43:13.106` đến
`06:10:16.472`, và đúng một chu trình trong số đó sạch: lệnh
`b3d8fc393ea14b1b836da9e91fb9f8ac` / `cc-1bac6713ce6d749fe849117770c4` vào hàng đợi lúc 05:43:13.368
(`SELL EURUSD`, giá kỳ vọng 1,11823, `decision: AUTO`), được giao lúc 05:43:15.374, và kết quả lúc
05:43:32.402 — `state: FILLED`, `broker_order_sent: true`, `requires_manual_reconciliation: false`. Phía
nhà môi giới đó là deal **10938325361**, vị thế **11189096387**, EURUSD SELL 0,09 @ 1,11823, SL 1,12042,
TP 1,11385, magic `8172026`, chú thích `CC:cc-1bac6713ce6d749fe`. Nghĩa là cửa sổ `entry_deals()` đã mở
rộng đọc được lệnh khớp trong phiên chạy thật, mười hai giờ sau khi chính mã đó báo `NO_FILL_OBSERVED`
cho một lệnh trên cùng tài khoản. Các cổng rủi ro của chính lệnh: `final_risk_value` 19,7229 với
`final_risk_fraction` 0,000197 (ngân sách 0,0002), `risk_reward_at_fill` 2,0, `slippage` 0,0,
`final_margin_required` 100,6407.

323 chu trình còn lại bị chặn sau vị thế đó: **241 `POSITION_CAP_REACHED`, 80
`POSITION_CAP_REACHED + SYMBOL_ALREADY_OPEN`, 2 `MINIMUM_LOT_EXCEEDS_RISK_BUDGET`, 1 sạch**.
`max_open_positions: 1` tính cho toàn tài khoản chứ không theo từng công cụ, nên một lệnh đã khớp chặn hết
mọi symbol trong bộ; hai chu trình lot tối thiểu là những công cụ mà ngay lô nhỏ nhất đã rủi ro trên
20 USD. Tài khoản lúc 06:10:18 UTC: số dư 99 968,87, equity 99 966,62, vị thế mở đang −2,25 nổi.

Phần đã thực hiện của cùng phép đọc sửa một con số tài liệu này từng mang. `history_deals_get` với cửa sổ
−2 ngày/+3 ngày trả về **7** deal: ba lệnh vào ở trên mang magic `8172026` và chú thích `CC:`, còn **mọi
deal đóng đều mang magic 0 và chú thích trống** — chúng không phải lệnh của chuỗi này và không dòng nhật ký nào
nhận chúng. Lợi nhuận của chúng là −1,56, −24,90 và −4,67, tổng **−31,13**, và 100 000 − 31,13 đúng
bằng số dư nhà môi giới báo, nên phép đọc không mất gì. Con số "−26,46" trước đó được đo trước khi 0,01
XAUUSD cuối cùng đóng lúc 05:17:11 UTC.

## Đường truyền không chết: vì sao `check_ea_transport.py` báo BROKEN và giờ nó đo gì

Cùng dòng trạng thái đó in `ea_silent (BROKEN)` trên một chuỗi vừa khớp lệnh hai giây trước. Nguyên nhân
nằm ở chính advisor: **nó không viết gì khi mọi thứ bình thường**. Đo trên cả ba tệp `MQL5\logs\*.log`,
toàn bộ corpus là **4904 dòng — 4875 `BLOCKED`, 10 dòng khởi tạo, 8 `automated trading is disabled`, 6
enabled, 3 `Order accepted`, 2 `PROBE`**; trong mã nguồn không có dòng "opros bình thường" nào, nên im
lặng trong log không phân biệt được vòng poll còn sống với một advisor đã gỡ khỏi đồ thị, mà công cụ lại
đang suy ra đúng điều đó.

Nhân chứng bây giờ là bridge. `BridgeState` đếm và lưu thời điểm của mỗi GET **đã xác thực** trên hai endpoint
mà chỉ advisor poll (`/v1/risk/kill-switch`, `/v1/execution/next`; yêu cầu bị từ chối, POST, header `Origin`
và bản thân lần đọc health không được tính — mỗi trường hợp có test cố định), còn `GET /v1/health` mới, có
token-guard, trả đúng các bộ đếm đó và tuổi của poll đo theo đồng hồ của bridge. Checker chỉ hỏi endpoint này
khi kết luận của nó dựa trên sự im lặng (`ea_silent`, `never_polled`), để một từ chối đã được nêu tên trong log
không bị đè bởi một bộ đếm từ nơi khác. Đo trên đúng thư mục log thật với một bridge bản hiện tại ở cổng tạm
(không phải `:8000`): `--no-bridge` cho `ea_silent (BROKEN)` và exit **1**; phép đọc kia với bridge cho
`polling (healthy)` và exit **0** kèm dòng `bridge: advisor polled 0.1 s ago, 2 polls total`; `/v1/health`
không token trả **401**; một poll đã cũ (900 s) giữ nguyên `ea_silent` và nói thẳng sự im lặng là thật; còn
`unreachable`, `http_401`, `http_404`, `not_json` và URL hỏng mỗi thứ trả về một lý do thay vì ném exception.
Rằng cổng đang được poll ngay lúc này cũng thấy được mà không cần mã mới: bảng socket loopback trong
06:05:06–06:06:36 UTC giữ **80 kết nối TIME_WAIT với 80 cổng peer khác nhau, trượt từ 62520–62673 sang
62631–62789**, tức khoảng 57 kết nối vừa đóng mỗi 45 giây.

Còn lại một điểm trung thực: runner trên `:8000` khởi động lúc 05:43:12, tức trước khi thay đổi này tồn tại,
nên bridge của nó chưa có `/v1/health` và checker nói rõ bằng chữ — `the bridge has no /v1/health: it is
running an older build than this checker` — và không sửa kết luận đọc từ log. Khởi động lại
`run_mt5_demo_trading.py` sẽ làm kết luận đúng; việc đó không được thực hiện ở đây, vì dừng tiến trình đang
giao dịch là quyết định của operator, còn SL/TP của vị thế vẫn nằm phía broker dù thế nào.

## Phiên chạy này chứng minh gì và không chứng minh gì

Đã chứng minh: chuỗi "nến thật nhà môi giới → cổng chính sách → tính khối lượng → thực thi qua
advisor → nhật ký liên kết hash" đã đi tới một lệnh thật của nhà môi giới và quay về. Deal
`10935023434`, magic `8172026`, bình luận `CC:cc-63dee1b6fd2debd42` đọc được trong chính lịch sử tài
khoản 114929502, độc lập với nhật ký của agent. Trong hai lệnh, tuyên bố rủi ro chỉ đúng với EURUSD:
`final_risk_value` 18,40 của nó khớp với −1,56 đã thực hiện tới từng cent. Với XAUUSD, câu cũ "rủi ro
17,95 USD so với ngân sách 20 USD" **được rút lại** — rủi ro thật tới stop là 179,54 USD, và −26,46
USD đã thực hiện tự nó đã vượt ngân sách ngày 20 USD (xem mục trên). Phát biểu "không lệnh nào tới
nhà môi giới" ngày 2026-11-01 áp dụng cho các phiên giấy 2026-10-31 và 2026-11-01 và chỉ còn đúng với
chúng.

Chưa chứng minh: bất cứ điều gì mang tính thống kê. Ba lệnh là n=3, và không lệnh nào chạm stop hay
target của nó. Hai lệnh đầu đóng ngoài EA — lệnh thứ nhất toàn bộ (deal `10935020451`, 0,06, −1,56 USD),
lệnh thứ hai một phần (deal `10935754921`, 0,03 trong 0,04, −24,90 USD), 0,01 còn lại đóng bởi deal
`10937991911` lúc 05:17:11 UTC với −4,67 USD — còn lệnh thứ ba vẫn mở, đo ở **−2,25** chưa thực hiện lúc
06:10:18 UTC. Mọi deal đi ra mang magic 0 và bình luận trống, và dữ liệu đó không xác định ai đã làm.
Tổng đã thực hiện của tài khoản là −31,13 USD, tức đã gấp ba ngân sách rủi ro 20 USD mỗi chu kỳ
(`risk_per_trade_fraction` 0,0002), dù từng lệnh vào riêng lẻ đều nằm trong ngân sách: thua lỗ không đến
từ một cổng bị phá, mà từ chỗ các lệnh đóng diễn ra ngoài chuỗi, nơi các cổng của nó không với tới. Nên
điều này không kiểm chứng edge EMA 50/200 trên H1 theo hướng nào cả. Một PENDING EXTERNAL còn lại, và nó không còn là việc agent thấy lệnh vào của mình:
cửa sổ đã mở rộng làm việc đó trong phiên chạy thật lúc 05:43:15, nơi deal `10938325361` được đọc và lệnh
đóng lại thành `FILLED` với `requires_manual_reconciliation: false`. Cái còn mở là điều mà việc mở rộng
không chạm tới — rằng agent thấy **lệnh đóng** của chính vị thế: mọi deal đi ra trong lịch sử của chuỗi
này đều mang magic 0 và bình luận trống, và không lần đóng nào trong ba lần được khớp với lệnh của các
phiên chạy này. Phần lệch nhau lịch sử vẫn đúng như đã đo: deal
EURUSD được nhà môi giới khớp (lịch sử tài khoản có deal với bình luận `CC:cc-3f62effe965d7ad51…`) còn
runner all-universe ghi `NO_FILL_OBSERVED` lúc 00:00:40, trong khi kết quả của runner vàng cho lệnh của
nó mang `broker_order_sent: true` và trọn deal `10935023434`. Cơ chế lệch nhau đã được tái lập trên
terminal sống (04:58:51 UTC, cùng mép trái −7 ngày): mép phải now+4h trả 1 dòng và không deal nào mang
magic `8172026`, now+8h trả 5 dòng và cả hai lệnh khớp — tức chính mép phải bị cắt, vì các mốc naive
đọc theo giờ server (+7200 s), và cửa sổ −1h/+1h đã ghi sẽ trả một sách trống tại 00:00:40. Bản nới
rộng chưa commit trong `entry_deals()` (−7 ngày/+3 ngày) nay bị `test_entry_deals_asks_for_a_window_wide_enough_to_see_a_server_clock_fill`
ghim lại. Thứ tự để phiên vàng (khởi động 00:06:33) đã đọc cửa sổ rộng trong khi phép kiểm lúc 00:00:40 thì
chưa, được mô tả ở mục trên; nó suy ra từ thời điểm sửa tệp và thời điểm tiến trình chạy, chứ không
từ chính nhật ký. Chưa chứng minh thứ hai, từ mục 04:51:
một phiên chạy **duy trì được**. Sau 00:07:36 UTC không có thêm lệnh khớp nào: runner all-universe im
lặng từ 03:09:36 UTC (5867 chu kỳ, 14 lệnh, 26 kết quả — 13 `COMMAND_EXPIRED`, 12
`VOLUME_ABOVE_DEMO_CAP`, 1 `NO_FILL_OBSERVED`), runner vàng còn sống và còn ghi nhật ký (448 chu kỳ,
cuối cùng 04:48:19,725) nhưng 300 chu kỳ gần nhất đều là `POSITION_CAP_REACHED, SYMBOL_ALREADY_OPEN`,
còn advisor thì không thăm bridge nữa — `check_ea_transport.py` báo `ea_silent` với
`stale_from: request_not_allowed` sau 356 lần từ chối liên tiếp, dòng cuối là `HTTP -1, error=4014` lúc
04:11:03 UTC. Vậy ở lần đọc đó số công cụ có thể khớp lệnh vẫn là không: một bị khoá bởi ô vị thế, một đã
chết, và cổng quyền của terminal chia bridge khỏi nhà môi giới — chứ không vì một quyết định
thị trường. Lần đọc này đúng tới 05:35 UTC và bị mục ở trên thay thế: runner đã được khởi động lại lúc
05:43:12 và một lệnh đã được giao rồi khớp. Nhưng "duy trì" vẫn chưa phải trạng thái đã đạt — sau
05:43:15 hàng đợi không còn được dùng lần nào, vì 321 chu trình tiếp theo đều dừng ở ô vị thế đã chiếm.
Điều ngăn một lệnh vào tiếp theo bây giờ là `max_open_positions: 1`, không phải đường truyền. Khẳng định "EA chưa gắn lên đồ thị và `Config/common.ini` chưa có allowlist" vẫn **sai và
đã được rút lại**; đường truyền được xác nhận bằng `retcode=10009` thật, không chỉ bằng tiếng vọng
trong log — nhưng cũng chính log đó đo được `error=4014` sau một lần khởi động lại `/config`, nên
allowlist phải được kiểm sau mỗi lần terminal restart, và `scripts/check_ea_transport.py` là lệnh làm
việc đó.
