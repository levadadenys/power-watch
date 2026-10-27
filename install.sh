#!/usr/bin/env bash
# power-watch installer: sudo ./install.sh            install or upgrade (settings are kept)
#                        sudo ./install.sh --uninstall
#                        ./install.sh --build FILE      only build the single-file executable
#
# Installs one self-contained executable (a Python zipapp of the power_watch package) at
# /usr/local/bin/power-watch and the systemd unit. On the first install it asks for the settings
# and writes /etc/power-watch.env (mode 600: it holds the bot token); on an upgrade it never
# touches that file.
set -euo pipefail

BIN=/usr/local/bin/power-watch
UNIT=/etc/systemd/system/power-watch.service
ENV_FILE=/etc/power-watch.env
LEGACY_DIR=/opt/power-watch # the pre-0.1 layout: two files and a symlink
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() {
	printf 'install.sh: %s\n' "$*" >&2
	exit 1
}

build() { # build FILE: zip the package with a __main__ that keeps the exit code
	local out=$1 stage
	stage=$(mktemp -d)
	trap 'rm -rf "$stage"' RETURN
	cp -R "$SRC/power_watch" "$stage/"
	find "$stage" -name __pycache__ -prune -exec rm -rf {} +
	printf 'import sys\nfrom power_watch.cli import main\nsys.exit(main())\n' >"$stage/__main__.py"
	# Reproducible: zipapp already sorts the entries; fix the modes (not the umask's) and the
	# timestamps (zip starts in 1980) too.
	find "$stage" -type d -exec chmod 755 {} +
	find "$stage" -type f -exec chmod 644 {} +
	find "$stage" -exec touch -t 198001010000 {} +
	python3 -m zipapp "$stage" -o "$out" -p "/usr/bin/env python3"
	chmod 755 "$out"
}

set_key() { # set_key KEY VALUE: replace KEY=... in the env file (values stay out of argv)
	K=$1 V=$2 awk 'BEGIN { k = ENVIRON["K"] } index($0, k "=") == 1 { print k "=" ENVIRON["V"]; next } { print }' \
		"$ENV_FILE" >"$ENV_FILE.new"
	cat "$ENV_FILE.new" >"$ENV_FILE" # keeps the file's mode and owner
	rm -f "$ENV_FILE.new"
}

ask() { # ask VAR "question" [default]
	local reply
	read -r -p "$2${3:+ [$3]}: " reply </dev/tty
	printf -v "$1" '%s' "${reply:-${3:-}}"
}

configure() {
	say "First install: a few settings (all can be changed later in $ENV_FILE)"
	local net device token chat users lang
	# Optional help: without iproute2 (or a route) the scan is skipped and the IP is typed in.
	net=$(ip -4 route show scope link 2>/dev/null | awk '$1 ~ /\// && $1 !~ /^169\.254/ { print $1; exit }') || net=""
	if [ -n "$net" ]; then
		say "Xiaomi (miIO) devices on $net:"
		"$BIN" --scan "$net" || true
	fi
	echo "Pick the device plugged into mains power, NOT into the UPS."
	ask device "Device IP"
	echo "Telegram: create a bot with @BotFather and add it to your channel as an admin."
	echo "Leave the token empty to only log to the journal."
	read -r -s -p "Bot token (input hidden): " token </dev/tty
	echo
	if [ -n "$token" ]; then
		echo "Channel: @name for a public one; for a private one, leave empty and run 'power-watch --find-chats' later."
		ask chat "Channel"
		echo "Telegram user ids allowed to ask the bot /status (comma-separated; empty = nobody yet)."
		ask users "Allowed user ids"
	fi
	ask lang "Language (en/uk)" en
	umask 077
	cp "$SRC/power-watch.env.example" "$ENV_FILE"
	chmod 600 "$ENV_FILE"
	set_key POWER_WATCH_DEVICE_IP "$device"
	set_key TELEGRAM_BOT_TOKEN "$token"
	set_key TELEGRAM_CHAT_ID "${chat:-}"
	set_key POWER_WATCH_ALLOWED_USERS "${users:-}"
	set_key POWER_WATCH_LANG "$lang"
}

uninstall() {
	say "Stopping and removing power-watch"
	systemctl disable --now power-watch.service 2>/dev/null || true
	rm -f "$UNIT" "$BIN"
	systemctl daemon-reload
	echo "Kept your settings ($ENV_FILE) and state (/var/lib/power-watch); remove them by hand if you want."
}

case "${1:-}" in
--build)
	build "${2:?usage: install.sh --build FILE}"
	exit 0
	;;
--uninstall)
	[ "$(id -u)" -eq 0 ] || die "run as root: sudo $0 --uninstall"
	uninstall
	exit 0
	;;
"") ;;
*) die "unknown option $1 (see the header of this script)" ;;
esac

[ "$(id -u)" -eq 0 ] || die "run as root: sudo $0"
command -v systemctl >/dev/null || die "systemd is required"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "Python 3.9 or newer is required"

say "Building and installing $BIN"
tmp=$(mktemp)
build "$tmp"
# Only when the old files are really there: a checkout or other data under /opt is never touched.
if [ ! -e "$LEGACY_DIR/.git" ] &&
	{ [ -L "$BIN" ] || [ -e "$LEGACY_DIR/power_watch.py" ] || [ -e "$LEGACY_DIR/purifier.py" ]; }; then
	say "Replacing the old layout ($LEGACY_DIR + symlink)"
	rm -f "$BIN"
	# Only our own files: anything else someone put there stays (and so does the directory).
	rm -f "$LEGACY_DIR/power_watch.py" "$LEGACY_DIR/purifier.py"
	rm -rf "$LEGACY_DIR/__pycache__"
	rmdir "$LEGACY_DIR" 2>/dev/null || say "Left $LEGACY_DIR in place: it holds files that aren't power-watch's"
fi
install -m 755 "$tmp" "$BIN"
rm -f "$tmp"

if ! cmp -s "$SRC/systemd/power-watch.service" "$UNIT"; then
	install -m 644 "$SRC/systemd/power-watch.service" "$UNIT"
	systemctl daemon-reload
fi

if [ -e "$ENV_FILE" ]; then
	say "Keeping your settings in $ENV_FILE"
elif [ -t 0 ]; then
	configure
else
	install -m 600 "$SRC/power-watch.env.example" "$ENV_FILE"
	say "Wrote $ENV_FILE from the example: edit it (sudoedit $ENV_FILE), then: sudo systemctl restart power-watch"
fi

if grep -q '^POWER_WATCH_PURIFIER_TOKEN=.' "$ENV_FILE" && ! python3 -c 'import cryptography' 2>/dev/null; then
	say "A purifier is configured: its readings need python3-cryptography (sudo apt install python3-cryptography)"
fi

if systemctl is-active --quiet power-watch.service; then
	systemctl restart power-watch.service
	say "Upgraded and restarted"
else
	systemctl enable --now power-watch.service
	say "Installed and started"
fi
sleep 3
journalctl -u power-watch.service -n 3 --no-pager -o cat || true
echo
echo "Check:   journalctl -u power-watch -f"
echo "Test:    sudo systemd-run --pipe --wait -q -p EnvironmentFile=$ENV_FILE -p DynamicUser=yes $BIN --test-notify"
