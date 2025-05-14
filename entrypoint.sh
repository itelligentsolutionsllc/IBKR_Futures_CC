#!/usr/bin/env bash
set -e

# 1) Generate IB Gateway headless‑login config
mkdir -p ~/.ibg/ibgconfig/10.30
cat > ~/.ibg/ibgconfig/10.30/ibgateway.json <<EOF
{
  "userName":   "${IBKR_USER}",
  "password":   "${IBKR_PASSWORD}",
  "extraArgs":  "",
  "trustedIPs": ["127.0.0.1"],
  "readOnly":   false
}
EOF

# 2) Start IB Gateway headlessly
/opt/ibgw/ibgatewaystart.sh --nogui --configfile ~/.ibg/ibgconfig/10.30/ibgateway.json &

# 3) Give it time to bind port 7496
sleep 15

# 4) Exec your Python bot (passes through any args)
exec python "$@"
