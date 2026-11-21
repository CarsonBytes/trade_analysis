# Quant weekend sleep: stop the quant containers over the weekend to free RAM

Status: **LIVE since 2026-11-21 21:33 HKT** (user: "Build with defaults", then "Yes, go live now"; D1-D5 as
recommended). All times are HKT unless marked.

Built: `scripts/weekend-docker.sh` + `scripts/weekend_docker_test.sh` (36/36), watchdog.ps1 hold
check (§4.2, loads only after the watchdog process is restarted), `stall_capture.sh` guard,
WSL cron `*/5 * * * * ... weekend-docker.sh tick` (crontab backup in
`/home/cap/crontab.bak-*`). Log: `/home/cap/weekend-docker.log`.
Deviations from the text below: (a) D4 -- sleep/wake notices go out as `info` (digest only),
because `gateway-push.sh` sends only `critical` to Telegram and that is capped at 10/day; a
wake failure is `critical`. (b) D5 -- no passwordless sudo, so the page-cache drop runs as root
through WSL interop (`wsl.exe -u root`), verified to work from a clean env. (c) The window keys
(`sleepStartDay/Time`, `wakeDay/Time`) default in the script; add them to `weekly-touch.json`
only to change them.
**Measured, first live sleep (Sat 2026-11-21 21:33 HKT):** inside WSL used 3481 -> 2096 MB, page
cache 1674 -> 380 MB; **vmmemWSL 5319 -> 2899 MB; Windows free 9795 -> 12515 MB (+2.7 GB)**.
3 min later: all three containers still stopped, watchdog.ps1 logged both dashboards as HELD (no
restart, no alert), infra-watchdog did not touch them. Rollout steps 2-3 were compressed into this
first weekend at the user's request; the acceptance criterion (two clean weekends) still stands.
Found while going live: the quant watchdog.ps1 process was NOT running (last log 19:35 HKT, pid
file stale, no crash line). Relaunched via its Startup VBS at 21:33. Cause unknown.
Related: IBKR_WEEKLY_2FA_SPEC (the weekend gateway hold this reuses), watchdog.ps1,
`/home/cap/infra-watchdog.sh` (llm-usage-dashboard repo), scripts/gateway-hold.sh.

## 1. Question: if I stop the containers, do they come back on their own?

**Yes, within about 2–3 minutes, from two different watchdogs.** Measured 2026-11-21:

| Actor | Where | What it does to a container you `docker stop` | Respects a hold? |
|---|---|---|---|
| `restart: unless-stopped` | compose files | Nothing: a manual stop survives daemon restarts and `wsl --shutdown` | n/a |
| `infra-watchdog.sh` | WSL cron, every minute | Restarts it after its grace period (≤3 restarts/h, then escalates) | **Yes**: skips any container with a file in `/home/cap/.container-hold/` |
| `watchdog.ps1` | Windows, 20 s loop | Port 18080/18081 down 6 cycles (~2 min) → `docker restart` + an `error` alert | **No**: it only checks the port. A hold file does not stop it |
| `gateway-login-watchdog.sh` | WSL cron, every minute | Paper gateway stall cycles; gateways honour the hold | Yes (gateways only) |
| `Docker Daily Restart` | Windows task, daily 20:45 HKT | `wsl --shutdown`; a manually stopped container stays stopped. Its health check covers only saas-cost/study/event-radar | n/a |
| `stall_capture.sh` | WSL cron, every minute | `docker exec` into the live dashboard; fails quietly when it is stopped | n/a |

So a plain `docker stop` today is undone by `watchdog.ps1` (and by `infra-watchdog.sh` if
no hold file exists), with a Telegram alert each time.

## 2. What it would save

Measured 2026-11-21 (`docker stats`, `free -m`, vmmemWSL):

| Container | RAM |
|---|---|
| quant-dashboard-live-docker | 493 MB |
| quant-ibgateway-docker (paper gateway, Java) | 468 MB |
| quant-dashboard-docker (paper) | 273 MB |
| quant-win-gateway-relay | 1 MB (not worth stopping) |
| **Total quant** | **≈1.2 GB** |

Context: Windows has 32 GB and 3.3 GB free; **vmmemWSL holds 6.6 GB**. Inside WSL, 3.4 GB
is used by processes and **3.5 GB is page cache**. `.wslconfig` has
`autoMemoryReclaim=gradual`, so Windows only gets memory back slowly after containers
stop. Expect the quant stop alone to return ~1.2 GB over minutes, not instantly.

**The bigger lever is the page cache, not the containers.** Dropping the WSL page cache
(`echo 3 > /proc/sys/vm/drop_caches` as root) or switching to
`autoMemoryReclaim=dropCache` returns up to ~3.5 GB every day, not just at weekends.
See decision D5.

## 3. Weekend window (reuses the 2FA hold)

The quant book needs nothing between the Friday close and the weekly login:

- US market closes Sat 05:00 HKT (winter) and reopens Mon 22:30 HKT.
- The LIVE gateway is already held from **Sat 06:00 → Sun 20:35** (`C:\IBC-Live\weekly-touch.json`,
  `holdStartDay/holdStartTime` → `touchDay/touchTime`). The live dashboard can't reach
  IBKR in that window anyway.
- Bracket stops/targets live on IBKR's servers, so stopping the dashboard removes no
  protection. With markets closed, nothing fills.

Proposed window: **sleep Sat 06:30 → wake Sun 20:00** (35 min before the weekly touch, so the
dashboard is up and reconciles on the first post-touch connection; the long-only guard's
SHORT check runs then). ≈37.5 h/week at ~1.2 GB.

Both times are new keys in the same `weekly-touch.json` (`sleepStartDay/Time`,
`wakeDay/Time`) so there is one weekend config, not two.

## 4. Design

### 4.1 One script, level-triggered: `scripts/weekend-docker.sh`

```
weekend-docker.sh tick       # cron, every 5 min: make reality match the window
weekend-docker.sh sleep      # manual: sleep now (until the next wake time)
weekend-docker.sh wake       # manual: wake now and stay awake until next weekend's sleep
weekend-docker.sh status
```

- **Desired state** = asleep iff now (computed in HKT with `TZ=Asia/Hong_Kong`; the WSL
  clock is America/Toronto) is inside [sleep, wake) **and** there is no override for this
  weekend.
- **Sleep:** for each of `quant-dashboard-live-docker`, `quant-dashboard-docker`,
  `quant-ibgateway-docker`: write `/home/cap/.container-hold/<name>` with reason
  `weekend-sleep <until>`, then `docker stop -t 60`. Then drop the page cache (if D5 = yes).
  One Telegram `info` line: what stopped, MB freed, wake time.
- **Wake:** remove those hold files only (never a hold with another reason, such as a gateway
  passkey hold), `docker start`, wait ≤3 min for `healthy` + favicon on 18080/18081. One
  Telegram line: ok, or `error` naming what didn't come up.
- **Level-triggered, not edge-triggered.** Each tick re-checks, so a missed minute (box asleep,
  WSL restarted by the 20:45 daily `wsl --shutdown`, a reboot) self-corrects on the next tick.
- **Overrides:** `wake` during a weekend writes `~/.weekend-sleep/override-<weekKey>` so the tick
  does not stop it again that weekend. `sleep` outside the window holds until the next wake time.
- **Holds by other owners win.** If a container has a hold whose reason is not `weekend-sleep`,
  wake leaves it alone.

### 4.2 `watchdog.ps1` must honour holds (required, small)

Before the dashboard restart branch (~L415), skip an instance when a hold file exists for its
container (`wsl -d Ubuntu -- test -f /home/cap/.container-hold/<name>`, cached per cycle) or
when `weekend-docker.sh status` says asleep. Log once per hold, with no alert. This also fixes
an existing gap: today a gateway-style hold on a dashboard would be undone by this loop.
watchdog.ps1 is a long-running process, so restarting it (via its Startup VBS) is needed to
load the change.

### 4.3 Small consumers

- `stall_capture.sh`: exit 0 at the top if the live dashboard isn't running.
- `gateway-login-watchdog.sh`: confirm no paper-dashboard alert path fires while the paper pair is
  held (it already honours gateway holds).
- `flex_nav_reconcile_weekly.sh` (cron `0 9 * * 1` **Toronto time** = Mon 22:00 HKT): runs well
  after the Sunday wake, so no change. It needs the live container, so a wake failure must page
  before then.
- `Docker Daily Restart` (20:45 HKT): unaffected; a held container stays stopped through
  `wsl --shutdown`. Sunday's run lands 45 min after wake and blips the dashboards once (they
  come back via `unless-stopped`). Acceptable; or shift wake to 21:00 (D3).

### 4.4 Data side effects to check

- `equity_history` gets no points for ~37 h. Markets are closed and USD/HKD is pegged, so NAV is
  flat. Check that charts, `daily_pnl_history` and the retro view treat the gap as "no data", not
  as a 0 or a jump.
- Notable events: the first reconcile after wake may carry a `PERSISTING n days` digest. That is
  correct, and should not be suppressed.
- Sat 06:30 sleep is after the Friday close (Sat 05:00), so Friday's final snapshot is taken
  first. Keep sleep ≥ 60 min after the close (summer: close is Sat 04:00).

## 5. Tests

Add to `scripts/windows/test-weekly2fa.ps1` (static) and a new `scripts/weekend_docker_test.sh`
(pure, no docker: inject `NOW=` and a fake `docker` on PATH):

1. Window math in HKT for: Sat 06:29/06:30, Sun 19:59/20:00, a Toronto DST change weekend, and a
   box clock in PST/EST/HKT. Same answer every time.
2. Sleep writes holds before stopping; wake removes only `weekend-sleep` holds and keeps a
   `passkey` hold.
3. A tick after a simulated `wsl --shutdown` mid-window keeps containers stopped; a tick after wake
   time with containers stopped starts them.
4. Override: manual `wake` on Saturday → the next ticks leave containers running until the next
   weekend.
5. watchdog.ps1: AST/grep check that the restart branch consults the hold. Live check: hold the
   paper dashboard for 5 min → no `docker restart`, no `restart:paper` alert.
6. `infra-watchdog.sh` with a hold file: no restart (already true; regression check).
7. Wake failure (fake `docker start` exits 1) → one `error` push naming the container.
8. Measured before/after: vmmemWSL working set and Windows free MB, logged by `status` so the
   saving in §2 is verified, not assumed.

## 6. Rollout

1. Build `weekend-docker.sh` + tests; `watchdog.ps1` hold check; `stall_capture.sh` guard.
2. Dry run: `tick --dry-run` for one weekend (logs what it would do, changes nothing).
3. First real weekend: manual `sleep` Saturday morning and watch the 20:45 daily restart and
   both watchdogs leave it alone; let the Sunday tick wake it before the touch.
4. Then enable the cron tick.

**Acceptance:** two consecutive weekends with the quant containers stopped across the window, zero
watchdog restarts or alerts during it, dashboards healthy before the Sunday touch, and a measured
Windows free-RAM gain recorded in this spec.

## 7. Open decisions for the user

| # | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | Which containers sleep? | quant live + paper (≈1.2 GB) / live only (0.5 GB) / all of WSL incl. study, event-radar, saas-cost, spendlens (`wsl --shutdown`, ≈6.6 GB but kills the other projects and fights WSLKeepAlive) | **quant live + paper** |
| D2 | Window | Sat 06:30 → Sun 20:00 / other | **Sat 06:30 → Sun 20:00** |
| D3 | Wake before or after the 20:45 daily `wsl --shutdown`? | 20:00 (up for the 20:35 touch, one blip at 20:45) / 21:00 (no blip, reconcile ~25 min after the touch) | **20:00** |
| D4 | Telegram on sleep/wake | one line each / wake failures only | **one line each** for the first month, then failures only |
| D5 | Also return WSL page cache to Windows? | drop cache at sleep only / `autoMemoryReclaim=dropCache` permanently (all week) / no | **drop at sleep** now; consider `dropCache` separately, since it frees ~3.5 GB every day but affects the other projects' I/O |
