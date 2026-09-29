#!/bin/sh
set -eu

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
lib_dir="$HOME/.local/lib/ilona-agent"
bin_dir="$HOME/.local/bin"
config_dir="$HOME/.config/ilona-agent"
unit_dir="$HOME/.config/systemd/user"
state_dir="$HOME/.local/state/ilona-agent"

install -d -m 0700 "$lib_dir" "$config_dir" "$state_dir"
install -d -m 0755 "$bin_dir" "$unit_dir"
install -m 0700 "$source_dir/ilona_agent.py" "$lib_dir/ilona_agent.py"
install -m 0644 "$source_dir/ilona-agent.service" "$unit_dir/ilona-agent.service"
install -m 0644 "$source_dir/server-ca.crt" "$config_dir/server-ca.crt"
install -m 0755 "$source_dir/ilona-agent-wrapper" "$bin_dir/ilona-agent"
systemctl --user daemon-reload

if [ -f "$config_dir/config.json" ]; then
    systemctl --user enable --now ilona-agent.service
    echo 'Ilona Agent installed and started.'
else
    echo 'Ilona Agent installed; enrollment is required before it can start.'
    echo 'Run: ilona-agent enroll'
fi
