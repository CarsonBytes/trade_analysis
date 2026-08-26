"""Per-instance health snapshot + a combined "fleet" page -- ADDED 2026-08-26.

Operational complexity (two always-on dashboards, each with its own gateway/tunnel/
watchdog) is this project's self-rated weakest dimension. Checking "is everything okay"
previously meant loading BOTH full dashboards over the tunnel from whatever device you
had -- often a phone. This module serves:

  /status   machine-readable JSON snapshot of THIS instance (tiny, fast, no auth beyond
            obscurity -- it exposes balances only at the same granularity the dashboard
            already does behind its own tunnel)
  /status?fmt=html   the same snapshot as a minimal phone-friendly HTML card
  /fleet    one page that JS-fetches BOTH instances' /status and renders one green/red
            row per instance -- the single bookmark for "is everything okay overall"

Deliberately NO NiceGUI import here: pure functions so they can be unit-tested without
spinning up a UI client (same reason resolve_mode() was extracted to core/mode.py).
"""
from __future__ import annotations

import datetime as dt
import os


def _mode() -> str:
    if os.environ.get("IB_ALLOW_LIVE", "").lower() in ("1", "true", "yes"):
        return "LIVE"
    fixed = (os.environ.get("DASH_FIXED_MODE") or "").upper()
    return fixed or "PAPER"


def status_snapshot() -> dict:
    """One-shot health read of THIS instance. Never raises -- any subsystem that fails
    to report contributes an explicit 'unavailable' value rather than a 500."""
    out: dict = {
        "mode": _mode(),
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "ok": False,
        "nl": None, "_ccy": "",
        "positions": None,
        "acct_age_sec": None,       # F3: None = never read live OR pre-F3 cached snapshot
        "dd_pct": None,
        "dd_halted": None,
        "tick_age_sec": None,
        "last_tick_duration_sec": None,
        "last_status": None,
        "sleeve_enabled": None,
        "notify_configured": None,
        "db": os.environ.get("DASH_DB_NAME", "dashboard.db"),
        "error": None,
    }
    try:
        from dashboard.core import paper, sleeve, store
        from dashboard.web import service

        acct = service.STATE.get("account") or {}
        out["nl"] = acct.get("NetLiquidation")
        out["_ccy"] = acct.get("_ccy", "")
        positions = service.STATE.get("positions") or {}
        out["positions"] = len(positions)
        # F3 2026-08-26: how old is the ACCOUNT data itself? A populated NL restored from
        # a cache snapshot is NOT the same as a live broker read -- today this distinction
        # (missing entirely) made a stuck-gateway outage look green for hours.
        _acct_ts = service.STATE.get("acct_ts")
        if _acct_ts:
            out["acct_age_sec"] = max(0, int(dt.datetime.now().timestamp()) - int(_acct_ts))

        # current deposit-adjusted drawdown % + whether the live DD-halt gate would be
        # active at that level -- the two numbers that answer "is my real money okay"
        hist, _ = store.cache_get("equity_history")
        hist = paper.with_inception(hist or [])
        flows, _ = store.cache_get("cash_flows")
        try:
            dd = paper.current_drawdown_pct(hist, flows) if len(hist) >= 2 else None
            out["dd_pct"] = round(dd, 2) if dd is not None else None
        except Exception:                              # noqa: BLE001
            out["dd_pct"] = None
        try:
            halt = float(os.environ.get("DD_HALT_PCT", "-13"))
            out["dd_halted"] = (out["dd_pct"] is not None and out["dd_pct"] <= halt)
        except Exception:                              # noqa: BLE001
            out["dd_halted"] = None

        last_tick = service.STATE.get("last_tick_ts")
        if last_tick is not None:
            out["tick_age_sec"] = int((dt.datetime.now() - last_tick).total_seconds())
        out["last_tick_duration_sec"] = service.STATE.get("last_tick_duration_sec")
        out["last_status"] = service.STATE.get("last_status")

        try:
            out["sleeve_enabled"] = bool(sleeve.sleeve_enabled())
        except Exception:                              # noqa: BLE001
            out["sleeve_enabled"] = None
        try:
            from dashboard.core import notify
            out["notify_configured"] = notify.is_configured()
        except Exception:                              # noqa: BLE001
            out["notify_configured"] = None

        # overall verdict: the tick loop is alive AND we have account data AND that data
        # is a FRESH broker read (not an old cached snapshot) AND the DD-halt gate is
        # not currently engaged
        out["ok"] = bool(
            out["tick_age_sec"] is not None and out["tick_age_sec"] < 300
            and out["nl"] is not None
            and out["acct_age_sec"] is not None and out["acct_age_sec"] < 900
            and not out["dd_halted"]
        )
    except Exception as e:                             # noqa: BLE001 -- never 500 on /status
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def render_status_html(snap: dict) -> str:
    """The JSON snapshot as one minimal, inline-styled HTML card -- readable on a phone,
    no external assets, safe to load over a slow tunnel."""
    ok = snap.get("ok")
    color, dot, word = ("#16a34a", "#16a34a", "OK") if ok else ("#dc2626", "#dc2626", "CHECK")
    nl = snap.get("nl")
    ccy = snap.get("_ccy") or ""
    age = snap.get("tick_age_sec")
    aage = snap.get("acct_age_sec")
    rows = [
        ("Mode", snap.get("mode", "?")),
        ("Total value", f"{ccy} {nl:,.0f}" if isinstance(nl, (int, float)) else "unavailable"),
        ("Open positions", str(snap.get("positions"))),
        (
            "Drawdown",
            f"{snap.get('dd_pct'):+.2f}%" if snap.get("dd_pct") is not None else "unavailable",
        ),
        ("Tick age", f"{age}s" if age is not None else "unavailable"),
        ("Account data age", f"{aage}s" if aage is not None else "stale/never read live"),
        ("Sleeve enabled", str(snap.get("sleeve_enabled"))),
        ("DB", str(snap.get("db"))),
    ]
    body = "".join(
        f"<tr><td style='padding:2px 12px 2px 0;color:#6b7280'>{k}</td>"
        f"<td style='padding:2px 0;font-weight:600'>{v}</td></tr>"
        for k, v in rows
    )
    err = (f"<div style='margin-top:8px;color:#dc2626;font-size:12px'>"
           f"error: {snap.get('error')}</div>") if snap.get("error") else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>QTS [{snap.get('mode', '?')}] status</title></head>"
        "<body style=\"font-family:system-ui,sans-serif;margin:24px;\">"
        "<div style='max-width:420px;border:1px solid #e5e7eb;border-radius:10px;"
        "padding:16px 20px'>"
        f"<div style='display:flex;align-items:center;gap:8px;margin-bottom:8px'>"
        f"<span style='width:10px;height:10px;border-radius:50%;background:{dot};"
        f"display:inline-block'></span>"
        f"<span style='font-size:18px;font-weight:700;color:{color}'>{word}</span>"
        f"<span style='font-size:13px;color:#6b7280'>{snap.get('ts','')}</span></div>"
        f"<table style='font-size:14px'>{body}</table>{err}</div></body></html>"
    )


_FLEET_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>QTS fleet status</title>
<style>
 body{font-family:system-ui,sans-serif;margin:24px;background:#f9fafb}
 .inst{border:1px solid #e5e7eb;border-radius:10px;padding:14px 18px;margin-bottom:12px;
       max-width:520px;background:#fff}
 .dot{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:8px}
 .name{font-weight:700;font-size:16px}
 .sub{color:#6b7280;font-size:13px;margin-top:4px}
 h1{font-size:20px}
</style></head><body>
<h1>QTS fleet status</h1>
<div id="rows">loading…</div>
<script>
const INSTANCES = __INSTANCES__;
function cell(ok){return ok===true ? '#16a34a' : '#dc2626';}
async function load(){
  const rows=document.getElementById('rows'); rows.innerHTML='';
  for(const [name,url] of INSTANCES){
    const div=document.createElement('div'); div.className='inst';
    div.innerHTML=`<span class="dot" style="background:#9ca3af"></span>`+
      `<span class="name">${name}</span><div class="sub">fetching ${url}…</div>`;
    rows.appendChild(div);
    try{
      const r=await fetch(url+'/status',{cache:'no-store'});
      const s=await r.json();
      const nl=(s.nl==null)?'unavailable':`${s._ccy||''} ${Number(s.nl).toLocaleString()}`;
      const dd=(s.dd_pct==null)?'n/a':(s.dd_pct>0?'+':'')+s.dd_pct+'%';
      const age=(s.tick_age_sec==null)?'n/a':s.tick_age_sec+'s';
      const aage=(s.acct_age_sec==null)?'<b>stale/cache</b>':s.acct_age_sec+'s';
      div.innerHTML=`<span class="dot" style="background:${cell(s.ok)}"></span>`+
        `<span class="name">${name} — ${s.ok?'OK':'CHECK'} <small>(${s.mode})</small></span>`+
        `<div class="sub">value ${nl} · positions ${s.positions??'?'} · DD ${dd}`+
        ` · tick ${age} · acct data ${aage}${s.dd_halted?' · <b style="color:#dc2626">DD HALT</b>':''}</div>`;
    }catch(e){
      div.innerHTML=`<span class="dot" style="background:#dc2626"></span>`+
        `<span class="name">${name} — UNREACHABLE</span><div class="sub">${e}</div>`;
    }
  }
}
load(); setInterval(load, 60000);
</script></body></html>"""


def render_fleet_html() -> str:
    """Fleet page listing both instances. URLs come from the same PAPER_URL/LIVE_URL env
    vars the header's cross-instance link already uses, so there's exactly one place
    where the hostnames are configured."""
    paper_url = os.environ.get("PAPER_URL", "https://quant.carsonng.com").rstrip("/")
    live_url = os.environ.get("LIVE_URL", "https://quant-live.carsonng.com").rstrip("/")
    instances = [["Paper", paper_url], ["Live", live_url]]
    import json
    return _FLEET_PAGE.replace("__INSTANCES__", json.dumps(instances))
