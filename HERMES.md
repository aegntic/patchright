# Patchright + Hermes Agent Integration

This directory contains the Hermes Agent browser provider that makes
[Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) the default
browser engine for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

## What This Does

- Replaces Playwright with Patchright for all browser automation in Hermes
- Applies stealth patches at the Chromium level (anti-detection, fingerprint spoofing)
- Zero external API calls — runs entirely on localhost
- Integrates directly into Hermes's agent run loop (`run_conversation()`)

## Quick Setup

```bash
# Run the setup script
bash setup_hermes.sh

# Or manually:
pip install patchright
patchright install chromium
cp hermes_provider.py ~/.hermes/hermes-agent/tools/browser_providers/patchright.py
hermes config set browser.cloud_provider patchright
```

## Architecture

```
┌─────────────────────────────────────────────┐
│              Hermes Agent                    │
│  run_agent.py → run_conversation() loop     │
│         │                                    │
│         ▼                                    │
│  tools/browser_tool.py                      │
│    browser_navigate() / browser_click() ... │
│         │                                    │
│         ▼                                    │
│  tools/browser_providers/patchright.py      │  ← this module
│    PatchrightProvider.create_session()       │
│         │                                    │
│         ▼                                    │
│  patchright (Python)                        │
│    pw.chromium.launch() + stealth patches   │
│         │                                    │
│         ▼                                    │
│  Chromium (stealth-patched)                 │
│    → CDP ws://127.0.0.1:{port}              │
│         │                                    │
│         ▼                                    │
│  agent-browser --cdp <url>                  │
│    (accessibility snapshots, ref selectors) │
└─────────────────────────────────────────────┘
```

## Configuration

In `~/.hermes/config.yaml`:

```yaml
browser:
  cloud_provider: patchright
  patchright:
    headless: true            # default: true
    stealth_level: maximum    # "standard" | "advanced" | "maximum"
    viewport:
      width: 1920
      height: 1080
    user_data_dir: null       # persistent profile path (optional)
    args: []                  # extra Chromium launch args
```

Or via environment variables:

```bash
export PATCHRIGHT_HEADLESS=true
export PATCHRIGHT_STEALTH_LEVEL=maximum
export PATCHRIGHT_USER_DATA_DIR=/path/to/profile
```

## Provider Priority (in browser_tool.py)

When no explicit `browser.cloud_provider` is set:

1. **Patchright** — if patchright Python package is importable (local, zero-cost)
2. **Browser Use** — if Nous subscription or API key is configured
3. **Browserbase** — if BROWSERBASE_API_KEY is set
4. **Local agent-browser** — plain headless Chromium (no stealth)

## Testing

```bash
cd ~/.hermes/hermes-agent
source venv/bin/activate
python3 -c "
from tools.browser_providers.patchright import PatchrightProvider
p = PatchrightProvider()
print(f'Configured: {p.is_configured()}')
session = p.create_session('test')
print(f'CDP: {session["cdp_url"]}')
p.close_session(session['bb_session_id'])
print('OK')
"
```

## Notes

- Patchright runs Chromium locally — no network calls to cloud services
- The first launch downloads Chromium (~300MB) to `~/.cache/ms-playwright`
- `stealth_level: maximum` applies the most aggressive anti-detection patches
- For persistent browser profiles (cookies, local storage), set `user_data_dir`
