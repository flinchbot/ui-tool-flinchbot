# UniFi × Claude

A local web app for chatting with Claude about a Ubiquiti UniFi network. Claude reads live data from the console (devices, clients, Wi-Fi, alerts, logs, firewall, port forwards) and can make changes. Every change waits for you to click **Approve**.

**New here? Read [SETUP.md](SETUP.md).** It walks through installing Python, getting an API key, and preparing the UniFi console. Quick start: double-click `start.bat`, and the app opens in your browser with a setup wizard.

## Features

- **Several networks, switched at runtime.** Save a connection per UniFi console and switch from the header menu. Switching clears the chat so data from different networks never mixes.
- **Setup and settings in the browser.** The first-run wizard and the ⚙ Settings screen write `.env` for you. Nothing needs editing by hand.
- **Works with:** UniFi OS consoles (UDM/UDR/UCG/Cloud Key Gen2+), UniFi OS Server, and self-hosted Network Applications. The console type is auto-detected.
- **Login methods:** a **local-only** console account (username/password), or a UniFi API key. UI.com cloud/SSO accounts with two-factor sign-in can't be used by apps; the app detects this and explains what to do.
- **Write approval enforced in code.** Claude can request a change, but only the Approve button runs it.
- **Secrets kept out of files:**
  - UniFi passwords and API keys live in Windows Credential Manager.
  - Wi-Fi passphrases and other `x_*` fields are redacted before anything is sent to Claude.
- **TLS certificate pinning.** The console's certificate fingerprint is saved when you test a connection. If the address later answers with a different certificate, the app stops and warns you. This matters when you move between networks that reuse the same IPs.
- **Always-current model list:** Settings pulls the model catalogue live from Anthropic, grouped by family with the newest starred. If your chosen model is deprecated, a banner shows its retirement date.
- **Action report:** a read-only review of the whole network, turned into a prioritised checklist (critical → low).
  - Download as an HTML page where ticks are saved, with progress, "hide done" and print/PDF.
  - Or download as Markdown with `- [ ]` task lists.
- **Chat export** to Markdown or HTML.
- **Locked to this PC:**
  - Listens on `127.0.0.1` only.
  - Every request needs a per-launch token.
  - Host and Origin headers are checked, which blocks other websites and DNS rebinding.
  - No CORS, and the page can't be framed.
- **Handles large networks.** Device and client lists are slimmed, searchable and paged, so Claude never gets cut-off JSON.
- **Session recovery.** If the UniFi session expires or the console reboots, the app re-logs in automatically.

## Layout

```
start.bat              launcher: creates .venv, installs requirements, starts the app
backend/server.py      local HTTP server + API routes (serves the frontend too)
backend/agent.py       Claude tool definitions, approval gate, tool-use loop
backend/unifi_client.py UniFi API client (UniFi OS + standalone, pinning, re-login)
backend/config_store.py .env + connection profiles + Credential Manager
backend/probe_events.py diagnostic: which endpoints the active console answers
frontend/index.html    the whole UI (no build step)
```

Not committed (see `.gitignore`):
- `.env`: Anthropic key and settings
- `data/`: connection profiles, plus a fallback secrets file if Credential Manager isn't available
- `.venv/`

## Requirements

- A **local-only admin account** on each UniFi console (Admins & Users → *Restrict to local access only*). See [SETUP.md step 3](SETUP.md#3-create-a-local-only-admin-account-for-the-app-required-once-per-network).
- Python 3.10+. The Anthropic SDK 2.x needs it.
- Packages: `anthropic`, `keyring` (installed by `start.bat`).
- Run without the launcher: `python backend/server.py [--port N] [--no-browser]`.
- Tested on 2026-10-27 with anthropic 2.3.0 and Python 3.13, against a simulated UniFi OS console and a simulated standalone controller.

## Known limitations

- **Unofficial UniFi API.** Most calls use the private Network API that the UniFi web UI itself uses. Ubiquiti can change it between versions. `probe_events.py` shows which endpoints a given console answers.
- **Legacy firewall rules.** `create_firewall_rule` writes classic `LAN_IN` rules. On consoles using Zone-Based Firewall they may not apply.
- **UniFi API keys.** Not every UniFi version accepts them on the private endpoints. The Test button reports this.
- **Server-side chat history.** The conversation lives in the running app (one per app instance), not per browser tab.
