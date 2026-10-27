# UniFi × flinchbot — Setup guide

This guide assumes no programming experience. Plan on about 20 minutes the first time.

**What you'll end up with:** a small app on your Windows PC or Mac. You double-click it, it opens in your web browser, and you ask an AI questions about a UniFi network, e.g. "why is the Wi-Fi slow in the hall?". The AI can be **Claude** (Anthropic) or **ChatGPT** (OpenAI). It can also make changes, but only after you click **Approve**.

> This guide is also inside the app: click the robot logo (top left) → **📖 Setup guide**.

**What you need**

| You need | Why / notes |
|---|---|
| A Windows 10 or 11 PC, or a Mac (macOS 12 Monterey or newer) | It must be able to reach the UniFi console: plugged into that network, on its Wi-Fi, or connected by VPN. |
| A **local-only admin account** on the UniFi console | **Required.** Your normal UI.com sign-in won't work. Step 3 explains why and how to create one. You need admin access to the console once, to create it. |
| An API account with credit at **Anthropic** (Claude) **or OpenAI** (ChatGPT) | This is how the AI is paid for (step 2). It's separate from a Claude.ai or ChatGPT chat subscription. |

---

<a id="install-python"></a>

## 1. Install Python (once per computer)

Python is the language the app is written in. You install it once and never have to open it. If it's missing or too old, `start.bat` / `start.command` tells you and offers to open the download page and this section.

<a id="python-mac"></a>

**On a Mac:**

1. Go to **https://www.python.org/downloads/** and download the **macOS installer** for the newest version (3.10 or newer is required). Open the downloaded `.pkg` and click through it.
   - Already use Homebrew? `brew install python` works too.
   - The `python3` that comes with macOS is too old or only a placeholder, so install it this way even if `python3` seems to exist.
2. Check it worked: open **Terminal** (press **⌘ + Space**, type `Terminal`, press Return) and type:
   ```
   python3 --version
   ```
   You should see something like `Python 3.14.3`.

<a id="python-windows"></a>

**On Windows:**

1. Go to **https://www.python.org/downloads/**. Download the Windows installer for the newest version (3.10 or newer is required).
   - On current releases, the download is the **Python install manager**. Run it, accept the defaults, and when it asks whether to run its configuration check, say **yes**.
   - If you get the older-style installer instead, **tick "Add python.exe to PATH"** on the first screen before clicking *Install Now*.
2. Check it worked. Press **Win + R**, type `cmd`, press Enter, and in the black window type:
   ```
   py --version
   ```
   You should see something like `Python 3.14.3`.

> **"python" opens the Microsoft Store instead?** Windows ships a fake `python` shortcut. Open **Settings → Apps → Advanced app settings → App execution aliases** and switch off the two *App Installer python* entries. `start.bat` tries the `py` command first, so you usually won't hit this.

## 2. Get an AI API key (once): Claude or ChatGPT

Pick one. You can add the other later and switch in Settings. Both work the same way in the app: same tools, same approval step, same reports.

**Option A: Claude (Anthropic)**

1. Sign in at **https://console.anthropic.com**.
2. Under **Billing**, add some credit.
3. Go to **API Keys → Create Key** and give it a name like `UniFi app`.
4. **Copy the key now.** It starts with `sk-ant-` and is shown only once. You'll paste it into the app in step 6.

**Option B: ChatGPT (OpenAI)**

1. Sign in at **https://platform.openai.com**. This is OpenAI's developer site, not chatgpt.com.
2. Under **Billing**, add credit. **A ChatGPT Plus/Pro subscription does not include API usage;** it's billed separately.
3. Go to **API keys → Create new secret key** and give it a name like `UniFi app`.
4. **Copy the key now.** It starts with `sk-`, usually `sk-proj-` for new keys, and is shown only once.

Treat any API key like a credit card number. Anyone who has it can spend your credit. If it ever leaks, delete it on the same page and create a new one.

**Which model?** The app lists the models your key can use, straight from the provider.

| AI | Good default | Smarter, costs more | Cheaper, less thorough |
|---|---|---|---|
| Claude | Claude Sonnet 5.5 | Claude Opus 5.5, Claude Fable 5.1 | Claude Haiku 5.5 |
| ChatGPT | GPT-6.1 Sol | GPT-6 Astra | GPT-6.1 Terra, GPT-6 Luna |

The two defaults cost about the same, roughly 5–15¢ for a typical health check; prices change, so check each provider's pricing page. The cheapest models are fine for quick lookups like "how many clients?" but tend to miss things in real troubleshooting.

## 3. Create a local-only admin account for the app (required, once per network)

> ⚠️ **The app can only sign in with a LOCAL account on the console.**
> The login you normally use (your UI.com / Ubiquiti cloud account) will **not** work. It uses two-factor sign-in, and apps can't answer the code prompt. If you try it anyway, the app tells you so; UniFi's own wording is *"MFA token required to authenticate to SSO"* (HTTP 499).
>
> A local account exists only on that one console and has no two-factor prompt, which is why it works. It's also safer: you can see what the app did, change its password, or delete it without touching your own login.
>
> Every network you add needs its own local account, created on that network's console.

1. In a browser, open the console by its **IP address**, e.g. `https://192.168.1.1`. Use the console's own address, not unifi.ui.com.
   - A security warning is normal: UniFi uses a self-signed certificate. Click **Advanced → Continue**.
2. Go to **Admins & Users** (sometimes **Settings → Admins & Users**) and choose **Create New / Add Admin**.
3. Fill it in:
   - **Restrict to local access only:** ✅ **must be ticked.** This is what makes it a local account. If you don't see this option, you're probably on unifi.ui.com; go back to the console's own IP address.
   - **Username / password:** for example `unifi-assistant` and a long random password. Use a plain username, not an email address. An email address usually means a cloud account.
   - **Role:**
     - **Full Management** (or *Site Admin*) if you want the AI to be able to make changes after your approval.
     - **View Only** if you only ever want answers and never changes. Any change the AI tries will simply be refused.
4. Click **Create**.
5. Check the new account in the admin list. It should show as **local** (local access only), not as a UI.com account.

**Alternative: a UniFi API key instead of the local account.** Newer UniFi Network versions can create API keys under **Settings → Control Plane → Integrations** (the menu name varies by version). Some versions only accept these keys for part of what this app does. When you save the connection, the app's sign-in check tells you whether the key works on your console. If it doesn't, use the local username and password instead.

**Note the console's address and port:**

| Console type | Port |
|---|---|
| UDM, UDM-Pro/SE, UDR, UCG (Cloud Gateway), Cloud Key Gen2+, UX | **443** |
| Self-hosted UniFi Network Application (Windows/Linux/Docker) | **8443** |
| UniFi OS Server | **11443** |

The app works out which type it is talking to automatically.

## 4. SSH on the console: optional, off by default

This app talks to UniFi through the same secure web connection the UniFi web page uses. That covers devices, clients, Wi-Fi, alerts, logs and settings. **You don't need SSH.**

SSH is an **optional extra per connection** for things the web API doesn't show: the console's own system log, running processes, memory and disk, config files, interface error counters. With it on, flinchbot adds those checks to health checks and troubleshooting, and the action report includes them too.

- **Read-only commands** (`cat`, `grep`, `ls`, `df`, `free`, `ps`, `ip`, `journalctl`, …) run straight away.
- **Anything that could change the console** (`reboot`, `rm`, `ip link set`, anything with `;` `&&` `>` or `$( )`, anything the app doesn't recognise) shows an Approve/Deny card with the exact command, like other changes. This is decided by the app, not by the AI.
- **Command output goes to the AI exactly as is.** The passphrase removal that applies to UniFi API data can't apply to free text, so don't ask it to print files that hold secrets.

To turn it on later (see step 6 for the form):
1. On the console, enable SSH and set a password: **Console Settings → Advanced → SSH** on UniFi OS (wording varies by version). The SSH user is `root`. On a self-hosted controller, use a login on that server.
2. In the app, open the connection (connection menu → **Manage connections… → Edit**), switch **SSH access** to **On**, enter the username, port (22) and password, and click **Test SSH**. The test records the console's SSH host key, so the app can tell if that address ever answers as a different machine. Saving with SSH on requires one successful test.

The SSH password is stored in Windows Credential Manager or the macOS Keychain, separately from the UniFi login. If you don't use SSH, leave it off on the console: SSH gives full root control, so it's one less password that can leak.

<a id="get-the-app"></a>

## 5. Get the app onto your computer

Use either of these:
- **Download:** on the GitHub page click **Code → Download ZIP**, then unzip it somewhere permanent, e.g. `C:\Apps\unifi-flinchbot` on Windows or your home folder's `Applications` or `Documents` on a Mac. Avoid running it from inside the ZIP or from Downloads.
- **Git** (if you use it): `git clone https://github.com/flinchbot/ui-tool-flinchbot.git`

The folder is self-contained, so you can move it or copy it to another PC later.

<a id="start-it"></a>

## 6. Start it

1. Double-click **`start.bat`** (Windows) or **`start.command`** (Mac).
   - It checks this computer first: that Python 3.10+ is installed, that the folder isn't read-only or still inside the ZIP, then the computer and OS, packages, where passwords will be kept, and whether the AI providers can be reached. Anything wrong is explained with the fix, and it offers to open the Python download page and the right part of this guide.
   - The first time, it creates a private Python environment (a `.venv` folder) and downloads the packages the app needs: the Anthropic and OpenAI connectors, a password-storage helper and an SSH library. This needs internet and takes about a minute.
   - **Windows:** SmartScreen may warn about running a downloaded file. Click **More info → Run anyway**. If Windows Firewall asks about Python, you can click **Cancel**: the app only listens on this computer (127.0.0.1) and doesn't need firewall access.
   - **Mac:** the first time, macOS may say *"start.command" can't be opened because Apple cannot check it*. **Right-click** (or Control-click) `start.command` → **Open** → **Open**. You only need to do this once. If it says you don't have permission, open Terminal in the app folder and run `bash start.command` instead.
   - **Mac:** the first time you save a password, macOS may ask whether Python may use your keychain. Click **Always Allow** so passwords are kept in the macOS Keychain.
2. Your browser opens at `http://127.0.0.1:8080/` and shows a **Welcome** screen:
   - **Step 1, Choose your AI:**
     1. Pick **Claude** or **ChatGPT**.
     2. Paste the key and click **Test key & load models**.
     3. Pick a model, then **Save & continue**.

     This writes the `.env` settings file for you; you never need to edit it by hand.
   - **Step 2, Your UniFi console:** enter the following, then click **Sign in & finish**:
     - a name (e.g. `Home`)
     - the console's IP address
     - the port
     - the local username and password from step 3

     That button signs in **once** to check the details. If the sign-in fails you stay on this screen with the reason, and nothing is saved. Fix it and press the button again. Pressing it again with the same rejected password does nothing, to protect you from UniFi's lockout. The check also detects the console type, lists its sites, and records the console's security certificate (see "DIFFERENT security certificate" in Troubleshooting).

     **SSH access** on the same form is **Off** by default. Leave it off unless you want what step 4 describes; you can turn it on for any connection later.
3. Keep the `start.bat` / `start.command` window open while you use the app. Close it (or press **Ctrl+C** in it) to stop the app.

## 7. Everyday use

- **Ask questions** in the chat, or click a quick prompt such as *Health check*.
- **📎 Attach text files:** click 📎 (or drag files onto the message box) to give the AI a log, config export, CSV and so on alongside your question. Up to 5 files and 150,000 characters per message; pictures, PDFs and other non-text files are refused. The file is read by your browser and sent with that message. It's never saved to disk, but it stays in the conversation (so you can ask follow-ups) until **Clear chat** or closing the app. It goes to the AI exactly as is, so remove passwords first. Big files make every later question in that chat cost more, because the AI re-reads the whole conversation each time; clear the chat when you're done with them.
- **Changes:** when the AI wants to restart a device, block a client, change Wi-Fi settings and so on, it shows a card listing exactly what will happen. Nothing changes until you click **Approve**. **Deny** cancels it. With SSH on (step 4), a command that could change the console shows the same card with the exact command.
- **Another network:** click the connection name at the top → **Add a connection…**. Once you have several, the same menu switches between them. Switching clears the chat so information from one network doesn't get mixed into another.
- **⚙ Settings:**
  - switch between **Claude** and **ChatGPT** (switching starts a new conversation)
  - change API keys or models
  - change the app's port
  
  The model list is live from the provider. If your chosen model is ever deprecated, a banner tells you its retirement date.
- **📋 Action report:** the AI reviews the whole network and lists what to fix, most urgent first. It's read-only, takes about 1–3 minutes, and costs roughly as much as a few questions.
  - **⤓ Checklist web page:** open it in any browser and tick items off as you fix them. Ticks are remembered in that browser, and **Print / PDF** works.
  - **⤓ Markdown:** for notes apps. Obsidian, VS Code and GitHub show the `[ ]` boxes as clickable checkboxes.
  - **↻ Regenerate** gives a fresh snapshot with an empty checklist, which is handy to confirm your fixes took.
- **🗺 Topology:** a map of your UniFi gear (gateway → switches → access points, with the port numbers on each link) plus **only the client devices that have a problem right now**, drawn under the device they're connected to: weak Wi-Fi signal, lots of Wi-Fi retries, low satisfaction, errors on their switch port, or flapping (connecting and disconnecting over and over in the last 24 hours). Each shows the name, IP address, MAC address, port or Wi-Fi details, and what's wrong. Scroll to zoom, drag to pan, hover for details. **⤓ Download SVG** saves it as a vector image that opens in any browser and stays sharp at any zoom. Healthy clients aren't drawn, so a quiet network shows just the gear.
- **Alerts** (left side, under the overview): active alerts in plain words, most recent first. Click **Explain** next to one and flinchbot looks into it: what it means, whether it's still happening, the likely cause, and what to do.
- **📈 WAN:** your internet speed (download and upload) and latency over the last day, week, month or 90 days, with outages marked in red. It uses the speed tests the console already runs, plus any this app runs.
  - **▶ Run a speed test now** uses the full internet connection for about a minute, so others on the network may notice.
  - **Scheduled speed test** runs one automatically every few hours while this connection is the one selected. It's off by default, and set separately for each connection.
  - **💬 Ask flinchbot about this** has the AI compare slow periods and outages and suggest causes.
- **🧾 Changes:** a log of every change made or refused through this app, with who, when, on which console and exactly what. Click an entry for details. **⤓ CSV** opens in Excel or Numbers. The log stays on this computer (`data/audit.jsonl`); passwords are never written to it.
- **↶ Undo:** after you approve a change to a Wi-Fi network or LAN/VLAN (including deleting a Wi-Fi network or creating a new one), an **↶ Undo** button appears on the approval card and in 🧾 Changes. It puts back exactly what was there before. If someone changed the same setting again in the meantime, the app asks before overwriting it. A deleted Wi-Fi network comes back as a new network with the same settings and password. Other changes (restarts, blocking a client, firewall rules) can't be undone automatically; the log tells you what was done so you can reverse it in UniFi.
- **💾 Backup:** one click saves a backup of this console's UniFi Network settings to your **Downloads** folder (a `.unf` file named after the network and date). To restore it, open the console in a browser and go to **Settings → System → Backups → Restore** (wording varies by version). It contains your whole network configuration, Wi-Fi passwords included, so keep it somewhere private.
- **⤓ Export** (above the chat): save the conversation as Markdown or a web page. Files go to your Downloads folder.
- **🎨 Theme:** pick a look: Default, Matrix, Windows 95, Star Wars, My Little Pony, Blade Runner or Candle Maker. Each one changes colours, fonts, text size and screen layout, and each has a light and a dark version (or *Auto*, which follows the Windows or macOS setting). The choice is remembered in that browser. If animations are turned off in Windows or reduced in macOS, the moving backgrounds stay still. The movie and show themes are fan-inspired: colours and moods only, no logos or characters.
- **About:** click the robot logo for the version, the AI in use, and this guide.

## 8. Updating the app

- **Downloaded ZIP:**
  1. Download the new ZIP.
  2. Unzip it over the old folder (or into a new one).
  3. Copy across your `.env` file and `data` folder.
- **Git:** run `git pull` in the app folder.

Your settings, connections and passwords aren't touched by an update. `start.bat` / `start.command` notices new requirements and installs them on the next start.

<a id="where-stored"></a>

## 9. Where your information is stored, and what is sent where

| What | Where |
|---|---|
| AI choice, API key(s), models, port | `.env` file in the app folder |
| Connection details (name, address, port, site, username, SSH on/off, SSH username and port) | `data\connections.json` |
| Change log, speed tests this app ran | `data\audit.jsonl`, `data\speedtests.jsonl` |
| UniFi passwords / API keys, SSH passwords, previous Wi-Fi passwords kept for Undo | **Windows:** Credential Manager (Control Panel → Credential Manager → Windows Credentials). **Mac:** the Keychain (open *Keychain Access* and search). Entries are named `unifi-claude`, the app's original name, kept so saved passwords keep working. |

If that password store isn't available, passwords fall back to `data/secrets.json` and the app shows a warning.

`.env` and `data\` are excluded from git, so they're never uploaded to GitHub. Don't email or share them.

**What leaves this computer:**
- When you ask a question, the network data the AI needs goes to the AI provider you chose (Anthropic or OpenAI): device names, client names, IPs, settings and logs.
- Wi-Fi passwords and other secret fields are **removed first**. (Files you attach and SSH command output are sent as they are; that removal only applies to data the app reads from the UniFi API.)
- ChatGPT requests are sent with OpenAI's "don't store" option.
- Themes download their fonts from Google Fonts the first time you use them (Windows 95 uses fonts already on the computer). Only the font request goes out; no network data does.
- Nothing is sent anywhere else, and the app can't be reached from other computers.

**Moving to a new computer** (Windows ↔ Mac works too):
1. Copy the folder.
2. Re-enter the UniFi and SSH passwords. Credential Manager and Keychain entries don't travel with the folder.
3. Skip copying `.venv`; `start.bat` / `start.command` rebuilds it (and rebuilds it automatically if you copied one from the other kind of computer).

<a id="troubleshooting"></a>

## 10. Troubleshooting

| Message / symptom | What to do |
|---|---|
| *"Python isn't installed"* or *"needs Python 3.10 or newer"* | Answer **Y** to open the download page and step 1 of this guide, install Python, then close and reopen the `start.bat` / `start.command` window. |
| A line marked **XX** in the *Environment check* | The app can't start until that is fixed; the line says how, and the launcher offers to open the right section of this guide. Lines marked **!!** are warnings: the app still starts. |
| *"running from inside the downloaded ZIP file"* | Right-click the ZIP → **Extract All…** into a permanent folder (step 5) and start it from there. |
| Mac: *"start.command" can't be opened because Apple cannot check it* | Right-click `start.command` → **Open** → **Open** (once). |
| Mac: *"permission denied"* when starting | In Terminal, in the app folder, run `bash start.command`, or `chmod +x start.command` once. |
| *"Could not start on 127.0.0.1:8080"* | Another program uses port 8080. Run `start.bat --port 8090` (Windows) or `./start.command --port 8090` (Mac), or change the port in Settings. |
| *"Timed out reaching https://…"* | This PC can't reach the console. Is it on that network or VPN? Can you open `https://<console IP>` in the browser? |
| *"refused the connection"* | Wrong port. See the port table in step 3. |
| *"UI.com cloud account with two-factor sign-in"* (UniFi's raw error: *HTTP 499, MFA token required*) | You entered your own UI.com login. Create the separate **local-only** admin from step 3 and use that instead. |
| *"Login rejected"* | Check the username and password. The account must be **local-only** (step 3). |
| *"UniFi has temporarily locked sign-ins"*, or a banner saying flinchbot **stopped trying** | Too many failed sign-ins (from any app or browser) make UniFi lock sign-ins for a while; Ubiquiti doesn't say how long. flinchbot never retries a rejected password by itself, and after a lockout it waits at least 10 minutes. Fix the password (**Edit connection**), wait, then press **Retry once**. Closing the app also guarantees no more attempts. |
| *"Permission denied … read-only"* | The UniFi account is *View Only*. Give it Full Management if you want changes to work. |
| *"rejected the API key"* (UniFi) | That UniFi version doesn't accept API keys for this feature. Switch the connection to username + password. |
| *"DIFFERENT security certificate"* | The address now answers with a different console certificate. Typical causes: a factory reset, a replaced console, or being on a different network where that IP belongs to something else. If you're sure it's your console, open the connection and click **Trust new certificate**. |
| *"DIFFERENT SSH host key"* | Same idea for SSH. If you're sure it's your console, open the connection, click **Test SSH**, and save. |
| *"SSH login as 'root' was rejected"* | On UniFi OS the SSH password is set under **Console Settings → Advanced → SSH**; it isn't the UniFi login. The app won't resend a rejected SSH password; change it and test again. |
| *"The command did not finish within … seconds"* | The AI ran something that waits for input or never ends. Ask again; it will use `-c` with ping, `-b` with top, and so on. |
| *"…rejected the API key"* / *"out of credits"* / *"no API credit"* (AI) | Fix the key in Settings. Or add API credit at console.anthropic.com (Claude) or platform.openai.com → Billing (ChatGPT). |
| *"…model … wasn't found"* or a *deprecated* banner | Open Settings and pick a current model from the list. |
| *"Reply hit the …-token output limit"* | Say "continue", ask a narrower question, or raise `MAX_OUTPUT_TOKENS` in `.env` (max 21000). |
| *"The app was restarted. Reload this page"* | Press F5. The page's security token changes each time the app starts. |
| Firewall rules the AI creates don't seem to work | Newer UniFi versions use **Zone-Based Firewall**. This app creates classic rules, which may not apply there. Check in the UniFi UI. |

Still stuck? Run the diagnostic and send its output to whoever is helping you. It contains no passwords.
```
.venv\Scripts\python backend\probe_events.py
```
On a Mac:
```
.venv/bin/python backend/probe_events.py
```
