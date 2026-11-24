#!/bin/sh
# Start the kiosk's X server + kiosk.py (ExecStart of jupiter-kiosk.service, as the kiosk user).
#
# Nothing outside this unit may talk to the X server: anything that can connect can read the
# screens (the Jess dashboard shows personal data) and type into a logged-in browser. So:
#   - a fresh MIT-MAGIC-COOKIE per start, readable by the kiosk user only, and handed to xauth on
#     stdin (printf is a shell builtin), so it never shows in a process's command line;
#   - -nolisten tcp and -nolisten local: no TCP port and no abstract-namespace socket. The abstract
#     socket would be reachable from every process in the host network namespace, including
#     host-networked containers (Home Assistant);
#   - the remaining /tmp/.X11-unix socket sits in the unit's PrivateTmp.
set -eu

runtime="${XDG_RUNTIME_DIR:-$HOME}"
export XAUTHORITY="$runtime/jupiter-kiosk.Xauthority"
umask 077
: > "$XAUTHORITY"
cookie="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
printf 'add :0 MIT-MAGIC-COOKIE-1 %s\n' "$cookie" | xauth -q -f "$XAUTHORITY" source -
unset cookie

exec /usr/bin/xinit /usr/local/lib/jupiter-kiosk/kiosk.py -- /usr/bin/Xorg :0 vt1 -keeptty \
    -nolisten tcp -nolisten local -nocursor -auth "$XAUTHORITY" -config jupiter-kiosk.conf
