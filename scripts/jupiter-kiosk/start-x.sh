#!/bin/sh
# Start the kiosk's X server (ExecStart of jupiter-kiosk-x.service, as the kiosk user). kiosk.py and
# Chrome run in jupiter-kiosk.service and connect over /tmp/.X11-unix/X0.
#
# Nothing outside the two kiosk units may talk to the X server: anything that can connect can read
# the screens (the Jess dashboard shows personal data) and type into a logged-in browser. So:
#   - a fresh MIT-MAGIC-COOKIE per start, readable by the kiosk user only, and handed to xauth on
#     stdin (printf is a shell builtin), so it never shows in a process's command line. kiosk.py
#     reads it from the same path ($XDG_RUNTIME_DIR/jupiter-kiosk.Xauthority);
#   - -nolisten tcp and -nolisten local: no TCP port and no abstract-namespace X socket;
#   - the unit's own network namespace (PrivateNetwork=) for the abstract sockets X can't switch off
#     (the NVIDIA driver's sideband socket): nothing in the host's network namespace reaches them;
#   - the remaining /tmp/.X11-unix socket sits in the unit's PrivateTmp, shared with
#     jupiter-kiosk.service only (JoinsNamespaceOf=).
# -noreset: with no client holding a connection (kiosk.py's xrandr calls come and go until Chrome
# starts), X would reset, and set every mode again, each time the last client leaves. xinit used to
# hold one open.
set -eu

runtime="${XDG_RUNTIME_DIR:-$HOME}"
export XAUTHORITY="$runtime/jupiter-kiosk.Xauthority"
umask 077
: > "$XAUTHORITY"
cookie="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
printf 'add :0 MIT-MAGIC-COOKIE-1 %s\n' "$cookie" | xauth -q -f "$XAUTHORITY" source -
unset cookie

exec /usr/bin/Xorg :0 vt1 -keeptty -noreset -nolisten tcp -nolisten local -nocursor -auth "$XAUTHORITY" \
    -config jupiter-kiosk.conf
