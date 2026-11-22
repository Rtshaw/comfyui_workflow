# 驗證記錄：Pixal3D GGUF Q4_K_M

日期：2026-11-22。審查類型：Implementer 自我檢查（非獨立 Reviewer）。

## 環境

隔離測試實例（port 8190，僅本機），使用正式 venv 的 python 作為直譯器，測試用 app 為 POC 副本（ComfyUI 0.43.0 / b5a4321）加 ComfyUI-GGUF 1.1.10。
torch 2.14.1+cu130、Python 3.12.15、RTX 3060 12GB。正式服務（8188）、模型目錄、venv 皆未修改；測試前佇列為空、顯存 121 MiB。
測試目錄（伺服器）：`/srv/comfyui-data/poc_pixal3d_gguf/`

## 合併腳本

- `merge_pixal3d_gguf.py --selftest`：PASS（正式 venv python，離線合成 GGUF）。Windows 本機未跑。
- 實際合併：2800 張量（4 × 700），約 1.7 秒；載入日誌 `gguf qtypes: F32 (1340), Q4_K (977), F16 (483)`，
  原生模型偵測成功（arch=flux 未造成鍵重映射問題）。

## Run 1（Q4_K_M）

- 輸入 `dragon_front.png`，seed 與 POC 相同（56 / 42 / 42），參數同 `workflow_api.json`
- 結果：history `status=success`；耗時 180 s（無 progress 外的錯誤、未 OOM、未使用卸載緩解）
- 顯存峰值 6871 MiB（nvidia-smi 每秒取樣，整卡）
- 輸出：`output/3d/pixal3d_gguf_Q4_K_M_00001_.glb`（121,722,412 bytes）sha256 `fca0cc5e15ec542b72c5329c287f8b556d5e22e58c7f051f6635d49983321803`
- 對照 int8 POC（同流程）：171 s、顯存峰值 11933 MiB（且 RemeshMesh 曾 OOM）

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

- WORKFLOW：PASS（API 提交、history success、產出 GLB 可被 trimesh 重新讀取）；GUI 載入 NOT RUN
- OUTPUT QUALITY：NOT VERIFIED（未目視評定；未產生預覽圖）
- PRINT READINESS：NOT VERIFIED（網格未封閉、有非流形邊；伺服器無切片器 CLI，未切片、未試印）
- Q8_0：NOT RUN

## 證據位置（伺服器）

`logs/q4_run1/{vram.csv,history.json,elapsed.txt,submit.json}`、`logs/server.log`、`src/SHA256SUMS`、`api_q4.json`、`extra_model_paths.yaml`、`start.sh`、`run_prompt.sh`。
