#!/usr/bin/env bash
# Run the Proshop Poland scanner behind a fresh residential proxy session.
#
# Why a wrapper: the proxy identity must change between cycles but stay
# constant within one. DataImpulse's documented session-id contract uses the
# rotating gateway (port 823) plus `sessid`; sticky ports 10000-20000 are a
# separate port-bound mechanism and must not be combined with this lifecycle.
# systemd's EnvironmentFile cannot build a value at start time, so the session
# id is generated here.
#
# Credentials come from PROXY_LOGIN/PROXY_PASSWORD in the mode-0600
# environment file; they are never written to the command line or the log.
# Mirrors ~/amazon/bin/amazon-de-proxy-run.sh and
# ~/rtv-euro-agd/bin/euro-proxy-run.sh.
set -euo pipefail

: "${PROXY_LOGIN:?PROXY_LOGIN missing (check amazon-de-proxy.env)}"
: "${PROXY_PASSWORD:?PROXY_PASSWORD missing (check amazon-de-proxy.env)}"

# One identity per cycle. sessttl.30 outlives the cycle limit, and the
# 15 min timer guarantees the next cycle starts after the session has expired.
session="proshoppl$(date +%s)$$"
proxy="http://${PROXY_LOGIN}__cr.pl;sessid.${session};sessttl.30:${PROXY_PASSWORD}@gw.dataimpulse.com:823"

export HTTPS_PROXY="$proxy"
export HTTP_PROXY="$proxy"
export https_proxy="$proxy"
export http_proxy="$proxy"
# Only Proshop needs the Polish exit. Rates, delivery and the local database
# must not spend proxy bandwidth.
no_proxy_defaults="api.nbp.pl,nbp.pl,discord.com,discordapp.com,localhost,127.0.0.1,::1"
export NO_PROXY="${NO_PROXY:+${NO_PROXY},}${no_proxy_defaults}"
export no_proxy="$NO_PROXY"

echo "proshop_proxy_session session=${session} host=gw.dataimpulse.com:823 exit=pl"

exec "$@"
