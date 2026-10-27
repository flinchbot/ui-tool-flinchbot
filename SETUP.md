# Setup guide (start here)

This guide assumes no programming experience. Plan on about 20 minutes the first time.

**What you'll end up with:** a small app on your Windows PC. You double-click it, it opens in your web browser, and you can ask Claude questions about a UniFi network ("why is the Wi-Fi slow in the hall?"). Claude can also make changes, but only after you click **Approve**.

**What you need**

| | |
|---|---|
| A Windows 10 or 11 PC | It must be able to reach the UniFi console: plugged into that network, on its Wi-Fi, or connected by VPN. |
| Admin access to the UniFi console | Used once, to create a dedicated login for this app (step 3). |
| An Anthropic account with API credits | This is how Claude is paid for (step 2). It is separate from a Claude.ai chat subscription. |

---

## 1. Install Python (once per PC)

Python is the language the app is written in. You install it once and never have to open it.

1. Go to **https://www.python.org/downloads/**. Download the Windows installer for the newest version (3.10 or newer is required).
   - On current releases, the download is the **Python install manager**. Run it, accept the defaults, and when it asks whether to run its configuration check, say **yes**.
   - If you get the older-style installer instead, **tick "Add python.exe to PATH"** on the first screen before clicking *Install Now*.
2. Check it worked. Press **Win + R**, type `cmd`, press Enter, and in the black window type:
   ```
   py --version
   ```
   You should see something like `Python 3.14.3`.

> **"python" opens the Microsoft Store instead?** Windows ships a fake `python` shortcut. Open **Settings → Apps → Advanced app settings → App execution aliases** and switch off the two *App Installer python* entries. `start.bat` tries the `py` command first, so you usually won't hit this.

## 2. Get an Anthropic API key (once)

1. Sign in at **https://console.anthropic.com**.
2. Under **Billing**, add some credits. A few dollars lasts a long time for occasional questions. Each question costs a fraction of a cent to a few cents, depending on the model and how much network data Claude reads.
3. Go to **API Keys → Create Key** and give it a name like `UniFi app`.
4. **Copy the key now.** It starts with `sk-ant-` and is shown only once. You'll paste it into the app in step 6.

Treat this key like a credit card number. Anyone who has it can spend your credits. If it ever leaks, delete it on the same page and create a new one.

## 3. Create a login for the app on the UniFi console (once per network)

Don't use your own UniFi login. Give the app its own local account. That way you can see what it did, change its password, or remove it without affecting you.

1. In a browser, open the console by its **IP address**, e.g. `https://192.168.1.1`. Use the console's own address, not unifi.ui.com.
   - A security warning is normal: UniFi uses a self-signed certificate. Click **Advanced → Continue**.
2. Go to **Admins & Users** (sometimes **Settings → Admins & Users**) and choose **Create New / Add Admin**.
3. Fill it in:
   - **Restrict to local access only:** ✅ ticked. This is what makes it a local account. Cloud UI.com accounts with two-factor sign-in cannot be used by apps.
   - **Username / password:** for example `claude-app` and a long random password.
   - **Role:**
     - **Full Management** (or *Site Admin*) if you want Claude to be able to make changes after your approval.
     - **View Only** if you only ever want answers and never changes. Any change it tries will then simply be refused.
4. Click **Create**.

**Optional: use a UniFi API key instead of a password.** Newer UniFi Network versions can create API keys under **Settings → Control Plane → Integrations** (the menu name varies by version). Some versions only accept these keys for part of what this app does. The app's **Test** button tells you whether the key works on your console. If it doesn't, use the username and password instead.

**Note the console's address and port:**

| Console type | Port |
|---|---|
| UDM, UDM-Pro/SE, UDR, UCG (Cloud Gateway), Cloud Key Gen2+, UX | **443** |
| Self-hosted UniFi Network Application (Windows/Linux/Docker) | **8443** |
| UniFi OS Server | **11443** |

The app works out which type it is talking to automatically.

## 4. Do you need to enable SSH on the UniFi gear? **No.**

This app talks to UniFi through the same secure web connection the UniFi web page uses. It never uses SSH.

Leave SSH turned off. SSH gives full root control of the console, so it's one less password that can leak. If you previously turned it on for this app, you can turn it off again under **Settings → Control Plane → Console** (wording varies).

## 5. Get the app onto the PC

Use either of these:
- **Download:** on the GitHub page click **Code → Download ZIP**, then unzip it somewhere permanent, e.g. `C:\Apps\ui-tool-claude`. Avoid running it from inside the ZIP or from Downloads.
- **Git** (if you use it): `git clone https://github.com/flinchbot/ui-tool-claude.git`

The folder is self-contained, so you can move it or copy it to another PC later.

## 6. Start it

1. Double-click **`start.bat`**.
   - The first time, it creates a private Python environment (a `.venv` folder) and downloads two packages. This needs internet and takes about a minute.
   - Windows SmartScreen may warn about running a downloaded file. Click **More info → Run anyway**.
   - If Windows Firewall asks about Python, you can click **Cancel**. The app only listens on this PC (127.0.0.1) and doesn't need firewall access.
2. Your browser opens at `http://127.0.0.1:8080/` and shows a **Welcome** screen:
   - **Step 1, Claude:** paste the `sk-ant-…` key and click **Test key & load models**. Pick a model, then **Save & continue**. This writes the `.env` file for you; you never need to edit it by hand.
   - **Step 2, your UniFi console:** enter the following, then click **Test, save & finish**:
     - a name (e.g. `Home`)
     - the console's IP address
     - the port
     - the username and password from step 3
3. Keep the black `start.bat` window open while you use the app. Close it (or press **Ctrl+C** in it) to stop the app.

## 7. Everyday use

- **Ask questions** in the chat, or click a quick prompt such as *Health check*.
- **Changes:** when Claude wants to restart a device, block a client, change Wi-Fi settings and so on, it shows a card listing exactly what will happen. Nothing changes until you click **Approve**. **Deny** cancels it.
- **Another network:** click the connection name at the top → **Add a connection…**. Once you have several, the same menu switches between them. Switching clears the chat so information from one network doesn't get mixed into another.
- **Settings (⚙):** change the API key, the model, or the app's port.

## 8. Where your information is stored

| What | Where |
|---|---|
| Anthropic API key, model, port | `.env` file in the app folder |
| Connection details (name, address, port, site, username) | `data\connections.json` |
| UniFi passwords / API keys | Windows Credential Manager (Control Panel → Credential Manager → Windows Credentials → entries named `unifi-claude`) |

If Credential Manager isn't available, passwords fall back to `data\secrets.json` and the app shows a warning.

`.env` and `data\` are excluded from git, so they're never uploaded to GitHub. Don't email or share them.

Moving to a new PC: copy the folder, then re-enter the passwords. Credential Manager entries don't travel with the folder. Skip copying `.venv`; `start.bat` rebuilds it.

## 9. Troubleshooting

| Message / symptom | What to do |
|---|---|
| *"Python 3.10 or newer was not found"* | Redo step 1, then close and reopen the `start.bat` window. |
| *"Could not start on 127.0.0.1:8080"* | Another program uses port 8080. Run `start.bat --port 8090`, or change the port in Settings. |
| *"Timed out reaching https://…"* | This PC can't reach the console. Is it on that network or VPN? Can you open `https://<console IP>` in the browser? |
| *"refused the connection"* | Wrong port. See the port table in step 3. |
| *"Login rejected"* | Check the username and password. The account must be **local-only** (step 3). Too many failed tries locks logins for a few minutes. |
| *"Permission denied … read-only"* | The UniFi account is *View Only*. Give it Full Management if you want changes to work. |
| *"rejected the API key"* (UniFi) | That UniFi version doesn't accept API keys for this feature. Switch the connection to username + password. |
| *"DIFFERENT security certificate"* | The address now answers with a different console certificate, e.g. after a factory reset, a replaced console, or being on a different network where that IP is something else. If you're sure it's your console, open the connection and click **Trust new certificate**. |
| *"Anthropic rejected the API key"* / *"out of credits"* | Fix in Settings, or add credits at console.anthropic.com. |
| *"The app was restarted. Reload this page"* | Press F5. The page's security token changes each time the app starts. |
| Firewall rules Claude creates don't seem to work | Newer UniFi versions use **Zone-Based Firewall**. This app creates classic rules, which may not apply there. Check in the UniFi UI. |

Still stuck? Run the diagnostic and send its output to whoever is helping you. It contains no passwords.
```
.venv\Scripts\python backend\probe_events.py
```
