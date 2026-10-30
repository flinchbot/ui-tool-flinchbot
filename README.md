# UniFi × flinchbot

> [!NOTE]
> **This app is fully vibe coded: it was written entirely by AI.** Every line of code, the tests and the documentation were written by Claude (Anthropic) through Claude Code. A person described what they wanted, tried it on a real network and reported what went wrong; no code was written by hand. Read the code before you trust it with your network. Every change it makes to UniFi still waits for you to click **Approve**.

<img src="frontend/static/flinchbot-icon.png" alt="flinchbot" width="96" align="right">

A local web app for chatting with **flinchbot**, an AI network assistant, about a Ubiquiti UniFi network. flinchbot is powered by your choice of **Claude** (Anthropic) or **ChatGPT** (OpenAI). It reads live data from the console (devices, clients, Wi-Fi, alerts, logs, firewall, port forwards) and can make changes. Every change waits for you to click **Approve**.

**New here? Read [SETUP.md](SETUP.md)** (also as [SETUP.html](SETUP.html), and inside the app under the robot logo → *Setup guide*). It walks through installing Python, getting an API key, and preparing the UniFi console. Quick start: double-click `start.bat` (Windows) or `start.command` (Mac), and the app opens in your browser with a setup wizard.

## Features

- **Several networks, switched at runtime.** Save a connection per UniFi console and switch from the header menu. Switching clears the chat so data from different networks never mixes.
- **Setup and settings in the browser.** The first-run wizard and the ⚙ Settings screen write `.env` for you. Nothing needs editing by hand.
- **Works with:** UniFi OS consoles (UDM/UDR/UCG/Cloud Key Gen2+), UniFi OS Server, and self-hosted Network Applications. The console type is auto-detected.
- **Login methods:** a **local-only** console account (username/password), or a UniFi API key. UI.com cloud/SSO accounts with two-factor sign-in can't be used by apps; the app detects this and explains what to do.
- **flinchbot, powered by Claude, ChatGPT or a local AI.** In the chat the assistant is always *flinchbot*; a small line under each answer, exports and reports show which AI and model answered.
- **Claude, ChatGPT or Local AI.** Save an API key for Claude and/or ChatGPT, set up a local AI, and switch in Settings; the conversation carries on with the new AI. All get the same tools, approval gate, redaction and reports. OpenAI calls are sent with `store=false`, so OpenAI doesn't keep the conversation.
- **Local AI:** any server that speaks OpenAI's Chat Completions API: Ollama, LM Studio (including Apple MLX models), llama.cpp, vLLM, Jan, or a hosted one like OpenRouter. Settings has presets for the usual ports, **Find on this computer**, and **Check this model**, which confirms the model calls tools and measures whether the server really reads a prompt as long as the context size set (Ollama silently cuts longer prompts by default). The app trims older chat to fit that context. Local answers cost $0 in the cost meter.
- **↪ Fallback when an AI can't answer:** Settings → *If it can't answer* lists the other AIs in order. When the chosen AI fails (server off or unreachable, an error, an empty answer, or tool calls the app can't run), the next one gets the question afresh, without the failed attempt; a note says who couldn't answer and why. An AI marked *ask me first* (the default for Claude and ChatGPT) waits for your OK, so network data meant to stay on your Local AI never goes to the cloud unasked. Stop works throughout. A wrong but confident answer isn't detected: that's what Second opinion is for.
- **⚖ Second opinion:** under each answer, ask another AI you've set up to check it. With one other AI the button goes straight to it; with two, it asks which. It sees the whole conversation, including the data the first AI gathered, can call tools itself, and says what was right, wrong or missing. Changes it suggests wait for Approve. The conversation is one record any of the AIs can read: each AI's own turns go back to it exactly as they came, and turns from another AI are rebuilt from their text and tool calls.
- **Write approval enforced in code.** The AI can request a change, but only the Approve button runs it.
- **Optional SSH per connection (off by default).** Turn it on for a console and flinchbot gets one more tool, `ssh_command`, for what the API can't show: system log, processes, memory, disk, config files, interface errors. Health checks and the action report then include those. Read-only commands run at once; anything else (or anything the app can't classify, including every `curl`) goes through the same Approve card with the exact command. Options are checked the way each program reads them, so a write hidden in an option (`sort -o FILE`, `ip a a`) still asks first. The host key is pinned on **Test SSH**, and the SSH password lives in Credential Manager next to the UniFi login.
- **Secrets kept out of files:**
  - UniFi passwords and API keys live in the OS password store: Windows Credential Manager or the macOS Keychain.
  - Wi-Fi passphrases, WireGuard VPN configs (they hold the private key), private pre-shared keys and other secret fields are redacted before anything is sent to the AI provider.
- **TLS certificate pinning.** The console's certificate fingerprint is saved when you test a connection. If the address later answers with a different certificate, the app stops and warns you before sending the password or API key, even when you press Test, and only trusts the exact new certificate you were shown. This matters when you move between networks that reuse the same IPs.
- **Always-current model list:** Settings pulls the model catalogue live from the provider (Anthropic or OpenAI), grouped by family with the newest first. If your chosen model is deprecated, a banner shows its retirement date.
- **Action report:** a read-only review of the whole network, turned into a prioritised checklist (critical → low). The latest report per connection is saved in `data/reports/` and reopened without an AI call until **↻ Refresh report**; **💬 Ask** on an item hands it to the chat.
  - Download as an HTML page where ticks are saved, with progress, "hide done" and print/PDF.
  - Or download as Markdown with `- [ ]` task lists.
- **Topology map (SVG):** 🗺 Topology draws the UniFi devices as a tree with port numbers on each link, like the UniFi UI, and adds **only the clients that currently have a problem** under the device they're on: weak signal, high Wi-Fi retry rate, low satisfaction, errors or half duplex on their switch port, or flapping (connect/disconnect churn in the last 24 h). Each shows name, IP, MAC, port or SSID/channel/signal, and the issues. Vector, so zoom in losslessly; shown inline with pan/zoom and downloadable as `.svg`. The AI gets the same data as the `get_topology` tool.
- **📈 WAN health:** download, upload and latency over the last day to 90 days (the console's own speed-test history plus tests this app ran), with WAN outages and other internet events from the system log marked across both charts. Run a speed test on demand, or schedule one per connection (off by default; runs only while that connection is active). Ask flinchbot to correlate slow periods with outages; the AI has the same data as `get_wan_history`. Vector chart, downloadable as SVG, with a table view.
- **Alerts with Explain:** the sidebar lists active alerts in plain words. **Explain** sends one to flinchbot, which investigates it with the tools.
- **🧾 Change log:** every change approved or refused in the chat, plus Undo, speed tests and backups, with who, when, which console, exactly what, and what the console answered. Stored locally in `data/audit.jsonl`; passwords are masked. Export as CSV or JSON.
- **↶ Undo:** Wi-Fi and LAN/VLAN edits, Wi-Fi deletions and newly created networks can be undone with one click, from the approval card or the change log. The app snapshots the previous settings before an approved change runs; an old Wi-Fi passphrase in a snapshot goes to the OS password store, never into the log. If the setting was changed again since, Undo asks before overwriting.
- **🧰 Tools menu:**
  - **🔌 Switch port map:** every switch's ports drawn like the front panel (SVG), coloured by link speed, with PoE watts, uplinks, errors and half duplex, and a table of what is plugged into each port (UniFi gear, LLDP neighbours, wired clients). Rename a port or power-cycle a PoE port from the table. The AI gets the same data (`get_port_map`) and can change a port's name, PoE mode or on/off (`set_switch_port`, with Undo) or power-cycle it, each behind Approve.
  - **🕵 Device detective:** unnamed and new clients, most mysterious first, with UniFi's fingerprint guess, the MAC maker, private (random) MAC detection and hostname hints. Name a device in UniFi (with Undo), scan it with Nmap, or hand it to flinchbot (`get_unknown_clients`, `scan_client`, `set_client_name`).
  - **🛡 Security scorecard:** an A–F grade from fixed rules over the console's settings: open/old Wi-Fi security, short Wi-Fi passwords, pending firmware, UPnP, IPS, risky port forwards, zone-based firewall policies that let the internet in or open one zone fully to another (and policies with the port on the wrong side), smart-home devices on the main LAN. Optional Nmap sweep for risky open ports. **Fix with flinchbot** turns a finding into a proposed change behind Approve.
  - **📘 Network handbook:** one printable HTML page documenting the whole network (networks, Wi-Fi, devices, topology, every switch port, firewall, routes, VPNs, fixed IPs). Wi-Fi passwords only if asked for.
  - **♻ Factory-reset kit:** snapshots the console's whole configuration, shows what differs from a factory-fresh console (exactly, once compared with the reset console), and writes a bash script of commented SSH commands that restore any part of it through the console's own API. Every reference is looked up by name at run time, since ids change on a reset. Secrets are prompted for, or written in on request; snapshot secrets are encrypted with a key in the OS password store. Tested end to end by running the generated script against a simulated factory-fresh console.
- **📚 Knowledge library** (⚙ Settings → Knowledge library): documents flinchbot can search when it answers. One click imports the whole Ubiquiti Help Center (every English article, through its public API), Ubiquiti's datasheets and user, install and quick start guides (UniFi only, or every product line), a curated set of 71 networking RFCs (IP, ARP, DHCP, DNS, IPv6, multicast, NAT, QoS, RADIUS/EAP, IPsec, routing, NTP, SNMP, syslog, security BCPs), each headed with its status, what it obsoletes and updates, what updated it, and its errata page, and NIST security guidance (CISA blocks automated downloads; add its pages yourself and they count as shareable guidance). Subscribe to RSS/Atom feeds (UniFi release notes, Packet Pushers suggested). Every document has an **authority** (Ubiquiti official → standards and your own → NIST/CISA → other vendors → expert → community) that ranks results and is shown to the AI, which may not let a lower source override a higher one. Add your own PDFs, Markdown, text or saved web pages, or a page by address. Everything is read and indexed on this computer: keyword search always, plus optional meaning search with an embedding model on your Local AI server (e.g. nomic-embed-text). The AI gets a read-only `search_knowledge` tool (offered only when the library has documents) and cites the document it used. With Local AI, **Search the knowledge library first** (on by default, Settings → Local AI) searches the library with each question and hands the model the best passages up front, so a local model can't skip the lookup. Re-running an import fetches only what changed. **Sharing is legal by construction:** each document has a licence class, and **Export shareable library** puts only RFCs (IETF Trust terms) and NIST/CISA publications (US Government works) in the ZIP, with a NOTICE crediting each and saying how many were left out and why (never their names); your own files only if you tick that you may share them; never the index or vectors. IEEE standards and vendor, Wi-Fi Alliance or community sites are not crawled.
- **💰 AI cost meter:** exact token counts from every response (input, output, cache reads and writes) and an estimated cost per chat, day, week and month, per model, with an optional monthly budget warning. Prices come from your own override, LiteLLM's public price list (one click to update), or the built-in Claude prices from Anthropic's pricing page.
- **⬆ Updates from GitHub:** a daily check of the version on the main branch, with its change notes. **Install and restart** writes the new version over the app folder (never `.env`, `data/`, `.venv/` or a git checkout), keeps the replaced files for **Roll back**, and the launcher restarts the app and installs new requirements. Private repositories work with a saved token, `GITHUB_TOKEN`, or the GitHub CLI's sign-in.
- **💾 One-click backup:** downloads a settings backup of the active console (`.unf`, the same file as UniFi's *Settings → System → Backups*) to your Downloads folder. Restore it from that same UniFi page.
- **Attachments: text files and images.** Attach logs, configs or CSVs, and screenshots or photos, to a question: 📎, drag-and-drop onto the chat, or paste a screenshot from the clipboard. 5 files per message; text up to 150,000 characters, images up to 5 MB each. Images go to the AI as pictures (PNG, JPEG, GIF, WebP; BMP, HEIC, TIFF and anything else the browser can decode are converted in the browser, and large images are scaled to 1568 px). Read in the browser and kept only in the in-memory conversation; never written to disk. Other binary files are refused.
- **■ Stop an answer:** while flinchbot works, Send turns into Stop (or press Esc). The question is dropped from the conversation as if it was never asked, and the request to the AI is cut off within a second or two. After an approved change, Stop keeps the change and drops only the answer that followed. A tool that is already running (an Nmap scan, an SSH command) finishes first; the page stops waiting after 3 seconds anyway.
- **Works offline with Local AI.** The app re-checks the internet every minute (it opens a connection to Anthropic, OpenAI and GitHub and sends nothing). Offline, what needs the internet is greyed out and marked *Not available: offline*: update check and install, price updates, testing a Claude or ChatGPT key, speed tests, and second opinions from Claude or ChatGPT. If Claude or ChatGPT is the chosen AI, a banner says so and the chat waits. It all comes back by itself when the connection does.
- **Chat export** to Markdown or HTML.
- **Themes:** Default, Matrix, Windows 95, Star Wars, My Little Pony, Blade Runner, Candle Maker, Notre Dame Football, FC Bayern München, 80s Arcade and Oktoberfest. Each is a full re-skin (colours, fonts, text size, layout) with light and dark versions; the choice is saved per browser, and moving backgrounds respect reduced-motion. Fonts come from Google Fonts on first use.
- **Windows and macOS:** `start.bat` on Windows, `start.command` on a Mac (also works on Linux). The same folder, `.env` and `data/` work on both.
- **Startup environment check:** the launcher checks for Python 3.10+, a writable folder and running from inside the ZIP, installs packages with `backend/install_deps.py`, which lists every package it adds or upgrades with its version and why it's there (also in `.venv/install-log.txt`), then `backend/envcheck.py` reports the OS, Python, packages, password store, internet reachability (offline is fine when Local AI is the chosen AI), whether the Local AI server answers and has the model picked, whether Python has root certificates for the app's own downloads (updates, price list), and whether the optional Nmap is installed. Problems come with the fix, and the launcher offers to open the Python download page and the matching section of the setup guide.
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
backend/providers.py   Claude (Messages API), ChatGPT (Responses API) and Local AI (Chat Completions) adapters, and the shared conversation record
backend/unifi_client.py UniFi API client (UniFi OS + standalone, pinning, re-login)
backend/ssh_client.py  optional SSH to the console: host-key pinning, read-only command classifier
backend/topology.py    device tree + problem-client rules, and the SVG renderer
backend/wan.py         speed tests (run, schedule, history) and the WAN health chart
backend/history.py     local change log (data/audit.jsonl) and recorded speed tests
backend/rollback.py    undo snapshots for Wi-Fi / LAN / switch-port / client-name changes (secrets kept in the secret store)
backend/portmap.py     switch port model (speed, PoE, errors, what is connected) and its SVG
backend/detective.py   unnamed / new client candidates and the clues about them
backend/scorecard.py   rule-based security checks and the A-F grade
backend/nmap_scan.py   optional Nmap TCP connect scans (private LAN targets only)
backend/handbook.py    the printable network handbook (HTML)
backend/resetkit.py    factory-reset kit: snapshot, variance, SSH restore script
backend/pricing.py     token usage log and cost estimate (prices: override / LiteLLM / built-in)
backend/updater.py     update check, install over the folder, roll back (GitHub)
backend/config_store.py .env + connection profiles + OS password store
backend/probe_events.py diagnostic: which endpoints the active console answers
backend/envcheck.py    startup environment report; opens SETUP.html at a section
backend/knowledge.py   knowledge library: reads PDF/Markdown/text/HTML, passages, keyword + meaning search
backend/kb_import.py   bulk imports: Ubiquiti Help Center, Ubiquiti datasheets and guides, networking RFCs
backend/install_deps.py installs requirements into .venv and lists what was added or upgraded, and why
frontend/index.html    the whole UI (no build step)
frontend/static/       themes.css + themes.js (themes), flinchbot icon, favicon, About artwork
SETUP.md / SETUP.html  setup guide (HTML is generated: python tools/build_setup_html.py)
tools/                 doc build script
```

Not committed (see `.gitignore`):
- `.env`: AI provider key(s) and settings
- `data/`: connection profiles, the change log, recorded speed tests, AI usage and prices, reset-kit snapshots (passwords in them encrypted), plus a fallback secrets file if the OS password store isn't available
- `.venv/`

## Requirements

- A **local-only admin account** on each UniFi console (Admins & Users → *Restrict to local access only*). See [SETUP.md step 3](SETUP.md#3-create-a-local-only-admin-account-for-the-app-required-once-per-network).
- Python 3.10+. Both the Anthropic and OpenAI SDKs need it.
- Windows 10/11 or macOS 12+ (Linux works with `start.command` too).
- Packages: `anthropic`, `openai`, `keyring`, `paramiko`, `cryptography` (installed by `start.bat` / `start.command`).
- Optional: [Nmap](https://nmap.org/download) for device scans (Windows/macOS installer, or `brew install nmap`).
- Run without the launcher: `python backend/server.py [--port N] [--no-browser]`.
- Tested on 2026-10-30 with anthropic 2.3.0, openai 3.40.0 and Python 3.13/3.14, against a simulated UniFi OS console, a simulated standalone controller, fake Anthropic/OpenAI APIs, and read-only calls to a real UniFi Network 10.6.109 console.

## Known limitations

- **Unofficial UniFi API.** Most calls use the private Network API that the UniFi web UI itself uses. Ubiquiti can change it between versions. `probe_events.py` shows which endpoints a given console answers.
- **Legacy firewall rules.** `create_firewall_rule` writes classic `LAN_IN` rules. On consoles using Zone-Based Firewall they may not apply.
- **UniFi API keys.** Not every UniFi version accepts them on the private endpoints. The sign-in check when saving a connection reports this.
- **Server-side chat history.** The conversation lives in the running app (one per app instance), not per browser tab.
- **Attachments and SSH output aren't redacted.** Secret-field removal applies to UniFi API data, not to files or images you attach or to what a command prints over SSH. Crop passwords out of screenshots before attaching them.
- **Reset-kit restore commands depend on the private API.** They were tested against a simulated console, not a real factory reset. A setting that a newer UniFi version renamed makes that one command fail with the console's reason; the others carry on.
- **A release is a version bump on main.** The updater compares `APP_VERSION` in `backend/server.py` on GitHub's main branch with the running one, so pushing a commit that raises it offers the update to everyone. Files a new version deletes are left in place (the update writes over the top).
- **Ubiquiti documents are copyrighted.** The knowledge library downloads them to your own computer for your own reference; they live in `data/`, which is never committed or uploaded. Don't share that folder: use **Export shareable library**, which leaves them out. The app's code contains only links to these sources, never their text. This is a careful reading of each source's published terms, not legal advice.
- **Local AI quality depends on the model.** Small models often call the wrong tool or invent arguments. Approve still guards every change. A local server has no password: anyone who can reach its port can use it.
- **Cost is an estimate.** Token counts are exact; dollars use published prices and leave out taxes, discounts and batch/priority pricing.
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
