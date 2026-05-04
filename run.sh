#!/bin/sh
set -e

OPTIONS=/data/options.json

get_opt() {
    python3 -c "import json; d=json.load(open('$OPTIONS')); print(d.get('$1',''))"
}

HA_URL=$(get_opt ha_url)
HA_TOKEN=$(get_opt ha_token)
REFRESH_INTERVAL=$(get_opt refresh_interval)
FEEDS=$(python3 -c "import json; d=json.load(open('$OPTIONS')); print(json.dumps(d.get('feeds', [])))")

[ -z "$HA_URL" ]   && HA_URL="http://homeassistant:8123"
[ -z "$REFRESH_INTERVAL" ] && REFRESH_INTERVAL=3600

export HA_URL HA_TOKEN REFRESH_INTERVAL FEEDS

mkdir -p /share/podcasts

echo "Video Podcasts starting..."
exec python3 /app/server.py
