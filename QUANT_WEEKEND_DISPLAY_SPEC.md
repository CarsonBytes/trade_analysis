# Quant weekend display mode: dashboards stay up, broker API goes off

Status: **BUILT 2026-11-28, not deployed, not switched on** (user: "Build with defaults", E1-E4 as
recommended). Changes QUANT_WEEKEND_SLEEP_SPEC (live since 2026-11-21). All times HKT unless marked.

Built:
- `dashboard/core/api_mode.py` (flag reader).
- Skips in `ib_client._ensure_conn` and `app._tick`.
- Header "API off (weekend)", the weekend banner and button guards in `app.py`.
- `/status` `api_mode` + `api_off_until` (`web/fleet.py`).
- `scripts/weekend-docker.sh` display mode.

Tests: `test_api_mode.py` 6/6, full dashboard suite 370 passed, and `weekend_docker_test.sh` 70/70 (D1-D7 new).

Deviations from the text below:
- (a) Broker buttons (Refresh, Log trades, Withdraw) stay clickable, but do nothing and show a toast giving
  the reason. They are not greyed out.
- (b) **Mode switch:** the script runs display mode only when `/home/cap/.weekend-sleep/mode` contains `display`
  (or `WEEKEND_MODE=display`). The default is the old sleep. So nothing changed on the box until the switch.
- (c) **Fallback:** a dashboard that doesn't confirm `api_mode=off` on `/status` within 90 s, for example an old
  image, is held and stopped for that weekend, with one `critical` push.
- (d) **Reconnect check:** the dashboard's `/status` must report `ok=true` (a fresh account read) by wake + 15 min
  for PAPER and wake + 60 min for LIVE. This is checked across ticks.
- (e) While API-off, `/status` `ok` doesn't require a fresh account read. The fleet page shows the instance
  grey "WEEKEND (API off)".

**To switch on (rollout steps 2-3):**
1. Deploy PAPER (push) and LIVE (manual, user-approved).
2. Then:
   `wsl -d Ubuntu -- bash -c 'echo display > /home/cap/.weekend-sleep/mode'`.
3. Undo with `rm /home/cap/.weekend-sleep/mode`.

User ask (2026-11-28): "I want quant live and quant paper to keep displaying; the API just turns off."

## 1. Today vs proposed

| | Today (weekend sleep) | Proposed (display mode) |
|---|---|---|
| quant.carsonng.com / quant-live.carsonng.com | down (403) Sat 06:30 → Sun 20:00 | **up**, showing Friday-close figures with a "Weekend mode" banner |
| quant-dashboard-docker, quant-dashboard-live-docker | stopped | **running**, in API-off mode |
| quant-ibgateway-docker (paper gateway) | stopped | stopped (no change) |
| LIVE gateway (Windows, C:\IBC-Live) | held by the 2FA weekly hold Sat 06:00 → Sun 20:35 | no change |
| Orders, reconcile, LLM scan, broker polling | none (process gone) | none (skipped in code) |
| Watchdogs | skip the held dashboards | **monitor the dashboards normally** (they are up) |
| Windows RAM returned | measured +2.7 GB | est. **+1.9 GB** (paper gateway 0.47 GB + page-cache drop; the dashboards keep ~0.77 GB). To be measured |

Why it's mostly there already: a dashboard with no gateway already degrades cleanly. `ib_client._ensure_conn`
throttles reconnects to one every 30 s, and `service.restore_cache()` reloads the last `portfolio_snapshot`
(account, positions, cash sweep, FX) and the last board scan on start. So a restart with no broker
still renders the last-known book. Before 2026-11-21 the LIVE dashboard ran through every weekend hold
this way. What's missing is to stop it *trying*, and to say so on screen.

## 2. Design

### 2.1 The switch: a flag file on the data volume

`/data/api-off` inside each dashboard container (named volumes `quant_docker_data` and `quant_docker_live_data`).
It holds one JSON line:

```json
{"reason": "weekend", "since": 1764282600, "until": 1764417600, "by": "weekend-docker.sh"}
```

- **On the volume**, so it survives the 20:45 `wsl --shutdown` and any restart. A dashboard that restarts
  mid-weekend comes up API-off and never touches the broker.
- **Self-expiring.** The dashboard treats the flag as active only while `now < until + 2h`. A flag left behind
  by a failed wake can't keep LIVE silently off on Monday. Expiry logs a WARNING, raises a notable event, and resumes.
- One helper, `core/api_mode.py`: `api_off() -> dict | None`, cached for 10 s.

### 2.2 What the dashboard does while API-off

1. `ib_client._ensure_conn()`: returns `None` without a connect attempt. That means no clientId churn and no
   reconnect log spam. An existing connection is disconnected once on entering the mode.
2. `_tick()`: skips `refresh_cheap` and `refresh_llm` entirely, so there is no order placement, sleeve, cash
   sweep, orphan sweep, re-protect, fill audit or reconcile. It still updates `last_tick_ts` so the health
   banner doesn't read "stalled".
   `refresh_news` stays off too (D3).
3. Manual action buttons that reach the broker are disabled, with the tooltip "Broker API off (weekend) until
   Sun 20:00 HKT". This covers place, withdraw-prepare and force LLM scan. Read-only views (journal, retro,
   reports, export) keep working.
4. Banner (neutral colour, not red): **"Weekend mode: broker API off since Sat 06:30, back Sun 20:00 HKT.
   Figures as of <portfolio_snapshot ts>."** Stale-data and "gateway down" health items show as "paused",
   not as errors.
5. `/status` (fleet page) gains `"api_mode": "off"` + `until`. The fleet page shows the instances grey, not red.
6. `notable_events`: suppress the "gateway down / not connected" class while API-off. **Don't** suppress the
   first post-wake reconcile digest (QUANT_WEEKEND_SLEEP_SPEC §4.4 still applies).

### 2.3 On leaving API-off (flag removed, or expired)

The next tick connects as on a fresh start. `needs_reconcile` is set on a fresh connect, so a broker
reconciliation runs first, before any placement in that cycle. That's the existing fresh-start path,
with no new logic.

### 2.4 `weekend-docker.sh` changes

- `CONTAINERS` (stopped and held) becomes `quant-ibgateway-docker` only.
- New `API_OFF_CONTAINERS="quant-dashboard-live-docker quant-dashboard-docker"`:
  - **sleep:** `docker exec <c> sh -c 'echo "$JSON" > /data/api-off'`. Verify that `/status` reports `api_mode=off`
    within 60 s, else push `critical`.
  - **wake:** start the paper gateway, wait for it to be healthy, then `docker exec <c> rm -f /data/api-off` and
    verify `/status` shows `api_mode=on`.
    - PAPER must show broker connected within 5 min, else push `critical`.
    - LIVE is expected to stay disconnected until the 20:35 weekly touch. Its connected check moves to 21:00
      (the existing post-touch checks cover it).
  - **tick (level-triggered):** re-asserts both. If the flag is missing in the window, write it. If it's
    present outside the window, remove it. If a dashboard is stopped, leave it to the watchdogs (it isn't held now).
- The page-cache drop at sleep stays. It was the larger part of the measured saving.
- `status` prints each dashboard's api_mode.
- **Migration:** on the first run, any existing `weekend-sleep` hold on a dashboard is released, and that dashboard
  is started into API-off mode.

### 2.5 Consumers to re-check

- `watchdog.ps1` and `infra-watchdog.sh`: the dashboards are no longer held, so they are watched normally. That's
  what we want. Confirm neither one alerts on "broker disconnected" (they check ports and health only).
- `stall_capture.sh`: the live dashboard is running again. Make sure an idle `_tick` doesn't read as a stall.
- `gateway-login-watchdog.sh`: the paper gateway is still held, so no change.
- Deploys (pre-push PAPER, manual LIVE): a recreated container keeps the volume, and with it the flag. A weekend
  deploy comes up API-off, which is correct.

## 3. Tests

1. `test_api_mode.py`: flag absent → on; flag present and fresh → off; flag past `until + 2h` → on, plus a warning;
   malformed flag → on, plus a warning (fail open to normal trading, loudly).
2. `_ensure_conn` makes zero connect attempts while off. `_tick` calls neither `refresh_cheap` nor `refresh_llm`
   while off (fakes, no IB).
3. `restore_cache` + API-off: account and positions render from `portfolio_snapshot` with no broker.
4. `weekend_docker_test.sh` (fake docker): sleep stops only the gateway and writes both flags; wake removes the
   flags only after the gateway is healthy; a tick in the window re-writes a deleted flag; a tick after the window
   removes a stale flag; existing `weekend-sleep` dashboard holds are migrated.
5. Live: at the Sat 06:30 sleep, both URLs answer 200 with the banner, the logs show 0 connect attempts over
   1 h, and there are 0 watchdog restarts. At the Sun wake, PAPER reconciles within 5 min, and LIVE reconciles
   after the 20:35 touch.

## 4. Rollout

1. Build the dashboard side (flag reader + skips + banner) and deploy PAPER via push. Hand-test on a weekday
   evening after the US close: write the flag, watch for an hour, then remove it.
2. LIVE deploy, user-approved as usual. Inert until a flag exists.
3. Switch `weekend-docker.sh` (dry run `tick --dry-run` first) before Sat 2026-12-05 06:30.
4. Measure Windows free RAM at that sleep and record it here.

**Acceptance:** two weekends with both URLs up all weekend, zero broker connect attempts while API-off, zero
watchdog restarts or alerts, and both books reconciled after wake before Monday's open.

## 5. Decisions for the user

| # | Decision | Options | Recommendation |
|---|---|---|---|
| E1 | How to keep it displaying | **A** real dashboards in API-off mode (this spec) / **B** a tiny static snapshot page (≈20 MB) served while the containers sleep: keeps the full RAM saving but is a frozen, simplified copy | **A** |
| E2 | RAM trade | Accept ≈0.8 GB less returned on weekends (≈+1.9 GB instead of +2.7 GB) | accept |
| E3 | News refresh while API-off | off / on (external HTTP only, no broker) | **off** |
| E4 | Flag safety expiry | wake + 2 h / wake + 0 / none | **wake + 2 h** |
