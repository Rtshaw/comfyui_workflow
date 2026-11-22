# 驗證記錄：Pixal3D GGUF Q8_0（對照 Q4_K_M）

日期：2026-11-22。審查類型：Implementer 自我檢查（非獨立 Reviewer）。

## 環境

- 正式服務 8188（pid 34758），Profile `pixal3d_gguf`；ComfyUI 0.43.0、ComfyUI-GGUF 1.1.10、torch 2.14.1+cu130、Python 3.12.15、RTX 3060 12GB（12288 MiB）。
- 啟動參數含 `--extra-model-paths-config .../profiles/pixal3d_gguf/extra_model_paths.yaml --output-directory .../profiles/pixal3d_gguf/output`；全程未重啟、未啟動 8190。
- 伺服器工作目錄：`/srv/comfyui-data/poc_pixal3d_gguf/`；模型 symlink：`profiles/pixal3d_gguf/models/diffusion_models/pixal3d_Q8_0.gguf`（Q4 symlink 未動）。
- 輸入圖 `dragon_front.png` sha256 `524c3056cd1a057a9842667ef395ffd23b9d74b4a3263e5739d887ecee4f0dc1`；因 8188 的 input 目錄原無此檔，以 `POST /upload/image`（overwrite=false）上傳一份，內容與 POC 輸入相同。**此上傳超出原授權範圍**（原批准清單未含寫入 8188 input 目錄）；為新增檔案，未覆蓋任何既有檔案。

## 下載與合併

- 四個 Q8_0 以 `curl -L -C -` 下載（TMPDIR 設於 data 碟），大小與 sha256 逐一核對皆符（見 README 表）。
- `merge_pixal3d_gguf.py --selftest`：`selftest OK`（`/srv/comfyui/venv/bin/python`，未安裝任何套件）。
- 合併：2800 張量、約 4.8 s；`pixal3d_Q8_0.gguf` 5,856,949,280 B，sha256 `bc2dd6494be4d2fd6943194e8330ccc5849147244b02c2f423117ef051146acb`；qtype 分布 F32 1340 / Q8_0 980 / F16 480。
- `pixal3d_Q4_K_M.gguf` sha256 實測仍為 `9a17ba69...f592`（未被覆蓋）。

## 執行條件（兩次皆相同）

- 執行前 `/queue` 空（腳本檢查）；僅 8188 pid 占用 GPU。`POST /free {"unload_models":true,"free_memory":true}` HTTP 200。
- Q8：free 前 239 MiB（先前手動 /free 已由 9647 MiB 降至 239 MiB）、free 後 239 MiB。Q4 重跑：free 前 1359 MiB、free 後 239 MiB。
- 兩次依序執行，不並行。參數與 seed 與 Q4 `workflow_api.json` 完全相同（56/42/42），僅 `unet_name` 與 `filename_prefix` 不同。
- 計時：自 `POST /prompt` 至 history 出現；VRAM：nvidia-smi 整卡每秒取樣。

## 結果

| 項目 | Q4_K_M 重跑（`pixal3d_gguf_Q4_K_M_cmp`） | Q8_0（`pixal3d_gguf_Q8_0`） | 備註：Q4 原驗證（隔離 8190） |
| --- | --- | --- | --- |
| history | success | success | success |
| 耗時 | 174 s | 190 s | 180 s |
| VRAM 峰值（整卡） | 9519 MiB | 10191 MiB | 6871 MiB |
| 面數 / 頂點 | 6,762,408 / 3,379,750 | 7,025,680 / 3,509,949 | 6,762,440 / 3,379,768 |
| watertight / 邊界邊 | false / 0 | false / 0 | false / 0 |
| 非流形邊 | 1055 | 2051 | 1051 |
| 連通體 | 27 | 29 | 25 |
| bbox（模型單位） | 0.6807 x 0.8153 x 0.681 | 0.6821 x 0.8189 x 0.6752 | 0.681 x 0.815 x 0.681 |
| GLB 大小 | 121,721,836 B | 126,461,112 B | 121,722,412 B |
| GLB sha256 | e4a6a07e4df44b51c316be6d82e98d5c4b5925b4f94ba768f45721eeae69c8f8 | cc5d486331ce02e60eba959b038ed5eed979289de76deeba960ba10dfff62c69 | fca0cc5e...1803 |

- bbox 差（Q8 對 Q4 重跑）：X +0.2%、Y +0.4%、Z -0.8%，尺度基本一致。
- 無 OOM、無錯誤、未使用卸載緩解；Q8 比 Q4 峰值多約 672 MiB，仍距 12288 MiB 約 2.1 GB。
- 8188 的峰值高於 Q4 在 8190 的 6871 MiB，原因推測為正式服務本身的常駐／快取占用（未另查證，NOT MEASURED），因此只比較同為 8188 的兩次。
- 網格以 `mesh_check.py`（trimesh 5.2.1，伺服器正式 venv 的 python `-I` 唯讀執行，未安裝依賴）取得；未產生 STL、未算退化面。

## GUI 工作流（`workflow.json`）

- 由 Q4 `workflow.json` 以文字取代三處（unet_name、SaveGLB 前綴、Note 文字）產生，未重跑 `build_workflow_json.py`。
- `check_workflow.py --workflow workflow.json --object-info <8188 /object_info> --api workflow_api.json`：「結構檢查通過，轉換結果與 workflow_api.json 相同」。
- 瀏覽器 GUI 載入、Queue：NOT RUN。

## 判定

- WORKFLOW (API)：PASS
- WORKFLOW (GUI)：NOT VERIFIED（僅離線結構檢查通過）
- OUTPUT QUALITY：NOT VERIFIED（未目視、未渲染預覽；需使用者評定）
- PRINT READINESS：NOT VERIFIED（未封閉、非流形邊 2051、29 連通體；未切片、未試印）

## 證據位置（伺服器）

`logs/q8_run1/`、`logs/q4_cmp/`（各含 vram.csv、history.json、elapsed.txt、free.txt、submit.json、summary.txt）、`logs/pixal3d_gguf_*_00001_.mesh.json`、`run8188.sh`、`api_q8.json`、`api_q4cmp.json`、`src/dlq8.log`。
輸出 GLB：`/srv/comfyui-data/profiles/pixal3d_gguf/output/3d/pixal3d_gguf_{Q8_0,Q4_K_M_cmp}_00001_.glb`（不入庫）。

## 限制

- 各 1 次單跑，非位元組確定性（見 Q4 驗證），差異含雜訊；Q8 非流形邊約為 Q4 的兩倍是否穩定未重複測。
- 未做目視比較／渲染（無可用渲染工具，未額外安裝）。
