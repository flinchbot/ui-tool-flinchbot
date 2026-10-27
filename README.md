# UniFi × flinchbot

<img src="frontend/static/flinchbot-icon.png" alt="flinchbot" width="96" align="right">

A local web app for chatting with **flinchbot**, an AI network assistant, about a Ubiquiti UniFi network. flinchbot is powered by your choice of **Claude** (Anthropic) or **ChatGPT** (OpenAI). It reads live data from the console (devices, clients, Wi-Fi, alerts, logs, firewall, port forwards) and can make changes. Every change waits for you to click **Approve**.

**New here? Read [SETUP.md](SETUP.md)** (also as [SETUP.html](SETUP.html), and inside the app under the robot logo → *Setup guide*). It walks through installing Python, getting an API key, and preparing the UniFi console. Quick start: double-click `start.bat` (Windows) or `start.command` (Mac), and the app opens in your browser with a setup wizard.

## Features

- **Several networks, switched at runtime.** Save a connection per UniFi console and switch from the header menu. Switching clears the chat so data from different networks never mixes.
- **Setup and settings in the browser.** The first-run wizard and the ⚙ Settings screen write `.env` for you. Nothing needs editing by hand.
- **Works with:** UniFi OS consoles (UDM/UDR/UCG/Cloud Key Gen2+), UniFi OS Server, and self-hosted Network Applications. The console type is auto-detected.
- **Login methods:** a **local-only** console account (username/password), or a UniFi API key. UI.com cloud/SSO accounts with two-factor sign-in can't be used by apps; the app detects this and explains what to do.
- **flinchbot, powered by Claude or ChatGPT.** In the chat the assistant is always *flinchbot*; About, exports and reports show which provider and model answered.
- **Claude or ChatGPT.** Save an API key for either or both and switch in Settings. Both get the same tools, approval gate, redaction and reports. OpenAI calls are sent with `store=false`, so OpenAI doesn't keep the conversation.
- **Write approval enforced in code.** The AI can request a change, but only the Approve button runs it.
- **Optional SSH per connection (off by default).** Turn it on for a console and flinchbot gets one more tool, `ssh_command`, for what the API can't show: system log, processes, memory, disk, config files, interface errors. Health checks and the action report then include those. Read-only commands run at once; anything else (or anything the app can't classify) goes through the same Approve card with the exact command. The host key is pinned on **Test SSH**, and the SSH password lives in Credential Manager next to the UniFi login.
- **Secrets kept out of files:**
  - UniFi passwords and API keys live in the OS password store: Windows Credential Manager or the macOS Keychain.
  - Wi-Fi passphrases and other `x_*` fields are redacted before anything is sent to the AI provider.
- **TLS certificate pinning.** The console's certificate fingerprint is saved when you test a connection. If the address later answers with a different certificate, the app stops and warns you. This matters when you move between networks that reuse the same IPs.
- **Always-current model list:** Settings pulls the model catalogue live from the provider (Anthropic or OpenAI), grouped by family with the newest first. If your chosen model is deprecated, a banner shows its retirement date.
- **Action report:** a read-only review of the whole network, turned into a prioritised checklist (critical → low).
  - Download as an HTML page where ticks are saved, with progress, "hide done" and print/PDF.
  - Or download as Markdown with `- [ ]` task lists.
- **Topology map (SVG):** 🗺 Topology draws the UniFi devices as a tree with port numbers on each link, like the UniFi UI, and adds **only the clients that currently have a problem** under the device they're on: weak signal, high Wi-Fi retry rate, low satisfaction, errors or half duplex on their switch port, or flapping (connect/disconnect churn in the last 24 h). Each shows name, IP, MAC, port or SSID/channel/signal, and the issues. Vector, so zoom in losslessly; shown inline with pan/zoom and downloadable as `.svg`. The AI gets the same data as the `get_topology` tool.
- **📈 WAN health:** download, upload and latency over the last day to 90 days (the console's own speed-test history plus tests this app ran), with WAN outages and other internet events from the system log marked across both charts. Run a speed test on demand, or schedule one per connection (off by default; runs only while that connection is active). Ask flinchbot to correlate slow periods with outages; the AI has the same data as `get_wan_history`. Vector chart, downloadable as SVG, with a table view.
- **Alerts with Explain:** the sidebar lists active alerts in plain words. **Explain** sends one to flinchbot, which investigates it with the tools.
- **🧾 Change log:** every change approved or refused in the chat, plus Undo, speed tests and backups, with who, when, which console, exactly what, and what the console answered. Stored locally in `data/audit.jsonl`; passwords are masked. Export as CSV or JSON.
- **↶ Undo:** Wi-Fi and LAN/VLAN edits, Wi-Fi deletions and newly created networks can be undone with one click, from the approval card or the change log. The app snapshots the previous settings before an approved change runs; an old Wi-Fi passphrase in a snapshot goes to the OS password store, never into the log. If the setting was changed again since, Undo asks before overwriting.
- **💾 One-click backup:** downloads a settings backup of the active console (`.unf`, the same file as UniFi's *Settings → System → Backups*) to your Downloads folder. Restore it from that same UniFi page.
- **Text attachments:** attach logs, configs or CSVs to a question (📎 or drag-and-drop; 5 files / 150,000 characters per message). Read in the browser and kept only in the in-memory conversation; never written to disk. Binary files are refused.
- **Chat export** to Markdown or HTML.
- **Themes:** Default, Matrix, Windows 95, Star Wars, My Little Pony, Blade Runner and Candle Maker. Each is a full re-skin (colours, fonts, text size, layout) with light and dark versions; the choice is saved per browser, and moving backgrounds respect reduced-motion. Fonts come from Google Fonts on first use.
- **Windows and macOS:** `start.bat` on Windows, `start.command` on a Mac (also works on Linux). The same folder, `.env` and `data/` work on both.
- **Locked to this computer:**
  - Listens on `127.0.0.1` only.
  - Every request needs a per-launch token.
  - Host and Origin headers are checked, which blocks other websites and DNS rebinding.
  - No CORS, and the page can't be framed.
- **Handles large networks.** Device and client lists are slimmed, searchable and paged, so the AI never gets cut-off JSON.
- **Session recovery.** If the UniFi session expires or the console reboots, the app re-logs in automatically.
- **Lockout-safe sign-in.** A connection is only saved after one successful sign-in check. A rejected password or API key is never retried automatically: polling stops, and a banner offers Edit / Retry once. After UniFi's "too many attempts" lockout, the app waits at least 10 minutes. Re-testing identical rejected credentials (UniFi or SSH) is refused without contacting the console.
- **Prompt caching.** The tool schemas, system prompt and earlier turns are served from Anthropic's prompt cache between rounds of a tool loop, so a long health check doesn't re-bill the whole conversation every round.

## Layout

```
start.bat              Windows launcher: creates .venv, installs requirements, starts the app
start.command          the same for macOS / Linux (double-click on a Mac)
backend/server.py      local HTTP server + API routes (serves the frontend too)
backend/agent.py       tool definitions, approval gate, tool-use loop, action report
backend/providers.py   Claude (Messages API) and ChatGPT (Responses API) adapters
backend/unifi_client.py UniFi API client (UniFi OS + standalone, pinning, re-login)
backend/ssh_client.py  optional SSH to the console: host-key pinning, read-only command classifier
backend/topology.py    device tree + problem-client rules, and the SVG renderer
backend/wan.py         speed tests (run, schedule, history) and the WAN health chart
backend/history.py     local change log (data/audit.jsonl) and recorded speed tests
backend/rollback.py    undo snapshots for Wi-Fi / LAN changes (secrets kept in the secret store)
backend/config_store.py .env + connection profiles + OS password store
backend/probe_events.py diagnostic: which endpoints the active console answers
frontend/index.html    the whole UI (no build step)
frontend/static/       themes.css + themes.js (themes), flinchbot icon, favicon, About artwork
SETUP.md / SETUP.html  setup guide (HTML is generated: python tools/build_setup_html.py)
tools/                 doc build script
```

Not committed (see `.gitignore`):
- `.env`: AI provider key(s) and settings
- `data/`: connection profiles, the change log, recorded speed tests, plus a fallback secrets file if the OS password store isn't available
- `.venv/`

## Requirements

- A **local-only admin account** on each UniFi console (Admins & Users → *Restrict to local access only*). See [SETUP.md step 3](SETUP.md#3-create-a-local-only-admin-account-for-the-app-required-once-per-network).
- Python 3.10+. Both the Anthropic and OpenAI SDKs need it.
- Windows 10/11 or macOS 12+ (Linux works with `start.command` too).
- Packages: `anthropic`, `openai`, `keyring`, `paramiko` (installed by `start.bat` / `start.command`).
- Run without the launcher: `python backend/server.py [--port N] [--no-browser]`.
- Tested on 2026-10-27 with anthropic 2.3.0, openai 3.40.0 and Python 3.13/3.14, against a simulated UniFi OS console, a simulated standalone controller, fake Anthropic/OpenAI APIs, and read-only calls to a real UniFi Network 10.6.109 console.

## Known limitations

- **Unofficial UniFi API.** Most calls use the private Network API that the UniFi web UI itself uses. Ubiquiti can change it between versions. `probe_events.py` shows which endpoints a given console answers.
- **Legacy firewall rules.** `create_firewall_rule` writes classic `LAN_IN` rules. On consoles using Zone-Based Firewall they may not apply.
- **UniFi API keys.** Not every UniFi version accepts them on the private endpoints. The sign-in check when saving a connection reports this.
- **Server-side chat history.** The conversation lives in the running app (one per app instance), not per browser tab.
- **Attachments and SSH output aren't redacted.** Secret-field removal applies to UniFi API data, not to files you attach or to what a command prints over SSH.
- **SSH read-only detection is an allowlist.** Only known commands with no state-changing options count as read-only; everything else needs approval. That errs on the side of asking, so expect an Approve card now and then for a harmless command.

## Editing the docs

`SETUP.md` is the source; `SETUP.html` is generated from it. After editing SETUP.md:

```
pip install markdown
python tools/build_setup_html.py
```

Commit both files. The app serves the HTML guide at `/setup`.

## Credits

Robot artwork: the flinchbot linocut print.
