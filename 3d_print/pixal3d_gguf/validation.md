# 驗證記錄：Pixal3D GGUF Q4_K_M

日期：2026-11-22。審查類型：Implementer 自我檢查，另經獨立 Reviewer 於 2026-11-22 唯讀核對（PASS WITH CONDITIONS，條件已處理）。

## 環境

隔離測試實例（port 8190，僅本機），使用正式 venv 的 python 作為直譯器，測試用 app 為 POC 副本（ComfyUI 0.43.0 / b5a4321）加 ComfyUI-GGUF 1.1.10。
torch 2.14.1+cu130、Python 3.12.15、RTX 3060 12GB。正式服務（8188）、模型目錄、venv 皆未修改；測試前佇列為空、顯存 121 MiB。
測試目錄（伺服器）：`/srv/comfyui-data/poc_pixal3d_gguf/`

## 合併腳本

- `merge_pixal3d_gguf.py --selftest`：PASS（正式 venv python，離線合成 GGUF）。Windows 本機未跑。第二輪擴充：涵蓋 rope_phases 丟棄、前綴改名、orig_shape KV 改名（舊名不殘留）、量化型別／形狀／位元組一致、32 對齊、重複合併位元組相同、缺階段／前綴重複／架構不一致報錯。
- 合併可重現性：在伺服器以暫存路徑重跑合併（改良版腳本），2800 張量，sha256 `9a17ba696cc8df727c66657ce40daf65247cc05534b74c429455f68ba767f592`，與既有 `pixal3d_Q4_K_M.gguf` 相同；暫存檔已刪除。
- 腳本審查：量化位元組與 orig_shape 處理正確；新增「必要四階段齊全且不重複」檢查（原 docstring 宣稱有前綴檢查但未實作）。
- 實際合併：2800 張量（4 × 700），約 1.7 秒；載入日誌 `gguf qtypes: F32 (1340), Q4_K (977), F16 (483)`，
  原生模型偵測成功（arch=flux 未造成鍵重映射問題）。

## Run 1（Q4_K_M）

- 輸入 `dragon_front.png`，seed 與 POC 相同（56 / 42 / 42），參數同 `workflow_api.json`
- 結果：history `status=success`；耗時 180 s（無 progress 外的錯誤、未 OOM、未使用卸載緩解）
- 顯存峰值 6871 MiB（nvidia-smi 每秒取樣，整卡）
- 輸出：`output/3d/pixal3d_gguf_Q4_K_M_00001_.glb`（121,722,412 bytes）sha256 `fca0cc5e15ec542b72c5329c287f8b556d5e22e58c7f051f6635d49983321803`
- 對照 int8 POC（同流程）：171 s、顯存峰值 11933 MiB（且 RemeshMesh 曾 OOM）

## GUI 工作流驗證（`workflow.json`，第二輪）

- `check_workflow.py`（離線、標準函式庫）：PASS — 30 節點（29 個實際節點 + 1 Note）、43 條 link；型別皆存在於 8190 `/object_info`，widget 數量、link 兩端與型別一致，seed 皆 `fixed`。
- UI->API 轉換與 `workflow_api.json` 比對：PASS（class_type / inputs 完全相同）。
- `GET /`（8190）200；`POST /api/userdata/workflows%2Fpixal3d_gguf.json` 200，`GET /api/userdata?dir=workflows` 列出 `pixal3d_gguf.json`。
- 轉換後 API 實跑（`api_from_ui.json`，同 seed 56/42/42）：history success，耗時 181 s（run1 180 s），顯存峰值 6839 MiB（run1 6871），輸出 `pixal3d_gguf_Q4_K_M_00002_.glb`（121,721,044 bytes）sha256 `e8dbf4fb71bb835697c4a76253f240ccdc39c587b2780b5ad5149a8db16016b5`（與 run1 不同，非位元組確定性）。網格：6,762,364 面、非流形邊 1052、25 連通體、未封閉（run1：6,762,440 / 1051 / 25）。
- 瀏覽器 GUI 載入並 Queue：**使用者回報 PASS（2026-11-22；Agent 未能驗證，無截圖或日誌留存）**。方式：使用者以 `ssh -L 8190:127.0.0.1:8190 comfyui-server` 後開 `http://127.0.0.1:8190/`，在工作流清單開 `pixal3d_gguf.json`。
- 證據（伺服器）：`logs/ui_run1/`、`api_from_ui.json`、`workflow.json`、`object_info.json`。

## 網格檢查（trimesh 5.2.1，`mesh_check.py` + 退化面）

| 項目 | GGUF Q4_K_M | int8 POC |
| --- | --- | --- |
| 面數 / 頂點 | 6,762,440 / 3,379,768 | 7,122,124 / 3,558,480 |
| watertight | false | false |
| 邊界（孔洞）邊 | 0 | 0 |
| 非流形邊 | 1051 | 1836 |
| 連通體 | 25 | 79 |
| bbox（模型單位） | 0.681 x 0.815 x 0.681 | 0.684 x 0.819 x 0.679 |
| 退化面（面積<=1e-12） | 7 | 未測 |

STL 以 trimesh 匯出（`output/3d/q4.stl`，約 338 MB，僅留在伺服器）。

## 判定

- WORKFLOW (API)：PASS（API 提交、history success、產出 GLB 可被 trimesh 重新讀取）
- WORKFLOW (GUI)：PASS（離線檢查、轉換比對、轉換後實跑 PASS；瀏覽器載入為使用者回報 PASS，無留存證據）
- OUTPUT QUALITY：NOT VERIFIED（未目視評定；未產生預覽圖）
- PRINT READINESS：NOT VERIFIED（網格未封閉、有非流形邊；伺服器無切片器 CLI，未切片、未試印）
- Q8_0：NOT RUN；trellis2 validated=false；獨立 Reviewer 已核對（未重跑 selftest/check_workflow）

## 證據位置（伺服器）

`logs/q4_run1/{vram.csv,history.json,elapsed.txt,submit.json}`、`logs/server.log`、`src/SHA256SUMS`、`api_q4.json`、`extra_model_paths.yaml`、`start.sh`、`run_prompt.sh`。
