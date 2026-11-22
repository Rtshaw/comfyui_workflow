# ComfyUI Profile Manager（MVP）

在同一台 Server 上，以網頁一鍵切換 `comfyui.service` 使用的 ComfyUI 環境（venv / 參數）。
只用 Python 標準函式庫（Server 為 `/usr/bin/python3.12`）。

> 狀態：僅完成本機實作與離線／模擬測試。**尚未部署，未在真實 Server 驗證**（WORKFLOW/部署驗證 NOT RUN）。
> 依 AGENTS.md，部署與推送都需另行批准與獨立 Reviewer 驗收。

## 架構

```
瀏覽器 --SSH tunnel--> 127.0.0.1:8189  comfyui-profile-manager.service (User=comfyui-ctl)
                          | 寫 active 名稱檔            | polkit: restart comfyui.service
                          v                              v
 /var/lib/comfyui-profile-manager/active      comfyui.service (既有服務使用者, 127.0.0.1:8188, Caddy 不變)
                                                 ExecStart = /usr/local/libexec/comfyui-profile-launch
                                                 -> 讀 active + /etc/comfyui-profiles/profiles.toml (root 擁有)
                                                 -> 驗證通過才 os.execv 對應 python main.py + 核准參數
```

- controller 只能寫「profile 名稱」；python 路徑與參數只存在 root 擁有的 `profiles.toml`，因此無法注入任意命令。
- 啟動器拒絕：名稱不合法、不在清單、`validated=false`、python 不可執行（退出碼 2）。
- 切換流程：取鎖 → 檢查（validated、路徑存在、非目前 profile）→ `/queue` 必須為空 → 記錄 last_good
  → 停止前再查佇列 → 寫 active → `systemctl restart` → 輪詢 `/system_stats`（預設逾時 180 秒）
  → 驗證 python/torch 版本前綴與 MainPID 的 `/proc/<pid>/cmdline` argv[0] == profile python → 成功更新 last_good。
  失敗則寫回舊 profile、restart、同樣驗證；成功才回報「已自動回復」，否則狀態 `RESCUE_NEEDED` 並顯示 SSH 救援步驟，
  且之後拒絕切換，直到手動救援並重啟管理服務。
- 外部副作用（systemctl、ComfyUI HTTP、/proc、nvidia-smi、時鐘）皆為注入介面，subprocess 一律 list 參數、無 shell。

## 權限模型

| 主體 | 能做 | 不能做 |
| --- | --- | --- |
| `comfyui-ctl`（系統使用者，非 wheel、無 sudo） | 寫 `/var/lib/comfyui-profile-manager`；對 `comfyui.service` 執行 restart（polkit，只放行此 verb） | 改 profiles.toml、改 unit、其他服務 |
| `root` | 擁有 profiles.toml、啟動器、drop-in、polkit 規則、`admin.hash` | — |

驗證：單一管理員，scrypt 密碼雜湊；HttpOnly + SameSite=Strict cookie；變更類 API 需 `X-CSRF-Token`；
所有請求檢查 Host，POST 另檢查 Origin；登入同一來源 5 次失敗鎖 5 分鐘。服務只監聽 127.0.0.1。
備援方案：若不用 polkit，可改 sudoers 只允許 `systemctl restart comfyui.service` 單一命令（本 MVP 未實作）。

## 部署步驟（**需另行批准，本輪未執行**）

實際部署以 `deploy/deploy.sh` 分四階段執行（每階段驗證、失敗即停止並回復）；以下為其內容說明。

1. 建立使用者：`useradd --system --shell /sbin/nologin comfyui-ctl`
2. 複製程式到 `/opt/comfyui-profile-manager/`（`profile_manager/`、`static/`，root 擁有）。
3. `launcher/comfyui-profile-launch` → `/usr/local/libexec/`（root:root 0755；注意 SELinux Enforcing，必要時 `restorecon -v`）。
4. `deploy/profiles.example.toml` → `/etc/comfyui-profiles/profiles.toml`（root:root 0644）。**先確認 standard 的 args 與現行 ExecStart 完全相同。**
5. `python3.12 deploy/make_password_hash.py > /etc/comfyui-profiles/admin.hash`；`chown root:comfyui-ctl`、`chmod 0640`。
6. 建立初始 active：`install -d -o comfyui-ctl -g comfyui-ctl /var/lib/comfyui-profile-manager && echo standard > /var/lib/comfyui-profile-manager/active && chown comfyui-ctl:comfyui-ctl /var/lib/comfyui-profile-manager/active`。unit 的 `StateDirectory=` 啟動時也會把目錄 chown 給 comfyui-ctl；controller 以 os.replace 更新檔案，只需目錄可寫。
7. 先**手動**以 `comfyui-profile-launch --dry-run` 核對輸出 argv 與現行 ExecStart 一致。
8. 放置 polkit 規則 `deploy/50-comfyui-profile-manager.rules` → `/etc/polkit-1/rules.d/`。
9. 放置 `deploy/comfyui-profile-manager.service` → `/etc/systemd/system/`。
10. 放置 drop-in `deploy/20-profile.conf` → `/etc/systemd/system/comfyui.service.d/`，`systemctl daemon-reload`，
    於無任務時 `systemctl restart comfyui.service`，確認 Standard 行為不變（health、版本、cmdline）。
11. `systemctl enable --now comfyui-profile-manager.service`。

## 使用方式

```
ssh -L 8189:127.0.0.1:8189 <user>@<server>
# 瀏覽器開 http://localhost:8189 ，登入後即可切換。
```
本機端口必須同為 8189：controller 檢查 Host 標頭（防 DNS rebinding），只接受 `localhost:8189`／`127.0.0.1:8189`。
切換會重啟 ComfyUI，需佇列為空。畫面顯示目前 profile、各 profile 安裝／驗證狀態、Python/PyTorch、GPU、VRAM、佇列、進度與結果。

## 新增 profile

1. 在 `profiles.toml`（root 編輯）新增 `[[profile]]`，名稱僅 `[a-z0-9_-]`，先 `validated = false`。
2. 人工安裝該環境並在**真實環境**實測（手動切換、載入工作流、執行）。
3. 全部通過後才改 `validated = true`。controller 每次啟動時讀取，需 `systemctl restart comfyui-profile-manager`。

## 手動 SSH 救援

切換失敗且自動回復也失敗（`RESCUE_NEEDED`），或管理服務無法使用時：

```
sudo rm /etc/systemd/system/comfyui.service.d/20-profile.conf
sudo systemctl daemon-reload
sudo systemctl restart comfyui.service
```

**不得使用 `systemctl revert comfyui.service`**：它會連帶移除 `10-data-mount.conf`（資料掛載保護）。
恢復後確認 `curl -s 127.0.0.1:8188/system_stats`，再重啟 `comfyui-profile-manager` 以清除 RESCUE_NEEDED。
想重新啟用管理機制時，先把 active 寫回實際運行的 profile（`echo standard | sudo tee /var/lib/comfyui-profile-manager/active`），
再把 `20-profile.conf` 放回並 daemon-reload，否則下次重啟會依舊的 active 名稱啟動。

## 已知限制

- ComfyUI 佇列在記憶體中；「停止前最後一次檢查」到實際 stop 之間仍有極短競態窗口，此時提交的任務會遺失。
- 登入限速以來源 IP 為 key；經 SSH tunnel 時來源皆為 127.0.0.1，實際上是全域限速，本機使用者可讓管理員被鎖 5 分鐘（僅造成 DoS）。
- controller 在切換途中被非預期重啟或主機 reboot 時，不會自動 rollback；需以 `systemctl status comfyui.service` 與 `/system_stats` 手動確認，必要時走救援步驟。
- 無法讀到佇列（ComfyUI 無回應）時一律拒絕切換；此時需 SSH 處理。
- session 與登入限速存在記憶體，重啟管理服務即全部登出。
- Windows 本機測試無 `fcntl`，檔案鎖退化為 `threading.Lock`（Server 為 Linux，兩者並用）。
- 只有 HTTP（綁 127.0.0.1，靠 SSH tunnel 加密），cookie 未設 Secure。
- 不做：網頁安裝套件、下載模型、多使用者、編輯任意設定、改 Caddy／防火牆。

## 測試

```
python -m unittest discover        # 於本目錄執行，離線
```
涵蓋：controller 狀態機（空佇列成功、running/pending 拒絕、停止前變忙中止、無效/未驗證/python 不存在拒絕、
啟動失敗/健康逾時/版本不符/cmdline 不符回復、鎖、回復失敗 RESCUE_NEEDED）、HTTP 驗證（未登入/缺 CSRF/錯 Origin/Host/限速）、
啟動器 `--dry-run`、以及真 HTTP server + 假 ComfyUI + 假 service 的登入→切換→成功／失敗→回復整合測試。

### 測試證據

- 日期 2026-11-22；本機 Windows 10，`python -B -W error::ResourceWarning -m unittest discover`：Python 3.13.0 與 3.11.9 皆 26 tests OK。
- 以上均為離線模擬，**不能取代**真實 Server 上的部署驗證（systemd、polkit、SELinux、真實 ComfyUI 重啟皆 NOT RUN）。
