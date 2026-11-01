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

## Phiên chạy này chứng minh gì và không chứng minh gì

Đã chứng minh: chuỗi "nến thật nhà môi giới → cổng chính sách → tính khối lượng → khớp lệnh giấy →
nhật ký liên kết hash" hoạt động và tái lập được bằng lệnh ở trên; rủi ro mỗi lệnh đúng cam kết (mỗi
stop −18…−20 USD so với ngân sách 20 USD); không có lệnh nào tới nhà môi giới.

Chưa chứng minh: khớp lệnh thật tại nhà môi giới. Khi chạy, thị trường đóng cửa cuối tuần (tick
EURUSD cuối đã quá tuổi cho phép 20,4 giờ, XAGUSD không có tick, US500 báo `trade_mode = 0`), nên
đường `scripts/run_mt5_demo_trading.py` + `scripts/mt5_demo_bridge.py` + EA bị cổng `QUOTE_STALE`
chặn (còn trong phiên chạy 2026-11-01 là `OUTSIDE_TRADING_WINDOW`) — đó là hành vi đúng thiết kế,
không phải kết quả khớp lệnh. Khẳng định "EA chưa gắn lên đồ thị và `Config/common.ini` chưa có
allowlist" tính đến ngày 2026-11-01 là **sai và đã được rút lại**: EA đã nằm trên đồ thị, còn
`Enabled=1`, `WebRequest=1` cùng bốn địa chỉ loopback trong `WebRequestUrl` đã được kiểm lại trong
file, và đường truyền được nhật ký xác nhận. Phía nhà môi giới vẫn chỉ chờ đúng một xác nhận: một
lệnh có magic `8172026` kèm bình luận `CC:` trong chính lịch sử tài khoản. Đó vẫn là
PENDING EXTERNAL, và khung thời gian đầu tiên mà một chu kỳ như vậy có thể xảy ra là thứ Hai
2026-11-02 00:00 UTC.
