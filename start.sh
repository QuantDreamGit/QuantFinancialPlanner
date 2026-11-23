#!/bin/sh
set -eu

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

lan=
config=
port_argument=
backup_now=
usage="Usage: $0 [--config FILE] [--lan] [port] or $0 [--config FILE] --backup-now"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --config)
            [ "$#" -ge 2 ] || fail "$usage"
            case "$2" in
                /*) config=$2 ;;
                *) config=$PWD/$2 ;;
            esac
            shift ;;
        --lan) lan=--lan ;;
        --backup-now) backup_now=--backup-now ;;
        -h|--help) printf '%s\n' "$usage"; exit 0 ;;
        *)
            [ -z "$port_argument" ] || fail "$usage"
            case "$1" in
                ''|*[!0-9]*|??????*) fail "$usage" ;;
                *) port_argument=$1 ;;
            esac ;;
    esac
    shift
done

project=$(CDPATH= cd -P "$(dirname "$0")" && pwd -P)
cd "$project"
set -- --config "${config:-$project/quant.ini}"
if [ -n "$backup_now" ]; then
    [ -z "$lan$port_argument" ] || fail "$usage"
    exec python3 -m apps.web "$@" --backup-now
fi
if [ -n "$lan" ]; then
    set -- "$@" "$lan"
fi
if [ -n "$port_argument" ]; then
    set -- "$@" --port "$port_argument"
fi
startup=$(python3 -m apps.web.settings "$@")
port=${startup%% *}
if [ "${startup#* }" = 1 ]; then
    cmake -S . -B build
    cmake --build build
fi

listeners() {
    lsof -nP -t -a -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u
}

if [ "$port" -ne 0 ]; then
    command -v lsof >/dev/null 2>&1 || fail "Install lsof to check who is using port $port."
    pid=$(listeners)
    if [ -n "$pid" ]; then
        conflict="Port $port is in use. Could not verify a Quant listener from this project."
        case "$pid" in *[!0-9]*) fail "$conflict" ;; esac
        owner=$(ps -p "$pid" -o uid= | tr -d ' ')
        cwd=$(lsof -nP -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')
        executable=$(ps -ww -p "$pid" -o comm=) || fail "$conflict"
        command_line=$(ps -ww -p "$pid" -o command=) || fail "$conflict"
        started=$(ps -p "$pid" -o lstart=) || fail "$conflict"
        [ "$owner" = "$(id -u)" ] && [ "$cwd" = "$project" ] || fail "$conflict"
        printf '%s\n' "${executable##*/}" | grep -Eq '^[Pp]ython([0-9]+(\.[0-9]+)*)?$' || fail "$conflict"
        case "$command_line" in
            "$executable -m apps.web"|"$executable -m apps.web "*) ;;
            *) printf '%s\n' "$command_line" |
                grep -Eq '^([^[:space:]]*/)?[Pp]ython([0-9]+(\.[0-9]+)*)? -m apps\.web( |$)' || fail "$conflict" ;;
        esac
        [ "$(listeners)" = "$pid" ] && [ -n "$started" ] &&
            [ "$(ps -p "$pid" -o lstart=)" = "$started" ] || fail "$conflict"
        printf 'Stopping the previous Quant dashboard on port %s (PID %s).\n' "$port" "$pid"
        kill -TERM "$pid" || fail "Could not stop Quant on port $port."
        # A suspended server needs to resume to receive its pending termination signal.
        case "$(ps -p "$pid" -o state=)" in
            T*) if [ "$(ps -p "$pid" -o lstart=)" = "$started" ]; then
                    kill -CONT "$pid" 2>/dev/null || :
                fi ;;
        esac
        remaining=10
        while current=$(listeners) && [ -n "$current" ]; do
            [ "$current" = "$pid" ] || fail "Another listener took port $port during restart."
            [ "$remaining" -gt 0 ] || fail "Port $port did not become available."
            sleep 1
            remaining=$((remaining - 1))
        done
    fi
fi

exec python3 -m apps.web "$@"
