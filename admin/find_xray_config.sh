#!/bin/bash
# Find Xray configuration file

echo "=== Looking for Xray config file ==="

# Common locations
CONFIG_PATHS=(
    "/etc/xray/config.json"
    "/usr/local/etc/xray/config.json"
    "/opt/3x-ui/bin/config.json"
    "/etc/x-ui/xray/config.json"
)

for path in "${CONFIG_PATHS[@]}"; do
    if [ -f "$path" ]; then
        echo "✓ Found: $path"
        echo "  Size: $(stat -c%s "$path") bytes"
    fi
done

# Find by process
echo -e "\n=== Xray process info ==="
if pgrep -x xray > /dev/null; then
    echo "Xray is running"
    ps aux | grep xray | grep -v grep

    # Find config from command line
    CONFIG_FROM_CMDLINE=$(ps aux | grep '[x]ray' | grep -oP '\-config\s+\K[^\s]+' | head -1)
    if [ -n "$CONFIG_FROM_CMDLINE" ]; then
        echo -e "\n✓ Config from command line: $CONFIG_FROM_CMDLINE"
        if [ -f "$CONFIG_FROM_CMDLINE" ]; then
            echo "  File exists, size: $(stat -c%s "$CONFIG_FROM_CMDLINE") bytes"
            echo -e "\n  First 20 lines:"
            head -20 "$CONFIG_FROM_CMDLINE"
        fi
    fi
else
    echo "Xray is NOT running"
fi

# Search entire filesystem (last resort)
echo -e "\n=== Searching for config.json files ==="
find /etc /opt /usr/local -name "config.json" 2>/dev/null | while read file; do
    if grep -q "inbounds\|outbounds" "$file" 2>/dev/null; then
        echo "  Possible Xray config: $file"
    fi
done
