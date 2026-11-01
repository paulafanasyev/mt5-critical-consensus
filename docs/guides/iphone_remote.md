# Controlling the demo system from an iPhone

What the phone can and cannot do, and the shortest safe setup that lets you start and watch
the system remotely.

## What the MT5 iPhone app can do

- Log in to the same account (server `MetaQuotes-Demo`, login `114929502`, demo password).
- See balance, equity, open positions, history and the Experts tab is **not** there.
- Place, modify and close orders manually.
- It **cannot** run an Expert Advisor, host the localhost bridge or start Python.

The EA, the bridge and the runner live on the Windows machine. The phone is a window into
the account, not a host for the system.

## What "turn it on remotely" means in practice

The Windows machine must be reachable and always on. Three ways, in order of safety:

1. **VPS (recommended).** A Windows VPS with MT5 installed; the system runs 24/5; the
   phone only watches. Nothing on the home network is exposed.
2. **Home PC + Tailscale.** Install Tailscale on the PC and the iPhone, enable Remote
   Desktop on Windows, connect to the PC's Tailscale IP. No port forwarding, no public RDP.
3. **AnyDesk / TeamViewer.** Simplest to set up, least private; use a strong unattended
   password and two-factor on the account.

Never forward RDP (3389) to the internet and never expose the bridge port `8000`.

## The scripts on the Windows machine

Three PowerShell files in `scripts/`:

- `system_status.ps1` — terminal process, bridge port, runner processes, last EA log lines,
  account balance and open positions (read through the MetaTrader5 package).
- `system_start.ps1` — starts the terminal **only if it is not already running** (restarting
  it with `/config` wipes the WebRequest allowlist, so a running terminal is never touched),
  then starts the demo runner with the gold policy and prints the status.
- `system_stop.ps1` — stops the runner and leaves the terminal running (so the allowlist
  survives), unless `-All` is passed.

Run them from Remote Desktop, or from a scheduled task at logon, or through a phone SSH
client if you also install OpenSSH Server on the PC.

## iPhone steps

1. App Store → **MetaTrader 5** → install.
2. Open → **Settings → New account → Connect to an existing account** → search
   `MetaQuotes-Demo` → login `114929502` → password → Connect.
3. You now see the demo balance and positions. Pull down to refresh.
4. Install **Telegram** and open your bot's chat: approval cards arrive there with
   Approve / Reject buttons; the card expires after five minutes.
5. For remote control of the PC: install **Tailscale** and **Microsoft Remote Desktop**
   (or AnyDesk), connect, and run `scripts/system_start.ps1`.

## Safety rules

- Demo only. The EA refuses a real account by design; do not remove that check.
- The phone's MT5 session can close positions manually — that is a real action on the demo
  account, not a simulation.
- Keep the bridge bound to `127.0.0.1`; it is not designed to face a network.
- If the phone is lost: change the demo password, revoke the Telegram bot token via
  BotFather, and stop the runner.

## What the phone cannot give you

- No EA, no localhost bridge, no Python runner.
- No approval if the PC is off: the card will not arrive and the signal expires.
- No historical backtesting or script running.
