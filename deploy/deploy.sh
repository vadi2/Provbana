#!/usr/bin/env bash
# Push this checkout to the box and bring the stack up. Run from the repo root:
#
#   deploy/deploy.sh <ip-or-host>
#
# Re-run it for every update - it syncs, rebuilds and recreates in place. The
# state volume is untouched, so results survive a redeploy.
set -euo pipefail

HOST=${1:?usage: deploy/deploy.sh <ip-or-host>}
USER=${DEPLOY_USER:-vadi}
KEY=${SSH_KEY:-$HOME/.ssh/id_ed25519_hackathon2026}
DEST=${DEST:-/opt/hackathon2026}
ROOT=$(cd "$(dirname "$0")/.." && pwd)

SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$USER@$HOST")

if [ ! -f "$ROOT/.env" ]; then
	echo "No .env in the repo root. Copy .env.example to .env and fill it in" >&2
	echo "(REGISTER_CODE and the matchbox heap for this box's RAM)." >&2
	exit 1
fi

# Existing but unfilled is the likelier mistake: someone copies .env.example,
# deploys, and finds out at the venue that Caddy asked Let's Encrypt for a
# certificate for an empty name. Check the values, not just the file.
for var in SITE_ADDRESS MATCHBOX_HEAP MATCHBOX_MEM_LIMIT; do
	if ! grep -qE "^$var=.+" "$ROOT/.env"; then
		echo "$var is empty in .env - see .env.example for what it wants." >&2
		exit 1
	fi
done
# Not fatal: an open register is the intended state while teams are practising.
# Loud, because it stops being intended the morning the doors open.
if ! grep -qE '^REGISTER_CODE=.+' "$ROOT/.env"; then
	echo "    note: REGISTER_CODE is empty - anyone who finds the URL can register." >&2
fi

# Compared before the sync overwrites the box's copy. Matchbox reads
# application.yaml once, at boot, and compose cannot see a bind-mounted file
# change - so a package added to the IG list would sit there unloaded until
# something unrelated happened to recreate the container.
MATCHBOX_CONF=matchbox/config/application.yaml

echo "==> Waiting for the cloud-init bootstrap"
"${SSH[@]}" 'until [ -f /var/lib/cloud/hackathon2026-ready ]; do sleep 5; done'

echo "==> Syncing to $USER@$HOST:$DEST"
"${SSH[@]}" "sudo install -d -o $USER -g $USER $DEST"
RECREATE=caddy
REMOTE_SUM=$("${SSH[@]}" "sha256sum $DEST/$MATCHBOX_CONF 2>/dev/null | cut -d' ' -f1" || true)
if [ -n "$REMOTE_SUM" ] \
		&& [ "$REMOTE_SUM" != "$(sha256sum "$ROOT/$MATCHBOX_CONF" | cut -d' ' -f1)" ]; then
	echo "    $MATCHBOX_CONF changed - the validator will be recreated too"
	RECREATE="caddy matchbox"
fi
# --delete so a file removed here is removed there; the excludes are the things
# that must never travel: local results, the venv, and git history.
# --inplace matters more than it looks: rsync's default is to write a temp file
# and rename it over the target, which gives the path a new inode. Caddyfile is
# bind-mounted into the caddy container by path, and a bind mount follows the
# inode it resolved at start - so a renamed file leaves the running container
# reading the old one forever. Writing in place keeps the inode, and the
# force-recreate below is what actually makes caddy re-read it.
rsync -az --delete --inplace \
	-e "ssh -i '$KEY' -o StrictHostKeyChecking=accept-new" \
	--exclude '.git/' \
	--exclude '.venv/' \
	--exclude '__pycache__/' \
	--exclude '.pytest_cache/' \
	--exclude 'connectathon/data/' \
	--exclude 'connectathon/backups/' \
	--exclude 'connectathon/data.replaced-*/' \
	--exclude '.env' \
	"$ROOT/" "$USER@$HOST:$DEST/"

# .env is excluded above and copied only when the box has none. It is the one
# file the box may legitimately hold a newer copy of - REGISTER_CODE gets set
# at the venue, and a redeploy from a laptop still carrying the practice .env
# would silently reopen registration to the internet mid-event.
if "${SSH[@]}" "test -f $DEST/.env"; then
	if ! "${SSH[@]}" "cat $DEST/.env" | diff -q - "$ROOT/.env" >/dev/null; then
		echo "    .env on the box differs from this checkout - the box's copy is kept."
		echo "    Edit it there, or re-run with ENV_PUSH=1 to overwrite it."
	fi
	if [ "${ENV_PUSH:-}" = 1 ]; then
		scp -q -i "$KEY" "$ROOT/.env" "$USER@$HOST:$DEST/.env"
	fi
else
	scp -q -i "$KEY" "$ROOT/.env" "$USER@$HOST:$DEST/.env"
fi

echo "==> Building and starting"
# Caddy is force-recreated every time: compose only recreates a container when
# its own definition changes, and editing Caddyfile changes nothing compose can
# see, so a config-only change would otherwise never reach the running proxy.
# Matchbox joins it only when its config changed, because recreating it costs
# the two-minute IG reload and the warm-up below.
"${SSH[@]}" "cd $DEST && docker compose -f docker-compose.prod.yml up -d --build \
	&& docker compose -f docker-compose.prod.yml up -d --force-recreate --no-deps $RECREATE"

echo "==> Waiting for the validator to load its IGs (a minute or two from cold)"
"${SSH[@]}" "cd $DEST && for i in \$(seq 1 60); do
	if docker compose -f docker-compose.prod.yml exec -T app python -c \"
import json,urllib.request
# \\\$status answers with a FHIR Parameters resource, so the value is one part
# among many rather than a top-level key. 'degraded' means Matchbox is up but
# missing a package application.yaml should have loaded, which scores real
# submissions wrong, so only 'up' will do.
p=json.load(urllib.request.urlopen('http://localhost:8095/fhir/\\\$status'))
m=[x.get('valueString') for x in p['parameter'] if x['name']=='matchbox']
raise SystemExit(0 if m[:1]==['up'] else 1)\" 2>/dev/null; then
		echo '    healthy'; exit 0
	fi
	sleep 10
done
echo '    still not healthy - check: docker compose -f docker-compose.prod.yml logs matchbox' >&2
exit 1"

echo "==> Warming the validator"
# $status says "up" as soon as Matchbox answers, but Matchbox only compiles
# the profile snapshots on the first $validate call - measured at 64s on this
# box, against a 60s MATCHBOX_TIMEOUT. Left alone, the first team to submit at
# the event eats a 502 and every submission after theirs takes under a second.
# So spend that minute here instead, with a timeout long enough to absorb it.
# Re-run this if you ever restart the matchbox container mid-event.
#
# Every category gets its own engine, so every category pays that cost once.
# The patient summary is warmed first and is the one that has to succeed - it
# is what the event scores. The rest are warmed with the same sample, which is
# not a document of their kind and will fail validation; that is fine and the
# verdict is discarded, because compiling the profile snapshots is the whole
# point and Matchbox does that before it can decide anything. A failure there
# costs a slow first submission of a second-class document, so it is reported
# and not fatal.
"${SSH[@]}" "cd $DEST && docker compose -f docker-compose.prod.yml exec -T app python -c \"
import time, app
app.MATCHBOX_TIMEOUT = 300
body = open('samples/andrea-cross-border.json', 'rb').read()
t = time.monotonic()
app.validate_with_matchbox(body, app.PRIMARY_DOCUMENT)
print('    %s warm after %.0fs' % (app.PRIMARY_DOCUMENT['id'], time.monotonic() - t))
for doc in app.SECONDARY_DOCUMENTS:
    t = time.monotonic()
    try:
        app.validate_with_matchbox(body, doc)
        print('    %s warm after %.0fs' % (doc['id'], time.monotonic() - t))
    except Exception as exc:
        print('    %s NOT warmed (%s) - first submission of one will be slow'
              % (doc['id'], exc))\"" || {
	echo "    warm-up failed - the validator answers \$status but cannot validate," >&2
	echo "    so the first submission at the event would time out. Check:" >&2
	echo "    docker compose -f docker-compose.prod.yml logs matchbox" >&2
	exit 1
}

echo
echo "==> Up. Check it:"
echo "    curl -sS https://\$(grep -oP '(?<=^SITE_ADDRESS=).*' $ROOT/.env)/fhir/\\\$status | python3 -m json.tool"
