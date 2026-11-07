"""Fully Vietnamese web UI for the paper-betting sandbox (no external assets)."""
from __future__ import annotations

PAGE = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sân chơi cá cược demo — Critical Consensus</title>
<style>
  :root { color-scheme: dark; --bg:#0d1117; --card:#161b22; --line:#30363d;
          --text:#e6edf3; --muted:#8b949e; --green:#3fb950; --red:#f85149;
          --amber:#d29922; --blue:#58a6ff; }
  * { box-sizing: border-box; }
  body { margin:0; padding:0 12px 48px; background:var(--bg); color:var(--text);
         font:15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  header { max-width:1100px; margin:0 auto; padding:24px 0 8px; }
  h1 { font-size:22px; margin:0 0 4px; }
  h2 { font-size:17px; margin:0 0 12px; }
  .sub { color:var(--muted); font-size:13px; }
  .banner { max-width:1100px; margin:10px auto 18px; padding:12px 14px; border:1px solid var(--amber);
            background:#2b2110; color:#f0d48a; border-radius:10px; font-size:13px; }
  main { max-width:1100px; margin:0 auto; display:grid; gap:16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; }
  .row { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
  .stat { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; }
  .stat div { background:#0f141b; border:1px solid var(--line); border-radius:9px; padding:10px; }
  .stat b { display:block; font-size:18px; }
  .stat span { color:var(--muted); font-size:12px; }
  input, select, button { font:inherit; border-radius:8px; border:1px solid var(--line);
                          background:#0f141b; color:var(--text); padding:8px 10px; }
  input[type=number] { width:150px; }
  button { cursor:pointer; }
  button:hover { border-color:var(--blue); }
  button.primary { background:#1f6feb; border-color:#1f6feb; color:#fff; }
  button.danger { background:#5a1e1e; border-color:#8b2c2c; }
  button.small { padding:4px 8px; font-size:12px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:7px 6px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { color:var(--muted); font-weight:600; font-size:12px; }
  .team { font-weight:600; }
  .odds { color:var(--blue); white-space:nowrap; }
  .pos { color:var(--green); }
  .neg { color:var(--red); }
  .muted { color:var(--muted); }
  .pill { display:inline-block; padding:1px 7px; border-radius:999px; font-size:11px;
          border:1px solid var(--line); }
  pre { white-space:pre-wrap; background:#0f141b; border:1px solid var(--line);
        border-radius:9px; padding:12px; margin:10px 0 0; font-size:13px; }
  .toast { position:fixed; right:14px; bottom:14px; max-width:420px; padding:10px 14px;
           border-radius:9px; background:#1f6feb; color:#fff; display:none; z-index:20; }
  .toast.error { background:#8b2c2c; }
  @media (max-width:700px) { td .row { gap:4px; } }
</style>
</head>
<body>
<header>
  <h1>Sân chơi cá cược bóng đá — TIỀN DEMO</h1>
  <div class="sub">Dữ liệu lịch sử thật từ football-data.co.uk · ví demo lưu cục bộ · không kết nối nhà cái</div>
</header>
<div class="banner">
  <b>Chỉ là mô phỏng.</b> Không có tiền thật, không có nạp/rút, không có cá cược thật.
  Cá cược có rủi ro và quá khứ không bảo đảm tương lai. Không có lợi nhuận được bảo đảm. 18+.
  Nếu bạn hoặc người thân gặp vấn đề về cờ bạc, hãy tìm hỗ trợ tại địa phương.
</div>
<main>
  <section class="card">
    <h2>Ví demo</h2>
    <div class="stat" id="wallet-stats"></div>
  </section>

  <section class="card">
    <h2>Tải dữ liệu thật</h2>
    <p class="muted">Nguồn: football-data.co.uk (CSV lịch sử, dùng cho nghiên cứu cá nhân).
      Mặc định Ngoại hạng Anh mùa 2024/25 (mã 2425, giải E0).</p>
    <div class="row">
      <label>Mùa <input id="season" value="2425" size="6" maxlength="4"></label>
      <label>Giải <input id="division" value="E0" size="4" maxlength="4"></label>
      <button class="primary" id="download">Tải dữ liệu</button>
      <span id="data-status" class="muted"></span>
    </div>
  </section>

  <section class="card">
    <h2>Trận đấu (kết quả đang được ẩn)</h2>
    <div class="row" style="margin-bottom:10px">
      <label>Quy tắc cược
        <select id="mode">
          <option value="flat">Flat 1% số dư</option>
          <option value="kelly">Kelly 25% (tối đa 2%)</option>
        </select>
      </label>
      <label>Số tiền (bỏ trống = theo quy tắc)
        <input id="stake" type="number" min="1" placeholder="tự động">
      </label>
      <span class="muted">Chỉ đặt khi lợi thế mô hình ≥ 2% và tỷ lệ cược trong 1.5–10.</span>
    </div>
    <div id="matches" class="muted">Đang tải…</div>
  </section>

  <section class="card">
    <h2>Cược đang chờ</h2>
    <div id="pending" class="muted">Chưa có cược nào.</div>
  </section>

  <section class="card">
    <h2>Lịch sử</h2>
    <div id="history" class="muted">Chưa có cược nào.</div>
  </section>

  <section class="card">
    <h2>Báo cáo</h2>
    <div class="row"><button id="report-btn">Tính báo cáo</button></div>
    <pre id="report">Bấm “Tính báo cáo” để xem ROI, tỷ lệ thắng, Brier và CLV.</pre>
  </section>

  <section class="card">
    <h2>Backtest walk-forward</h2>
    <p class="muted">Mỗi trận chỉ dùng dữ liệu trước đó để dự đoán; không nhìn trước kết quả.
      Đây là thước đo trung thực hơn so với đặt cược thủ công.</p>
    <div class="row">
      <button class="primary" id="backtest">Chạy backtest</button>
      <span class="muted">Khởi động 80 trận, hồi quy bán rã 180 ngày.</span>
    </div>
    <pre id="backtest-out">Chưa chạy.</pre>
  </section>

  <section class="card">
    <h2>Đặt lại ví demo</h2>
    <div class="row">
      <button class="danger" id="reset">Đặt lại về 1.000.000 ₫</button>
      <button id="change-key">Đổi mã truy cập</button>
    </div>
  </section>
</main>
<div class="toast" id="toast"></div>
<script>
const $ = (id) => document.getElementById(id);
let sandboxKey = localStorage.getItem('sandboxKey') || '';

function headers(extra) {
  const h = Object.assign({ 'Content-Type': 'application/json' }, extra || {});
  if (sandboxKey) h['X-Sandbox-Key'] = sandboxKey;
  return h;
}
function toast(message, isError) {
  const el = $('toast');
  el.textContent = message;
  el.className = 'toast' + (isError ? ' error' : '');
  el.style.display = 'block';
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.style.display = 'none'; }, 5000);
}
async function api(path, options) {
  const opts = Object.assign({}, options || {});
  opts.headers = headers(opts.headers);
  const response = await fetch(path, opts);
  if (response.status === 401) {
    const key = prompt('Nhập mã truy cập (X-Sandbox-Key):');
    if (key) {
      sandboxKey = key.trim();
      localStorage.setItem('sandboxKey', sandboxKey);
      return api(path, options);
    }
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    const message = typeof detail === 'string' ? detail
      : (detail && (detail.message_vi || detail.code)) || ('HTTP ' + response.status);
    throw new Error(message);
  }
  return data;
}
function fmt(value) {
  return new Intl.NumberFormat('vi-VN').format(Math.round(value)) + ' ₫';
}
function pct(value) {
  return (Number(value) * 100).toFixed(1) + '%';
}
function edgeClass(value) {
  return Number(value) >= 0 ? 'pos' : 'neg';
}
function td(text, className) {
  const cell = document.createElement('td');
  if (text instanceof Node) cell.appendChild(text); else cell.textContent = text;
  if (className) cell.className = className;
  return cell;
}
function button(label, className, handler) {
  const el = document.createElement('button');
  el.textContent = label;
  el.className = className || 'small';
  el.addEventListener('click', handler);
  return el;
}

async function refresh() {
  try {
    const health = await api('/api/health');
    $('data-status').textContent = health.data_loaded
      ? ('Đã nạp ' + health.matches + ' trận · ' + health.season + '/' + health.division)
      : 'Chưa có dữ liệu. Bấm “Tải dữ liệu”.';
    const wallet = await api('/api/wallet');
    renderWallet(wallet);
    const matches = await api('/api/matches?limit=40');
    renderMatches(matches);
    const bets = await api('/api/bets?limit=200');
    renderBets(bets.bets);
  } catch (error) {
    toast(error.message, true);
  }
}

function renderWallet(data) {
  const box = $('wallet-stats');
  box.textContent = '';
  const items = [
    ['Số dư demo', fmt(data.wallet.balance_units)],
    ['Tổng cược đã đặt', fmt(data.stats.staked_units)],
    ['Đã trả về', fmt(data.stats.returned_units)],
    ['Lợi nhuận', fmt(data.stats.profit_units)],
    ['ROI', pct(data.stats.roi)],
    ['Thắng / Thua', data.stats.won + ' / ' + data.stats.lost],
    ['Đang chờ', String(data.stats.pending)],
  ];
  for (const [label, value] of items) {
    const div = document.createElement('div');
    const b = document.createElement('b'); b.textContent = value;
    const span = document.createElement('span'); span.textContent = label;
    div.appendChild(b); div.appendChild(span); box.appendChild(div);
  }
}

function renderMatches(data) {
  const box = $('matches');
  box.textContent = '';
  if (!data.matches || !data.matches.length) {
    box.textContent = 'Chưa có dữ liệu. Hãy bấm “Tải dữ liệu” ở trên.';
    return;
  }
  const table = document.createElement('table');
  const head = document.createElement('tr');
  for (const label of ['Ngày', 'Trận', 'Tỷ lệ (1X2)', 'Mô hình (1X2)', 'Lợi thế', 'Đặt cược demo']) {
    const th = document.createElement('th'); th.textContent = label; head.appendChild(th);
  }
  table.appendChild(head);
  for (const match of data.matches) {
    const tr = document.createElement('tr');
    tr.appendChild(td(match.date));
    const teams = document.createElement('div');
    teams.className = 'team';
    teams.textContent = match.home + ' — ' + match.away;
    const league = document.createElement('div');
    league.className = 'muted'; league.textContent = match.division;
    const teamCell = document.createElement('td');
    teamCell.appendChild(teams); teamCell.appendChild(league);
    tr.appendChild(teamCell);
    tr.appendChild(td(match.odds.home.toFixed(2) + ' / ' + match.odds.draw.toFixed(2) + ' / ' + match.odds.away.toFixed(2), 'odds'));
    tr.appendChild(td(pct(match.model.home) + ' / ' + pct(match.model.draw) + ' / ' + pct(match.model.away)));
    const edgeCell = document.createElement('td');
    for (const selection of ['home', 'draw', 'away']) {
      const span = document.createElement('div');
      span.className = edgeClass(match.edges[selection]);
      span.textContent = selection.toUpperCase() + ' ' + pct(match.edges[selection]);
      edgeCell.appendChild(span);
    }
    tr.appendChild(edgeCell);
    const actions = document.createElement('td');
    const row = document.createElement('div');
    row.className = 'row';
    for (const selection of ['HOME', 'DRAW', 'AWAY']) {
      const label = selection === 'HOME' ? 'Nhà' : selection === 'DRAW' ? 'Hòa' : 'Khách';
      row.appendChild(button(label + ' (' + match.odds[selection.toLowerCase()].toFixed(2) + ')', 'small',
        () => placeBet(match.match_id, selection)));
    }
    actions.appendChild(row);
    tr.appendChild(actions);
    table.appendChild(tr);
  }
  box.appendChild(table);
}

function renderBets(bets) {
  const pendingBox = $('pending');
  const historyBox = $('history');
  pendingBox.textContent = '';
  historyBox.textContent = '';
  const pending = bets.filter((bet) => bet.status === 'PENDING');
  const settled = bets.filter((bet) => bet.status !== 'PENDING');
  if (!pending.length) {
    pendingBox.textContent = 'Chưa có cược nào đang chờ.';
  } else {
    const table = document.createElement('table');
    const head = document.createElement('tr');
    for (const label of ['Trận', 'Chọn', 'Tiền cược', 'Tỷ lệ', 'Lợi thế', '']) {
      const th = document.createElement('th'); th.textContent = label; head.appendChild(th);
    }
    table.appendChild(head);
    const seen = new Set();
    for (const bet of pending) {
      const tr = document.createElement('tr');
      tr.appendChild(td(bet.home + ' — ' + bet.away));
      tr.appendChild(td(bet.selection === 'HOME' ? 'Nhà' : bet.selection === 'DRAW' ? 'Hòa' : 'Khách'));
      tr.appendChild(td(fmt(bet.stake_units)));
      tr.appendChild(td(bet.odds.toFixed(2), 'odds'));
      tr.appendChild(td(pct(bet.edge), edgeClass(bet.edge)));
      const cell = document.createElement('td');
      if (!seen.has(bet.match_id)) {
        seen.add(bet.match_id);
        cell.appendChild(button('Mở kết quả', 'small', () => settleMatch(bet.match_id)));
      }
      tr.appendChild(cell);
      table.appendChild(tr);
    }
    pendingBox.appendChild(table);
  }
  if (!settled.length) {
    historyBox.textContent = 'Chưa có cược nào đã chốt.';
    return;
  }
  const table = document.createElement('table');
  const head = document.createElement('tr');
  for (const label of ['Ngày', 'Trận', 'Chọn', 'Tiền', 'Tỷ lệ', 'Kết quả', 'Nhận về']) {
    const th = document.createElement('th'); th.textContent = label; head.appendChild(th);
  }
  table.appendChild(head);
  for (const bet of settled.slice(0, 60)) {
    const tr = document.createElement('tr');
    tr.appendChild(td(bet.match_date));
    tr.appendChild(td(bet.home + ' — ' + bet.away));
    tr.appendChild(td(bet.selection === 'HOME' ? 'Nhà' : bet.selection === 'DRAW' ? 'Hòa' : 'Khách'));
    tr.appendChild(td(fmt(bet.stake_units)));
    tr.appendChild(td(bet.odds.toFixed(2), 'odds'));
    tr.appendChild(td(bet.status === 'WON' ? 'Thắng' : 'Thua', bet.status === 'WON' ? 'pos' : 'neg'));
    tr.appendChild(td(fmt(bet.payout_units)));
    table.appendChild(tr);
  }
  historyBox.appendChild(table);
}

async function placeBet(matchId, selection) {
  const rawStake = $('stake').value.trim();
  const body = {
    match_id: matchId,
    selection: selection,
    mode: $('mode').value,
    idempotency_key: (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()),
  };
  if (rawStake) body.stake = parseInt(rawStake, 10);
  try {
    const result = await api('/api/bets', { method: 'POST', body: JSON.stringify(body) });
    const bet = result.bet;
    toast('Đã đặt cược demo: ' + fmt(bet.stake_units) + ' · ' + bet.selection
      + (result.stake_clamped ? ' (đã giới hạn theo quy tắc)' : ''));
    await refresh();
  } catch (error) {
    toast(error.message, true);
  }
}

async function settleMatch(matchId) {
  try {
    const result = await api('/api/matches/' + encodeURIComponent(matchId) + '/settle', { method: 'POST' });
    toast('Kết quả: ' + result.result + ' · đã chốt ' + result.settled.length + ' cược.');
    await refresh();
  } catch (error) {
    toast(error.message, true);
  }
}

function reportText(metrics, wallet) {
  const lines = [
    'Số cược đã chốt: ' + metrics.bets,
    'Tổng cược: ' + fmt(metrics.staked_units),
    'Đã trả về: ' + fmt(metrics.returned_units),
    'Lợi nhuận: ' + fmt(metrics.profit_units),
    'ROI: ' + pct(metrics.roi),
    'Tỷ lệ thắng: ' + pct(metrics.hit_rate) + ' (95% CI '
      + pct(metrics.hit_rate_ci95[0]) + '–' + pct(metrics.hit_rate_ci95[1]) + ')',
    'Brier (theo lựa chọn): ' + (metrics.brier_selection === null ? '—' : metrics.brier_selection.toFixed(4)),
    'Log loss: ' + (metrics.log_loss_selection === null ? '—' : metrics.log_loss_selection.toFixed(4)),
    'CLV trung bình: ' + (metrics.mean_clv === null ? '—' : pct(metrics.mean_clv)),
    'Số mẫu CLV: ' + metrics.clv_samples,
    'Drawdown tối đa: ' + fmt(metrics.max_drawdown_units),
    'Số dư hiện tại: ' + fmt(wallet.balance_units),
    '',
    'Lưu ý: mô phỏng trên dữ liệu lịch sử, không bảo đảm lợi nhuận.',
  ];
  return lines.join('\\n');
}

async function runReport() {
  try {
    const data = await api('/api/report');
    $('report').textContent = reportText(data.metrics, data.wallet);
  } catch (error) {
    toast(error.message, true);
  }
}

async function runBacktest() {
  try {
    $('backtest-out').textContent = 'Đang chạy…';
    const data = await api('/api/backtest', {
      method: 'POST',
      body: JSON.stringify({ mode: $('mode').value, warmup_matches: 80, half_life_days: 180 }),
    });
    const s = data.summary;
    const lines = [
      'Chế độ: ' + data.mode + ' · số trận xét: ' + data.matches,
      'Số cược: ' + s.bets,
      'Tổng cược: ' + fmt(s.staked_units),
      'Đã trả về: ' + fmt(s.returned_units),
      'Lợi nhuận: ' + fmt(s.profit_units),
      'ROI: ' + pct(s.roi),
      'Tỷ lệ thắng: ' + pct(s.hit_rate) + ' (95% CI '
        + pct(s.hit_rate_ci95[0]) + '–' + pct(s.hit_rate_ci95[1]) + ')',
      'CLV trung bình: ' + (s.mean_clv === null ? '—' : pct(s.mean_clv)),
      'Brier (theo lựa chọn): ' + (s.brier_selection === null ? '—' : s.brier_selection.toFixed(4)),
      'Drawdown tối đa: ' + fmt(s.max_drawdown_units),
      'Số dư cuối: ' + fmt(s.final_bankroll_units),
      '',
      'Đây là backtest trên dữ liệu quá khứ; không bảo đảm lợi nhuận tương lai.',
    ];
    $('backtest-out').textContent = lines.join('\\n');
  } catch (error) {
    $('backtest-out').textContent = 'Lỗi: ' + error.message;
  }
}

async function downloadData() {
  try {
    $('data-status').textContent = 'Đang tải…';
    const data = await api('/api/download', {
      method: 'POST',
      body: JSON.stringify({ season: $('season').value.trim(), division: $('division').value.trim() }),
    });
    toast('Đã nạp ' + data.matches + ' trận (' + data.season + '/' + data.division + ').');
    await refresh();
  } catch (error) {
    $('data-status').textContent = 'Lỗi tải dữ liệu.';
    toast(error.message, true);
  }
}

async function resetWallet() {
  if (!confirm('Đặt lại ví demo về 1.000.000 ₫? Toàn bộ cược demo sẽ bị xóa.')) return;
  try {
    await api('/api/reset', { method: 'POST', body: JSON.stringify({ confirm: 'RESET' }) });
    toast('Đã đặt lại ví demo.');
    await refresh();
  } catch (error) {
    toast(error.message, true);
  }
}

$('download').addEventListener('click', downloadData);
$('report-btn').addEventListener('click', runReport);
$('backtest').addEventListener('click', runBacktest);
$('reset').addEventListener('click', resetWallet);
$('change-key').addEventListener('click', () => {
  const key = prompt('Mã truy cập mới (để trống = xóa):', sandboxKey);
  if (key === null) return;
  sandboxKey = key.trim();
  if (sandboxKey) localStorage.setItem('sandboxKey', sandboxKey);
  else localStorage.removeItem('sandboxKey');
  refresh();
});
refresh();
</script>
</body>
</html>
"""


def render_index_page() -> str:
    return PAGE
