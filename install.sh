#!/bin/sh
# Install or update the ESPHome teleinfo grid meter on a Venus OS device.
#
#   sh install.sh --host <esphome-host>              install, then start the service
#   sh install.sh --host <host> --dry-run            print the plan, change nothing
#   sh install.sh --host <host> --test               second instance (41), to try it
#
# The host is written to the settings file, never into this repository: the
# project is public and an installation address is nobody else's business.
#
# Assumes the repository lives at INSTALL_DIR (git clone, like the sibling
# drivers). Nothing is written outside /data, /service and
# /opt/victronenergy/service, so a firmware update keeps the checkout and the
# settings but restores the stock service tree: re-run this script after an
# update. A plain reboot needs nothing, the service comes back on its own.
set -eu

INSTALL_DIR="${INSTALL_DIR:-/data/etc/dbus-teleinfo-esphome}"
STATE_DIR="${STATE_DIR:-/data/etc/dbus-teleinfo-state}"
# The runit service must live in TWO places:
#   /opt/victronenergy/service/  persistent, copied into /service at every boot
#   /service/                    the live bind mount of that copy (overlays.sh)
# Writing only to /service works until the next reboot, silently: the meter
# disappears from VRM and nothing says why.
SERVICE_SRC="/opt/victronenergy/service/teleinfo-grid"
SERVICE_DIR="/service/teleinfo-grid"
TEST_SERVICE_SRC="/opt/victronenergy/service/teleinfo-grid-test"
TEST_SERVICE_DIR="/service/teleinfo-grid-test"
LOGDIR="/var/log/dbus-teleinfo"
DEVICE_INSTANCE="${TELEINFO_DEVICE_INSTANCE:-40}"
HOST="${TELEINFO_HOST:-}"
PORT="${TELEINFO_PORT:-80}"
DRY_RUN=0
TEST_MODE=0
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --host=*) HOST="${1#--host=}"; shift ;;
        --port) PORT="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --test) TEST_MODE=1; SERVICE_DIR="$TEST_SERVICE_DIR"; SERVICE_SRC="$TEST_SERVICE_SRC"; shift ;;
        --force) FORCE=1; shift ;;
        *) echo "argument inconnu: $1" >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }
run() {
    if [ "$DRY_RUN" = 1 ]; then
        say "  [dry-run] $*"
    else
        say "  $*"
        sh -c "$*"
    fi
}

say "== Vérifications"
[ "$(id -u)" = 0 ] || { echo "à lancer en root" >&2; exit 1; }
[ -f /etc/venus/swu-name ] || echo "attention: /etc/venus absent, êtes-vous sur Venus OS ?"
[ -d "$INSTALL_DIR/.git" ] || { echo "dépôt absent de $INSTALL_DIR" >&2; exit 1; }
for mod in dbus gi; do
    python3 -c "import $mod" 2>/dev/null && say "  python3 -m $mod : ok" \
        || { echo "  python3 -m $mod : MANQUANT" >&2; exit 1; }
done
# vedbus is a module (velib_python/vedbus.py), not a package directory: testing
# for a directory reported "submodule missing" on a perfectly good checkout.
if [ -f "$INSTALL_DIR/velib_python/vedbus.py" ]; then
    say "  velib_python : présent"
else
    say "  velib_python : submodule à récupérer"
    run "cd '$INSTALL_DIR' && git submodule update --init --depth 1 velib_python"
fi

say "== Mise à jour du code"
run "cd '$INSTALL_DIR' && git fetch --quiet && git merge --ff-only --quiet @{u}"

if [ "$TEST_MODE" = 1 ]; then
    RUN_INSTANCE=$((DEVICE_INSTANCE + 1))
    SERVICE_NAME="com.victronenergy.grid.esphome-teleinfo.test"
    SETTINGS="$STATE_DIR/settings-test.json"
    say "== Mode test : instance $RUN_INSTANCE, service $SERVICE_NAME, réglages $SETTINGS"
    say "   les deux compteurs cohabitent ; systemcalc retient l'instance la plus basse"
else
    RUN_INSTANCE="$DEVICE_INSTANCE"
    SERVICE_NAME="com.victronenergy.grid.esphome-teleinfo"
    SETTINGS="$STATE_DIR/settings.json"
fi

say "== Réglages"
run "mkdir -p '$STATE_DIR' '$LOGDIR'"
if [ -z "$HOST" ] && [ ! -f "$SETTINGS" ]; then
    echo "aucun hôte ESPHome: --host <nom-ou-adresse> (ou TELEINFO_HOST)" >&2
    exit 2
fi
if [ -n "$HOST" ]; then
    if [ "$DRY_RUN" = 1 ]; then
        say "  [dry-run] écrire host=$HOST port=$PORT dans $SETTINGS"
    else
        python3 - "$SETTINGS" "$HOST" "$PORT" <<'PY'
import json, os, sys
path, host, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    with open(path) as handle:
        values = json.load(handle)
except (OSError, ValueError):
    values = {}
values["host"] = host
values["port"] = port
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp"
with open(tmp, "w") as handle:
    json.dump(values, handle, indent=2, sort_keys=True)
os.replace(tmp, path)
PY
        say "  $SETTINGS : host=$HOST port=$PORT"
    fi
fi

say "== Sonde de la liaison ESPHome"
if [ "$DRY_RUN" = 1 ]; then
    say "  [dry-run] python3 dbus-teleinfo.py --probe --host $HOST"
elif [ "$FORCE" = 1 ]; then
    say "  ignorée (--force)"
elif python3 "$INSTALL_DIR/dbus-teleinfo.py" --probe --host "$HOST" --port "$PORT"; then
    say "  liaison ok"
else
    cat >&2 <<'MSG'
  ECHEC: le GX ne lit pas les entités ESPHome.
  Vérifier dans cet ordre :
    1. le GX joint-il le serveur web ?  python3 -c "import urllib.request;
       print(urllib.request.urlopen('http://HOTE/sensor/PAPP', timeout=5).read())"
    2. une règle pare-feu autorise-t-elle ce VLAN à joindre le VLAN des ESP ?
    3. le web_server de l'ESPHome est-il actif (pas seulement l'API native) ?
  Le service serait enregistré mais resterait /Connected=0 : --force pour
  l'installer quand même.
MSG
    exit 1
fi

say "== Service runit"
if [ "$DRY_RUN" = 1 ]; then
    say "  [dry-run] créer $SERVICE_SRC/run (persistant) et $SERVICE_DIR/run (actif)"
else
    mkdir -p "$SERVICE_SRC" "$SERVICE_DIR"
    cat > "$SERVICE_SRC/run" <<EOF
#!/bin/sh
exec 2>&1
. /etc/profile.d/profile.sh
exec python3 '$INSTALL_DIR/dbus-teleinfo.py' \\
    --settings-file '$SETTINGS' \\
    --device-instance $RUN_INSTANCE \\
    --service-name $SERVICE_NAME \\
    >> '$LOGDIR/current' 2>&1
EOF
    chmod +x "$SERVICE_SRC/run"
    cp "$SERVICE_SRC/run" "$SERVICE_DIR/run"
    chmod +x "$SERVICE_DIR/run"
    say "  $SERVICE_SRC/run créé (survit au reboot), copié dans $SERVICE_DIR"
fi

say "== Démarrage"
# svc -u only starts a stopped service: after an update the running process would
# keep the old code. -t restarts it whether it is up or down.
run "svc -t '$SERVICE_DIR' || svc -u '$SERVICE_DIR' || true"
run "sleep 3; svstat '$SERVICE_DIR' || true"

say ""
say "== Contrôle (à lancer dans 15 s)"
say "  tail -n 30 $LOGDIR/current"
say "  python3 $INSTALL_DIR/verify-runtime.py            # lit le service sur D-Bus"
say "  dbus-send --system --dest=com.victronenergy.Bus --print-reply / \\"
say "      com.victronenergy.Bus.ListNames | grep -i teleinfo"
say "  dbus-send --system --print-reply --dest=com.victronenergy.system /Ac/Grid/L1/Power \\"
say "      com.victronenergy.BusItem.GetValue"
say ""
if [ "$TEST_MODE" = 1 ]; then
    say "Instance de test : pour la retirer, mv $SERVICE_DIR /data/teleinfo-test.off && svc -d $SERVICE_DIR"
    say "Puis redémarrer le service réel pour qu'il redevienne le compteur retenu."
fi
