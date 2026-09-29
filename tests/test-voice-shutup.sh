#!/usr/bin/env bash
# Regression: TTS shutup hooks kill real players and spare argv decoys.
# Portable bash (Linux procps + macOS BSD). Tokens are concatenated so this
# script's own argv never contains them literally.
set -u

if [ $# -ne 1 ] || [ ! -f "$1" ]; then
    echo "usage: $0 hook.sh" >&2
    exit 2
fi
hook_file=$1
hook_base=$(basename "$hook_file")

af="af""play"
pa="pa""play"
novid="--no-vid""eo"
nodisp="-nodi""sp"
tts="edge""-tts"
hookn="voice-stop""-hook"

case "$hook_base" in
    shutup.sh) expect_af=alive ;;
    *) expect_af=dead ;;
esac

tmp=$(mktemp -d)
home_sandbox=$(mktemp -d)
pids=()
lock=/tmp/sonia-tts.lock
lockbak_dir=""
if [ -e "$lock" ] || [ -L "$lock" ]; then
    lockbak_dir=$(mktemp -d)
    cp -a "$lock" "$lockbak_dir/lock"
fi

children_of() {
    ps -ax -o pid=,ppid= 2>/dev/null | awk -v p="$1" '$2 == p { print $1 }'
}

kill_tree() {
    local pid=$1 child
    for child in $(children_of "$pid"); do
        kill_tree "$child"
    done
    kill "$pid" 2>/dev/null || true
}

cleanup() {
    local pid
    for pid in "${pids[@]+"${pids[@]}"}"; do
        kill_tree "$pid"
    done
    wait 2>/dev/null || true
    if [ -n "$lockbak_dir" ] && { [ -e "$lockbak_dir/lock" ] || [ -L "$lockbak_dir/lock" ]; }; then
        rm -f "$lock"
        cp -a "$lockbak_dir/lock" "$lock"
    else
        rm -f "$lock"
    fi
    [ -z "$lockbak_dir" ] || rm -rf "$lockbak_dir"
    rm -rf "$tmp" "$home_sandbox"
}
trap cleanup EXIT

track() {
    pids+=("$1")
}

is_alive() {
    local pid=$1 st
    if ! kill -0 "$pid" 2>/dev/null; then
        return 1
    fi
    st=$(ps -p "$pid" -o state= 2>/dev/null | tr -d '[:space:]')
    case "$st" in
        ""|Z*) return 1 ;;
    esac
    return 0
}

# Poll for at most three seconds; this avoids timing assumptions after pkill.
wait_for_state() {
    local pid=$1 desired=$2 attempt=0
    while [ "$attempt" -lt 30 ]; do
        if [ "$desired" = alive ]; then
            is_alive "$pid" && return 0
        else
            ! is_alive "$pid" && return 0
        fi
        sleep 0.1
        attempt=$((attempt + 1))
    done
    return 1
}

fail=0
pass=0
report() {
    local expect=$1 pid=$2 label=$3
    if wait_for_state "$pid" "$expect"; then
        echo "PASS $label"
        pass=$((pass + 1))
    else
        if is_alive "$pid"; then
            echo "FAIL $label (still alive)"
        else
            echo "FAIL $label (killed)"
        fi
        fail=$((fail + 1))
    fi
}

check_started() {
    local pid=$1 label=$2
    if wait_for_state "$pid" alive; then
        return 0
    fi
    echo "FAIL $label (did not start)"
    fail=$((fail + 1))
    return 1
}

launch_real_targets() {
    local prefix=$1 pid
    "$tmp/$tts" &
    pid=$!; track "$pid"; printf -v "${prefix}_shebang" '%s' "$pid"
    python3 -u "$tmp/$tts" &
    pid=$!; track "$pid"; printf -v "${prefix}_tts_flags" '%s' "$pid"
    "$tmp/Python3.13t" -u "$tmp/$tts" &
    pid=$!; track "$pid"; printf -v "${prefix}_tts_suffix" '%s' "$pid"
    PYTHONPATH="$tmp" python3 -u -m edge_tts &
    pid=$!; track "$pid"; printf -v "${prefix}_tts_module" '%s' "$pid"
    python3 -u "$tmp/${hookn}.py" &
    pid=$!; track "$pid"; printf -v "${prefix}_hook" '%s' "$pid"
    perl -e '$0 = "mpv " . $ARGV[0]; sleep 300' -- "$novid" &
    pid=$!; track "$pid"; printf -v "${prefix}_mpv" '%s' "$pid"
    perl -e '$0 = "ffplay " . $ARGV[0]; sleep 300' -- "$nodisp" &
    pid=$!; track "$pid"; printf -v "${prefix}_ffplay" '%s' "$pid"
    "$tmp/$af" 300 &
    pid=$!; track "$pid"; printf -v "${prefix}_afplay" '%s' "$pid"
    "$tmp/$pa" 300 &
    pid=$!; track "$pid"; printf -v "${prefix}_paplay" '%s' "$pid"
}

report_real_targets() {
    local expect=$1 prefix=$2 var pid name target_expect
    for var in shebang tts_flags tts_suffix tts_module hook mpv ffplay afplay paplay; do
        name="${prefix}_${var}"
        pid=${!name}
        target_expect=$expect
        if [ "$var" = afplay ] && [ "$expect" = dead ]; then
            target_expect=$expect_af
        fi
        case "$var" in
            shebang) report "$target_expect" "$pid" "${prefix}-real-tts-shebang" ;;
            tts_flags) report "$target_expect" "$pid" "${prefix}-real-tts-python-flags" ;;
            tts_suffix) report "$target_expect" "$pid" "${prefix}-real-tts-interpreter-suffix" ;;
            tts_module) report "$target_expect" "$pid" "${prefix}-real-tts-module" ;;
            hook) report "$target_expect" "$pid" "${prefix}-real-stop-hook-with-unbuffered-flag" ;;
            mpv) report "$target_expect" "$pid" "${prefix}-real-mpv-with-flag" ;;
            ffplay) report "$target_expect" "$pid" "${prefix}-real-ffplay-with-flag" ;;
            afplay) report "$target_expect" "$pid" "${prefix}-real-afplay" ;;
            paplay) report "$target_expect" "$pid" "${prefix}-real-paplay" ;;
        esac
    done
}

check_real_targets_started() {
    local prefix=$1 var name pid
    for var in shebang tts_flags tts_suffix tts_module hook mpv ffplay afplay paplay; do
        name="${prefix}_${var}"
        pid=${!name}
        if ! wait_for_state "$pid" alive; then
            echo "FAIL ${prefix}-real-${var} (did not start)"
            fail=$((fail + 1))
        fi
    done
}

# Symlinks retain the test basename without copying system binaries.
ln -s /bin/sleep "$tmp/$af"
ln -s /bin/sleep "$tmp/$pa"
python_path=$(command -v python3)
if [ -z "$python_path" ]; then
    echo "FAIL python3-unavailable"
    exit 1
fi
ln -s "$python_path" "$tmp/Python3.13t"

printf '%s\n' '#!/usr/bin/env python3' 'import time' 'time.sleep(300)' > "$tmp/$tts"
chmod +x "$tmp/$tts"
printf '%s\n' 'import time' 'time.sleep(300)' > "$tmp/${hookn}.py"
printf '%s\n' 'import time' 'time.sleep(300)' > "$tmp/edge_tts.py"

# Decoys: the relevant words occur in argv text, not in executable position.
bash -c 'exec -a "$1" sleep 300' _ "claude talk about ${af} ${pa} mpv ${novid} ffplay ${nodisp} ${tts} ${hookn}.py" &
decoy_cmd=$!; track "$decoy_cmd"
python3 -c 'import time;time.sleep(300)' "${hookn}.py" &
decoy_hook=$!; track "$decoy_hook"
python3 -c 'import time;time.sleep(300)' "$tts" &
decoy_tts=$!; track "$decoy_tts"
bash -c 'exec -a mpv sleep 300' &
decoy_mpv=$!; track "$decoy_mpv"

launch_real_targets base

echo "== $hook_base =="
check_started "$decoy_cmd" decoy-cmdline
check_started "$decoy_hook" decoy-python-hook-trailing-arg
check_started "$decoy_tts" decoy-python-tts-trailing-arg
check_started "$decoy_mpv" decoy-mpv-without-flag
check_real_targets_started base

if [ "$fail" -ne 0 ]; then
    echo "RESULT $pass passed, $fail failed"
    exit "$fail"
fi

export HOME="$home_sandbox"
unset CLAUDE_CODE_SHELBY_WORKER || true
printf '%s\n' '{}' | bash "$hook_file"
hook_ec=$?
if [ "$hook_ec" -eq 0 ]; then
    echo "PASS hook-exit-0"
    pass=$((pass + 1))
else
    echo "FAIL hook-exit-0 (exit $hook_ec)"
    fail=$((fail + 1))
fi

report alive "$decoy_cmd" decoy-cmdline-alive
report alive "$decoy_hook" decoy-python-hook-trailing-arg-alive
report alive "$decoy_tts" decoy-python-tts-trailing-arg-alive
report alive "$decoy_mpv" decoy-mpv-without-flag-alive
report_real_targets dead base

# Day's session lock must spare processes owned by another fresh session.
if [ "$hook_base" = day-voice-shutup.sh ]; then
    launch_real_targets mismatch
    check_real_targets_started mismatch
    if [ "$fail" -eq 0 ]; then
        printf '%s\n' 'fresh-lock' 'A' > "$lock"
        touch "$lock"
        printf '%s\n' '{"session_id":"B"}' | bash "$hook_file"
        mismatch_ec=$?
        if [ "$mismatch_ec" -eq 0 ]; then
            echo "PASS day-lock-mismatched-session-hook-exit-0"
            pass=$((pass + 1))
        else
            echo "FAIL day-lock-mismatched-session-hook-exit-$mismatch_ec"
            fail=$((fail + 1))
        fi
        report_real_targets alive mismatch
    fi
fi

echo "RESULT $pass passed, $fail failed"
exit "$fail"
