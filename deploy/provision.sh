#!/usr/bin/env bash
# Create the event box at Hetzner. Run from the repo root:
#
#   HCLOUD_TOKEN=$(cat ~/.config/hcloud-eps.token) deploy/provision.sh
#
# Safe to re-run: every step checks for the resource first, so a half-finished
# run continues rather than duplicating. It does NOT deploy the app - that is
# deploy/deploy.sh, once DNS points at the address this prints.
set -euo pipefail

NAME=${NAME:-provbana}
# The account deploy/deploy.sh logs in as, created by cloud-init.
DEPLOY_USER=${DEPLOY_USER:-deploy}
# 16 vCPU / 32 GB / 320 GB, EUR 29.99/mo ex VAT + EUR 0.50 primary IPv4.
#
# Do not trust `hcloud server-type list`'s locations[].available flag when
# picking a type: it reported every CX size as unavailable everywhere while the
# API happily accepted them, and the deprecated `datacenter` endpoint got it
# wrong in the opposite direction for CAX. The only reliable test is a create.
# To probe one for free, send a deliberately wrong-architecture image:
# "image has wrong architecture" means the location/type pair is orderable,
# "unsupported location for server type" means it is not.
TYPE=${TYPE:-cx53}
LOCATION=${LOCATION:-hel1}  # Helsinki - closest Hetzner region to Stockholm
IMAGE=${IMAGE:-ubuntu-24.04}
SSH_KEY_FILE=${SSH_KEY_FILE:-$HOME/.ssh/id_ed25519_provbana.pub}
HERE=$(cd "$(dirname "$0")" && pwd)

: "${HCLOUD_TOKEN:?set HCLOUD_TOKEN (a Read & Write project token)}"
export HCLOUD_TOKEN

command -v hcloud >/dev/null || { echo "hcloud not on PATH" >&2; exit 1; }
[ -f "$SSH_KEY_FILE" ] || { echo "no public key at $SSH_KEY_FILE - set SSH_KEY_FILE" >&2; exit 1; }

# cloud-init.yaml is a template: the account and the key are this run's, so
# nobody's personal key is baked into the repo.
USER_DATA=$(mktemp)
trap 'rm -f "$USER_DATA"' EXIT
sed -e "s|__DEPLOY_USER__|$DEPLOY_USER|g" \
	-e "s|__SSH_PUBLIC_KEY__|$(head -n1 "$SSH_KEY_FILE")|" \
	"$HERE/cloud-init.yaml" >"$USER_DATA"

echo "==> SSH key"
if hcloud ssh-key describe "$NAME" >/dev/null 2>&1; then
	echo "    $NAME already uploaded"
else
	hcloud ssh-key create --name "$NAME" --public-key-from-file "$SSH_KEY_FILE"
fi

echo "==> Firewall"
# 22, 80, 443 and nothing else. This sits outside the host, which is the point:
# Docker publishes ports by writing iptables rules of its own and would walk
# straight through a ufw policy on the box. Matchbox (8080) and the service
# (8095) are not published by docker-compose.prod.yml either - belt and braces.
if ! hcloud firewall describe "$NAME" >/dev/null 2>&1; then
	hcloud firewall create --name "$NAME"
fi
# Replaced on every run rather than added once beside the create. A Hetzner
# firewall denies inbound by default, so a run that created the firewall and
# then died before adding rules leaves a box nobody can SSH into - and the next
# run would have seen "already exists" and left it that way. Replacing makes
# the rules whatever this file says, every time. ICMP is among them so the box
# answers ping when you are debugging from the venue.
#
# A bare array, not the {"rules": [...]} object --rules-file's help points at:
# the CLI unmarshals the file straight into []FirewallRule and rejects the
# object with "cannot unmarshal object into Go value of type []schema.FirewallRule".
hcloud firewall replace-rules "$NAME" --rules-file - <<'RULES'
[
  {"direction": "in", "protocol": "tcp", "port": "22",
   "source_ips": ["0.0.0.0/0", "::/0"], "description": "inbound 22"},
  {"direction": "in", "protocol": "tcp", "port": "80",
   "source_ips": ["0.0.0.0/0", "::/0"], "description": "inbound 80"},
  {"direction": "in", "protocol": "tcp", "port": "443",
   "source_ips": ["0.0.0.0/0", "::/0"], "description": "inbound 443"},
  {"direction": "in", "protocol": "icmp",
   "source_ips": ["0.0.0.0/0", "::/0"], "description": "ping"}
]
RULES

echo "==> Server"
if hcloud server describe "$NAME" >/dev/null 2>&1; then
	echo "    $NAME already exists"
else
	hcloud server create \
		--name "$NAME" \
		--type "$TYPE" \
		--location "$LOCATION" \
		--image "$IMAGE" \
		--ssh-key "$NAME" \
		--firewall "$NAME" \
		--user-data-from-file "$USER_DATA" \
		--label project=provbana
fi

# --firewall above only applies on create, so a server that came up before the
# firewall did - or was ever detached from it - would stay unprotected across
# any number of re-runs. Attaching here is the check the "safe to re-run"
# promise needs; already-attached is the expected answer, not a failure.
if ! out=$(hcloud firewall apply-to-resource "$NAME" --type server --server "$NAME" 2>&1); then
	case $out in
		*firewall_already_applied*) echo "    already attached to $NAME" ;;
		*) echo "$out" >&2; exit 1 ;;
	esac
fi

IPV4=$(hcloud server ip "$NAME")
IPV6=$(hcloud server ip -6 "$NAME" 2>/dev/null || true)

cat <<EOF

==> Done.

    IPv4  $IPV4
    IPv6  ${IPV6:-none}

Next:
  1. DNS: an A record for your SITE_ADDRESS  ->  $IPV4.  Behind Cloudflare,
     make it DNS only (grey cloud): Caddy gets its own certificate, and a zone
     on SSL/TLS "Flexible" would talk plain HTTP to Caddy on 443.
  2. Wait for the bootstrap to finish (installs Docker, ~2-3 min):
       ssh -i ${SSH_KEY_FILE%.pub} $DEPLOY_USER@$IPV4 'until [ -f /var/lib/cloud/provbana-ready ]; do sleep 5; done; echo ready'
  3. DEPLOY_USER=$DEPLOY_USER SSH_KEY=${SSH_KEY_FILE%.pub} deploy/deploy.sh $IPV4
EOF
