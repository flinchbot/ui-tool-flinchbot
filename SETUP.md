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
3. In Finder, open **Applications → Python 3.x** and double-click **Install Certificates.command**. It gives Python the list of trusted websites it needs to download app updates and AI prices. (The app brings its own copy too, but this makes sure.) Close the window it opens when it says it's done. Skip this step if you installed Python with Homebrew.

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

## 2. Get an AI API key (once): Claude or ChatGPT, or use a local AI

Pick one. You can add the others later and switch in Settings. They work the same way in the app: same tools, same approval step, same reports. To run the AI on your own hardware instead, see Option C below.

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

<a id="local-ai"></a>

**Option C: Local AI (your own computer or one in your office)**

Instead of, or as well as, Claude and ChatGPT, the app can use an AI that runs on a computer you own: **Ollama** (Windows, Mac, Linux), **LM Studio** (Windows, Mac), llama.cpp, vLLM, Jan, or anything else that speaks OpenAI's Chat Completions API. Your network data then goes only to that computer. It costs nothing per question, but the answers are only as good as the model, and a model needs a capable computer (a recent graphics card, or an Apple silicon Mac with plenty of memory).

Two things decide whether it works well:
- **The model must support tools.** The app gives the AI about 40 tools, and small 7–8B models often pick the wrong one. Pick a model with tool support, ideally 20B parameters or larger, for example qwen3, gpt-oss, mistral-small or llama3.3. Approve still stops any bad change.
- **The server must read a long enough prompt (its context size).** The app's instructions and tool list alone are about 6,000 tokens. Use **32,768** or more. Ollama's default is much smaller, and it silently drops the start of a longer prompt, which is exactly the instructions and tool list.

*Ollama on a PC in your office:*
1. Install Ollama from **https://ollama.com** and download a model in a terminal, for example `ollama pull qwen3:30b`.
2. Set two environment variables on that PC (Windows: Settings → System → About → Advanced system settings → Environment Variables), then quit and restart Ollama:
   - `OLLAMA_CONTEXT_LENGTH` = `32768`
   - `OLLAMA_HOST` = `0.0.0.0` (only if the app runs on a *different* computer; it makes Ollama accept connections from the network)
3. Ollama has no password, so in that PC's firewall allow port 11434 only from your own computers.

*LM Studio on a Mac:*
1. Download a model in LM Studio. When you load it, set **Context Length** to 32768 or more.
2. Open the **Developer** tab and start the server (port 1234). If the app runs on another computer, turn on **Serve on local network**.

*In the app:* ⚙ Settings → **Local AI**. Pick the server type, type the address (on the same computer `localhost`, otherwise that computer's name or IP, for example `http://office-pc:11434`), or click **🔍 Find on this computer**. Click **Test & load models**, pick a model, then **Check this model**. It checks that the model calls tools, and that the server really reads a prompt as long as the context size you entered. It tells you what to change if not.

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
   - It checks this computer first: that Python 3.10+ is installed, that the folder isn't read-only or still inside the ZIP, then the computer and OS, packages, where passwords will be kept, whether the internet can be reached (offline is fine with Local AI), and whether your Local AI server answers. Anything wrong is explained with the fix, and it offers to open the Python download page and the right part of this guide.
   - The first time, it creates a private Python environment (a `.venv` folder) and downloads the packages the app needs: the Anthropic and OpenAI connectors, a password-storage helper, an SSH library, an encryption library and a list of root certificates. This needs internet and takes about a minute. It then lists every package it installed, with its version and why it's there: one the app asks for (and what for), or one another package needs. The same list is kept in `.venv/install-log.txt`. This happens again only when an update changes the list of packages.
   - **Windows:** SmartScreen may warn about running a downloaded file. Click **More info → Run anyway**. If Windows Firewall asks about Python, you can click **Cancel**: the app only listens on this computer (127.0.0.1) and doesn't need firewall access.
   - **Mac:** the first time, macOS may say *"start.command" can't be opened because Apple cannot check it*. **Right-click** (or Control-click) `start.command` → **Open** → **Open**. You only need to do this once. If it says you don't have permission, open Terminal in the app folder and run `bash start.command` instead.
   - **Mac:** the first time you save a password, macOS may ask whether Python may use your keychain. Click **Always Allow** so passwords are kept in the macOS Keychain.
2. Your browser opens at `http://127.0.0.1:8080/` and shows a **Welcome** screen:
   - **Step 1, Choose your AI:**
     1. Pick **Claude**, **ChatGPT** or **Local AI** (for Local AI, see [Option C](#local-ai)).
     2. Paste the key (Local AI: the address) and click **Test key & load models**.
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
- **📎 Attach text files and images:** click 📎, drag files onto the chat, or paste a screenshot (Ctrl+V / Cmd+V) in the message box to give the AI a log, config export, CSV, or a screenshot or photo alongside your question. Up to 5 files per message: text up to 150,000 characters, images up to 5 MB each (PNG, JPEG, GIF, WebP; BMP, HEIC and TIFF are converted by your browser if it can open them; large images are scaled down to 1568 px, which is all the AI looks at anyway). PDFs and other files are refused. The file is read by your browser and sent with that message. It's never saved to disk, but it stays in the conversation (so you can ask follow-ups) until **Clear chat** or closing the app. It goes to the AI exactly as is, so remove passwords first and crop them out of screenshots. Big files and images make every later question in that chat cost more, because the AI re-reads the whole conversation each time; clear the chat when you're done with them.
- **⚖ Second opinion:** under each answer you see which AI wrote it. Click **Second opinion**: with one other AI set up it goes straight to it; with two, pick one (Claude, ChatGPT or Local AI). It reads the whole conversation, including the data the first AI gathered, checks the answer, may look up more itself, and replies below: what was right, what was wrong or missing, and its own answer. Your next question goes back to the AI chosen in Settings. If you mainly use a local AI, this sends that conversation to Anthropic or OpenAI, but only when you click it. A change it suggests still waits for **Approve**.
- **Offline:** with Local AI as the chosen AI, the app works without internet. Buttons that need it (update check, price updates, testing a Claude or ChatGPT key, speed tests, second opinions from Claude or ChatGPT) are greyed out and say *Not available: offline*, and come back by themselves when the connection does. If Claude or ChatGPT is the chosen AI, a banner explains that it can't answer until you're back online.
- **■ Stop:** changed your mind while the AI is working? Click **■** (where Send was) or press **Esc**. The question is dropped, so it isn't part of the conversation, and you can ask something else straight away. If you had just approved a change, the change stays done; only the answer after it is dropped. The AI provider may still charge for what it generated before you stopped it.
- **Changes:** when the AI wants to restart a device, block a client, change Wi-Fi settings and so on, it shows a card listing exactly what will happen. Nothing changes until you click **Approve**. **Deny** cancels it. With SSH on (step 4), a command that could change the console shows the same card with the exact command.
- **Another network:** click the connection name at the top → **Add a connection…**. Once you have several, the same menu switches between them. Switching clears the chat so information from one network doesn't get mixed into another.
- **⚙ Settings:**
  - switch between **Claude**, **ChatGPT** and **Local AI** (the conversation carries on with the new AI)
  - change API keys or models, and the Local AI address
  - change the app's port
  
  The model list is live from the provider. If your chosen model is ever deprecated, a banner tells you its retirement date.
- **📋 Action report:** the AI reviews the whole network and lists what to fix, most urgent first. It's read-only, takes about 1–3 minutes, and costs roughly as much as a few questions.
  - The latest report of each connection is saved, so opening it again is instant and free, even after a restart. **↻ Refresh report** at the top runs a new review.
  - **💬 Ask** on an item closes the report and asks flinchbot about that item in the chat: it checks whether it's still the case and walks you through the fix.
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
- **🧰 Tools** (top right) opens the rest:
  - **🔌 Switch port map:** a drawing of every UniFi switch's ports, like the front panel, coloured by link speed, with PoE watts, the port's uplink, and a list of what is plugged into each port (other UniFi gear, LLDP neighbours, wired clients with name and IP). Ports with errors get an orange outline, or red for 1,000+ errors or half duplex: usually a bad cable or connector. Error counts are since the switch last restarted. **✎** renames a port and **⏻** power-cycles a PoE port (to reboot a stuck camera or access point); both are logged, and a rename can be undone.
  - **🕵 Device detective:** every client without a name, and anything that joined in the last 7 days, most mysterious first, with every clue: UniFi's own fingerprint guess, the maker from the MAC address, a *private MAC* badge (phones, tablets and laptops make up an address per Wi-Fi network, so the maker is hidden), and what the hostname suggests. **✎ Name** names it in UniFi (with Undo). **🔍 Scan** runs an Nmap scan of that one device (see [Nmap](#nmap)) to see what it runs. **💬 Ask** hands it to flinchbot to work out.
  - **🛡 Security scorecard:** a grade from A to F, worked out by fixed rules from your console's settings (no AI): open or weak Wi-Fi, short Wi-Fi passwords, firmware waiting, UPnP, intrusion prevention, port forwards to risky services, firewall policies that let the internet in or give one network full access to another, smart-home devices on the main network. Each finding says why it matters and how to fix it in UniFi; **💬 Fix with flinchbot** asks the AI to propose the change, which you approve as usual. With Nmap installed, **🔍 Scan devices for risky ports** checks every connected device for telnet, file sharing, remote desktop, VNC, unencrypted smart-home messaging and the like. It takes a few minutes, and some devices or the gateway's intrusion prevention may log it as a port scan.
  - **📘 Network handbook:** one web page documenting the whole network: networks and VLANs with DHCP ranges, Wi-Fi, every device with firmware and uplink, the topology, every switch port, firewall policies, port forwards, routes, VPNs, named clients and fixed IPs. It downloads and opens in a new tab; **Print → Save as PDF** makes a PDF. Wi-Fi passwords are left out unless you tick the box.
  - **♻ Factory-reset kit:** see [Before a factory reset](#reset-kit).
  - **💰 AI usage and cost:** see below.
  - **⬆ Updates:** see [Updating the app](#updates).
- **💰 Cost meter** (above the chat): tokens used and the estimated cost of this conversation. Click it for today, the last 7 days and this month, per model, with an optional monthly budget that turns the meter orange at 80% and red when it's reached (a warning only; nothing is blocked). Token counts are exact, because every AI response reports them. Dollars are an estimate: tokens × the published price per million tokens. Neither Anthropic nor OpenAI offers a price feed, so the app uses, in order: a price you type in (✎), LiteLLM's public price list (**↻ Update prices** downloads it from GitHub; it covers Claude and GPT models), or Claude prices copied from Anthropic's pricing page. Your provider's invoice is the final word.
- **⤓ Export** (above the chat): save the conversation as Markdown or a web page. Files go to your Downloads folder.
- **🎨 Theme:** pick a look: Default, Matrix, Windows 95, Star Wars, My Little Pony, Blade Runner, Candle Maker, Notre Dame Football, FC Bayern München, 80s Arcade or Oktoberfest. Each one changes colours, fonts, text size and screen layout, and each has a light and a dark version (or *Auto*, which follows the Windows or macOS setting). The choice is remembered in that browser. If animations are turned off in Windows or reduced in macOS, the moving backgrounds stay still. The movie, show and team themes are fan-inspired: colours and moods only, no logos, crests or characters.
- **About:** click the robot logo for the version, the AI in use, and this guide.

<a id="reset-kit"></a>

## 8. Before a factory reset

**🧰 Tools → ♻ Factory-reset kit** keeps a copy of everything you set up, shows how it differs from a fresh console, and writes SSH commands that put back whichever parts you want, one at a time.

1. **Before the reset:**
   1. Click **📸 Take a snapshot**. It reads the console's whole configuration (nothing is changed): networks and VLANs with DHCP pools, WANs, VPN clients and servers, firewall zones and policies, port forwards, routes and policy-based routes, DNS records, Wi-Fi networks, client names and fixed IPs, device names and port settings, scheduled tasks and every settings page.
   2. Also click **💾 Backup** for a full UniFi backup as a safety net.
   3. Open the snapshot to see the list: **added** (things you created), **changed** (built-in things you changed, such as the main LAN's address), and **built-in** sections with their current values. Built-in sections are unticked, because the app can't know a fresh console's values until it sees one.
2. **Reset the console** and set it up again in UniFi.
3. **After the reset:**
   1. Create a local admin (step 3) and turn SSH on (step 4).
   2. Fix this app's connection: new address if it changed, the new password, and **Trust the new certificate** when asked.
   3. Open the snapshot again and tick **Compare with … now**. The list shrinks to exactly what is missing or different.
   4. Tick what you want back and click **⤓ SSH script (.sh)**.
4. **Run the commands:**
   1. `ssh root@<console address>` (the SSH password from step 4).
   2. Paste the **SETUP** block first, all at once. It asks for the local admin's username and password and signs in to the console's own API.
   3. Then paste the commands you want, top to bottom. Each one has a comment saying what it does and what it needs first (for example, a Wi-Fi network needs its VLAN). Blocks that must run together say **RUN TOGETHER**. Each prints **OK**, or **FAILED** with the console's reason.
   4. Paste a command that asks for a password or key **on its own**: anything pasted after it would be taken as the answer. A WireGuard config is pasted whole, followed by a line with just `END`. **🔑 Show passwords** in the app shows each one with a Copy button.
   5. Adopt your UniFi devices before the *devices* section.
   6. The **RUN LAST** section changes the console's own LAN address (if you had changed it), so your SSH session drops there. Reconnect at the new address.

Passwords, Wi-Fi keys and VPN private keys in a snapshot are encrypted on disk; the key is kept in Credential Manager or the Keychain. The script asks for them while it runs, unless you choose **Write them into the script file**. In that case the file holds them in plain text, so delete it when you're done. Keys the console makes for itself (device adoption, mesh, its API token) are never written back.

<a id="nmap"></a>

### Nmap (optional)

Nmap lets the device detective and the security scorecard scan devices on your network. It's optional; everything else works without it.

- **Windows:** download the installer from [nmap.org/download](https://nmap.org/download) and accept the Npcap option.
- **Mac:** download the installer from the same page, or in Terminal: `brew install nmap`.
- **Linux:** `sudo apt install nmap` (or your distribution's equivalent).

Restart the app afterwards; the startup check shows **Nmap found**. The app only ever runs basic TCP connect scans (no administrator rights needed), only on private LAN addresses that the console knows as clients, and only when you click a scan button or approve flinchbot's request. Every scan is logged in 🧾 Changes.

<a id="updates"></a>

## 9. Updating the app

The app checks GitHub for a new version once a day. When there is one, **⬆ Update to v…** appears at the top right.

1. Click it to see what changed, then **Install … and restart**.
   - The app downloads the new version from GitHub and writes its files over the app folder.
   - Your `.env` file (settings and AI keys), the `data` folder (connections, change log, snapshots) and `.venv` are never touched, and saved passwords stay where they are.
   - Before a file is replaced, it is copied into `data\updates\`. **Roll back** in the same window puts the previous version back (the last 3 are kept).
2. The app restarts in its own window, installs any new packages, and the browser page reconnects by itself.

**🧰 Tools → ⬆ Updates** checks at any time and has the switch for the daily check. The check only asks GitHub for the newest version number and its change notes; nothing about you or your network is sent.

- **The repository is private:** the app needs a GitHub token that can read it. Paste it in the Updates window; it is kept in Credential Manager or the Keychain and only sent to GitHub. To make one, on github.com go to your picture → **Settings → Developer settings → Personal access tokens**:
  - **If you own the repository:** **Fine-grained tokens → Generate new token**. Under *Repository access* choose **Only select repositories** and pick this one. Under *Permissions → Repository permissions*, set **Contents** to **Read-only**. Generate it and copy it (it starts with `github_pat_`).
  - **If someone shared the repository with you:** fine-grained tokens can't reach another person's repository, so use **Tokens (classic) → Generate new token (classic)** with the **repo** scope. Note that it can read every repository you can.
  - If the GitHub CLI (`gh`) is installed and signed in on this computer, the app uses it and you don't need a token.
- **Git checkout** (you used `git clone`): the app doesn't overwrite it. Run `git pull` in the app folder, then restart the app.
- **Versions before 2.7.0** don't have the updater. Update by hand once:
  1. Download the new ZIP.
  2. Unzip it over the old folder (your `.env` and `data` folder stay as they are).
  3. Start the app again.
- **A fork or another branch:** set `UPDATE_REPO=owner/name` (and `UPDATE_BRANCH=…`) in `.env`.

<a id="update-rollback"></a>

### If an update goes wrong

If the new version won't start, put the previous one back from the app folder:
- **Windows:** in the app folder, click the address bar, type `cmd`, press Enter, then run `.venv\Scripts\python backend\updater.py --rollback`.
- **Mac:** in Terminal, in the app folder: `.venv/bin/python backend/updater.py --rollback`.

Then start the app again.

<a id="where-stored"></a>

## 10. Where your information is stored, and what is sent where

| What | Where |
|---|---|
| AI choice, API key(s), models, Local AI address and context size, port | `.env` file in the app folder |
| Connection details (name, address, port, site, username, SSH on/off, SSH username and port) | `data\connections.json` |
| Change log, speed tests this app ran | `data\audit.jsonl`, `data\speedtests.jsonl` |
| AI token use and estimated cost, prices and budget | `data\usage.jsonl`, `data\prices.json` |
| Factory-reset kit snapshots (passwords in them encrypted) | `data\resetkit\` |
| The latest action report of each connection | `data\reports\` |
| Update check result, and the files the last updates replaced (for Roll back) | `data\updates\` |
| UniFi passwords / API keys, SSH passwords, previous Wi-Fi passwords kept for Undo, the key to each reset-kit snapshot, a GitHub token for updates (if you saved one) | **Windows:** Credential Manager (Control Panel → Credential Manager → Windows Credentials). **Mac:** the Keychain (open *Keychain Access* and search). Entries are named `unifi-claude`, the app's original name, kept so saved passwords keep working. |

If that password store isn't available, passwords fall back to `data/secrets.json` and the app shows a warning.

`.env` and `data\` are excluded from git, so they're never uploaded to GitHub. Don't email or share them.

**What leaves this computer:**
- When you ask a question, the network data the AI needs goes to the AI you chose: Anthropic, OpenAI, or your Local AI server. That data is device names, client names, IPs, settings and logs. With Local AI it stays on that computer, unless you click **Second opinion** and pick Claude or ChatGPT.
- Wi-Fi passwords and other secret fields are **removed first**. (Files you attach and SSH command output are sent as they are; that removal only applies to data the app reads from the UniFi API.)
- ChatGPT requests are sent with OpenAI's "don't store" option.
- **↻ Update prices** (AI usage) downloads LiteLLM's public price list from GitHub. Nothing is sent with that request.
- Nmap scans stay on your own network.
- **Updates:** once a day (and when you click **Check now**) the app asks GitHub (`api.github.com`) for the newest version number and its change notes, sending the GitHub token if you saved one. Installing downloads the new version from GitHub. Nothing about you or your network is sent.
- Themes download their fonts from Google Fonts the first time you use them (Windows 95 uses fonts already on the computer). Only the font request goes out; no network data does.
- **Internet check:** every minute the app opens (and at once closes) a connection to api.anthropic.com, api.openai.com and api.github.com, to know whether you're online. Nothing is sent. Behind a proxy it doesn't check.
- Nothing is sent anywhere else, and the app can't be reached from other computers.

**Moving to a new computer** (Windows ↔ Mac works too):
1. Copy the folder.
2. Re-enter the UniFi and SSH passwords. Credential Manager and Keychain entries don't travel with the folder.
3. Skip copying `.venv`; `start.bat` / `start.command` rebuilds it (and rebuilds it automatically if you copied one from the other kind of computer).

<a id="troubleshooting"></a>

## 11. Troubleshooting

| Message / symptom | What to do |
|---|---|
| *"Python isn't installed"* or *"needs Python 3.10 or newer"* | Answer **Y** to open the download page and step 1 of this guide, install Python, then close and reopen the `start.bat` / `start.command` window. |
| A line marked **XX** in the *Environment check* | The app can't start until that is fixed; the line says how, and the launcher offers to open the right section of this guide. Lines marked **!!** are warnings: the app still starts. |
| *"running from inside the downloaded ZIP file"* | Right-click the ZIP → **Extract All…** into a permanent folder (step 5) and start it from there. |
| Mac: *"start.command" can't be opened because Apple cannot check it* | Right-click `start.command` → **Open** → **Open** (once). |
| Mac: *"permission denied"* when starting | In Terminal, in the app folder, run `bash start.command`, or `chmod +x start.command` once. |
| *"UniFi x flinchbot … is already running on port 8080"* | The app is already open in another window, maybe an older copy you didn't close before updating. Close that window (or press Ctrl+C in it), then start the app again. |
| *"Unknown endpoint …"* in a window of an older version (before 2.7.1) | An older copy of the app is still running, and its files were updated underneath it. Close every app window and start it once. |
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
| *Nmap isn't installed* (🔍 Scan is greyed out) | Nmap is optional. Install it ([Nmap](#nmap)), then restart the app. |
| A scan finds nothing on a device you know is there | Phones, tablets and many smart-home devices answer on no ports at all. A firewall on the device (Windows, macOS) hides ports too. |
| Updates: *GitHub didn't find … If the repository is private* | The repository is private, so the app needs a GitHub token. See [Updating the app](#updates). |
| Updates: *GitHub refused the token* | The token expired or was revoked. Make a new one and save it in **⬆ Updates**, or remove it. |
| Updates: *This app folder is a git checkout* | Run `git pull` in the app folder, then restart the app. |
| The app doesn't come back after an update | Look at the app's window for the message. If the new version won't start, see [If an update goes wrong](#update-rollback). |
| Restore script: *Sign-in FAILED* | Use a **local** admin of the console (step 3), not your UI.com account, and check SSH is to the console itself. |
| Restore script: *NOT FOUND: nothing in … has name = …* | That command needs something that isn't there yet (its comment says what). Run that one first, then this one again. |
| Restore script: *FAILED: HTTP 400 …* | The console refused that setting; its reason follows. Usually a newer UniFi version names a setting differently: set that one by hand in the UniFi UI and carry on with the rest. |
| The cost meter shows **—** for a model | The app has no price for it. Click the meter → **↻ Update prices**, or ✎ to type the price from your provider's pricing page. |
| *Can't reach the local AI at …* | The AI server isn't running, or doesn't accept connections from this computer. On the AI computer: Ollama needs `OLLAMA_HOST=0.0.0.0` (then restart Ollama); LM Studio needs **Serve on local network**. Check its firewall allows the port (11434 for Ollama, 1234 for LM Studio). See [Option C](#local-ai). |
| **Check this model** says the model *didn't call a tool* | That model can't use tools, or is too small to do it reliably. Pick one with tool support, ideally 20B or larger (qwen3, gpt-oss, mistral-small, llama3.3). |
| **Check this model** says the server *read only part* of the test | Its context size is smaller than the one in Settings, so it would silently cut long conversations. Ollama: set `OLLAMA_CONTEXT_LENGTH` to the same number and restart Ollama. LM Studio: reload the model with a larger **Context Length**. |
| Local AI answers are slow | A big model on a computer without a strong graphics card (or Apple silicon) can take minutes. Try a smaller model, or ask narrower questions. **■ Stop** ends a slow answer. |
| *Couldn't download the price list* or *Couldn't reach GitHub*: … CERTIFICATE_VERIFY_FAILED … unable to get local issuer certificate | Python from python.org on a Mac ships without root certificates. Since v2.10.0 the app brings its own (the `certifi` package), and the startup check shows **Certs** with how many it found. On an older version the updater can't reach GitHub to fetch that fix: open **Applications → Python 3.x** in Finder, double-click **Install Certificates.command**, then click **Check now** under Updates. |

Still stuck? Run the diagnostic and send its output to whoever is helping you. It contains no passwords.
```
.venv\Scripts\python backend\probe_events.py
```
On a Mac:
```
.venv/bin/python backend/probe_events.py
```
