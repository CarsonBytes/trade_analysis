# IBKR Gateway Passkey Spec (GATEWAY_PASSKEY)

Status: P0 + P1 **implemented and applied 2026-10-06**; P2 open (needs hardware/decision).

## Finding

From 2026-10-03, the LIVE username (U12991898) has exactly one second factor: a **passkey**.
This follows the SFC Hong Kong passkey mandate: IBKR HK restricts accounts without a passkey to
closing-only. See <https://www.interactivebrokers.com.hk/en/general/activate-passkey.php>.

The evidence is the gateway's own `launcher.log`. All 105 handshakes on 10-04/10-05 advertise:

```
NS_AUTH_START version=51 pwd=1 soft=0 token(s)=[token=5, tokenSubtype=4[Passkey]] initialTokenType=PWD
```

There is no IB Key push token any more. A headless Linux gateway cannot complete a passkey:

- There is no Bluetooth, so the phone "hybrid" (QR + BLE proximity) flow is impossible.
- The Linux gateway reportedly offers only USB security keys, and IBKR says this
  "cannot be changed from the client side". Source: [QuantRocket thread](https://support.quantrocket.com/t/ib-gateway-login-fails-required-passkey-is-not-supported-please-upgrade-to-latest-version-ibsj-japan/2256/1).

Commit `2427d25` (RELOGIN_AFTER_TWOFA_TIMEOUT=yes) misread this as a missed push tap. It turned
every timeout into another failed login: 105 attempts, 89 of them answered
"Too many failed login attempts", and IBKR then reset connections outright.

## How often a full second-factor login is needed

| Regime | Full logins |
|---|---|
| Push era as built (Sep 1 - Oct 2) | ~10 per week: no AutoRestartTime, so the gateway logged off daily; plus the 20:00 hard relogin, watchdog `docker restart` cycles, and deploys |
| IBKR minimum (AutoRestartTime + persisted settings + no hard restarts) | **1 per week**, on the first restart after Sunday 01:00 ET ([IBC user guide](https://github.com/IbcAlpha/IBC/blob/master/userguide.md)) |

Unavoidable extras on top of the weekly login: a WSL/Docker/host reboot, or a gateway image or
config change (`--with-gateway` deploy).

## P0 - stop the lockout loop (done)

- `docker-compose.live.yml`: `RELOGIN_AFTER_TWOFA_TIMEOUT=no`. IBC makes one attempt, then sits.
- **Hold switch** `scripts/gateway-hold.sh {hold|release|status} <paper|live>`. A hold is one
  file, `/home/cap/.container-hold/<container>`, and it stops the container. Every automatic
  actor honors it:
  - `gateway-login-watchdog.sh`: no stall cycles while held. A dashboard "Restart" click
    releases the hold, because a human is acting. A successful login clears it.
  - `gateway-relogin.sh`: skips held targets unless the reason is `manual*`.
  - `infra-watchdog.sh` (llm-usage-dashboard repo, deployed at `/home/cap/infra-watchdog.sh`):
    never restarts a held container.
  - `wsl2-docker-deploy-live.sh`: never starts a held gateway.
- **Auth probe** `scripts/gateway-auth-probe.sh` runs inside the container. It prints
  `PASSKEY_ONLY` / `THROTTLED` / nothing, based on the newest handshake. Before any stall cycle,
  the watchdog runs the probe. On a blocker it holds the gateway and sends **one** critical page
  instead of cycling.
- Cron: the 20:00 scheduled relogin is now `paper` only. The previous crontab is saved at
  `/home/cap/crontab.bak.20261006`.
- Notification texts are per-target. Live says "PASSKEY login at the gateway PC", not
  "approve the push".
- Applied 2026-10-06 10:53 HKT: the live gateway is **HELD** (container `Created`, not running).

## P1 - one login per week (done, unverified until the first passkey login)

- `AUTO_RESTART_TIME=11:00 AM` with `TIME_ZONE=Etc/UTC`, i.e. 19:00 HKT, 07:00 ET (EDT). The
  gateway restarts itself daily and reuses its session token.
- `TWS_SETTINGS_PATH=/home/ibgateway/tws_settings` on the named volume
  `quant_live_tws_settings`. `Dockerfile.gateway` pre-creates the mount point owned by
  ibgateway (verified as 1000:1000). The autorestart token and jts.ini survive restarts and
  recreates.
- `wsl2-docker-deploy-live.sh` only rebuilds and recreates the **dashboard** by default
  (`up -d --no-deps dashboard`). `--with-gateway` is required to touch the gateway.
  `gateway-login-live.sh` (xdotool credential typing) now runs only when the deploy actually
  restarted an unheld gateway.
- To verify after the first successful login: the next day's IBC log should show
  `autorestart file found ... authentication will not be required` after 11:00 UTC.

## P2 finding, 2026-10-06 - how IB Gateway actually does the passkey (verified)

Verified on the native Windows IB Gateway 10.47 (`C:\Jts\ibgateway\1047`), using its
launcher.log, captures of its windows, and the `twslaunch-1047.jar` classes:

- After the password, the gateway opens a "Passkey Authentication" window. That window is an
  embedded Chromium (JxBrowser 8.9.4) that loads `https://www.interactivebrokers.com.hk/auth/dtws/webauthn`.
- The gateway injects its own script, which calls `navigator.credentials.get()`.
- The assertion (`clientDataJSON` / `authenticatorData` / `credentialId`) goes back to Java over
  a JS bridge and on through the gateway's own session. The browser page is only there to
  provide the IBKR origin.
- Two user-run attempts (19:38, 19:41) ended in `WebAuthn error: NotAllowedError ... timed out`,
  and **no Windows Security / Windows Hello / phone-QR prompt ever appeared**. JxBrowser does not
  hand WebAuthn to Windows, which rules out:
  - Windows Hello passkeys
  - phone hybrid (QR + Bluetooth)
  - password-manager passkeys (the user's "PC passkey" is Bitwarden inside Brave, which the
    embedded browser cannot reach)
- What remains is Chromium's built-in USB HID support: a **physical FIDO2 security key**. IBKR
  explicitly supports these ("Yubikey, Google Titan, Token2, etc." per ibkrguides
  securelogin/sls/using-passkey). It is not yet tested in the gateway.
- Relaying the challenge to Brave + Bitwarden elsewhere would mean hooking the closed gateway's
  JS bridge. That was rejected: fragile, and it tampers with the broker's auth client on a
  real-money account.

## P2 feasibility: USB security key -> WSL2 -> Docker (checked 2026-10-06, no key yet)

| Link | Status |
|---|---|
| WSL2 kernel 6.6.87.2: `USBIP_VHCI_HCD=m`, `USB_HID=m`, `HIDRAW=y`, with the modules shipped | OK |
| Docker Engine runs natively inside the Ubuntu distro (not Docker Desktop), so `devices:` pass-through works | OK |
| systemd-udevd is active in WSL (`/run/udev` can be mounted read-only into the container) | OK |
| `usbipd-win` on Windows | **missing**: install the MSI from github.com/dorssel/usbipd-win (admin). There is no winget here |
| Gateway image's JxBrowser Chromium | **cannot start today**: 15 unresolved libs (GTK3, NSS, ATK/AT-SPI, cairo, pango, ALSA, CUPS, Xcomposite). A test image with `libgtk-3-0t64 libnss3 libasound2t64 libcups2t64 libxcomposite1 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 libcairo2 libpango-1.0-0 libgbm1 libxkbcommon0 libxdamage1 libxrandr2` resolves **all** of them. This also explains why the Docker passkey window vanished on 10-05 |
| Chromium is compiled with FIDO-over-HID (hidraw / U2F) | OK |
| Security-key **PIN** | **risk**: neither JxBrowser 8.9.4 nor the gateway has a passkey PIN UI. The injected JS passes IBKR's server `publicKey` options straight to `navigator.credentials.get`. If IBKR sets `userVerification: "required"`, a PIN-protected key cannot complete in the gateway. If it is "preferred", a touch alone should work. Only a real login can tell |
| Container sees the key, and the key works for IBKR in JxBrowser | untestable without the key |

## P2 test results 2026-10-13 (YubiKey Security Key NFC fw 5.4.3, registered on .com.hk)

- Behind two chained USB hubs (VIA 2109:2812 > 214B:7250), the key never answered CTAPHID_INIT
  (`0x5B4` timeouts). It works **only in a port directly on the PC**.
- Registration on www.interactivebrokers.com.hk forced creating a **key PIN** (Windows'
  BioEnrollmentHost flow), so the key now has a PIN.
- **IBC-Live bug fixed** (`C:\IBC-Live\scripts\StartIBC.bat`, backup `.bak-20261013`). Its
  jxBrowserKey patch used `!X:*-DjxBrowserKey=!`, whose first `=` ends the search string, so it
  passed `-DjxBrowserKey==D...` (341 chars vs the 340-char key). JxBrowser then died with
  `NumberFormatException "=D"` / "Failed to create browser". After stripping the leading `=`, a
  dry run matched the source key exactly, and the live run logged `Browser created successfully`.
  The Linux IBC in the Docker image extracts the key correctly (`sed`, 340 chars).
- **On Windows, JxBrowser delegates WebAuthn to the Windows API.** The 08:51:47 run issued a
  GetAssertion for rpId `interactivebrokers.com.hk` and waited 300 s for the key. Only the
  touch, which needs a human, was missing.
- **IBKR's assertion options**, from the WebAuthN/Operational log:
  - `userVerification = 1` (**REQUIRED**)
  - `filterHybridTransport = true` (no phone QR)
  - 3 allowCredentials
- Consequences:
  - Windows: Windows' own dialog collects the PIN, so the gateway path is **fully viable**.
  - Docker/Linux: JxBrowser has no PIN UI, so a PIN key **cannot satisfy UV-required there**.
    The only Docker route would be an authenticator with on-device UV (YubiKey Bio
    fingerprint), which is untested.
- **Decision input: live gateway should run on Windows (IBC-Live), with the Docker dashboard
  connecting to it**, unless a Bio key is bought for Docker.
- Side note: IBC-Live's own restart loop relaunched the gateway after a killed attempt (08:51:05).
  Any Windows automation must stop the whole cmd tree, and must not loop logins.

## P2 build 2026-10-13 - LIVE gateway on Windows (user chose this over a Bio key for Docker)

Applied: the live dashboard was redeployed at 09:10 HKT and is healthy.

- **Relay:** `win-gateway-relay` service (`scripts/win-gateway-relay.sh`).
  - It runs with `network_mode: host` and resolves Windows' address at start from
    /proc/net/route, because WSL2 NAT means that address can change per boot.
  - Then it runs `socat 0.0.0.0:4003 -> <windows>:4001`.
  - Verified: it resolves 172.27.64.1, and the dashboard reaches it.
- **Dashboard:** `IB_HOST=host.docker.internal` (host-gateway), with `LIVE_IB_PORT` left at
  4003, so `ib_exec`'s live-port guard (7496/4001/4003) is unchanged.
- **Docker gateway:** `ib-gateway-live` is parked behind `profiles: ["docker-gateway"]` and is
  still HELD.
- **Deploy script:** `wsl2-docker-deploy-live.sh` builds and starts relay + dashboard only.
  `--with-gateway` opts into the profile.
- **Watchdog:** `gateway-login-watchdog.sh` uses `LIVE_GATEWAY_MODE=windows` (default).
  - Live is probed by TCP on Windows:4001.
  - It is never relogged automatically.
  - One critical page per outage (after STALL_MIN, outside quiet hours).
  - A dashboard Restart click for live sends login instructions instead.
- **Desktop shortcut "IBKR Live Login"** runs `scripts/windows/ibkr-live-login.vbs`, which
  launches `ibkr-live-login.ps1` hidden. The script:
  - Guards: refuses if already logged in, if an IBC-Live instance is running, or if the key is
    missing; warns if the key sits behind a hub.
  - Launches IBC-Live, which fills the stored credentials itself.
  - Auto-clicks "Authenticate >".
  - The human enters the PIN and touches the key.
  - On a 330 s timeout it kills the whole IBC cmd tree, so there is no retry loop.
  - Log: `C:\IBC-Live\Logs\live-login.log`.
- **Firewall:** the "java" inbound-allow rule (Public profile) covers the exact i4j `java.exe`
  IBC launches.
- **IBC-Live config:** existing `AutoRestartTime=08:30 PM` (HKT) and `ColdRestartTime=20:30`.
  `TWOFA_TIMEOUT_ACTION=exit` means no IBC self-retry.
- **Still to verify on the first real login (needs the human):**
  1. The dashboard log shows `ib_client: connected` through the relay. This also proves the
     gateway accepts non-localhost API clients; `AcceptIncomingConnectionAction=accept`.
  2. The next day after 20:30 HKT, the gateway survives its auto-restart without a passkey.

## P2 fix 2026-10-14 - TrustedIPs root cause + two-hop relay chain (VERIFIED end-to-end)

The 2026-10-13 build's single-hop relay (`socat -> Windows:4001`) did NOT work: every
WSL/container handshake got EOF while localhost worked. Root cause found 2026-10-14:

**`C:\Jts-Live\jts.ini` has `TrustedIPs=127.0.0.1`.** The Gateway accepts a TCP connection
from a remote IP then closes it the moment any data arrives. There is NO
"Enable ActiveX and Socket API" checkbox on the Gateway (that is TWS-only) -- do not look
for it again. The only fix is to make the gateway see a localhost client.

**Working chain (handshake verified at every hop, server version 176):**
```
dashboard container
  -> host.docker.internal:4003        (win-gateway-relay socat, compose, WIN_PORT=4003)
  -> Windows:4003                     (user-mode TcpRelay, scheduled task \IBKR\API Relay)
  -> 127.0.0.1:4001                   (IBKR Gateway API -- localhost, satisfies TrustedIPs)
```

- **Windows relay** `scripts/windows/win-api-relay.ps1`: C# TcpRelay, listens
  `0.0.0.0:4003`, forwards to `127.0.0.1:4001`. Registered as scheduled task
  `\IBKR\API Relay` (AtLogOn, Limited privileges -- port 4003 >1024 needs no admin) by
  `scripts/windows/setup-api-relay.ps1`. Log: `C:\IBC-Live\Logs\relay.log`.
- **`win-gateway-relay` compose service** now sets `WIN_PORT=4003` (was defaulting to
  4001 -- that was the bug). `scripts/win-gateway-relay.sh` still resolves Windows' address
  at start from /proc/net/route (WSL NAT).
- **Firewall:** user added the inbound allow for TCP 4003.
- **IB handshake protocol** (for probes): client sends `b'API\0' + struct.pack('>I', len(ver))
  + ver` (e.g. `v100..176`) in ONE write; server replies 4-byte length + fields (first =
  server version). Sending only `API\0` yields "Client disconnected before version was sent".
  Reference probe: `tmp/ib_hs2.py` (`python ib_hs2.py 127.0.0.1 4003` -> HANDSHAKE OK).
- **Verified 2026-10-14:** handshake OK from WSL (`127.0.0.1:4003`) and from the dashboard
  container (`host.docker.internal:4003`); dashboard log `ib_client: connected ...
  clientId=41` (23:25:26, and again 23:52:12 after the relay was recreated via compose).
  Live account U12991898 confirmed; mirror-qty heal, flagged-position heal and cash-sweep
  all ran against the real broker.

**Verified login flow (automated, human touches the key):** `ibkr-live-login.ps1` guards ->
IBC-Live (stored credentials) -> auto-click Authenticate -> elevated `\IBKR\Passkey Helper`
selects Security key + enters PIN from Credential Manager -> human touches. Clean restart
binds 4001 within ~24s. **Success test is now a real API handshake** (`LoggedIn` via
`scripts/windows/ib-api-handshake.ps1`, same protocol as `tmp/ib_hs2.py`), replacing the
port-open-only checks that produced false "logged in" reports.

**Logon auto-start (2026-10-14):** `scripts/windows/ibkr-live-autostart.ps1` (+ `.vbs`
hidden launcher) registered as scheduled task `\IBKR\Live Auto-Start` (AtLogOn, Limited).
Mid-week fully unattended (session token reused; no second factor). At the weekly passkey
boundary it auto-clicks Authenticate + triggers the Passkey Helper; the Windows Security
dialog is the notification, the human only touches the key. No MessageBoxes (runs hidden).
Timeout 300s; on timeout IBC is left running so a late tap still completes.

**AutoRestartTime fact (CORRECTED 2026-10-17 -- supersedes this file's original 2026-10-14
wording, which was wrong):** the `autorestart` file is NOT written after a token-based
re-login. It is written at the moment a *healthy* gateway shuts down for its scheduled
20:30 restart and consumed+deleted by the launcher seconds later (proof chain: written
20:30:08, read+deleted 20:30:10, absent again by 20:35 -- see HANDOFF.md's 2026-10-17
"Corrected token-file model" section). Consequences: right after any full-passkey login
the file is *expected* absent (not a bug); every session that survives to a healthy 20:30
writes the next one; a session that dies before 20:30 breaks the chain and the next start
demands a full passkey. Cold restart scheduled for 2026-10-18 20:30 (Sunday, full
passkey). One-time check task `\IBKR\Autorestart Check 20261014` already ran (outcome in
`C:\IBC-Live\Logs\autorestart-check.log`).

**Daily-restart observability (MIN-2FA, 2026-10-17):** tasks `\IBKR\Restart Observe`
(20:29/20:34, archives the per-launch-overwritten IBC/launcher logs and records
pre/post state) and `\IBKR\Restart Verify` (20:50, asserts the token restart was healthy
and pushes PASS/FAIL to Telegram) now watch every 20:30 restart; see the MIN-2FA block in
HANDOFF.md for the schedule table and audit-trail layout.

## P2 automation 2026-10-13 - everything except the touch (user decision)

The verification run at 09:14 (screenshots in `D:\claude\passkey-verify\`) reached the login
script, IBC login, the passkey dialog, the Authenticate auto-click, IBKR's webauthn page, and
the Windows Security "選擇金鑰" (choose a key) dialog. The timeout then stopped everything
cleanly, with 0 IBC processes left.

Two Windows behaviours found:
- The Security dialog (CredentialUIBroker) **rejects synthetic input from non-elevated
  processes**, and the process can't be stopped.
- **Dialogs from aborted attempts never close** and block the next request.

The fix is an elevated on-demand helper: task `\IBKR\Passkey Helper` running
`scripts/windows/ibkr-passkey-helper.ps1`.
- `ibkr-live-login.ps1` writes a request file, clicks Authenticate, then runs the task.
- The helper:
  - cancels leftover dialogs
  - selects 安全性金鑰 (Security key)
  - types the PIN from Credential Manager ("IBKR-YubiKey-PIN")
- Its safety rules:
  - it types only if the foreground window is CredentialUIBroker
  - it acts only on a fresh (<5 min) request, then deletes it
  - it never logs the PIN
- Verified unelevated: it refuses with no request and refuses a stale request.
- **One-time setup by the user at the PC (needs UAC):**
  `setup-passkey-helper.ps1` registers the task and runs `cmdkey` so the user types the PIN.
- ~~The elevated path is untested until that setup is done.~~ **Setup done and elevated
  path verified end-to-end 2026-10-13 (run #5, below).**

The human step that remains is the **touch**.

**Verified end-to-end 2026-10-13, run #5 (10:09:43 → 10:10:03, no human input):** shortcut target,
IBC login, Authenticate, elevated helper, 安全性金鑰, PIN entered, then "觸控您的安全性金鑰"
(Touch your security key). The WebAuthN log shows GetAssertion sent to the USB key after the PIN.
Screenshots are in `D:\claude\passkey-verify\run5-success\`.

Facts learned on the way (all handled in the helper):
- **UIA can't see the dialog.** UI Automation does not list the dialog among the desktop's
  children. Find it with Win32 `EnumWindows` (class `Credential Dialog Xaml Host`).
- **Non-elevated input is ignored.** Non-elevated clicks into the live dialog have no effect
  (UIPI). Elevated clicks work.
- **Orphaned dialogs need an elevated kill.** Orphaned dialogs (their chromium was killed) ignore
  all input, and Windows won't show a new request while they exist. An elevated `Stop-Process`
  removes them. A dialog whose attempt ends normally (gateway 2FA timeout) closes on its own.
- **The PIN screen is a new process.** Choosing the key opens the PIN screen in a NEW
  CredentialUIBroker process, so the helper must follow the newest one.
- **Click into the PIN box first.** The PIN box is not focused reliably. Click it at (0.50, 0.585)
  of the dialog before typing; without that, the keystrokes did not land.
- **Monitor layout.** The desktop has a second monitor at negative x, so virtual-screen
  screenshots are offset. Window rects are correct.

Docker gateway: still not possible with a PIN key, because Linux JxBrowser has no PIN UI, so
there is nothing to automate into. The Docker dashboard uses the Windows login via the relay.

## P2 - the weekly passkey ceremony (open)

> **STATUS 2026-10-17: superseded in practice by the NATIVE Windows path -- this section's
> Docker/usbipd plan was never executed.** The weekly ceremony now runs automatically on
> the Windows gateway (HANDOFF.md): the Sunday 20:30 cold restart (or the next logon)
> raises the prompt, `\IBKR\Live Auto-Start` auto-clickes Authenticate, the elevated
> `\IBKR\Passkey Helper` selects 安全性金鑰 + enters the PIN, and the human only touches the
> key (verified end-to-end 2026-10-13 run #5). Steps 1-2 below are DONE; step 3's usbipd/
> Docker plumbing was never needed because production moved to the Windows gateway (the
> parked Docker gateway keeps `gateway-hold.sh` as its mechanism). Kept for archaeology.

Steps for when the key arrives:

1. **User:** register the key on **www.interactivebrokers.com.hk** (passkeys are domain-bound; the
   gateway uses .com.hk, and a `.com.au` Bitwarden passkey can never match it). In Brave, dismiss
   Bitwarden's save prompt and choose "security key".
2. **Decisive cheap test on the native Windows gateway** (`C:\Jts`): log in and touch the key.
   It uses the same JxBrowser and the same IBKR options, so it settles the PIN/UV question with
   none of the USB-IP plumbing. If it fails there, Docker cannot do better: stop and ask IBKR.
3. **Docker plumbing**, only after step 2 passes:
   - Install usbipd-win. Run `usbipd bind` once (admin). Run
     `usbipd attach --wsl --busid <id> --auto-attach` from a silent logon task.
   - WSL udev rule (as root via `wsl -u root`): Yubico vendor `1050`, hidraw, mode 0666,
     `SYMLINK+="ibkr-fido"`.
   - `Dockerfile.gateway`: add the 14 packages above.
   - Live compose: `devices: ["/dev/ibkr-fido:/dev/hidraw0"]` plus `/run/udev:/run/udev:ro`.
   - Dry run before any login: `/dev/hidraw0` is readable inside the container as ibgateway.
4. **Weekly:** one Telegram alert; at the PC, run `gateway-hold.sh release live` and touch the
   key.
5. **Fallback if the USB-IP chain proves flaky** (auto-attach after reboots, hot-plug): run live
   on the native Windows gateway, which step 2 has already proven, and point the dashboard at it.

Relaying a QR code (e.g. to Telegram) **cannot work**. The FIDO cross-device (hybrid) flow
requires a Bluetooth proximity check between the phone and the machine showing the QR, and the
Linux gateway does not offer the QR flow at all. Options:

1. **USB security key at the gateway PC (recommended).** Register a FIDO2 key (e.g. a YubiKey)
   as an additional IBKR passkey. Attach it to WSL2 with `usbipd-win`, pass it into the
   container, and add the libraries the gateway's passkey dialog needs (GTK). The weekly login
   is then: `gateway-hold.sh release live`, and touch the key. This needs physical presence
   once a week.
2. **USB/IP from wherever you are.** Forward the key from your laptop over Tailscale (USB/IP),
   so the touch can happen remotely. It is more moving parts and is unproven with this gateway.
3. **Software authenticator on the server.** This is technically conceivable, but it puts both
   factors on the same box and defeats the point of the mandate. Not recommended.
4. Ask IBKR HK whether a remote/server passkey option exists. They told the Japan user they
   were working on one.

## Releasing the hold (once P2 exists)

From the gateway PC, ready to complete the passkey:

```
wsl -d Ubuntu -- bash /home/cap/quant/scripts/gateway-hold.sh release live
```

The release starts the gateway and waits 300s for the API port. If it fails and the account is
still passkey-only, the watchdog re-holds the gateway on its next active-window check.
