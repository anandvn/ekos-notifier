#!/bin/sh
# Runs install.sh end to end in a throwaway home folder: install, the service, an upgrade and
# --uninstall, with stand-ins for systemctl and loginctl so the real user's services are never
# touched. Needs the internet (pip). Used by CI; run it from a checkout: sh tools/check_install.sh
set -eu

repo="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/home" "$work/bin"
for tool in systemctl loginctl; do
    cat >"$work/bin/$tool" <<STUB
#!/bin/sh
echo "$tool \$*" >>"$work/calls"
[ "\${1:-}" = show-user ] && echo Linger=no
exit 0
STUB
    chmod +x "$work/bin/$tool"
done

run() {
    env -i HOME="$work/home" PATH="$work/bin:$work/home/.local/bin:/usr/local/bin:/usr/bin:/bin" \
        USER=ci ${PYTHON:+PYTHON="$PYTHON"} sh "$repo/install.sh" "$@" </dev/null
}
check() { if ! eval "$1"; then echo "FAILED: $2" >&2; exit 1; fi; }

echo "== install, no service"
run --from "$repo" --no-service
check '[ -x "$work/home/.local/bin/ekos-notifier" ]' "the command is linked"
check '"$work/home/.local/bin/ekos-notifier" --version' "the command runs"
config="$work/home/.config/ekos-notifier/config.yaml"
check '[ "$(stat -c %a "$config")" = 600 ]' "config.yaml is written, private"
check '! [ -e "$work/calls" ]' "no systemctl without the service"

echo "== again, with the service (no terminal: the default, yes)"
echo "notify: []" >"$config"
run --from "$repo"
unit="$work/home/.config/systemd/user/ekos-notifier.service"
check 'grep -q "ExecStart=$work/home/.local/share/ekos-notifier/venv/bin/python -m ekos_notifier run" "$unit"' "the unit runs the venv's Python"
check 'grep -q "enable --now ekos-notifier.service" "$work/calls"' "the service is enabled"
check '[ "$(cat "$config")" = "notify: []" ]' "an existing config is left alone"

echo "== upgrade"
: >"$work/calls"
run --from "$repo"
check 'grep -q "restart ekos-notifier.service" "$work/calls"' "an upgrade restarts the service"

echo "== uninstall"
run --uninstall
check '! [ -e "$unit" ] && ! [ -e "$work/home/.local/share/ekos-notifier/venv" ]' "service and venv removed"
check '! [ -e "$work/home/.local/bin/ekos-notifier" ]' "the command is removed"
check '[ -f "$config" ]' "the config is kept"
echo "install.sh: all checks passed"
