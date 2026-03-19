"""
Performance dashboard server.
Run: python dashboard/server.py
Then open: http://localhost:5000
"""

import json
import os
from datetime import datetime
from pathlib import Path
from collections import defaultdict

from flask import Flask, jsonify, render_template_string
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(dotenv_path=ROOT / "config" / ".env")

app = Flask(__name__)
TRADES_LOG = ROOT / "logs" / "trades.jsonl"


# ── Data loading ───────────────────────────────────────────

def load_trades() -> list[dict]:
    if not TRADES_LOG.exists():
        return []
    trades = []
    with open(TRADES_LOG) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    trades.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return sorted(trades, key=lambda t: t.get("timestamp", ""))


def compute_stats(trades: list[dict]) -> dict:
    executed = [t for t in trades if t.get("executed")]
    skipped  = [t for t in trades if not t.get("executed")]

    if not trades:
        return {
            "total_scanned": 0, "executed": 0, "skipped": 0,
            "total_deployed_usdc": 0, "avg_edge": 0, "avg_confidence": 0,
            "by_model": {}, "by_bet": {}, "skip_reasons": {},
            "timeline": [], "top_edges": [],
        }

    total_deployed = sum(t.get("size_usdc", 0) for t in executed)
    avg_edge = sum(t.get("edge", 0) for t in executed) / len(executed) if executed else 0
    avg_conf = sum(t.get("confidence", 0) for t in executed) / len(executed) if executed else 0

    # By model
    by_model = defaultdict(int)
    for t in trades:
        model = t.get("model", "unknown")
        label = "Haiku" if "haiku" in model else "Sonnet"
        by_model[label] += 1

    # By bet direction
    by_bet = defaultdict(int)
    for t in executed:
        by_bet[t.get("bet", "?")] += 1

    # Skip reasons
    skip_reasons = defaultdict(int)
    for t in skipped:
        reason = t.get("skip_reason", "unknown")
        # Shorten reason to category
        if "Confidence" in reason:
            skip_reasons["Low confidence"] += 1
        elif "Edge" in reason:
            skip_reasons["Low edge"] += 1
        elif "SKIP" in reason:
            skip_reasons["Claude SKIP"] += 1
        elif "Daily loss" in reason:
            skip_reasons["Daily limit"] += 1
        elif "position" in reason.lower():
            skip_reasons["Max positions"] += 1
        else:
            skip_reasons["Other"] += 1

    # Timeline — cumulative deployed per hour
    timeline = []
    cumulative = 0
    hour_buckets = defaultdict(float)
    for t in executed:
        ts = t.get("timestamp", "")[:13]  # YYYY-MM-DDTHH
        hour_buckets[ts] += t.get("size_usdc", 0)
    for hour in sorted(hour_buckets):
        cumulative += hour_buckets[hour]
        timeline.append({"hour": hour.replace("T", " "), "cumulative": round(cumulative, 2), "deployed": round(hour_buckets[hour], 2)})

    # Top edge opportunities
    top_edges = sorted(executed, key=lambda t: t.get("edge", 0), reverse=True)[:5]
    top_edges_clean = [{
        "question": t["question"][:70] + ("…" if len(t["question"]) > 70 else ""),
        "bet": t["bet"],
        "edge": round(t["edge"] * 100, 1),
        "confidence": round(t["confidence"] * 100, 0),
        "size": t.get("size_usdc", 0),
        "model": "Haiku" if "haiku" in t.get("model", "") else "Sonnet",
        "timestamp": t["timestamp"][:16].replace("T", " "),
    } for t in top_edges]

    return {
        "total_scanned": len(trades),
        "executed": len(executed),
        "skipped": len(skipped),
        "total_deployed_usdc": round(total_deployed, 2),
        "avg_edge": round(avg_edge * 100, 1),
        "avg_confidence": round(avg_conf * 100, 1),
        "by_model": dict(by_model),
        "by_bet": dict(by_bet),
        "skip_reasons": dict(skip_reasons),
        "timeline": timeline,
        "top_edges": top_edges_clean,
        "last_updated": datetime.now().strftime("%H:%M:%S"),
        "mode": trades[-1].get("mode", "paper").upper() if trades else "PAPER",
        "heartbeat": _read_heartbeat(),
    }


def _read_heartbeat() -> str:
    try:
        return (ROOT / "logs" / ".heartbeat").read_text().strip()[:16].replace("T", " ")
    except FileNotFoundError:
        return "not running"


# ── API endpoints ──────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    trades = load_trades()
    return jsonify(compute_stats(trades))

@app.route("/api/trades")
def api_trades():
    trades = load_trades()
    # Return last 50, most recent first
    return jsonify(list(reversed(trades[-50:])))

@app.route("/")
def dashboard():
    return render_template_string(HTML_TEMPLATE)


# ── HTML Template ──────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Bot — Performance</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {
    --bg:       #080c0f;
    --bg2:      #0d1318;
    --bg3:      #111920;
    --border:   #1e2d38;
    --dim:      #2a3f50;
    --text:     #c8dde8;
    --muted:    #4a6478;
    --accent:   #00e5a0;
    --accent2:  #00aaff;
    --warn:     #ffb347;
    --danger:   #ff4f6a;
    --yes:      #00e5a0;
    --no:       #ff4f6a;
    --font-mono: 'IBM Plex Mono', monospace;
    --font-sans: 'IBM Plex Sans', sans-serif;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-sans);
    font-size: 14px;
    line-height: 1.6;
    min-height: 100vh;
  }

  /* scanline effect */
  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.03) 2px, rgba(0,0,0,0.03) 4px);
    pointer-events: none;
    z-index: 1000;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 28px;
    border-bottom: 1px solid var(--border);
    background: var(--bg2);
  }

  .logo {
    font-family: var(--font-mono);
    font-size: 13px;
    font-weight: 600;
    color: var(--accent);
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .logo span { color: var(--muted); font-weight: 400; }

  .status-bar {
    display: flex;
    align-items: center;
    gap: 20px;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--muted);
  }

  .pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 3px 10px;
    border-radius: 2px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .pill.paper { background: rgba(255,179,71,0.12); color: var(--warn); border: 1px solid rgba(255,179,71,0.25); }
  .pill.live  { background: rgba(0,229,160,0.12);  color: var(--accent); border: 1px solid rgba(0,229,160,0.25); }

  .dot {
    width: 6px; height: 6px;
    border-radius: 50%;
    background: var(--accent);
    animation: pulse 2s ease-in-out infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
  }

  /* Main grid */
  .grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 1px;
    background: var(--border);
    border-bottom: 1px solid var(--border);
  }

  .stat-card {
    background: var(--bg2);
    padding: 20px 24px;
  }

  .stat-label {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 8px;
  }

  .stat-value {
    font-family: var(--font-mono);
    font-size: 28px;
    font-weight: 600;
    color: var(--text);
    line-height: 1;
  }

  .stat-value.green { color: var(--accent); }
  .stat-value.blue  { color: var(--accent2); }
  .stat-value.warn  { color: var(--warn); }

  .stat-sub {
    font-size: 11px;
    color: var(--muted);
    margin-top: 6px;
    font-family: var(--font-mono);
  }

  /* Content area */
  .content {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1px;
    background: var(--border);
  }

  .panel {
    background: var(--bg2);
    padding: 20px 24px;
  }

  .panel.full { grid-column: 1 / -1; }

  .panel-title {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 16px;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
  }

  /* Chart containers */
  .chart-wrap { position: relative; height: 180px; }

  /* Trade feed */
  .trade-feed { display: flex; flex-direction: column; gap: 0; }

  .trade-row {
    display: grid;
    grid-template-columns: 110px 48px 1fr 60px 60px;
    gap: 12px;
    align-items: center;
    padding: 10px 0;
    border-bottom: 1px solid var(--border);
    font-family: var(--font-mono);
    font-size: 11px;
    transition: background 0.15s;
    cursor: default;
  }

  .trade-row:hover { background: var(--bg3); margin: 0 -24px; padding: 10px 24px; }
  .trade-row:last-child { border-bottom: none; }

  .trade-row.skipped { opacity: 0.45; }

  .trade-time { color: var(--muted); font-size: 10px; }

  .bet-badge {
    display: inline-block;
    padding: 2px 7px;
    border-radius: 2px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-align: center;
  }

  .bet-badge.YES  { background: rgba(0,229,160,0.15); color: var(--yes); border: 1px solid rgba(0,229,160,0.3); }
  .bet-badge.NO   { background: rgba(255,79,106,0.15); color: var(--no); border: 1px solid rgba(255,79,106,0.3); }
  .bet-badge.SKIP { background: rgba(74,100,120,0.2); color: var(--muted); border: 1px solid var(--dim); }

  .trade-question {
    color: var(--text);
    font-size: 11px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    font-family: var(--font-sans);
  }

  .trade-question.skipped { color: var(--muted); }

  .trade-edge {
    color: var(--accent);
    text-align: right;
    font-size: 11px;
  }

  .trade-size {
    text-align: right;
    color: var(--text);
  }

  /* Top edges table */
  .edge-table { width: 100%; border-collapse: collapse; }
  .edge-table th {
    font-family: var(--font-mono);
    font-size: 9px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--muted);
    text-align: left;
    padding: 0 8px 10px 0;
    border-bottom: 1px solid var(--border);
  }

  .edge-table td {
    padding: 9px 8px 9px 0;
    border-bottom: 1px solid var(--border);
    font-size: 12px;
    vertical-align: middle;
  }

  .edge-table tr:last-child td { border-bottom: none; }

  .edge-bar-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .edge-bar {
    height: 4px;
    border-radius: 2px;
    background: var(--accent);
    min-width: 4px;
    max-width: 80px;
    transition: width 0.5s ease;
  }

  .edge-num {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--accent);
    min-width: 36px;
  }

  .model-tag {
    font-family: var(--font-mono);
    font-size: 9px;
    color: var(--muted);
    background: var(--bg3);
    padding: 2px 5px;
    border-radius: 2px;
    border: 1px solid var(--border);
  }

  /* Skip reasons */
  .reason-list { display: flex; flex-direction: column; gap: 10px; }

  .reason-row { display: flex; align-items: center; gap: 10px; }
  .reason-label { font-size: 12px; color: var(--text); min-width: 130px; }
  .reason-bar-wrap { flex: 1; height: 6px; background: var(--bg3); border-radius: 3px; }
  .reason-bar { height: 100%; border-radius: 3px; background: var(--dim); }
  .reason-count { font-family: var(--font-mono); font-size: 11px; color: var(--muted); min-width: 24px; text-align: right; }

  /* Loading state */
  .loading {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 120px;
    color: var(--muted);
    font-family: var(--font-mono);
    font-size: 12px;
    letter-spacing: 0.08em;
  }

  footer {
    padding: 12px 28px;
    border-top: 1px solid var(--border);
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--muted);
    display: flex;
    justify-content: space-between;
    background: var(--bg2);
  }
</style>
</head>
<body>

<header>
  <div class="logo">POLY<span>/</span>BOT &nbsp; PERFORMANCE</div>
  <div class="status-bar">
    <span id="mode-pill" class="pill paper">● PAPER</span>
    <span>BOT: <span id="heartbeat-val">—</span></span>
    <span>UPDATED: <span id="last-updated">—</span></span>
    <span><div class="dot"></div></span>
  </div>
</header>

<!-- KPI row -->
<div class="grid" id="kpi-grid">
  <div class="stat-card">
    <div class="stat-label">Signals Scanned</div>
    <div class="stat-value" id="kpi-scanned">—</div>
    <div class="stat-sub" id="kpi-scanned-sub">loading…</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Trades Executed</div>
    <div class="stat-value green" id="kpi-executed">—</div>
    <div class="stat-sub" id="kpi-exec-rate">—</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">USDC Deployed</div>
    <div class="stat-value blue" id="kpi-deployed">—</div>
    <div class="stat-sub" id="kpi-deployed-sub">paper positions</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Avg Edge</div>
    <div class="stat-value warn" id="kpi-edge">—</div>
    <div class="stat-sub" id="kpi-conf">avg confidence —</div>
  </div>
</div>

<!-- Charts + tables -->
<div class="content">

  <!-- Timeline chart -->
  <div class="panel">
    <div class="panel-title">Cumulative USDC Deployed</div>
    <div class="chart-wrap"><canvas id="timeline-chart"></canvas></div>
  </div>

  <!-- Bet direction + model breakdown -->
  <div class="panel">
    <div class="panel-title">Bet Direction &amp; Model Usage</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;height:180px">
      <div class="chart-wrap"><canvas id="bet-chart"></canvas></div>
      <div class="chart-wrap"><canvas id="model-chart"></canvas></div>
    </div>
  </div>

  <!-- Top edge plays -->
  <div class="panel">
    <div class="panel-title">Top Edge Plays</div>
    <table class="edge-table">
      <thead>
        <tr>
          <th>Market</th>
          <th>Bet</th>
          <th>Edge</th>
          <th>Size</th>
          <th>Model</th>
        </tr>
      </thead>
      <tbody id="top-edges-body">
        <tr><td colspan="5" class="loading">loading…</td></tr>
      </tbody>
    </table>
  </div>

  <!-- Skip reasons -->
  <div class="panel">
    <div class="panel-title">Skip Reasons</div>
    <div class="reason-list" id="skip-reasons">
      <div class="loading">loading…</div>
    </div>
  </div>

  <!-- Live trade feed -->
  <div class="panel full">
    <div class="panel-title">Recent Signals — Last 20</div>
    <div class="trade-feed" id="trade-feed">
      <div class="loading">loading…</div>
    </div>
  </div>

</div>

<footer>
  <span>polymarket-claude-bot &nbsp;·&nbsp; claude-haiku-4-5 + claude-sonnet-4-6</span>
  <span>Refreshes every 30s &nbsp;·&nbsp; <a href="/api/stats" style="color:var(--muted)">api/stats</a> &nbsp;·&nbsp; <a href="/api/trades" style="color:var(--muted)">api/trades</a></span>
</footer>

<script>
let timelineChart, betChart, modelChart;

const CHART_DEFAULTS = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: { legend: { display: false } },
};

function initCharts() {
  // Timeline
  const tlCtx = document.getElementById('timeline-chart').getContext('2d');
  timelineChart = new Chart(tlCtx, {
    type: 'line',
    data: { labels: [], datasets: [{
      data: [],
      borderColor: '#00e5a0',
      backgroundColor: 'rgba(0,229,160,0.06)',
      borderWidth: 1.5,
      fill: true,
      tension: 0.3,
      pointRadius: 3,
      pointBackgroundColor: '#00e5a0',
    }]},
    options: {
      ...CHART_DEFAULTS,
      scales: {
        x: { ticks: { color: '#4a6478', font: { family: 'IBM Plex Mono', size: 9 } }, grid: { color: '#1e2d38' } },
        y: { ticks: { color: '#4a6478', font: { family: 'IBM Plex Mono', size: 9 }, callback: v => '$'+v }, grid: { color: '#1e2d38' } }
      }
    }
  });

  // Bet direction donut
  const betCtx = document.getElementById('bet-chart').getContext('2d');
  betChart = new Chart(betCtx, {
    type: 'doughnut',
    data: { labels: ['YES', 'NO'], datasets: [{ data: [0, 0], backgroundColor: ['rgba(0,229,160,0.7)', 'rgba(255,79,106,0.7)'], borderColor: ['#00e5a0','#ff4f6a'], borderWidth: 1 }] },
    options: {
      ...CHART_DEFAULTS,
      cutout: '65%',
      plugins: { legend: { display: true, position: 'bottom', labels: { color: '#4a6478', font: { family: 'IBM Plex Mono', size: 9 }, boxWidth: 8 } } }
    }
  });

  // Model donut
  const modelCtx = document.getElementById('model-chart').getContext('2d');
  modelChart = new Chart(modelCtx, {
    type: 'doughnut',
    data: { labels: ['Haiku', 'Sonnet'], datasets: [{ data: [0, 0], backgroundColor: ['rgba(0,170,255,0.7)', 'rgba(150,100,255,0.7)'], borderColor: ['#00aaff','#9664ff'], borderWidth: 1 }] },
    options: {
      ...CHART_DEFAULTS,
      cutout: '65%',
      plugins: { legend: { display: true, position: 'bottom', labels: { color: '#4a6478', font: { family: 'IBM Plex Mono', size: 9 }, boxWidth: 8 } } }
    }
  });
}

function updateStats(s) {
  // KPIs
  document.getElementById('kpi-scanned').textContent = s.total_scanned;
  document.getElementById('kpi-scanned-sub').textContent = `${s.skipped} skipped`;
  document.getElementById('kpi-executed').textContent = s.executed;
  document.getElementById('kpi-exec-rate').textContent = s.total_scanned > 0 ? `${Math.round(s.executed/s.total_scanned*100)}% execution rate` : '—';
  document.getElementById('kpi-deployed').textContent = `$${s.total_deployed_usdc}`;
  document.getElementById('kpi-deployed-sub').textContent = `${s.mode} positions`;
  document.getElementById('kpi-edge').textContent = `${s.avg_edge}%`;
  document.getElementById('kpi-conf').textContent = `avg confidence ${s.avg_confidence}%`;

  // Mode pill
  const pill = document.getElementById('mode-pill');
  pill.className = `pill ${s.mode.toLowerCase()}`;
  pill.textContent = `● ${s.mode}`;

  // Heartbeat
  document.getElementById('heartbeat-val').textContent = s.heartbeat;
  document.getElementById('last-updated').textContent = s.last_updated;

  // Timeline chart
  if (s.timeline.length > 0) {
    timelineChart.data.labels = s.timeline.map(t => t.hour.slice(-5));
    timelineChart.data.datasets[0].data = s.timeline.map(t => t.cumulative);
    timelineChart.update('none');
  }

  // Bet chart
  betChart.data.datasets[0].data = [s.by_bet['YES'] || 0, s.by_bet['NO'] || 0];
  betChart.update('none');

  // Model chart
  modelChart.data.datasets[0].data = [s.by_model['Haiku'] || 0, s.by_model['Sonnet'] || 0];
  modelChart.update('none');

  // Top edges table
  const tbody = document.getElementById('top-edges-body');
  if (s.top_edges.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" style="color:#4a6478;font-family:monospace;padding:16px 0">No executed trades yet</td></tr>';
  } else {
    tbody.innerHTML = s.top_edges.map(t => `
      <tr>
        <td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px">${t.question}</td>
        <td><span class="bet-badge ${t.bet}">${t.bet}</span></td>
        <td>
          <div class="edge-bar-wrap">
            <div class="edge-bar" style="width:${Math.min(t.edge*3,80)}px"></div>
            <span class="edge-num">${t.edge}%</span>
          </div>
        </td>
        <td style="font-family:monospace;font-size:11px;color:#c8dde8">$${t.size}</td>
        <td><span class="model-tag">${t.model}</span></td>
      </tr>`).join('');
  }

  // Skip reasons
  const reasons = s.skip_reasons;
  const maxCount = Math.max(...Object.values(reasons), 1);
  const reasonEl = document.getElementById('skip-reasons');
  if (Object.keys(reasons).length === 0) {
    reasonEl.innerHTML = '<div style="color:#4a6478;font-size:12px">No skipped trades yet</div>';
  } else {
    reasonEl.innerHTML = Object.entries(reasons)
      .sort((a,b) => b[1]-a[1])
      .map(([label, count]) => `
        <div class="reason-row">
          <span class="reason-label">${label}</span>
          <div class="reason-bar-wrap">
            <div class="reason-bar" style="width:${Math.round(count/maxCount*100)}%"></div>
          </div>
          <span class="reason-count">${count}</span>
        </div>`).join('');
  }
}

function updateFeed(trades) {
  const feed = document.getElementById('trade-feed');
  const recent = trades.slice(0, 20);
  if (recent.length === 0) {
    feed.innerHTML = '<div class="loading">No signals yet — bot may not be running</div>';
    return;
  }
  feed.innerHTML = recent.map(t => {
    const executed = t.executed;
    const ts = t.timestamp.slice(0,16).replace('T',' ');
    const edge = t.edge > 0 ? `+${(t.edge*100).toFixed(1)}%` : '—';
    const size = executed ? `$${t.size_usdc}` : '—';
    return `
      <div class="trade-row ${executed ? '' : 'skipped'}">
        <span class="trade-time">${ts}</span>
        <span class="bet-badge ${t.bet}">${t.bet}</span>
        <span class="trade-question ${executed ? '' : 'skipped'}">${t.question}</span>
        <span class="trade-edge">${edge}</span>
        <span class="trade-size">${size}</span>
      </div>`;
  }).join('');
}

async function refresh() {
  try {
    const [statsRes, tradesRes] = await Promise.all([
      fetch('/api/stats'), fetch('/api/trades')
    ]);
    const stats  = await statsRes.json();
    const trades = await tradesRes.json();
    updateStats(stats);
    updateFeed(trades);
  } catch(e) {
    console.error('Refresh failed:', e);
  }
}

// Boot
initCharts();
refresh();
setInterval(refresh, 30000);  // refresh every 30 seconds
</script>
</body>
</html>"""


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    print(f"\n  Dashboard running at: http://localhost:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False)
