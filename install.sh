#!/bin/sh
# Ekos Notifier installer: https://github.com/anandvn/ekos-notifier
#
#   curl -fsSL https://raw.githubusercontent.com/anandvn/ekos-notifier/main/install.sh | sh
#
# Installs Ekos Notifier into a Python venv in your home folder. No root and no extra tools:
# only Python 3.10+ with its own venv module (StellarMate has it; on Debian, Ubuntu and
# Raspberry Pi OS it's the python3-venv package). Run it again to upgrade.
#
#   sh install.sh [--from SPEC] [--service | --no-service] [--uninstall]
#
#   --from SPEC    what to install: a version ("1.0.0"), "main" (the latest code on GitHub), a
#                  path to a checkout, or anything pip takes. Default: the latest release.
#   --service      install the systemd user service without asking (it's offered otherwise).
#   --no-service   don't install or offer it.
#   --uninstall    remove the service, the venv and the command; keeps your config and data.
#
# Environment: PYTHON picks the Python to use (default: python3, then python).

set -eu

REPO="https://github.com/anandvn/ekos-notifier"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/ekos-notifier"
VENV="$DATA/venv"
BIN="$HOME/.local/bin"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/ekos-notifier.service"

from=""
service="ask"
uninstall=0

say() { printf '%s\n' "$*"; }
fail() { printf 'ekos-notifier install: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --from) [ $# -ge 2 ] || fail "--from needs a value"; from="$2"; shift 2 ;;
        --from=*) from="${1#--from=}"; shift ;;
        --service) service="yes"; shift ;;
        --no-service) service="no"; shift ;;
        --uninstall) uninstall=1; shift ;;
        -h|--help) sed -n '2,19p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//' || say "See $REPO"; exit 0 ;;
        *) fail "unknown option '$1' (try --help)" ;;
    esac
done

[ "$(id -u)" -ne 0 ] || fail "run it as yourself, not as root: it installs into your home folder"

# A yes/no question, read from the terminal even when this script arrives on stdin (curl | sh).
# Without a terminal, the default answer.
ask() {
    question="$1" default="$2"
    if (: </dev/tty) 2>/dev/null; then  # a subshell: a failed redirect would end this shell
        hint="[Y/n]"; [ "$default" = "y" ] || hint="[y/N]"
        printf '%s %s ' "$question" "$hint" >/dev/tty
        read -r answer </dev/tty || answer=""
        [ -n "$answer" ] || answer="$default"
    else
        answer="$default"
    fi
    case "$answer" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

service_installed() { [ -f "$UNIT" ]; }

# -- uninstall ------------------------------------------------------------------------------

if [ "$uninstall" -eq 1 ]; then
    if service_installed && [ -x "$VENV/bin/ekos-notifier" ]; then
        "$VENV/bin/ekos-notifier" install-service --uninstall || true
    elif service_installed; then
        systemctl --user disable --now ekos-notifier.service 2>/dev/null || true
        rm -f "$UNIT"
        systemctl --user daemon-reload 2>/dev/null || true
        say "Removed $UNIT"
    fi
    if [ -L "$BIN/ekos-notifier" ] && [ "$(readlink "$BIN/ekos-notifier")" = "$VENV/bin/ekos-notifier" ]; then
        rm -f "$BIN/ekos-notifier"
        say "Removed $BIN/ekos-notifier"
    fi
    if [ -d "$VENV" ]; then
        rm -rf "$VENV"
        say "Removed $VENV"
    fi
    say "Ekos Notifier is uninstalled. Your config (~/.config/ekos-notifier) and your sessions"
    say "($DATA) are still there; delete them yourself if you don't want them."
    exit 0
fi

# -- Python ---------------------------------------------------------------------------------

python=""
for candidate in ${PYTHON:-python3 python}; do
    if command -v "$candidate" >/dev/null 2>&1; then python="$candidate"; break; fi
done
[ -n "$python" ] || fail "no Python found. Install Python 3.10 or newer and run this again."

version="$("$python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
"$python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || fail "Python $version is too old: Ekos Notifier needs 3.10 or newer."
"$python" -c 'import venv, ensurepip' 2>/dev/null \
    || fail "Python $version has no venv module. On Debian, Ubuntu or Raspberry Pi OS:
    sudo apt install python3-venv
then run this again."

# -- the venv -------------------------------------------------------------------------------

upgrading=0
if [ -x "$VENV/bin/python" ]; then
    venv_version="$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
    if [ "$venv_version" = "$version" ]; then
        upgrading=1
    else
        # The system Python moved on (an OS update): the old venv's link is broken. Start over.
        say "Python changed (${venv_version:-unknown} → $version): rebuilding $VENV"
        rm -rf "$VENV"
    fi
elif [ -e "$VENV" ]; then
    rm -rf "$VENV"
fi

if [ "$upgrading" -eq 0 ]; then
    say "Creating $VENV (Python $version)"
    mkdir -p "$DATA"
    "$python" -m venv "$VENV" || fail "couldn't create the venv in $VENV"
fi

case "$from" in
    "") spec="ekos-notifier" ;;
    main) spec="$REPO/archive/refs/heads/main.tar.gz" ;;
    [0-9]*) spec="ekos-notifier==$from" ;;
    *) spec="$from" ;;
esac

if [ "$upgrading" -eq 1 ]; then say "Upgrading Ekos Notifier ($spec)"; else say "Installing Ekos Notifier ($spec)"; fi
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade "$spec" \
    || fail "pip couldn't install $spec. Is the internet up?"
installed="$("$VENV/bin/ekos-notifier" --version 2>/dev/null || true)"
[ -n "$installed" ] || fail "the install finished but ekos-notifier doesn't run"

# -- the command ----------------------------------------------------------------------------

mkdir -p "$BIN"
if [ -e "$BIN/ekos-notifier" ] && ! [ -L "$BIN/ekos-notifier" ]; then
    say "Note: $BIN/ekos-notifier exists and isn't this installer's link; left as it is."
else
    ln -sf "$VENV/bin/ekos-notifier" "$BIN/ekos-notifier"
fi
on_path=0
case ":$PATH:" in *":$BIN:"*) on_path=1 ;; esac

"$VENV/bin/ekos-notifier" config init >/dev/null

# -- the service ----------------------------------------------------------------------------

if service_installed; then
    # Already set up: rewrite the unit (it names the venv's Python); install-service restarts
    # the running service on the new code, or starts it if it's stopped.
    "$VENV/bin/ekos-notifier" install-service \
        || say "Couldn't update the service: run ekos-notifier install-service to see why."
elif [ "$service" = "yes" ] || { [ "$service" = "ask" ] && ask "Start Ekos Notifier with your session (systemd user service)?" y; }; then
    "$VENV/bin/ekos-notifier" install-service
fi

# -- done -----------------------------------------------------------------------------------

config="${XDG_CONFIG_HOME:-$HOME/.config}/ekos-notifier/config.yaml"
say ""
say "$installed is installed."
if [ "$on_path" -eq 0 ]; then
    say "Add $BIN to your PATH to type 'ekos-notifier' (then open a new terminal):"
    say "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.profile"
fi
if "$VENV/bin/ekos-notifier" config check 2>/dev/null | grep -q "notify: none"; then
    say "Next: put your notifier (any Apprise URL, e.g. Pushover) under notify: in"
    say "    $config"
    say "then: ekos-notifier config check && ekos-notifier test-notify"
fi
