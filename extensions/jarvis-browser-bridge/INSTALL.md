# JARVIS Browser Bridge — Manual Installation

This extension is opt-in. JARVIS never installs it for you and never changes browser security settings.

## What it can do
- Connect to local JARVIS with a short pairing code
- After you click **Inspect active tab**, send only that tab’s title and URL to JARVIS
- After you click **Read active page**, send a limited visible-text snapshot (up to 12,000 characters) to JARVIS
- After you click **Scan action targets**, send short-lived safe element identities for approval-gated actions
- After JARVIS approval, **Run approved action** performs one click, focus, type, or select-all on a revalidated target
- JARVIS still requires a separate approval before keeping reader results or running actions
- Summarization happens only when you click **Summarize snapshot** in JARVIS

## What it cannot do
- Read history, cookies, passwords, or other tabs in the background
- Navigate, download, screenshot, submit forms, or run arbitrary scripts
- Talk to AI providers by itself
- Use Chrome DevTools Protocol or remote debugging

## Permissions
- `nativeMessaging` — talk only to the registered JARVIS native host
- `activeTab` — access the active tab only after you use the extension UI
- `scripting` — run fixed one-shot extract/scan/action helpers in the active tab only when you click

No broad host permissions are requested.

## Install (Chrome or Edge)

### 1. Enable the bridge in JARVIS
1. Start JARVIS.
2. Open the **Browser Bridge** section.
3. Click **Enable Browser Bridge**.
4. Copy the pairing code shown there.
5. Optionally enable **Browser Reader**, **Browser Actions**, and **Browser Workflow** as needed.

### 2. Load the unpacked extension
1. Chrome: open `chrome://extensions`
2. Edge: open `edge://extensions`
3. Turn on **Developer mode**.
4. Click **Load unpacked**.
5. Choose `D:\jarvis\extensions\jarvis-browser-bridge`.
6. Copy the extension ID shown on the card.

### 3. Register the native messaging host
1. Copy `native_host/com.jarvis.browser_bridge.json.template` to a working file.
2. Set `path` to an absolute launcher that runs:

```text
D:\jarvis\.venv\Scripts\python.exe D:\jarvis\jarvis\browser\bridge_host.py
```

On Windows a small `.bat` launcher is fine, for example:

```bat
@echo off
"D:\jarvis\.venv\Scripts\python.exe" "D:\jarvis\jarvis\browser\bridge_host.py"
```

Use the absolute path to that `.bat` in the native host JSON `path` field.
3. Replace `REPLACE_WITH_EXTENSION_ID` with your unpacked extension ID.
4. Register the host for your browser:

Chrome user-level registry example:

```text
HKCU\Software\Google\Chrome\NativeMessagingHosts\com.jarvis.browser_bridge
```

Edge user-level registry example:

```text
HKCU\Software\Microsoft\Edge\NativeMessagingHosts\com.jarvis.browser_bridge
```

Set the default value to the absolute path of your completed `com.jarvis.browser_bridge.json` file.

### 4. Connect
1. Click the extension icon.
2. Paste the JARVIS pairing code.
3. Click **Connect**.
4. Use **Inspect active tab** or **Read active page**, then approve the request in JARVIS.

## Notes
- Keep JARVIS running while using the bridge.
- Enabling the bridge writes a local host config under JARVIS `data/` for the native host only. That folder is not committed.
- Emergency Stop disconnects the bridge session and clears inspection results and snapshots. Resume does not reconnect automatically.
- If the native host or pairing code is wrong, the bridge fails closed.
