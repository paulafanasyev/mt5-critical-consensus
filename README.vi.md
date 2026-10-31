# Intelligence → Critical Consensus → Execution (MT5)

[Русский](README.md) | **Tiếng Việt**

**Trạng thái: reference scaffold / ưu tiên demo, KHÔNG phải hệ thống giao dịch sẵn sàng production.** Kho lưu trữ này chuyển đặc tả thành một bộ khung có thể kiểm chứng: schema thông điệp, các cổng fail-closed, tiện ích hiệu chuẩn, FastAPI, cấu hình, pytest, DDL Neo4j và bản nháp EA bằng MQL5.

## Mặc định an toàn

- Giao dịch tự động bị cấm. Mỗi lệnh cần một sự cho phép riêng của người dùng.
- Thực thi live trong mẫu MQL5 bị chặn và không thể bật bằng một tùy chọn; trước tiên cần audit, paper/demo-forward và một thay đổi chính sách riêng.
- Endpoint API nhận lệnh trong bộ khung trả `204 No Content` cho đến khi hàng đợi thực thi được triển khai và kiểm chứng. Vì vậy, việc bật EA tự nó không được tạo ra lệnh.
- Bất kỳ verdict bị thiếu, challenge còn mở, source stale, VETO, cờ rủi ro, hiệu chuẩn thất bại, thiếu risk snapshot/news state, khung giờ UTC bị cấm, TTL hết hạn, xác nhận không hợp lệ, hoặc risk snapshot không khớp với tham số lệnh chính xác và equity baseline đều chặn thực thi.
- Nhắc đến `confidence >= 0.98` chỉ là ngưỡng chấp nhận theo chính sách. CertaintyGate còn cần lịch sử hiệu chuẩn và health-metric: tối thiểu 30 quan sát cho `dissent_rate` và `false_certainty_rate`; thiếu metric sẽ chặn PASS. Điều này **không** có nghĩa là 98% khả năng sinh lời và không bảo đảm kết quả giao dịch. Tỉ lệ thành công 98% không được đo bởi mã này và không thể đạt được với nó.

## Thành phần

- `src/critical_consensus/gates.py` — self-adversarial checks, peer-review validation, vòng challenge, các cổng certainty/chief/permission/authorization fail-closed.
- `src/critical_consensus/pipeline.py` — đường đi tất định Certainty → Chief chín bước → Permission → human-review với hash `signal_input` bất biến gắn vào mọi verdict; `scripts/evaluate_signal_bundle.py` đánh giá evidence packet đáng tin cậy mà không chấp nhận human approval do client cung cấp; `--persist` còn yêu cầu kill switch đã lưu ở trạng thái inactive và risk snapshot MT5 mới, gắn với đúng tham số lệnh được yêu cầu.
- `src/critical_consensus/calibration.py` — Brier score, shrinkage penalty, weighted consensus; `calibration_eval.py` — hiệu chuẩn Platt walk-forward theo thời gian và báo cáo reliability theo cỡ mẫu.
- `src/critical_consensus/api.py` — các endpoint chính sách FastAPI; mỗi lần đánh giá chính sách cần evidence wrapper mới được ký HMAC và, với certainty, một attestation riêng của calibration-service. Endpoint authorization và gửi lệnh cố ý giữ ở trạng thái tắt cho đến khi có permission state bền vững và bộ xác minh 2FA/chữ ký thật.
- `src/critical_consensus/evidence.py` — envelope HMAC-SHA256 ràng buộc payload canonical chính xác, issuer, nonce, TTL và payload hash, với các domain ký riêng biệt `evidence`, `governance`, `operator` và `review`; `governance.py` — attestation lịch sử dissent/false-certainty đã ký; `authorization.py` — nonce dùng một lần cho mỗi operator cùng tombstone một lần cho mỗi permission (in-memory cho test, dòng PostgreSQL insert-only cho môi trường bền vững, migration 006).
- `src/critical_consensus/market_data.py` — adapter lịch sử MT5 + CSV không gửi lệnh và kiểm tra OHLC (đường MT5 gọi `symbol_select` để thêm symbol vào Market Watch khi nó chưa có); `providers.py` / `agent_runtime.py` — agent nghiên cứu tương thích OpenAI có cấu hình, ràng buộc input-hash chính xác, kiểm tra TTL, kiểm tra provider-origin duy nhất và thu thập fail-closed. `scripts/run_agent_analysis.py` yêu cầu đủ chín ID agent bắt buộc theo chính sách, sau đó chạy peer review ẩn danh do provider hỗ trợ (hai reviewer cho mỗi verdict) và tối đa ba vòng giải quyết challenge gắn bằng chứng. `READY_FOR_CHIEF` chỉ có nghĩa là review ledger đã đầy đủ; nó không chạy Certainty Gate và không cấp quyền giao dịch.
- `src/critical_consensus/redis_streams.py` — publish sự kiện idempotent, consumer group, retry/DLQ và phục hồi message đang pending; `scripts/run_worker.py` ghi bền vững sự kiện hợp lệ vào PostgreSQL trước khi ACK. Sự kiện parse-invalid được ghi vào DLQ ngay lần giao đầu tiên và chỉ sau đó mới ACK; sự kiện mà handler ném lỗi sẽ ở trạng thái pending và được reclaim (`XAUTOCLAIM`) ở mỗi vòng worker, nên được retry tối đa `max_attempts=3` lần giao trước khi bị dead-letter và ACK. Worker này lưu trữ sự kiện; nó không dispatch lệnh.
- `src/critical_consensus/mt5_agent/` — lớp agent không gửi lệnh bên trên terminal (`backend.py`, `LiveMT5Backend` không có bất kỳ hàm giao dịch nào; `symbol_spec()` chỉ gọi `symbol_select()` khi symbol chưa có trong Market Watch, việc này thêm nó vào danh sách symbol của terminal), `policy.py` fail-closed, tín hiệu tất định `strategy.py`, khối lượng từ đặc tả symbol `sizing.py`, paper broker không nhìn trước `paper.py`, nhật ký SQLite hash-chain `journal.py` và vòng lặp `agent.py`; `scripts/run_mt5_agent.py` chạy trên CSV export hoặc lịch sử terminal. Không chế độ nào gửi lệnh.
- `schemas/` — JSON Schema Draft 2020-12 cho 11 contract.
- `config/` — ngưỡng chính sách và permission mặc định từ chối (deny-by-default).
- `mql5/CriticalConsensusEA.mq5` — EA demo: chỉ tài khoản demo, tắt mặc định; kiểm tra loopback-URL nghiêm ngặt (host chính xác `127.0.0.1`/`localhost`, cổng số, không userinfo/query), `BridgeToken` không rỗng và header-safe, kiểm tra TTL/SL/TP/slippage và idempotency-stub có kiểm tra kết quả `GlobalVariablesFlush()` trước `OrderSend`.
- `src/critical_consensus/persistence.py` — repository PostgreSQL cho market bar bất biến, forecast trước outcome, label một lần, kill-switch/risk evidence, sự kiện Redis inbox và lưu bền vững toàn bộ decision/audit của pipeline. `db/postgres_schema.sql` + `db/migrations/` — schema gốc cùng các migration có thứ tự, chỉ tiến (bảng evidence bất biến và tombstone nonce insert-only); `neo4j/schema.cypher` — schema đồ thị.
- `tests/` — unit test đối kháng.
- `.github/workflows/security-audit.yml`, `tests.yml` và `validation-suite.yml` — quét bảo mật dependency/source, contract unit và kiểm tra tích hợp PostgreSQL/Redis — tất cả chỉ mang tính scaffold.
- `docs/` — kiến trúc, công thức, trace ví dụ, roadmap theo tuần và `MT5_AGENT.md` bằng cả ba ngôn ngữ (`MT5_AGENT.md`, `MT5_AGENT.en.md`, `MT5_AGENT.vi.md`).

## Chạy nhanh (Linux, Python 3.11+)

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

`MT5_EVIDENCE_HMAC_KEY`, `MT5_GOVERNANCE_HMAC_KEY`, `MT5_OPERATOR_HMAC_KEY` và `MT5_REVIEW_HMAC_KEY` phải là bốn khóa khác nhau, mỗi khóa dài 32–256 byte; các issuer tương ứng (`MT5_EVIDENCE_ISSUER`, `MT5_GOVERNANCE_ISSUER`, `MT5_OPERATOR_ISSUER`, `MT5_REVIEW_ISSUER`) cũng phải khác nhau. Khóa thứ nhất ký evidence-packet tại ingress tin cậy, khóa thứ hai ký metric lịch sử của calibration-service, khóa thứ ba ký quyết định của operator (approval) trong domain `operator`, khóa thứ tư ký peer reviews, challenge ledger và ChiefReasoning trong domain `review`. Không có operator key/issuer, bundle có quyết định của con người sẽ bị chặn thay vì được chấp nhận không chữ ký. Review registry chỉ được chấp nhận với `review_attestation` hợp lệ, bao phủ `{signal_id, verdicts, peer_reviews, challenge_records, chief_reasoning}`; ingress key không thể ký nó. `MT5_BRIDGE_TOKEN` được kiểm tra ở mỗi request được bảo vệ (không phải lúc khởi động process): phải chứa 24–256 ký tự `[A-Za-z0-9._~-]` và không trùng bất kỳ signing key nào — nếu không endpoint trả HTTP 503, còn `/health` hiển thị `bridge_token_distinct_from_signing_keys: false`. `MT5_KILL_SWITCH_ADMIN_TOKEN` riêng (≥32 ký tự) chỉ cần để tắt kill switch và cũng không được trùng bridge token hoặc signing key. Khóa provider chỉ được đặt qua biến dạng `MT5_AGENT_KEY_*` mà `api_key_env` tham chiếu; tên biến tùy ý và secret inline bị từ chối.

Với Windows VPS có MT5: Python bridge chỉ được lắng nghe trên `127.0.0.1`; trong MT5, thêm localhost vào danh sách địa chỉ được phép cho `WebRequest`. Không công khai bridge trên `0.0.0.0` nếu không có kiểm soát mạng và mật mã riêng.

## Lớp dữ liệu cục bộ tùy chọn

`docker-compose.yml` khởi động PostgreSQL 16 (image TimescaleDB; hypertable không được tạo trong schema), Redis và Neo4j chỉ trên loopback. Kho có PostgreSQL repository adapter (`persistence.py`), migration và worker script (`scripts/run_worker.py`); chúng được phủ bởi integration test chỉ chạy khi `TEST_DATABASE_URL`/`TEST_REDIS_URL` được cấu hình. Worker service đã triển khai và vận hành thực tế cùng broker/database thật chưa được kiểm chứng.

```bash
cp .env.example .env
python -c 'import secrets; print(secrets.token_urlsafe(32))'  # tạo POSTGRES_PASSWORD riêng
python -c 'import secrets; print(secrets.token_urlsafe(32))'  # tạo NEO4J_PASSWORD riêng
docker compose up -d
```

Điền hai giá trị đã tạo khác nhau vào `.env` và thay mật khẩu trong `DATABASE_URL` bằng `POSTGRES_PASSWORD` trước khi kết nối DB client. Không dùng giá trị mẫu trong production. Ở phiên bản này, ứng dụng không tự mở kết nối DB.

## Luồng demo evaluation đầu-cuối

`evaluate_signal_bundle(...)` nhận các verdict, peer review, challenge ledger, source report và ChiefReasoning đã được hình thành. Nó tính lại coverage peer-review, dùng metadata hiệu chuẩn cho consensus, kiểm tra review-domain, gắn PermissionRequest với hash ChiefReasoning, consensus đã tính lại và tóm tắt nguồn chính xác, rồi áp dụng certainty gate, ChiefAgent và PermissionGate. Nếu người dùng chưa truyền quyết định, nó trả `REQUIRE_HUMAN` cùng `human_review_packet`: toàn bộ peer review, dissenting opinion, challenge ledger, ChiefReasoning, nguồn, risk snapshot và tail-risk. Packet hiển thị đúng persisted terminal snapshot (kèm validation/reconciliation) chứ không phải tóm tắt rủi ro do client cung cấp. Quyết định của con người chỉ được chấp nhận như một operator-envelope được ký riêng, gắn với `signal_id`, hash ChiefReasoning, `permission_request_sha256` (tham số lệnh chính xác) và — trên đường persisted-risk — `risk_snapshot_sha256` (snapshot mà risk gate đã kiểm tra), và chỉ khi `require_persisted_risk=True`. Quyết định hợp lệ chỉ dẫn tới `AUTHORIZED_ONCE`; lệnh không được tạo.

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

Bundle bắt buộc mang `evidence_envelope` mới; verdict, peer review, challenge ledger và ChiefReasoning chỉ được lấy từ `review_attestation` riêng của review-service (khóa và issuer thứ tư), nên ingress key không thể "viết thêm" review độc lập hay Chief CONFIRM. `dissent_rate`/`false_certainty_rate` lịch sử chỉ được lấy từ attestation đã ký của calibration-service (khóa riêng, issuer riêng). `mandatory_agents` và `compliance_agent_id` chỉ lấy từ chính sách. `compliance_status` được tính từ verdict `ComplianceAgent`, không từ trường của bundle. PermissionRequest phải khớp `signal_input` về `instrument` và `direction`. Persisted MT5 risk snapshot phải khớp lệnh về symbol và request-binding hash; `risk_value`/`risk_fraction` được tính lại từ `size × |giá vào xấu nhất − SL|` (xấu nhất giữa reviewed và reference price), tick size/tick value và `min(account_equity, account_balance)` rồi so với dung sai 1%; daily loss và drawdown được tính lại từ equity baseline, day-start equity phải khớp `balance − daily_pnl` theo cả hai chiều (1%), còn high-water mark không được thấp hơn day-start. Snapshot phải mang định danh tài khoản `account_login` (số nguyên `>= 1`) và `account_server` (chuỗi không rỗng); chính sách có thể ghim giá trị kỳ vọng, khi đó không khớp sẽ chặn. Vị thế phải nhất quán: `position_symbols` có độ dài đúng bằng `open_positions`, `correlated_open_positions <= open_positions`, `open_stop_risk_fraction > 0` bị cấm khi không có vị thế, và mỗi vị thế mở cần tick mới của đúng symbol — nếu không, rủi ro stop của vị thế là không xác định và snapshot bị chặn. Slippage lớn hơn 10% khoảng cách tới stop hoặc reference price vượt SL/TP sẽ chặn snapshot; RR còn được kiểm tra theo giá vào xấu nhất so với `min_expected_rr`. Snapshot bắt buộc có `open_stop_risk_fraction` là số `[0, 1]` (rủi ro stop chưa đóng tính từ giá hiện tại; thiếu, kiểu chuỗi hoặc `null` là DENY), và các hạn mức prospective cộng nó với daily-loss/drawdown đã thực hiện và rủi ro lệnh mới. Phê duyệt của con người cần `decision_id` và `nonce` dùng một lần, được tiêu thụ nguyên tử qua `InMemoryNonceStore` (test/demo) hoặc `PostgresNonceStore`; nonce dùng một lần cho mỗi operator, còn bản ghi insert-only riêng trong `authorization_permissions` (migration 006) gắn với hash do server tạo từ chính lệnh (signal/instrument/direction/size/entry/SL/TP/mode), không phải `permission_id` của client, nên cả replay nonce lẫn đổi tên permission đều không thể xác nhận cùng một lệnh hai lần. `PostgresNonceStore` lưu tombstone insert-only: ứng dụng không xóa dòng khi xử lý request, chỉ được dọn thủ công sau `expires_at` và trong cửa sổ retention đã thống nhất. Evidence-envelope không dùng một lần: việc lặp lại bị giới hạn bởi `min(max_age_seconds, ttl_seconds)` (mặc định 300 giây, trần envelope 3600 giây), còn tính idempotent do nonce/permission store đảm bảo.

Các endpoint chính sách được bảo vệ yêu cầu `X-Bridge-Key` và body `{"payload": ..., "envelope": ...}` có chữ ký HMAC hợp lệ. Nếu `MT5_BRIDGE_TOKEN`, evidence key/issuer, governance key/issuer hoặc review key/issuer chưa được cấu hình hoặc các signing domain trùng nhau, endpoint trả HTTP 503; thiếu hoặc sai `X-Bridge-Key` — HTTP 401; evidence signature sai/bị sửa, hoặc `review_attestation` thiếu/không thuộc về mình (`review_attestation_invalid`) — HTTP 403; schema body sai — HTTP 422. API tính lại `reviewer_count`, consensus và `open_challenges` từ bản ghi thô và bỏ qua aggregate do client khai báo; metadata verdict (signal_id, `input_hash` so với `signal_input` canonical, `created_at`/TTL) được kiểm tra lại giống pipeline. `/v1/permission/check` cố ý trả HTTP 423 và yêu cầu endpoint persisted-risk `/v1/risk/permission/check`. `/health` không chứa secret và trung thực cho biết những kiểm tra nào đã được cấu hình (`evidence`/`governance`/`operator`/`review` verification, tính tách biệt của bridge token, kill-switch admin và signing key/issuer), nhưng luôn báo execution disabled. Bridge key là bảo vệ cho transport localhost, không phải 2FA/chữ ký số của người dùng. `/v1/authorization/check` cố ý trả HTTP 423: nó không chấp nhận đối tượng PermissionResult do client truyền vào như bằng chứng của một permission đã cấp, vì chưa có server-side permission store dài hạn và bộ xác minh 2FA/signature thật.

## Agent MT5: observe/paper bên trên terminal

Package riêng `src/critical_consensus/mt5_agent/` (không nằm trong cơ chế cấp phép) đọc trạng
thái terminal **không gửi lệnh**, áp dụng các cổng fail-closed và ghi mọi quyết định vào nhật ký
SQLite dạng chuỗi băm. **Không có API đặt lệnh trong package**: chính sách từ chối `mode: live`,
mỗi bản ghi nhật ký đều mang `broker_order_sent: false`, và test
`test_agent_never_reports_a_broker_order` cùng phép quét mã nguồn `mt5_agent` với allowlist các
lời gọi terminal không gửi lệnh (`symbol_spec()` chỉ gọi `symbol_select()` khi symbol chưa có
trong Market Watch, việc này thêm nó vào danh sách symbol của terminal) và kiểm tra `dir(MT5Agent)`
khoá chặt điều này.

```bash
cp config/mt5_agent.example.yaml config/mt5_agent.yaml
python scripts/run_mt5_agent.py --db agent_data/mt5_agent.db run \
  --policy config/mt5_agent.yaml --source csv --csv export_eurusd_h1.csv \
  --source-timezone Europe/London --balance 10000 --language vi --execution confirm
python scripts/run_mt5_agent.py --db agent_data/mt5_agent.db report
```

Thứ tự cổng trong một vòng: đồng hồ UTC → kill switch → tài khoản demo kèm login/server cố định →
mốc equity và hạn mức ngày → cửa sổ giao dịch và blackout → giới hạn vị thế → độ mới của báo giá →
rủi ro stop của các vị thế đang mở và lỗ đã chốt trong ngày → tín hiệu SMA/ATR (kể cả giới hạn
spread) → tỷ lệ rủi ro/lợi nhuận và khối lượng tính từ khoảng cách stop và `volume_step` → tổng
rủi ro stop cộng lỗ trong ngày → chế độ thực thi
→ báo giá lại sau khi xác nhận (lệch tối đa 10% khoảng cách stop, tính lại RR và khối lượng, không
vượt lot đã duyệt). Chế độ `paper` khớp lệnh mô phỏng trên nến lịch
sử, không nhìn trước (stop được kiểm trước take-profit trong cùng một nến); chế độ `observe` chỉ ghi
nhận đề xuất. `--execution confirm` (mặc định) hỏi `y/n/a/q` trước mỗi vị thế, `--execution auto`
chỉ được phép trên tài khoản demo; `--language ru|en|vi` đổi toàn bộ giao diện (Nga, Anh, Việt), còn
nhật ký chỉ lưu mã máy.

Đọc terminal thật trên máy này là **ĐÃ XÁC MINH từ 2026-10-31**: `MetaTrader5` 5.0.6231,
`terminal64.exe` build 6296, tài khoản demo 10013404855 trên `MetaQuotes-Demo` (số dư 10.00 USD),
specification thật và 120 nến H1 của EURUSD/GBPUSD/AUDUSD/NZDUSD/XAUUSD/XAGUSD. Các con số trong
test vẫn là chuỗi tổng hợp và **không phải** ước tính lợi nhuận.

Lệnh chỉ có thể đi vào terminal qua con đường demo nằm ngoài package:
`scripts/run_mt5_demo_trading.py` đặt đúng một lệnh vào cầu loopback
`scripts/mt5_demo_bridge.py`, còn `OrderSend` do expert advisor bên trong terminal gọi; trong các
script Python không có bất kỳ lời gọi giao dịch MT5 nào. EA demo được biên dịch
(`scripts/build_demo_ea.py`: MetaEditor báo 0 lỗi, 0 cảnh báo) và cài vào terminal, còn
`scripts/setup_demo_terminal.py` chuẩn bị host cho việc đó (mặc định là dry-run, yêu cầu terminal
đang đóng, sửa `[Experts]` trong `Config/common.ini` và lưu bản backup). **Vẫn CHƯA XÁC MINH và
cần thao tác tay:** gắn expert advisor vào biểu đồ và một lệnh demo thật từ broker — ngày
2026-10-31 thị trường đóng cửa nên mọi vòng lặp chặn ở `QUOTE_STALE`, và số dư 10 USD không đủ cho
một giao dịch đúng chính sách rủi ro 0.5% (một lô tối thiểu rủi ro 1.93–4.24 USD). Hướng dẫn đầy đủ
và quan sát thực tế: [docs/MT5_AGENT.vi.md](docs/MT5_AGENT.vi.md) (bản tiếng Nga: [docs/MT5_AGENT.md](docs/MT5_AGENT.md),
bản tiếng Anh: [docs/MT5_AGENT.en.md](docs/MT5_AGENT.en.md)).

## Những gì chưa có chức năng production

Đây chưa phải hệ thống giao dịch hoàn chỉnh. Chưa có API key/model thật cho chín agent bắt buộc và nguồn market/news độc lập đã xác nhận. Peer-review/challenge service và provider callback đã được nối, nhưng hoạt động thực tế chưa được kiểm chứng cho đến khi có khóa và chạy trên tín hiệu thật. Chưa có metric dissent/false-certainty đã xác nhận và hiệu chuẩn out-of-sample có nhãn. Walk-forward evaluator và lưu forecast đã được triển khai, nhưng chưa thu được kết quả hiệu chuẩn thực. CLI Chief Gate đầy đủ chỉ nhận evidence packet nội bộ, yêu cầu review attestation được ký riêng, không tạo review/challenge, không làm giả credibility nguồn và không nhận human approval. Chưa có TOTP/signature verifier riêng, triển khai production worker/API và dispatch lệnh thật. Không kết nối tài khoản thật. EA demo đã được MetaEditor biên dịch với 0 lỗi, 0 cảnh báo và đã cài vào terminal (2026-10-31), nhưng chưa lần nào in `OnInit` ra log của expert advisor — nghĩa là việc gắn vào biểu đồ và một lệnh demo thật vẫn CHƯA XÁC MINH; kiểm tra tĩnh không thay thế các thử nghiệm đó.

## Invariant chuẩn tắc

1. `VETO` luôn chặn.
2. Một `REJECT`, `ABSTAIN` không lý do hoặc challenge còn mở không được đi qua certainty gate.
3. `FAIL` -> `GATHER_MORE`; deadlock -> `ESCALATE`; cả hai đều không có lệnh.
4. `ChiefAgent CONFIRM` không thể xảy ra khi các bước `a`–`i` chưa hoàn tất.
5. `PermissionGate` không thay thế user authorization.
6. `UserAuthorizationGate` không tạo lệnh, chỉ trả về chấp thuận cho lớp thực thi riêng.
7. `OrderCommand` dùng một lần và idempotent; kết quả không xác định sau khi gửi cần reconcile, không retry mù quáng.
8. Tin đồn công khai không được coi là sự thật đã xác nhận; `MNPI-SUSPECT` luôn dẫn tới VETO.
9. Evidence bundle không được ký, hết hạn hoặc bị sửa sẽ không tới được bất kỳ gate nào.
10. Health-metric lịch sử không có attestation đã ký của calibration-service sẽ chặn certainty PASS.
11. Peer review, challenge ledger và ChiefReasoning không có chữ ký review-domain sẽ chặn evaluation; ingress key không thể ký chúng.
12. Cùng một xác nhận của con người không thể dùng hai lần — cả theo nonce lẫn theo lệnh: tombstone gắn với hash tham số lệnh do server tạo, không phải `permission_id`.

## Kiểm tra bộ khung hiện tại

- Kiểm tra cục bộ nhánh `harden/10of10` trên Python 3.12 (Windows): `pytest -q` — **479 passed, 10 skipped**; `python scripts/check_mql5_safety.py` — **423 checks (including 85 mutation rejections)** (biên dịch EA và demo execution vẫn NOT PROVEN). Tất cả skip cần `TEST_DATABASE_URL` và/hoặc `TEST_REDIS_URL` và chưa được chạy ở đây. Kết quả GitHub Actions thuộc các commit trước và cần một run riêng sau khi push để xác nhận cho nhánh này; chúng không tự động kế thừa.
- Các test kiểm tra fail-closed gate, HMAC evidence/governance envelope, authorization nonce dùng một lần, ràng buộc integrity giữa ChiefReasoning/consensus/source summary và PermissionRequest, việc phát hành human review packet đầy đủ, hash chain, API key gate, serialization canonical nghiêm ngặt và Redis Streams helper.
- Test xanh chứng minh hành vi của hàm cục bộ trên input tổng hợp, nhưng không kiểm tra broker, biên dịch MQL5, hàng đợi thật, dữ liệu thị trường hay khả năng sinh lời.
- ChiefReasoning hash được tính từ JSON canonical không có trường `reasoning_hash`; thay đổi bất kỳ bước nào làm hash mất hiệu lực.

## Điểm tích hợp chính thức

- Python integration MetaTrader 5: https://www.mql5.com/en/docs/python_metatrader5
- MQL5 `OrderSend`: https://www.mql5.com/en/docs/trading/ordersend
- MQL5 network / `WebRequest`: https://www.mql5.com/en/docs/network
- Redis Streams: https://redis.io/docs/latest/develop/data-types/streams/
