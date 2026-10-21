#!/bin/bash


set -euo pipefail

PROJECT_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$PROJECT_ROOT"


_load_env_file() {
    local file="$1" line key value quote rest
    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do

        [[ $line =~ ^[[:space:]]*# ]] && continue
        [[ -z $line ]] && continue
        [[ $line != *=* ]] && continue
        key="${line%%=*}"


        key="${key#"${key%%[![:space:]]*}"}"
        key="${key#export }"
        key="${key%"${key##*[![:space:]]}"}"
        if [[ ! $key =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
            echo "hime.sh: ignoring invalid line in $file: ${line:0:40}" >&2
            continue
        fi
        value="${line#*=}"
        value="${value#"${value%%[![:space:]]*}"}"
        case "$value" in
            \'*|\"*)


                quote="${value:0:1}"
                rest="${value#?}"
                value="${rest%"$quote"*}"
                ;;
            *)


                case "$value" in
                    *[[:space:]]#*) value="${value%%[[:space:]]#*}" ;;
                esac
                value="${value%"${value##*[![:space:]]}"}"
                ;;
        esac
        export "$key=$value"
    done < "$file"
}

_load_env_file .env


RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()  { echo -e "${CYAN}$*${NC}"; }
ok()    { echo -e "${GREEN}✓ $*${NC}"; }
warn()  { echo -e "${YELLOW}⚠  $*${NC}"; }
fail()  { echo -e "${RED}✗ $*${NC}"; exit 1; }


_hime_mode() {
    local arg
    for arg in "$@"; do
        case "$arg" in
            --docker) echo docker; return 0 ;;
            --native) echo native; return 0 ;;
        esac
    done
    case "${HIME_RUN_MODE:-}" in
        docker|native) echo "$HIME_RUN_MODE"; return 0 ;;
    esac
    if command -v docker >/dev/null 2>&1 \
       && [ -n "$(docker compose ps -a -q 2>/dev/null)" ]; then
        echo docker; return 0
    fi
    echo native
}


_show_storage() {
    echo ""
    info "Storage:"
    [ -d "logs" ]             && echo "  logs/            $(du -sh logs 2>/dev/null | cut -f1)"
    [ -d "memory" ]           && echo "  memory/          $(du -sh memory 2>/dev/null | cut -f1)"
    [ -d "data/data_stores" ] && echo "  data/data_stores $(du -sh data/data_stores 2>/dev/null | cut -f1)"


    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        local vol_rows
        vol_rows="$(docker system df -v 2>/dev/null \
            | awk '/^VOLUME NAME/{flag=1; next} flag && NF==0{flag=0} flag && $1 ~ /^hime_/ {print "  docker:"$1"  "$NF}')"
        if [ -n "$vol_rows" ]; then
            echo "$vol_rows"
        fi
    fi
}


_require_ports_free() {
    command -v lsof >/dev/null 2>&1 || return 0
    local held=() p holders
    for p in 5173 8000 8765; do
        if lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
            holders="$(
                lsof -nP -iTCP:"$p" -sTCP:LISTEN 2>/dev/null | awk '
                    NR>1 && !seen[$2]++ { if (out!="") out=out", "; out=out $1" (PID "$2")" }
                    END { print out }
                '
            )"
            held+=("$p -> ${holders:-unknown}")
        fi
    done
    [ ${#held[@]} -eq 0 ] && return 0
    echo -e "${RED}✗ CareAgent ports in use:${NC}"
    local item
    for item in "${held[@]}"; do
        echo "    $item"
    done
    echo ""
    echo "  Run './hime.sh stop' to clear them (safe; idempotent), then retry."
    exit 1
}


cmd_stop() {
    info "Stopping services..."


    if command -v docker >/dev/null 2>&1 \
       && [ -n "$(docker compose ps -a -q 2>/dev/null)" ]; then
        docker compose down >/dev/null 2>&1 || true
    fi


    _native_kill_processes

    ok "All services stopped."
}


PROJECT_ROOT_REAL="$(cd "$PROJECT_ROOT" && pwd -P)"


_pid_cwd() {
    local pid="$1" cwd=""
    if [ -d "/proc/$pid" ]; then
        cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
    elif command -v lsof >/dev/null 2>&1; then
        cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n1 || true)"
    fi
    echo "$cwd"
}


_pid_in_project() {
    local pid="$1" cmd cwd root
    cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
    cwd="$(_pid_cwd "$pid")"
    for root in "$PROJECT_ROOT" "$PROJECT_ROOT_REAL"; do
        case "$cmd" in *"$root/"*) return 0 ;; esac
        case "$cwd" in "$root"|"$root"/*) return 0 ;; esac
    done
    return 1
}


_is_own_ancestor() {
    local target="$1" cur="$$" i
    for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16; do
        [ "$cur" = "$target" ] && return 0
        cur="$(ps -o ppid= -p "$cur" 2>/dev/null | tr -d ' ')"
        case "$cur" in ''|0|1) return 1 ;; esac
    done
    return 1
}


_terminate_pids() {
    [ "$#" -gt 0 ] || return 0
    local -a pids=("$@") alive=()
    local pid i
    kill -TERM "${pids[@]}" 2>/dev/null || true
    for i in 1 2 3 4 5 6 7 8 9 10; do
        alive=()
        for pid in "${pids[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then alive+=("$pid"); fi
        done
        [ "${#alive[@]}" -eq 0 ] && return 0
        sleep 0.5
    done
    warn "Still running after SIGTERM, force-killing: ${alive[*]}"
    kill -KILL "${alive[@]}" 2>/dev/null || true
}

_native_kill_processes() {
    local -a pids=()
    local port pid pat p dup


    if command -v lsof >/dev/null 2>&1; then
        for port in 8000 5173 8765; do
            for pid in $(lsof -t -i:"$port" -sTCP:LISTEN 2>/dev/null || true); do
                if _pid_in_project "$pid"; then
                    pids+=("$pid")
                else
                    warn "Port $port is held by PID $pid, which is not part of this checkout — leaving it alone."
                fi
            done
        done
    fi


    for pat in "backend.main" "ios/Server/server.py" "vite" "npm run dev" "multiprocessing"; do
        for pid in $(pgrep -f "$pat" 2>/dev/null || true); do
            if _is_own_ancestor "$pid"; then continue; fi
            if _pid_in_project "$pid"; then pids+=("$pid"); fi
        done
    done


    local -a uniq=()
    for p in ${pids[@]+"${pids[@]}"}; do
        dup=false
        for pid in ${uniq[@]+"${uniq[@]}"}; do
            [ "$pid" = "$p" ] && dup=true
        done
        $dup || uniq+=("$p")
    done
    _terminate_pids ${uniq[@]+"${uniq[@]}"}
}


cmd_start() {
    case "$(_hime_mode "$@")" in
        docker) _docker_start "$@" ;;
        *)      _native_start "$@" ;;
    esac
}

_docker_start() {
    echo -e "${BOLD}🚀 Starting CareAgent${NC} (docker)"
    echo "═══════════════════════════════════════"

    command -v docker >/dev/null 2>&1 \
        || fail "Docker is not installed. Install Docker Desktop first."
    docker info >/dev/null 2>&1 \
        || fail "Docker daemon is not running. Start Docker Desktop first."
    [ -f docker-compose.yml ] \
        || fail "docker-compose.yml missing. Are you in the CareAgent project root?"


    if [ -z "$(docker compose ps -a -q 2>/dev/null)" ]; then
        _require_ports_free
        info "Building images (first run may take a few minutes)..."
        docker compose up --build -d
    else
        info "Reconciling compose stack..."
        docker compose up -d
    fi

    ok "Docker stack running."
    echo ""
    if [ -n "${DASHBOARD_URL:-}" ]; then
        echo "   External Dashboard: $DASHBOARD_URL"
        echo "   External API:       ${API_URL:-n/a}"
        echo "   External Watch:     ${WATCH_URL:-n/a}"
    else
        echo "   Local UI:           http://localhost:5173"
        echo "   Local API:          http://localhost:8000"
    fi
    echo ""
    ok "Use './hime.sh logs' to follow or './hime.sh status' to check."
}


_gen_token() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c 'import secrets; print(secrets.token_hex(32))'
    else
        head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
    fi
}


_set_env_value() {
    local key="$1" value="$2" file="$3" tmp
    tmp="$(mktemp "${file}.XXXXXX")"
    HIME_NEW_VALUE="$value" awk -v key="$key" '
        BEGIN { v = ENVIRON["HIME_NEW_VALUE"]; done = 0 }
        index($0, key "=") == 1 && !done { print key "=" v; done = 1; next }
        { print }
        END { if (!done) print key "=" v }
    ' "$file" > "$tmp" && mv "$tmp" "$file"
}

_first_run_api_token() {
    local tok
    tok="$(_gen_token)" || tok=""
    if [ -z "$tok" ]; then
        warn "Could not generate an API token. Set API_AUTH_TOKEN in .env yourself —"
        warn "without it the API is open to every device that can reach port 8000."
        return 0
    fi
    _set_env_value API_AUTH_TOKEN "$tok" .env
    ok "Generated API_AUTH_TOKEN (saved in .env):"
    echo "    $tok"
    warn "Paste it into the dashboard when it asks, and into the iOS app (Settings -> Auth Token)."
}

_native_start() {
    local detached=true
    local _start_pids=()

    _cleanup_on_fail() {
        echo ""
        warn "Startup failed, cleaning up child processes..."


        for pid in ${_start_pids[@]+"${_start_pids[@]}"}; do
            kill "$pid" 2>/dev/null || true
        done
        _native_kill_processes
    }
    trap _cleanup_on_fail EXIT

    echo -e "${BOLD}🚀 Starting CareAgent${NC} (native)"
    echo "═══════════════════════════════════════"


    _require_ports_free


    if [ ! -f ".env" ]; then
        if [ -f ".env.example" ]; then
            warn ".env not found, creating from .env.example..."
            cp .env.example .env
            _first_run_api_token
            warn "Edit .env to add your API keys!"
        else
            fail ".env missing and no .env.example found."
        fi
    fi


    _load_env_file .env

    command -v python3 &>/dev/null || fail "Python 3 not found."

    if ! python3 -c "import uvicorn" &>/dev/null; then
        info "Installing backend dependencies..."


        if ! python3 -m pip install -r backend/requirements.txt; then
            echo ""
            warn "pip install failed."
            warn "On Homebrew Python 3.12+ / Debian 12+ pip refuses to install into"
            warn "the system interpreter (PEP 668 'externally-managed-environment')."
            warn "Create and activate a virtualenv, then retry:"
            echo "    python3 -m venv .venv && source .venv/bin/activate"
            echo "    python3 -m pip install -r backend/requirements.txt"
            fail "Backend dependencies not installed."
        fi
    fi


    mkdir -p logs
    [ -f "logs/backend.log" ] && mv logs/backend.log logs/backend.log.prev
    [ -f "logs/watch.log" ] && mv logs/watch.log logs/watch.log.prev


    info "Starting Watch Exporter (8765)..."
    PYTHONUNBUFFERED=1 nohup python3 ios/Server/server.py --port 8765 > logs/watch.log 2>&1 &
    WATCH_PID=$!
    _start_pids+=("$WATCH_PID")


    local tries=0
    while ! curl -s http://localhost:8765/ping > /dev/null 2>&1; do
        sleep 1
        tries=$((tries + 1))
        if [ $tries -ge 15 ]; then
            warn "Watch Exporter slow to start, continuing anyway..."
            break
        fi
    done
    if [ $tries -lt 15 ]; then
        ok "Watch Exporter ready (PID $WATCH_PID)"
    fi


    info "Starting backend..."
    PYTHONUNBUFFERED=1 nohup python3 -m backend.main > logs/backend.log 2>&1 &
    BACKEND_PID=$!
    _start_pids+=("$BACKEND_PID")
    

    local tries=0
    while ! curl -s http://localhost:8000/health > /dev/null 2>&1; do
        sleep 1
        tries=$((tries + 1))
        if [ $tries -ge 60 ]; then
            fail "Backend failed to start (60s timeout). Check logs/backend.log"
        fi
    done
    ok "Backend running — http://localhost:8000  (PID $BACKEND_PID)"


    local fe_env="frontend/.env.local"
    if [ -f "$fe_env" ] && grep -q '^VITE_API_AUTH_TOKEN=' "$fe_env"; then
        sed -i.bak '/^VITE_API_AUTH_TOKEN=/d' "$fe_env" && rm -f "$fe_env.bak"
        info "Removed VITE_API_AUTH_TOKEN from $fe_env (the dashboard prompts for it instead)."
    fi
    if [ -n "${API_AUTH_TOKEN:-}" ]; then
        info "API auth is ON — paste API_AUTH_TOKEN from .env into the dashboard when it asks."
    fi


    FRONTEND_PID=""
    if [ -d "frontend" ]; then
        info "Starting frontend..."
        if $detached; then
            cd frontend
            [ ! -d "node_modules" ] && npm install --silent
            nohup npm run dev > ../logs/frontend.log 2>&1 &
            FRONTEND_PID=$!
            cd ..
        else
            (
                cd frontend
                [ ! -d "node_modules" ] && npm install --silent
                npm run dev > ../logs/frontend.log 2>&1
            ) &
            FRONTEND_PID=$!
        fi
        _start_pids+=("$FRONTEND_PID")
        ok "Frontend unit started (PID $FRONTEND_PID)"


        local tries=0
        while ! curl -s -o /dev/null http://localhost:5173/ 2>/dev/null; do
            sleep 1
            tries=$((tries + 1))
            if [ $tries -ge 30 ]; then
                warn "Frontend slow to start (30s), continuing anyway..."
                break
            fi
        done
        if [ $tries -lt 30 ]; then
            ok "Frontend ready on http://localhost:5173"
        fi
    else
        warn "frontend/ not found, skipping."
    fi


    trap - EXIT
    echo ""
    echo -e "${BOLD}🎉 CareAgent is ready!${NC}"
    if [ -n "${DASHBOARD_URL:-}" ]; then
        echo "   External Dashboard: $DASHBOARD_URL"
        echo "   External API:       ${API_URL:-n/a}"
        echo "   External Watch:     ${WATCH_URL:-n/a}"
    else
       echo "   Local UI:           http://localhost:5173"
       echo "   Local API:          http://localhost:8000"
    fi
    echo ""

    ok "Services running in background. Use './hime.sh logs' to follow or './hime.sh status' to check."
}

_cleanup_start() {
    echo ""
    info "Stopping services..."
    local tpid="${3:-}"
    [ -n "$tpid" ] && kill "$tpid" 2>/dev/null || true
    _native_kill_processes
    ok "All services stopped."
    exit 0
}


cmd_restart() {
    local rebuild=false clean=false arg
    for arg in "$@"; do
        case "$arg" in
            --rebuild)  rebuild=true ;;
            --clean|-c) clean=true   ;;
        esac
    done
    case "$(_hime_mode "$@")" in
        docker) _docker_restart "$rebuild" ;;
        *)      _native_restart "$clean"   ;;
    esac
}

_docker_restart() {
    local rebuild="$1"
    command -v docker >/dev/null 2>&1 \
        || fail "Docker is not installed."
    if [ "$rebuild" = true ]; then
        info "Restarting Docker stack with rebuild (code/Dockerfile changes)..."
        docker compose up -d --build --force-recreate
    else
        info "Restarting Docker stack (.env changes are picked up)..."
        docker compose up -d --force-recreate
    fi
    ok "Docker stack restarted."
}

_native_restart() {
    local clean="$1"
    cmd_stop
    sleep 2


    local pids pid
    local -a mine=()
    pids=$(lsof -t -i:8000 -sTCP:LISTEN 2>/dev/null || true)
    for pid in $pids; do
        if _pid_in_project "$pid"; then mine+=("$pid"); fi
    done
    if [ "${#mine[@]}" -gt 0 ]; then
        warn "Port 8000 still occupied, terminating..."
        _terminate_pids "${mine[@]}"
    fi

    if [ "$clean" = true ]; then
        info "Cleaning Python cache..."
        find . -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
        find . -name "*.pyc" -delete 2>/dev/null || true
        ok "Cache cleaned."
    fi

    _native_start
}


_find_delete() {
    local dir="$1"; shift
    [ -d "$dir" ] || return 0
    if find "$dir" -mindepth 1 -maxdepth 1 "$@" -exec rm -rf {} + 2>/dev/null; then
        return 0
    fi
    if _docker_usable; then
        warn "Permission denied in $dir — retrying as root via a throwaway container..."
        if docker run --rm -v "$(cd "$dir" && pwd -P)":/target alpine \
                find /target -mindepth 1 -maxdepth 1 "$@" -exec rm -rf {} + ; then
            return 0
        fi
    fi
    fail "Cannot delete files in $dir (permission denied). Docker-created files are often root-owned; run: sudo chown -R \"$(id -un)\" $dir   then retry."
}

_docker_usable() {
    command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1
}


_preflight_deletable() {
    local d bad=""
    for d in "$@"; do
        [ -d "$d" ] || continue
        if [ -n "$(find "$d" -type d ! -writable -print -quit 2>/dev/null)" ]; then
            bad="$bad $d"
        fi
    done
    [ -z "$bad" ] && return 0
    if _docker_usable; then
        info "Some directories are not writable by you (${bad# }); will fall back to a root container for those."
        return 0
    fi
    fail "Not writable:${bad}. Docker-created files are often root-owned; run: sudo chown -R \"$(id -un)\"${bad}   then retry. (Nothing was deleted.)"
}

cmd_reset() {
    local skip_confirm=false
    for arg in "$@"; do
        case "$arg" in
            --yes|-y) skip_confirm=true ;;
        esac
    done

    echo -e "${BOLD}${RED}☢️  CareAgent Factory Reset${NC}"
    echo "═══════════════════════════════════════"
    if ! $skip_confirm; then
        warn "This will PERMANENTLY DELETE:"
        echo "  - All Agent Memory (Chat history, learned facts, token usage)"
        echo "  - All Ingested Data (Wearable health databases)"
        echo "  - All Personalised Pages (data/personalised_pages/)"
        echo "  - All System Logs"
        echo "  - Your learned User Profile (prompts/user.md)"
        echo "  - Docker named volumes (watch-data: raw WatchExporter DB)"
        echo "  - APNs device tokens (memory/device_tokens.db) and Telegram/WeChat"
        echo "    poller cursors — the iOS app must re-register for push afterwards"
        echo ""
        echo -en "${YELLOW}Are you absolutely sure? (y/N) ${NC}"
        read -r -n 1 reply
        echo
        [[ ! "$reply" =~ ^[Yy]$ ]] && { echo "Aborted."; exit 0; }
    fi
    _preflight_deletable memory data/data_stores data/personalised_pages logs ios/Server


    info "Stopping services and removing Docker volumes..."
    if command -v docker >/dev/null 2>&1 \
       && [ -n "$(docker compose ps -a -q 2>/dev/null)" ]; then
        docker compose down --volumes --remove-orphans >/dev/null 2>&1 || true
    fi


    if command -v docker >/dev/null 2>&1; then
        docker volume rm hime_watch-data >/dev/null 2>&1 || true
    fi
    _native_kill_processes
    ok "Services stopped, Docker volumes removed."


    info "Clearing agent memory (memory/)..."
    _find_delete memory
    mkdir -p memory/agent_states


    rm -f memory_db health_db 2>/dev/null || true
    ok "Memory cleared."


    info "Clearing health data stores (data/data_stores/)..."
    _find_delete data/data_stores
    mkdir -p data/data_stores


    info "Clearing Live Watch database (ios/Server/watch.db)..."
    _find_delete ios/Server -name 'watch.db*' ! -type d


    _find_delete data/memory_dbs || true

    ok "Data stores cleared."


    info "Clearing personalised pages (data/personalised_pages/)..."
    _find_delete data/personalised_pages ! -name '_shared'
    mkdir -p data/personalised_pages/_shared
    ok "Personalised pages cleared."


    info "Clearing logs..."
    _find_delete logs
    mkdir -p logs

    _find_delete data/agent_logs || true
    ok "Logs cleared."


    info "Resetting personal user profile..."
    cat > prompts/user.md << 'EOF'
# User Profile

> This file is written and maintained by the agent itself.
> It captures preferences, habits, and communication style learned from
> conversations with the user over Telegram.
> Use the `update_user_profile` tool to update this file.

<!-- Agent: append your observations below this line. -->
EOF
    ok "Profile reset."

    echo ""
    echo -e "${BOLD}${GREEN}✅ System is now in a factory-fresh state.${NC}"
    echo "Run './hime.sh start' to begin a new session."
}


cmd_forget() {
    local skip_confirm=false
    for arg in "$@"; do
        case "$arg" in
            --yes|-y) skip_confirm=true ;;
        esac
    done

    echo -e "${BOLD}${CYAN}🧠 Selective Memory Forget${NC}"
    echo "═══════════════════════════════════════"
    if ! $skip_confirm; then
        warn "This will PERMANENTLY DELETE:"
        echo "  - All Agent Chat History (Telegram conversations)"
        echo "  - All Loop & Turn History (Action traces, internal thoughts)"
        echo "  - All Generated Reports (Stored in DB & shown on dashboard)"
        echo "  - All System Logs (logs/*.log)"
        echo "  - All Personalised Pages (data/personalised_pages/)"
        echo ""
        echo "Data that will be PRESERVED:"
        echo "  - All Ingested Health Data (data/data_stores/)"
        echo "  - Live Watch Database (ios/Server/watch.db)"
        echo "  - Your learned User Profile (prompts/user.md)"
        echo "  - Agent's learned experience (prompts/experience.md)"
        echo "  - APNs device tokens and Telegram/WeChat poller cursors (memory/)"
        echo ""
        echo -en "${YELLOW}Are you sure you want the agent to forget? (y/N) ${NC}"
        read -r -n 1 reply
        echo
        [[ ! "$reply" =~ ^[Yy]$ ]] && { echo "Aborted."; exit 0; }
    fi
    _preflight_deletable memory data/personalised_pages logs


    cmd_stop


    info "Clearing agent conversation states (memory/agent_states/)..."
    _find_delete memory/agent_states
    mkdir -p memory/agent_states


    info "Clearing agent activity logs and reports (memory/*.db)..."

    _find_delete memory -type f \( -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' -o -name '*.json' \) \
        ! -name 'device_tokens.db*' ! -name 'telegram_poller_state.json' ! -name 'weixin_poller_state.json'


    rm -f memory.db memory_db health_db 2>/dev/null || true


    info "Clearing personalised pages (data/personalised_pages/)..."
    _find_delete data/personalised_pages ! -name '_shared'
    mkdir -p data/personalised_pages/_shared


    info "Clearing server logs..."
    _find_delete logs
    mkdir -p logs
    
    ok "Agent selective amnesia complete."
    echo ""
    echo -e "${BOLD}${GREEN}✅ Forget complete.${NC}"
    echo "Run './hime.sh start' to begin a new session."
}


cmd_logs() {

    local target="" arg
    for arg in "$@"; do
        case "$arg" in
            --docker|--native) ;;
            *) [ -z "$target" ] && target="$arg" ;;
        esac
    done
    case "$(_hime_mode "$@")" in
        docker) _docker_logs "${target:-all}" ;;
        *)      _native_logs "${target:-all}" ;;
    esac
}

_docker_logs() {
    local target="$1"
    command -v docker >/dev/null 2>&1 \
        || fail "Docker is not installed."
    if [ "$target" = all ]; then
        echo -e "${CYAN}Tailing docker compose logs (Ctrl+C to exit)${NC}"
        docker compose logs -f --tail=100
    else

        echo -e "${CYAN}Tailing docker compose logs for '$target' (Ctrl+C to exit)${NC}"
        docker compose logs -f --tail=100 "$target"
    fi
}

_native_logs() {
    local target="$1"
    if [ "$target" = all ]; then
        info "Tailing all logs (backend, frontend, watch)..."
        local logs_to_tail="" log_name
        for log_name in backend frontend watch; do
            [ -f "logs/${log_name}.log" ] && logs_to_tail="$logs_to_tail logs/${log_name}.log"
        done
        [ -z "$logs_to_tail" ] && fail "No log files found in logs/ directory."
        tail -f $logs_to_tail
    else
        local logfile="logs/${target}.log"
        if [ ! -f "$logfile" ]; then
            warn "Log file '$logfile' not found."
            echo "Available logs:"
            ls -1 logs/*.log 2>/dev/null | sed 's/logs\///; s/\.log//' || echo "  (None)"
            exit 1
        fi
        echo -e "${CYAN}Tailing $logfile (Ctrl+C to exit)${NC}"
        tail -f "$logfile"
    fi
}


cmd_status() {
    local mode
    mode="$(_hime_mode "$@")"
    echo -e "${BOLD}CareAgent Status${NC} (${mode})"
    echo "═══════════════════════════════════════"
    case "$mode" in
        docker) _docker_status ;;
        *)      _native_status ;;
    esac
    _show_storage
}

_docker_status() {
    if ! command -v docker >/dev/null 2>&1; then
        echo -e "  Docker   — ${RED}not installed${NC}"
        return
    fi
    info "Containers:"
    local out
    out="$(docker compose ps 2>/dev/null || true)"
    if [ -z "$out" ] || [ "$(echo "$out" | wc -l)" -le 1 ]; then
        echo -e "  ${RED}No containers for this compose project.${NC}"
        echo "  Run './hime.sh start' to bring the stack up."
    else
        echo "$out" | sed 's/^/  /'
    fi


    echo ""
    info "Health:"
    if curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health 2>/dev/null | grep -q '^2'; then
        ok "Backend  /health OK       (:8000)"
    else
        echo -e "  Backend  ${RED}/health unreachable${NC}  (:8000)"
    fi
    if curl -s -o /dev/null http://localhost:5173/ 2>/dev/null; then
        ok "Frontend reachable        (:5173)"
    else
        echo -e "  Frontend ${RED}unreachable${NC}          (:5173)"
    fi
    if curl -s -o /dev/null http://localhost:8765/ping 2>/dev/null; then
        ok "Watch    /ping OK         (:8765)"
    else
        echo -e "  Watch    ${RED}/ping unreachable${NC}    (:8765)"
    fi
}

_native_status() {

    local bpids=$(lsof -t -i:8000 2>/dev/null || true)
    if [ -n "$bpids" ]; then
        ok "Backend  — running (PIDs: $(echo $bpids | tr '\n' ' '))"
        if curl -s http://localhost:8000/health > /dev/null 2>&1; then
            ok "           /health endpoint OK"
        else
            warn "           /health endpoint unreachable"
        fi
    else
        echo -e "  Backend  — ${RED}not running${NC}"
    fi


    local fpids=$(lsof -t -i:5173 2>/dev/null || true)
    if [ -n "$fpids" ]; then
        ok "Frontend — running (PIDs: $(echo $fpids | tr '\n' ' '))"
    else
        echo -e "  Frontend — ${RED}not running${NC}"
    fi


    local wpids=$(lsof -t -i:8765 2>/dev/null || true)
    if [ -n "$wpids" ]; then
        ok "Watch Ex — running (PIDs: $(echo $wpids | tr '\n' ' '), Port: 8765)"
    else
        echo -e "  Watch Ex — ${RED}not running${NC} (Port 8765)"
    fi
}


cmd_help() {
    cat << 'HELP'
CareAgent — unified platform CLI

Usage: ./hime.sh <command> [options]

Run mode is picked up from HIME_RUN_MODE in .env (set by setup.sh). The
commands below auto-dispatch to the Docker or native implementation; pass
--docker or --native to any command to force a specific mode for this call.

Commands:
  start               Start all services (Backend, Frontend, Watch Exporter).
  stop                Stop all running services (both modes, idempotent).
  restart [--rebuild] Restart the stack; in docker mode this is equivalent to
                      `docker compose up -d --force-recreate`, which always
                      picks up .env changes.
  restart [--clean]   (native only) also wipe __pycache__/*.pyc.
  status              Show running status and storage usage.
  logs [service]      Follow logs. Defaults to all services.
                      Services: backend | frontend | watch | all
  reset [--yes]       Delete agent memory and ingested data.
  forget [--yes]      Selective erasure: clear chat history but KEEP health data.
  help                Show this help message.

Flags:
  --docker            Force docker-mode dispatch for this invocation.
  --native            Force native-mode dispatch for this invocation.
  --rebuild           (docker restart) also rebuild images. Use this when
                      you changed code, Dockerfile, or a VITE_* env var that
                      gets baked into the frontend bundle.
  --clean, -c         (native restart) clear Python cache.
  --yes,   -y         Skip confirmation (for reset / forget).

Log locations:
  native mode         logs/backend.log, logs/frontend.log, logs/watch.log
  docker mode         docker compose logs (per-service)
HELP
}


command="${1:-help}"
shift 2>/dev/null || true

case "$command" in
    start)   cmd_start "$@" ;;
    stop)    cmd_stop "$@" ;;
    restart) cmd_restart "$@" ;;
    reset)   cmd_reset "$@" ;;
    forget)  cmd_forget "$@" ;;
    logs)    cmd_logs "$@" ;;
    status)  cmd_status "$@" ;;
    help|--help|-h) cmd_help ;;
    *)       fail "Unknown command: $command. Run './hime.sh help' for usage." ;;
esac
