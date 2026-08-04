RUNTIME=${VULN_SCAN_AGENT_RUNTIME:-runsc}
NET=${VULN_SCAN_AGENT_NET:-vsa-sandbox}
PROXY_NAME=vsa-egress-proxy

docker info --format '{{range $k,$v := .Runtimes}}{{k}} {{end}}' \
    | tr ' ' '\n' | grep -qx "$RUNTIME" \
    || { echo "error: runtime '$RUNTIME' not found in docker info; please run scripts/setup_sandbox.sh" >&2; exit 1; }

proxy_ip=$(docker inspect "$PROXY_NAME" --format \
    '{{(index .NetworkSettings.Networks "'$NET'").IPAddress}}' 2>/dev/null) \
    || { echo "error: proxy container '$PROXY_NAME' not found; please run scripts/setup_sandbox.sh" >&2; exit 1; }

export VULN_HARNESS_AGENT_RUNTIME="$RUNTIME"
export VULN_HARNESS_AGENT_NETWORK="$NET"
export VULN_HARNESS_EGRESS_PROXY="http://${proxy_ip}:3128"

if [ -n "${ANTHROPIC_BASE_URL:-}" ]; then
    echo "warning: ANTHROPIC_BASE_URL is set.  The egress proxy allowlist defaults to" >&2
    echo "  api.anthropic.com:443 only - set VSA_EGRESS_ALLOW" >&2
    echo "  before scripts/setup_sandbox.sh to allow other hosts." >&2
fi

if [ "${VULNHUNT_MODEL:-}" = "claude-mythos-5" ]; then
    echo "note: Mythos egress must be restricted to aws-external-anthropic.us-east-1.api.aws:443" >&2
fi
