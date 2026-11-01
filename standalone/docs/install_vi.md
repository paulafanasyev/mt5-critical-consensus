# Cài đặt: máy tính + điện thoại (Telegram) — hướng dẫn đầy đủ

> ⚠️ Đây là reference scaffold: dữ liệu thị trường thật, tiền demo, **không có lệnh nào được gửi đi**. Xác nhận từ điện thoại là xác nhận *siêu dữ liệu duyệt lệnh* trong pipeline, chứ không phải lệnh gửi cho broker.

---

## Phương án A: chỉ cần MetaTrader 5 — không Python, không cài đặt ⭐

Toàn bộ logic nằm trong expert `mql5/CriticalConsensusStandalone.mq5`: các agent, certainty gate, hai chế độ và Telegram — ngay trong MT5. Không cần Python.

1. Tạo bot qua **@BotFather** (`/newbot`) — nhận **token**.
2. Trong MT5: **File → Open Data Folder** → copy `CriticalConsensusStandalone.mq5` vào thư mục `MQL5\Experts\`.
3. Trong MT5: **Tools → Options → Expert Advisors** → tick "Allow WebRequest for URL" và thêm: `https://api.telegram.org`
4. Trong Navigator mở expert (MetaEditor, phím F7 để biên dịch), kéo vào biểu đồ **EURUSD, M15**.
5. Trong cài đặt expert: dán **token**, chọn **WorkMode** (confirmation — xác nhận qua điện thoại / autonomous — không hỏi, trần 0.05 lot). ChatID để trống.
6. Gửi `/start` cho bot trong Telegram một lần — chat_id tự động nhận diện.

Expert tự động: 5 agent bỏ phiếu theo giá thật (xu hướng, RSI, biến động, nến, phiên), tất cả "ĐỒNG Ý" → vào lệnh (confirmation — thẻ xác nhận trên điện thoại; autonomous — ngay lập tức, tối đa 0.05 lot và 3 lệnh/ngày). Chỉ chạy trên **tài khoản demo**: với tài khoản thật expert từ chối giao dịch.

> Expert đã viết nhưng chưa biên dịch (máy này không có MetaEditor). Nếu MetaEditor báo lỗi khi F7 — gửi текст lỗi, tôi sẽ sửa.

## Phương án B: đường Python (nhiều kiểm tra hơn, human review packet)

---

## Cách nhanh nhất Python (30 giây)

1. Cài Python từ python.org (tick chọn **"Add python.exe to PATH"**!).
2. Mở thư mục dự án và **nhấp đúp vào `start.bat`**.
3. Lần chạy đầu tiên: trình hướng dẫn tự cài dependencies; dán token bot từ @BotFather; gửi `/start` cho bot — chat_id tự động được nhận diện.
4. Chọn chế độ: `[1]` confirmation (xác nhận trên điện thoại) hoặc `[2]` autonomous (không hỏi, chỉ demo).
5. Xong — demo giao dịch trên dữ liệu thật EUR/USD bằng tiền demo. Mọi thứ được ghi nhớ: lần sau chỉ cần chạy `start.bat`.

Autonomous ngay lập tức: `start_autonomous.bat`. Đặt lại cấu hình: `python scripts\run.py --reconfigure`.

Dưới đây là chi tiết từng bước.

---

## Phần 1. Máy tính (Windows)

### Bước 1. Python

1. Tải Python 3.12: https://www.python.org/downloads/
2. Khi cài đặt **bắt buộc** tick chọn **"Add python.exe to PATH"**.
3. Kiểm tra: mở Command Prompt (Win+R → `cmd`), gõ:
   ```
   python --version
   ```
   Kết quả phải là `Python 3.12.x`.

### Bước 2. Tải mã nguồn

Cách A (có git):
```
git clone https://github.com/paulafanasyev/mt5-critical-consensus.git
cd mt5-critical-consensus
```
Cách B (không có git): tải zip trên GitHub (Code → Download ZIP), giải nén, mở cmd trong thư mục đã giải nén.

### Bước 3. Cài dependencies

```
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

### Bước 4. Kiểm tra mọi thứ chạy được

```
python -m pytest -q
```
Kết quả mong đợi: `176 passed` (một warning từ starlette là bình thường).

### Bước 5. Tạo Telegram bot (2 phút)

1. Trong Telegram tìm **@BotFather** → lệnh `/newbot`.
2. Đặt tên và username cho bot (username phải kết thúc bằng `bot`).
3. BotFather đưa cho bạn **token** dạng `1234567890:AAEhBOweik6ad9r_QXMENQjcrGbqCr5G-p4` — lưu lại.
4. Lấy **chat_id**: gửi cho bot của bạn một tin nhắn bất kỳ (ví dụ `/start`), rồi mở trong trình duyệt:
   ```
   https://api.telegram.org/bot<TOKEN_CỦA_BẠN>/getUpdates
   ```
   Trong kết quả tìm `"chat":{"id":123456789,...}` — đó là **chat_id** của bạn.

### Bước 6. Biến môi trường

Trong cmd (hoặc lưu vào `.env`, dùng `env.example` làm mẫu):
```
set TELEGRAM_BOT_TOKEN=1234567890:AAEhBOweik6ad9r_QXMENQjcrGbqCr5G-p4
set TELEGRAM_CHAT_ID=123456789
```
Để chạy vĩnh viễn, đặt biến trong "Environment Variables" của Windows.

### Bước 7. Chạy demo với dữ liệu thật

**Chế độ confirmation (mỗi lệnh đều cần bạn xác nhận):**
```
python scripts/demo_real_data.py --trades 3 --telegram
```
Telegram sẽ gửi thẻ: giá EUR/USD thật, hướng lệnh, SL/TP, rủi ro bằng USD — kèm nút **✅ Approve / ❌ Reject**. Bấm từ điện thoại → lệnh được duyệt trong pipeline.

**Chế độ autonomous (không hỏi, chỉ demo, trần 0.05 lot):**
```
python scripts/demo_real_data.py --trades 3 --autonomous
```

**Không có Telegram (console):**
```
python scripts/demo_real_data.py --trades 1
```

### Bước 8 (tuỳ chọn). Dịch vụ API

```
set MT5_BRIDGE_TOKEN=một-chuỗi-bí-mật-dài-tối-thiểu-24-ký-tự
python -m uvicorn critical_consensus.api:app --host 127.0.0.1 --port 8000
```
Kiểm tra: `curl http://127.0.0.1:8000/health`

---

## Phần 2. Điện thoại (iPhone / Android)

1. **Telegram** — App Store / Google Play, đăng nhập tài khoản đã tạo bot.
2. Mở chat với bot của bạn, nhấn `/start`. Từ giờ thẻ xác nhận sẽ đến đây.
3. **MetaTrader 5** (App Store / Google Play) — cài đặt, mở **tài khoản demo** (Menu → New Account → Demo) để xem giá và số dư demo.
4. Khi bạn bấm Approve trong Telegram, bạn đang xác nhận quyết định của pipeline trên dữ liệu thật; nếu muốn, tự mở lệnh tương ứng trên tài khoản demo trong MT5 — scaffold không tự gửi gì vào MT5.

## Phần 3. Chạy 24/7: VPS

1. Thuê Windows VPS (host châu Âu nào cũng được, từ ~$10/tháng).
2. Kết nối qua RDP (iPhone: ứng dụng **Windows App** / Microsoft Remote Desktop).
3. Lặp lại các bước 1–7 trên VPS. Demo chạy 24/7, điện thoại chỉ là điều khiển.

---

## An toàn

- Không bao giờ công khai token của bot. Nếu lộ — thu hồi qua BotFather `/revoke`.
- API chỉ lắng nghe `127.0.0.1` — đừng mở port ra ngoài.
- Chế độ autonomous: opt-in kép, chỉ demo, giới hạn khối lượng. Scaffold vẫn không gửi lệnh nào.
