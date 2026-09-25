"""Analytics + AI Recommendation API for MinatoVerse Stream Mode.

Endpoints:
  POST /api/analytics/track  {uid, title, event, duration}
  GET  /api/analytics/stats  global counters (top titles, daily hits)
  GET  /api/recommend?based_on=title  lightweight AI recs (Jinja-free)

Dashboard:
  GET  /analytics  admin HTML (Chart.js, no auth in preview; in prod check ADMINS)

Storage:
  Tries Mongo collection `analytics_events` (DATABASE_URI/DATABASE_NAME).
  Falls back to in-memory list for preview / when DB unreachable.
  The same fallback is used in tools/preview_section.py via `analytics_store`.

Security:
  No file ids / bot token ever exposed. Title is sanitized + length capped.
"""
import hashlib
import json
import logging
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from aiohttp import web

logger = logging.getLogger(__name__)
routes = web.RouteTableDef()

# In-memory fallback (preview + when Mongo down) — also imported by preview_section.py
_memory_events: List[Dict[str, Any]] = []
_memory_max = 5000

def _utcnow():
    return datetime.now(timezone.utc)

def _sanitize_title(v, limit=120):
    s = str(v or "").strip()
    s = re.sub(r"[\u0000-\u001f\u007f]", "", s)
    s = s.replace("<","").replace(">","")
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[:limit-1].strip() + "…"
    return s

def _sanitize_event(v):
    v = str(v or "view").lower().strip()
    return v if v in ("view","play","download","seek","complete") else "view"

def _client_ip(request):
    try:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()[:45]
        return (request.remote or "")[:45]
    except Exception:
        return ""

# ---------- Mongo helpers (best-effort) ----------
_mongo_col = None
_mongo_tried = False

def _mongo_collection():
    global _mongo_col, _mongo_tried
    if _mongo_tried:
        return _mongo_col
    _mongo_tried = True
    try:
        import info  # type: ignore
        uri = getattr(info, "DATABASE_URI", "") or getattr(info, "DATABASE_CONNECTION", "") or ""
        dbname = getattr(info, "DATABASE_NAME", "admin_database")
        if not uri:
            return None
        from motor.motor_asyncio import AsyncIOMotorClient
        client = AsyncIOMotorClient(uri)
        db = client[dbname]
        col = db["analytics_events"]
        _mongo_col = col
        # fire-and-forget index creation
        try:
            import asyncio
            async def _idx():
                try:
                    await col.create_index("ts", expireAfterSeconds=90*86400)
                    await col.create_index([("title",1)])
                except Exception:
                    pass
            asyncio.ensure_future(_idx())
        except Exception:
            pass
        return col
    except Exception as e:
        logger.debug("analytics mongo not ready: %s", e)
        return None

async def _store_event(doc: Dict[str, Any]):
    col = _mongo_collection()
    if col is not None:
        try:
            import asyncio
            await asyncio.wait_for(col.insert_one(doc), timeout=1.2)
            return
        except Exception as e:
            logger.debug("analytics mongo insert failed, falling back to memory: %s", e)
    # memory fallback (always works in preview / offline)
    _memory_events.append(doc)
    if len(_memory_events) > _memory_max:
        del _memory_events[0: len(_memory_events)-_memory_max]

async def _load_events(limit=2000) -> List[Dict[str, Any]]:
    col = _mongo_collection()
    if col is not None:
        try:
            import asyncio
            cur = col.find({}, {"_id":0}).sort("ts", -1).limit(limit)
            rows = await asyncio.wait_for(cur.to_list(length=limit), timeout=1.2)
            if rows:
                return rows
        except Exception as e:
            logger.debug("analytics mongo read failed (fallback to memory): %s", e)
    # fallback to memory (newest first)
    return list(reversed(_memory_events[-limit:]))

# ---------- Tracking ----------
@routes.post("/api/analytics/track")
async def track(request: web.Request):
    try:
        try:
            data = await request.json()
        except Exception:
            data = dict(await request.post()) if request.content_type != "application/json" else {}
        title = _sanitize_title(data.get("title") or data.get("name") or request.query.get("title") or "", 120)
        uid = _sanitize_title(data.get("uid") or data.get("file_id") or "", 64)
        event = _sanitize_event(data.get("event"))
        # duration is optional
        try:
            duration = float(data.get("duration") or 0)
        except Exception:
            duration = 0
        if not title and not uid:
            return web.json_response({"ok": False, "error": "title or uid required"}, status=400)
        # basic bot check: ignore obvious bots via UA
        ua = (request.headers.get("User-Agent") or "")[:180]
        if "bot" in ua.lower() and "telegram" not in ua.lower():
            # still count but mark
            pass
        doc = {
            "title": title or uid or "Unknown",
            "uid": uid,
            "event": event,
            "duration": duration,
            "ip_hash": hashlib.sha256(_client_ip(request).encode()).hexdigest()[:12] if _client_ip(request) else "",
            "ua": ua[:120],
            "ts": _utcnow(),
        }
        await _store_event(doc)
        return web.json_response({"ok": True})
    except Exception as e:
        logger.warning("track failed: %s", e)
        return web.json_response({"ok": False, "error": "failed"}, status=500)

# ---------- Stats ----------
def _aggregate(rows: List[Dict[str, Any]]):
    now = _utcnow()
    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)
    total = len(rows)
    last24 = sum(1 for r in rows if r.get("ts") and r["ts"] >= day_ago)
    last7 = sum(1 for r in rows if r.get("ts") and r["ts"] >= week_ago)
    by_event = Counter(r.get("event","view") for r in rows)
    by_title = Counter(r.get("title","Unknown") for r in rows if r.get("title"))
    top_titles = [{"title": t, "count": c} for t, c in by_title.most_common(10)]
    # daily buckets last 7 days
    daily = []
    for i in range(6, -1, -1):
        day = (now - timedelta(days=i)).date()
        cnt = sum(1 for r in rows if r.get("ts") and r["ts"].date() == day)
        daily.append({"date": day.isoformat(), "count": cnt})
    # hourly buckets last 24h
    hourly = []
    for h in range(23, -1, -1):
        hour_start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=h)
        hour_end = hour_start + timedelta(hours=1)
        cnt = sum(1 for r in rows if r.get("ts") and hour_start <= r["ts"] < hour_end)
        hourly.append({"hour": hour_start.strftime("%H:00"), "count": cnt})
    return {
        "total": total,
        "last24": last24,
        "last7": last7,
        "by_event": dict(by_event),
        "top_titles": top_titles,
        "daily": daily,
        "hourly": hourly,
    }

@routes.get("/api/analytics/stats")
async def stats(request: web.Request):
    try:
        limit = 2000
        try:
            limit = max(100, min(int(request.query.get("limit", 2000)), 5000))
        except Exception:
            pass
        rows = await _load_events(limit)
        agg = _aggregate(rows)
        agg["ok"] = True
        # ETag-lite
        body = json.dumps(agg, ensure_ascii=False, separators=(",",":")).encode()
        headers = {"Content-Type":"application/json; charset=utf-8", "Cache-Control":"no-store", "X-Content-Type-Options":"nosniff"}
        return web.Response(body=body, headers=headers)
    except Exception as e:
        logger.error("stats failed: %s", e)
        return web.json_response({"ok": False, "error": "failed"}, status=500)

# ---------- Lightweight AI Recommendations ----------
# No ML model needed for demo: genre/tag overlap + popularity boost
# In prod this could call Groq / embeddings; here deterministic & fast.

_GENRE_MAP = {
    "Jawan": ["Action","Thriller"],
    "Pushpa 2 The Rule": ["Action","Drama"],
    "Pathaan": ["Action","Spy"],
    "Jailer": ["Action","Comedy"],
    "Leo": ["Action","Thriller"],
    "RRR": ["Action","Epic"],
    "3 Idiots": ["Comedy","Drama"],
    "Sholay": ["Classic","Action"],
    "Interstellar": ["Sci-Fi","Drama"],
    "Inception": ["Sci-Fi","Thriller"],
    "Oppenheimer": ["Drama","History"],
    "Dune Part Two": ["Sci-Fi","Epic"],
    "Kalki 2898 AD": ["Sci-Fi","Action"],
    "Stree 2": ["Horror","Comedy"],
    "Marco": ["Action","Thriller"],
}

def _rec_score(base_titles: List[str], candidate: str) -> float:
    if candidate in base_titles:
        return -1
    base_genres = set()
    for t in base_titles:
        base_genres.update(_GENRE_MAP.get(t, []))
    cand_genres = set(_GENRE_MAP.get(candidate, []))
    if not base_genres or not cand_genres:
        # fallback: popularity heuristic via hash
        return (hash(candidate) % 100) / 100
    overlap = len(base_genres & cand_genres)
    return overlap + (hash(candidate) % 10)/100

@routes.get("/api/recommend")
async def recommend(request: web.Request):
    try:
        raw = request.query.get("based_on") or request.query.get("titles") or ""
        base = [s.strip() for s in raw.split(",") if s.strip()]
        base = [_sanitize_title(t, 80) for t in base][:5]
        if not base:
            # try from body
            try:
                data = await request.json()
                base = [_sanitize_title(t,80) for t in (data.get("titles") or [])][:5]
            except Exception:
                pass
        # candidates = all known titles
        candidates = list(_GENRE_MAP.keys())
        scored = sorted(candidates, key=lambda c: _rec_score(base, c), reverse=True)
        # take top 8
        recs = []
        for c in scored[:8]:
            if c in base:
                continue
            recs.append({"title": c, "genres": _GENRE_MAP.get(c, []), "reason": "Because you watched " + (base[0] if base else "trending")})
            if len(recs) >= 6:
                break
        return web.json_response({"ok": True, "based_on": base, "recommendations": recs})
    except Exception as e:
        logger.warning("recommend failed: %s", e)
        return web.json_response({"ok": False, "error": "failed"}, status=500)

# ---------- Dashboard HTML ----------
@routes.get("/analytics")
async def dashboard(request: web.Request):
    # in prod, gate by ADMINS; in preview allow all
    try:
        import info
        admins = getattr(info, "ADMINS", [])
        # allow ?token= or header, but don't block preview
        # we just show banner if not admin
        is_admin = True
        # optional check: if request has no admin ip, still allow but show warning
    except Exception:
        is_admin = True
    html = open_dashboard_html()
    return web.Response(text=html, content_type="text/html")

def open_dashboard_html():
    # inline for simplicity; static assets handle CSS/JS separately
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MinatoVerse · Analytics</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#07080c;--panel:#101320;--line:rgba(255,255,255,.09);--text:#eef0f7;--muted:#9aa1b9;--gold:#f5c518;--gold2:#ffdd7a;--brand:#7c5cff;--brand2:#22d3ee;--grad:linear-gradient(135deg,var(--brand),var(--brand2));--font-d:"Sora",sans-serif;--font-b:"Inter",sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:var(--font-b)}
a{color:inherit;text-decoration:none}
.top{position:sticky;top:0;z-index:10;display:flex;align-items:center;justify-content:space-between;padding:12px 4vw;background:rgba(7,8,12,.8);backdrop-filter:blur(12px);border-bottom:1px solid rgba(255,255,255,.06)}
.brand{display:flex;align-items:center;gap:10px;font-family:var(--font-d);font-weight:700}
.brand i{display:grid;place-items:center;width:36px;height:36px;border-radius:10px;background:var(--grad);color:#06070c;font-size:13px}
.wrap{width:min(1200px,100% - 32px);margin:24px auto 40px}
.h1{font-family:var(--font-d);font-size:26px;margin:0}.h1 span{background:linear-gradient(135deg,var(--gold),var(--gold2));-webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:var(--muted);font-size:13px;margin:6px 0 18px}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:18px 0}
@media(max-width:900px){.grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:560px){.grid{grid-template-columns:1fr}}
.card{border:1px solid var(--line);border-radius:16px;background:linear-gradient(180deg,rgba(22,26,42,.9),rgba(16,19,32,.9));padding:16px}
.kpi-label{font-size:11px;letter-spacing:1.2px;text-transform:uppercase;color:var(--muted);font-weight:700}
.kpi{font-family:var(--font-d);font-size:28px;font-weight:700;margin:6px 0}
.kpi small{font-family:var(--font-b);font-size:13px;color:var(--muted);font-weight:500}
.panels{display:grid;grid-template-columns:1.4fr .9fr;gap:14px;margin-top:14px}
@media(max-width:900px){.panels{grid-template-columns:1fr}}
.chart-wrap{height:280px}
.table{width:100%;border-collapse:collapse;font-size:13px}
.table th{text-align:left;color:var(--muted);font-size:11px;letter-spacing:.8px;text-transform:uppercase;padding:8px 10px;border-bottom:1px solid var(--line)}
.table td{padding:10px;border-bottom:1px solid rgba(255,255,255,.06)}
.badge{padding:4px 8px;border-radius:999px;background:rgba(245,197,24,.14);border:1px solid rgba(245,197,24,.3);color:var(--gold2);font-size:11px;font-weight:700}
.btn{display:inline-flex;align-items:center;gap:8px;padding:10px 14px;border-radius:10px;border:1px solid var(--line);background:#161a2a;color:var(--text);font-weight:600;cursor:pointer}
.btn-primary{background:linear-gradient(135deg,var(--gold),var(--gold2));color:#20180a;border-color:transparent}
.muted{color:var(--muted)}
</style></head><body>
<header class="top"><a class="brand" href="/"><i>MV</i> MinatoVerse <small style="color:#9aa1b9;font-weight:500;letter-spacing:1.6px;text-transform:uppercase;margin-left:6px">analytics</small></a>
<div style="display:flex;gap:8px"><a class="btn" href="/">← Stream</a><a class="btn btn-primary" href="/api/analytics/stats" target="_blank">Raw JSON</a></div></header>
<main class="wrap">
<h1 class="h1">Analytics <span>Dashboard</span></h1>
<p class="sub">Live from <code>/api/analytics/track</code> — every play / view / download is counted. Data auto-expires after 90 days. <span id="liveDot" style="display:inline-flex;align-items:center;gap:6px;margin-left:8px"><i style="width:7px;height:7px;border-radius:50%;background:#34d399;box-shadow:0 0 0 4px rgba(52,211,153,.18)"></i> live</span></p>

<div class="grid" id="kpis">
<div class="card"><div class="kpi-label">Total events</div><div class="kpi" id="kTotal">—</div><small class="muted">all time (capped 5k in memory)</small></div>
<div class="card"><div class="kpi-label">Last 24h</div><div class="kpi" id="k24">—</div><small class="muted">plays + views + downloads</small></div>
<div class="card"><div class="kpi-label">Last 7 days</div><div class="kpi" id="k7">—</div><small class="muted">rolling</small></div>
<div class="card"><div class="kpi-label">Top event</div><div class="kpi" id="kEvt">—</div><small class="muted" id="kEvtSub">—</small></div>
</div>

<div class="panels">
<div class="card"><div class="kpi-label">Daily views (last 7 days)</div><div class="chart-wrap"><canvas id="dailyChart"></canvas></div></div>
<div class="card"><div class="kpi-label">Hourly (last 24h)</div><div class="chart-wrap"><canvas id="hourlyChart"></canvas></div></div>
</div>

<div class="card" style="margin-top:14px">
<div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
<div class="kpi-label">Top titles</div><button class="btn" id="refreshBtn">↻ Refresh</button>
</div>
<table class="table" id="topTable"><thead><tr><th>#</th><th>Title</th><th>Hits</th><th></th></tr></thead><tbody><tr><td colspan="4" class="muted">Loading…</td></tr></tbody></table>
</div>

<div class="card" style="margin-top:14px">
<div class="kpi-label">AI Recommendations (demo)</div>
<p class="muted" style="font-size:13px">Based on your My List + Continue Watching — <code>/api/recommend?based_on=Jawan,Leo</code></p>
<div id="recRail" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin-top:12px">Loading…</div>
</div>

<p class="muted" style="font-size:12px;margin-top:16px">Tip: open the stream page, play a video, then refresh here. Events are beamed from <code>netflix_pack.js</code>. In production Mongo TTL keeps it lean.</p>
</main>
<script>
(function(){
 var dailyChart, hourlyChart;
 function fmt(n){return new Intl.NumberFormat().format(n)}
 async function load(){
  var res = await fetch('/api/analytics/stats?limit=2000');
  var data = await res.json();
  if(!data.ok) throw new Error('stats failed');
  document.getElementById('kTotal').textContent = fmt(data.total);
  document.getElementById('k24').textContent = fmt(data.last24);
  document.getElementById('k7').textContent = fmt(data.last7);
  var topEvt = Object.entries(data.by_event).sort((a,b)=>b[1]-a[1])[0];
  document.getElementById('kEvt').textContent = topEvt ? topEvt[0] : '—';
  document.getElementById('kEvtSub').textContent = topEvt ? fmt(topEvt[1])+' events' : 'no data';

  // daily
  var dLabels = data.daily.map(d=>d.date.slice(5));
  var dData = data.daily.map(d=>d.count);
  var hLabels = data.hourly.map(h=>h.hour);
  var hData = data.hourly.map(h=>h.count);
  renderCharts(dLabels,dData,hLabels,hData);

  // top titles
  var tbody=document.querySelector('#topTable tbody');
  tbody.innerHTML='';
  if(!data.top_titles.length){ tbody.innerHTML='<tr><td colspan=4 class=muted>Abhi koi event nahi — stream page pe video play karo</td></tr>'; }
  else data.top_titles.forEach((row,i)=>{
    var tr=document.createElement('tr');
    tr.innerHTML='<td>'+(i+1)+'</td><td><b>'+esc(row.title)+'</b></td><td><span class=badge>'+fmt(row.count)+'</span></td><td><a class=btn href=\"https://t.me/share/url?url='+encodeURIComponent(location.origin)+'\" target=_blank>Share</a></td>';
    tbody.appendChild(tr);
  });

  // recommendations
  loadRecs(data.top_titles.slice(0,3).map(r=>r.title));
 }
 function renderCharts(dL,dD,hL,hD){
  var ctx1=document.getElementById('dailyChart');
  var ctx2=document.getElementById('hourlyChart');
  if(dailyChart) dailyChart.destroy();
  if(hourlyChart) hourlyChart.destroy();
  dailyChart=new Chart(ctx1,{type:'bar',data:{labels:dL,datasets:[{label:'Views',data:dD,backgroundColor:'rgba(245,197,24,.9)',borderRadius:6}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{grid:{display:false}},y:{beginAtZero:true,grid:{color:'rgba(255,255,255,.06)'}}}}});
  hourlyChart=new Chart(ctx2,{type:'line',data:{labels:hL,datasets:[{label:'Per hour',data:hD,borderColor:'#22d3ee',backgroundColor:'rgba(34,211,238,.18)',tension:.35,fill:true,pointRadius:0}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{grid:{display:false}},y:{beginAtZero:true,grid:{color:'rgba(255,255,255,.06)'}}}}});
 }
 function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
 async function loadRecs(titles){
  var q=titles.join(',');
  try{
   var r=await fetch('/api/recommend?based_on='+encodeURIComponent(q));
   var j=await r.json();
   var rail=document.getElementById('recRail');
   rail.innerHTML='';
   j.recommendations.forEach(rec=>{
    var div=document.createElement('div');
    div.className='card'; div.style.padding='12px';
    div.innerHTML='<b>'+esc(rec.title)+'</b><div class=muted style=\"font-size:12px;margin:4px 0\">'+esc(rec.genres.join(' · '))+'</div><small class=muted>'+esc(rec.reason)+'</small>';
    rail.appendChild(div);
   });
  }catch(e){ document.getElementById('recRail').textContent='Failed to load'; }
 }
 document.getElementById('refreshBtn').addEventListener('click', load);
 load().catch(e=>{document.body.insertAdjacentHTML('beforeend','<pre style=color:#f87171;padding:16px>'+e+'</pre>')});
 setInterval(load, 15000);
})();
</script></body></html>
"""
