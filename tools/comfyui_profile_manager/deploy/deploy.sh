#!/usr/bin/env bash
# ComfyUI Profile Manager 分階段部署腳本（在 Server 上以 root 執行；每次執行都需另行批准）
#
# 用法（於解壓後的 comfyui_profile_manager 目錄）：
#   sudo bash deploy/deploy.sh stage1     準備檔案、帳號、密碼；不重啟 ComfyUI
#   sudo bash deploy/deploy.sh stage2     接上啟動器＋實測救援機制；ComfyUI 重啟 4 次
#   sudo bash deploy/deploy.sh stage3     放置 polkit 規則並啟用 controller；不重啟 ComfyUI
#   sudo bash deploy/deploy.sh stage4     透過 controller 實際切換／自動回復；ComfyUI 重啟約 4 次
#   sudo bash deploy/deploy.sh rescue     README 的手動救援三步驟＋驗證
#   sudo bash deploy/deploy.sh uninstall  完整移除（保留 comfyui-ctl 帳號、venv、模型與輸出）
#
# 原則：
# - 每階段先做前置檢查，再變更，最後驗證；任何失敗立即停止，並自動執行該階段的回復（UNDO）。
# - 不修改 comfyui.service 本體與 10-data-mount.conf，也不加任何新的 systemd 設定（保留現行 Restart 行為）。
# - 每次重啟 ComfyUI 前都確認佇列為空。
# - Standard 驗證基準＝stage1 當下正式程序的 cmdline 與 /system_stats 版本（存於 baseline-cmdline）。
# - 全部輸出另存 /var/log/comfyui-profile-deploy.log 供驗收。
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
UNIT=comfyui.service
CTL_UNIT=comfyui-profile-manager.service
DROPIN=/etc/systemd/system/comfyui.service.d/20-profile.conf
LAUNCHER=/usr/local/libexec/comfyui-profile-launch
OPT=/opt/comfyui-profile-manager
ETC=/etc/comfyui-profiles
STATE=/var/lib/comfyui-profile-manager
POLKIT=/etc/polkit-1/rules.d/50-comfyui-profile-manager.rules
CTL_UNIT_FILE=/etc/systemd/system/$CTL_UNIT
BASELINE=$ETC/baseline-cmdline
COMFY=http://127.0.0.1:8188
PM=http://127.0.0.1:8189
PY=/usr/bin/python3.12
LOG=/var/log/comfyui-profile-deploy.log
UNDO=""     # 目前階段失敗時要執行的回復函式名稱
JAR=""      # stage4 的 cookie 暫存目錄

log() { printf '\n[%s] %s\n' "$(date +%T)" "$*"; }
die() { printf '\n[FAIL] %s\n' "$*" >&2; exit 1; }

# 任何非 0 結束（die 或 set -e）都會觸發：執行 UNDO、清掉 cookie 暫存
on_exit() {
    local rc=$?
    [ -n "$JAR" ] && rm -rf "$JAR"
    if [ "$rc" -ne 0 ] && [ -n "$UNDO" ]; then
        local u=$UNDO; UNDO=""
        printf '\n[回復] 執行 %s\n' "$u"
        set +e; "$u"
    fi
    exit "$rc"
}

# 從 stdin 的 JSON 取值；$1 為固定的 Python 運算式（d 為解析後物件）
js() { "$PY" -c "import json,sys;d=json.load(sys.stdin);print($1)"; }

# ---- ComfyUI 狀態工具 ----
main_pid() { systemctl show -p MainPID --value "$UNIT"; }
main_cmdline() { local p; p=$(main_pid); [ "$p" != 0 ] && tr '\0' ' ' < "/proc/$p/cmdline" | sed 's/ $//'; }
stats_versions() { curl -sf -m3 "$COMFY/system_stats" | js 'd["system"]["python_version"]+"|"+d["system"]["pytorch_version"]'; }
comfy_up() { curl -sf -m3 "$COMFY/system_stats" >/dev/null; }

# 佇列 running 與 pending 都必須為空；讀不到也視為不可重啟
queue_empty() { curl -sf -m5 "$COMFY/queue" | "$PY" -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if not d["queue_running"] and not d["queue_pending"] else 1)'; }
require_queue_empty() { queue_empty || die "ComfyUI 佇列非空或無法讀取；為避免中斷任務，停止"; }
# 救援／移除用：ComfyUI 已停擺時允許繼續，只有「在線且佇列非空」才停止
require_queue_empty_or_down() { if comfy_up && ! queue_empty; then die "ComfyUI 佇列非空，停止"; fi; }

wait_healthy() { local i; for ((i = 0; i < ${1:-180}; i += 3)); do stats_versions >/dev/null 2>&1 && return 0; sleep 3; done; return 1; }

# Standard 驗證：健康、版本、MainPID cmdline 與部署前基準完全相同、SELinux domain 不變
verify_standard() {
    wait_healthy 180 || { echo "  健康檢查逾時（180 秒）"; return 1; }
    local cmd ver ctx
    cmd=$(main_cmdline); ver=$(stats_versions); ctx=$(ps -o label= -p "$(main_pid)")
    echo "  cmdline: $cmd"; echo "  版本:    $ver"; echo "  SELinux: $ctx"
    [ "$cmd" = "$(sed -n 1p "$BASELINE")" ] || { echo "  cmdline 與基準不同"; return 1; }
    [ "$ver" = "$(sed -n 2p "$BASELINE")" ] || { echo "  版本與基準不同"; return 1; }
    [[ "$ctx" == *:unconfined_service_t:* ]] || { echo "  SELinux domain 不是 unconfined_service_t"; return 1; }
}

# 原子寫入 active 名稱檔（comfyui-ctl 擁有、0644 讓服務使用者可讀）
write_active() {
    local t=$STATE/.active.deploy
    printf '%s\n' "$1" > "$t"; chown comfyui-ctl:comfyui-ctl "$t"; chmod 0644 "$t"; mv -f "$t" "$STATE/active"
}

install_dropin() {
    install -o root -g root -m 0644 "$SRC/deploy/20-profile.conf" "$DROPIN"
    restorecon "$DROPIN"
    systemctl daemon-reload
    systemctl show -p ExecStart --value "$UNIT" | grep -qF "$LAUNCHER" || die "drop-in 未生效"
    systemctl show -p DropInPaths --value "$UNIT" | grep -qF 10-data-mount.conf || die "10-data-mount.conf 不在 DropInPaths"
}

# 與 README「手動 SSH 救援」相同的三步驟（不得用 systemctl revert）
do_rescue() {
    log "救援：移除 20-profile.conf → daemon-reload → restart"
    rm -f "$DROPIN"; systemctl daemon-reload; systemctl restart "$UNIT"
    verify_standard || die "救援後仍未恢復，請人工處理（勿用 systemctl revert）"
    echo "  救援完成：ComfyUI 已回到原始 ExecStart"
}

avc_report() {
    echo "  近 10 分鐘相關 SELinux AVC："
    ausearch -m AVC -ts recent 2>/dev/null | grep -E 'comfyui|python' || echo "  （無）"
}

http_code() { curl -s -o /dev/null -w '%{http_code}' -m5 "$@"; }

wait_controller() {
    local i
    for ((i = 0; i < 20; i++)); do
        [ "$(http_code -H 'Host: localhost:8189' "$PM/")" = 200 ] && return 0; sleep 1
    done
    return 1
}

# ---------------- Stage 1：準備 ----------------
undo_stage1() { rm -rf "$OPT" "$ETC" "$STATE"; rm -f "$LAUNCHER"; echo "  已移除 stage1 檔案（comfyui-ctl 帳號保留）"; }

stage1() {
    log "Stage 1：準備（不重啟 ComfyUI）"
    echo "  SELinux: $(getenforce)"
    systemctl is-active -q "$UNIT" || die "comfyui.service 未在執行"
    local p
    for p in "$OPT" "$ETC" "$STATE" "$LAUNCHER" "$DROPIN" "$POLKIT" "$CTL_UNIT_FILE"; do
        [ ! -e "$p" ] || die "$p 已存在；請先 uninstall 或人工確認"
    done
    [ -z "$(ss -Hltn 'sport = :8189')" ] || die "8189 已被佔用"
    local pid0; pid0=$(main_pid)
    comfy_up || die "ComfyUI 無回應，無法建立基準"

    UNDO=undo_stage1
    id comfyui-ctl >/dev/null 2>&1 || useradd --system --no-create-home --shell /sbin/nologin comfyui-ctl
    id comfyui-ctl | grep -qw wheel && die "comfyui-ctl 不得在 wheel 群組"

    install -d -o root -g root -m 0755 "$OPT" "$ETC"
    cp -r "$SRC/profile_manager" "$SRC/static" "$OPT/"
    find "$OPT" -name __pycache__ -prune -exec rm -rf {} +
    chown -R root:root "$OPT"; chmod -R u=rwX,go=rX "$OPT"
    install -o root -g root -m 0755 "$SRC/launcher/comfyui-profile-launch" "$LAUNCHER"
    install -o root -g root -m 0644 "$SRC/deploy/profiles.example.toml" "$ETC/profiles.toml"
    { main_cmdline; stats_versions; } > "$BASELINE"; chmod 0644 "$BASELINE"

    log "設定管理員密碼（至少 12 字元，輸入不回顯）"
    local h; h=$(cd "$SRC" && "$PY" -I deploy/make_password_hash.py)
    [[ "$h" =~ ^scrypt\$[0-9]+\$[0-9]+\$[0-9]+\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+$ ]] || die "密碼雜湊格式錯誤"
    install -o root -g comfyui-ctl -m 0640 /dev/null "$ETC/admin.hash"
    printf '%s\n' "$h" > "$ETC/admin.hash"

    install -d -o comfyui-ctl -g comfyui-ctl -m 0755 "$STATE"
    write_active standard
    restorecon -R "$OPT" "$ETC" "$STATE" "$LAUNCHER"

    log "Stage 1 驗證"
    ls -Z "$LAUNCHER"
    echo "  基準 cmdline: $(sed -n 1p "$BASELINE")"
    local dry; dry=$(runuser -u "$SVC_USER" -- "$LAUNCHER" --dry-run) || die "啟動器 dry-run 失敗"
    [ "$(printf '%s' "$dry" | js '" ".join(d["argv"])')" = "$(sed -n 1p "$BASELINE")" ] \
        || die "啟動器 argv 與現行 ExecStart 不同：$dry"
    [ "$(printf '%s' "$dry" | js 'd["cwd"]')" = "$(systemctl show -p WorkingDirectory --value "$UNIT")" ] \
        || die "啟動器 cwd 與 WorkingDirectory 不同"
    echo "  啟動器 dry-run（以 $SVC_USER 執行）與現行 ExecStart、WorkingDirectory 完全相同"
    runuser -u comfyui-ctl -- test -r "$ETC/admin.hash" || die "comfyui-ctl 讀不到 admin.hash"
    runuser -u comfyui-ctl -- test -w "$STATE" || die "comfyui-ctl 無法寫入 $STATE"
    runuser -u comfyui-ctl -- test ! -w "$ETC/profiles.toml" || die "comfyui-ctl 不應能寫 profiles.toml"
    [ "$(main_pid)" = "$pid0" ] || die "ComfyUI 主程序意外變動"
    UNDO=""
    log "Stage 1 PASS（ComfyUI 未重啟）"
}

# ---------------- Stage 2：接上啟動器＋救援演練 ----------------
stage2() {
    log "Stage 2：接上啟動器並實測救援（ComfyUI 重啟 4 次）"
    [ -f "$BASELINE" ] && [ -x "$LAUNCHER" ] && [ -f "$STATE/active" ] || die "請先完成 stage1"
    [ ! -e "$DROPIN" ] || die "$DROPIN 已存在"
    require_queue_empty

    log "2a 放置 drop-in，改由啟動器啟動 Standard"
    UNDO=do_rescue
    write_active standard
    install_dropin
    systemctl restart "$UNIT"
    verify_standard || die "以啟動器啟動 Standard 驗證失敗"
    avc_report

    log "2b 救援演練：active 改為未驗證的 trellis2，預期啟動器拒絕（ComfyUI 暫停服務約 30 秒）"
    require_queue_empty
    local t; t=$(date '+%Y-%m-%d %H:%M:%S')
    write_active trellis2
    systemctl restart "$UNIT" || true
    sleep 25   # 涵蓋至少一次 RestartSec=10 的自動重試
    local n; n=$(journalctl -u "$UNIT" --since "$t" -o cat | grep -c '拒絕啟動' || true)
    [ "$n" -ge 1 ] || die "未看到啟動器拒絕訊息"
    if comfy_up; then die "ComfyUI 不應在線"; fi
    echo "  啟動器拒絕 $n 次；systemd 依現行 Restart=on-failure 持續重試（符合預期）"

    log "2c 依 README 手動救援步驟恢復"
    UNDO=""
    do_rescue

    log "2d 依 README 重新啟用：active 寫回 standard → 放回 drop-in → daemon-reload → restart"
    require_queue_empty
    UNDO=do_rescue
    write_active standard
    install_dropin
    systemctl restart "$UNIT"
    verify_standard || die "重新啟用後 Standard 驗證失敗"
    UNDO=""
    log "Stage 2 PASS（啟動器生效、拒絕與救援皆實測通過）"
}

# ---------------- Stage 3：polkit＋controller ----------------
undo_stage3() {
    systemctl disable --now "$CTL_UNIT" 2>/dev/null
    rm -f "$CTL_UNIT_FILE" "$POLKIT"; systemctl daemon-reload
    echo "  已停用並移除 controller 與 polkit 規則（ComfyUI 未受影響）"
}

# 確認 comfyui-ctl 執行某個 systemctl 動作會被 polkit 拒絕
expect_denied() {
    local out
    if out=$(runuser -u comfyui-ctl -- systemctl --no-ask-password "$@" 2>&1); then die "systemctl $* 竟被允許"; fi
    echo "  systemctl $* → $out"
    echo "$out" | grep -qiE 'access denied|authentication required|not authorized' || die "非預期的錯誤訊息，請人工確認"
}

stage3() {
    log "Stage 3：放置 polkit 規則並啟用 controller（不重啟 ComfyUI）"
    [ -e "$DROPIN" ] || die "請先完成 stage2"
    [ ! -e "$POLKIT" ] && [ ! -e "$CTL_UNIT_FILE" ] || die "polkit 規則或 controller unit 已存在"
    local pid0; pid0=$(main_pid)

    UNDO=undo_stage3
    install -o root -g root -m 0644 "$SRC/deploy/50-comfyui-profile-manager.rules" "$POLKIT"
    install -o root -g root -m 0644 "$SRC/deploy/comfyui-profile-manager.service" "$CTL_UNIT_FILE"
    restorecon "$POLKIT" "$CTL_UNIT_FILE"
    systemctl daemon-reload
    sleep 2   # polkit 監看 rules.d，自動重新載入

    log "3a polkit 最小授權：restart 以外的動作、其他 unit 都必須被拒"
    expect_denied reload "$UNIT"          # 即使誤放行也無副作用（comfyui 無 ExecReload）
    expect_denied restart "$CTL_UNIT"     # 即使誤放行也只是提早啟動 controller；不測 stop 以免誤停 ComfyUI

    log "3b 啟用 controller"
    systemctl enable --now "$CTL_UNIT"
    wait_controller || { journalctl -u "$CTL_UNIT" -n 30 --no-pager; die "controller 未在 20 秒內回應"; }
    [ "$(ps -o user= -p "$(systemctl show -p MainPID --value "$CTL_UNIT")")" = comfyui-ctl ] || die "controller 執行身分不是 comfyui-ctl"
    [ -z "$(ss -Hltn 'sport = :8189' | grep -v '127.0.0.1:8189 ' || true)" ] || die "8189 不只綁 127.0.0.1"
    [ "$(http_code -H 'Host: localhost:8189' "$PM/api/status")" = 401 ] || die "未登入的 /api/status 應為 401"
    [ "$(http_code -H 'Host: evil.example:8189' "$PM/")" = 403 ] || die "錯誤 Host 應為 403"
    [ "$(http_code -H 'Host: localhost:8189' -H 'Origin: http://evil.example' -d '{}' "$PM/api/switch")" = 403 ] \
        || die "錯誤 Origin 應為 403"
    echo "  controller 以 comfyui-ctl 執行、只綁 127.0.0.1:8189；未登入 401、錯 Host/Origin 403"
    [ "$(main_pid)" = "$pid0" ] || die "ComfyUI 主程序意外變動"
    UNDO=""
    log "Stage 3 PASS（ComfyUI 未重啟）"
}

# ---------------- Stage 4：透過 controller 實際切換 ----------------
undo_stage4() {
    systemctl stop "$CTL_UNIT"
    [ -f "$ETC/profiles.toml.pre-stage4" ] && mv -f "$ETC/profiles.toml.pre-stage4" "$ETC/profiles.toml"
    if [ "$(cat "$STATE/active")" != standard ] || ! verify_standard; then
        write_active standard
        systemctl restart "$UNIT"
        verify_standard || do_rescue
    fi
    echo "  controller 已停止、測試 profile 已移除；確認後以 systemctl start $CTL_UNIT 重新啟用"
}

api() { curl -s -m10 -b "$JAR/c" -H 'Host: localhost:8189' "$@"; }

# 經 Web API 切換到 $1，等待結束並要求結果為 $2（SUCCESS / ROLLED_BACK）
switch_to() {
    local name=$1 want=$2 code st i
    log "切換到 $name（預期 $want）"
    require_queue_empty
    code=$(api -o /dev/null -w '%{http_code}' -H 'Origin: http://localhost:8189' -H "X-CSRF-Token: $CSRF" \
        -H 'Content-Type: application/json' --data-binary "{\"profile\":\"$name\"}" "$PM/api/switch")
    [ "$code" = 202 ] || die "切換 $name 未被接受（HTTP $code）"
    for ((i = 0; i < 600; i += 3)); do
        sleep 3; st=$(api "$PM/api/status")
        [ "$(printf '%s' "$st" | js 'd["state"]')" != SWITCHING ] && break
    done
    printf '%s' "$st" | js '"\n".join("    - "+s for s in d["steps"])+"\n  結果："+str(d["result"])'
    [ "$(printf '%s' "$st" | js '(d["result"] or {}).get("outcome")')" = "$want" ] || die "$name 結果不是 $want"
    [ "$(printf '%s' "$st" | js 'd["state"]')" = IDLE ] || die "controller 狀態不是 IDLE"
}

stage4() {
    log "Stage 4：透過 controller 實際切換與自動回復（ComfyUI 重啟約 4 次）"
    systemctl is-active -q "$CTL_UNIT" || die "請先完成 stage3"
    [ "$(cat "$STATE/active")" = standard ] || die "active 不是 standard"
    require_queue_empty
    verify_standard || die "起點不是正常的 Standard"

    UNDO=undo_stage4
    cp -p "$ETC/profiles.toml" "$ETC/profiles.toml.pre-stage4"
    # 暫時加入兩個測試 profile：內容沿用 standard，只改名稱與預期 torch 版本（不安裝任何東西）
    "$PY" - "$ETC/profiles.toml" <<'PYEOF'
import sys, tomllib
path = sys.argv[1]
std = next(p for p in tomllib.load(open(path, "rb"))["profile"] if p["name"] == "standard")
q = lambda s: '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
def block(name, torch):  # 產生一個 [[profile]] 區塊
    return "\n".join(["", "[[profile]]", f"name = {q(name)}", f"display_name = {q(name + ' (deploy test)')}",
                      f"python = {q(std['python'])}", f"app_dir = {q(std['app_dir'])}",
                      "args = [" + ", ".join(q(a) for a in std["args"]) + "]",
                      f"expected_python = {q(std['expected_python'])}", f"expected_torch = {q(torch)}",
                      "validated = true", ""])
with open(path, "a", encoding="utf-8") as f:
    f.write(block("standard_check", std["expected_torch"]) + block("rollback_check", "9.9"))
PYEOF
    systemctl restart "$CTL_UNIT"
    wait_controller || die "controller 重啟後未回應"

    JAR=$(mktemp -d); chmod 700 "$JAR"
    local pw resp
    read -rsp "管理員密碼（只嘗試一次）：" pw; echo
    resp=$(PMPW="$pw" "$PY" -c 'import json,os;print(json.dumps({"password":os.environ["PMPW"]}))' \
        | curl -s -m10 -c "$JAR/c" -H 'Host: localhost:8189' -H 'Origin: http://localhost:8189' \
            -H 'Content-Type: application/json' --data-binary @- "$PM/api/login")
    pw=""
    CSRF=$(printf '%s' "$resp" | js 'd.get("csrf","")' 2>/dev/null || true)
    [ -n "$CSRF" ] || die "登入失敗：$resp"

    switch_to standard_check SUCCESS
    [ "$(cat "$STATE/active")" = standard_check ] || die "active 未變為 standard_check"
    verify_standard || die "standard_check 執行狀態與基準不符"
    switch_to standard SUCCESS
    verify_standard || die "切回 standard 後驗證失敗"
    switch_to rollback_check ROLLED_BACK
    [ "$(cat "$STATE/active")" = standard ] || die "自動回復後 active 不是 standard"
    [ "$(cat "$STATE/last_good")" = standard ] || die "last_good 不是 standard"
    verify_standard || die "自動回復後 Standard 驗證失敗"

    log "清理：登出、移除測試 profile、重啟 controller"
    api -o /dev/null -H 'Origin: http://localhost:8189' -H "X-CSRF-Token: $CSRF" -d '{}' "$PM/api/logout" || true
    mv -f "$ETC/profiles.toml.pre-stage4" "$ETC/profiles.toml"
    systemctl restart "$CTL_UNIT"
    wait_controller || die "controller 重啟後未回應"
    UNDO=""
    log "Stage 4 PASS（成功切換、切回、失敗自動回復皆實測通過；測試 profile 已移除）"
}

# ---------------- 救援與完整移除 ----------------
rescue() { require_queue_empty_or_down; do_rescue; echo "  如需清除 RESCUE_NEEDED：systemctl restart $CTL_UNIT"; }

uninstall() {
    log "完整移除（保留 comfyui-ctl 帳號、venv、模型與輸出）"
    systemctl disable --now "$CTL_UNIT" 2>/dev/null || true
    rm -f "$CTL_UNIT_FILE" "$POLKIT"
    if [ -e "$DROPIN" ]; then
        require_queue_empty_or_down
        do_rescue
    else
        systemctl daemon-reload
    fi
    rm -rf "$OPT" "$ETC" "$STATE"; rm -f "$LAUNCHER"
    echo "  DropInPaths: $(systemctl show -p DropInPaths --value "$UNIT")"
    log "uninstall 完成"
}

# ---------------- 進入點 ----------------
[ "$(id -u)" = 0 ] || die "必須以 root 執行（sudo bash deploy/deploy.sh <stage>）"
case "${1:-}" in
    stage1 | stage2 | stage3 | stage4 | rescue | uninstall) ;;
    *) echo "用法：sudo bash deploy/deploy.sh stage1|stage2|stage3|stage4|rescue|uninstall" >&2; exit 2 ;;
esac
exec > >(tee -a "$LOG") 2>&1
trap on_exit EXIT
SVC_USER=$(systemctl show -p User --value "$UNIT")   # 正式服務使用者，不寫死帳號
[ -n "$SVC_USER" ] || die "無法取得 comfyui.service 的 User"
log "deploy.sh $1（來源 $SRC）"
"$1"
