#!/usr/bin/env bash
# Setup patchright as the default browser engine for Hermes Agent
# Run: bash setup_hermes.sh

set -e

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_AGENT="$HERMES_HOME/hermes-agent"
VENV_PYTHON="$HERMES_AGENT/venv/bin/python"
PIP="$HERMES_AGENT/venv/bin/pip"

echo "◆ Patchright + Hermes Agent Integration"
echo ""

# 1. Install patchright Python package
echo "→ Installing patchright Python package..."
$PIP install --upgrade patchright

# 2. Install Chromium browser
echo "→ Installing patchright Chromium..."
$VENV_PYTHON -m patchright install chromium

# 3. Deploy provider module to Hermes
echo "→ Deploying provider module..."
PROVIDER_DIR="$HERMES_AGENT/tools/browser_providers"
mkdir -p "$PROVIDER_DIR"
if [ -f "hermes_provider.py" ]; then
    cp hermes_provider.py "$PROVIDER_DIR/patchright.py"
    echo "  Installed to $PROVIDER_DIR/patchright.py"
else
    echo "  ERROR: hermes_provider.py not found in current directory"
    exit 1
fi

# 4. Set patchright as default browser provider
echo "→ Configuring Hermes to use patchright..."
hermes config set browser.cloud_provider patchright 2>/dev/null || {
    echo "  Could not auto-configure. Add this to ~/.hermes/config.yaml:"
    echo "    browser:"
    echo "      cloud_provider: patchright"
}

echo ""
echo "✓ Patchright integration complete"
echo ""
echo "  Verify: hermes doctor"
echo "  Config: hermes config edit"
echo "  Docs: https://github.com/aegntic/patchright"
