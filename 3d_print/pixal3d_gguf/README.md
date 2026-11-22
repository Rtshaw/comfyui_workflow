# Pixal3D（GGUF Q4_K_M）圖生 3D 網格工作流

以 ComfyUI 原生 Pixal3D 節點，搭配社群量化的 GGUF 權重（經 ComfyUI-GGUF 的 `UnetLoaderGGUF` 載入），單張圖片輸出 1024 解析度重網格化的 GLB。
提供 `workflow_api.json`（API 實跑驗證）與 `workflow.json`（GUI 工作流，與 API 版等價，經轉換比對與實跑驗證；**瀏覽器 GUI 載入尚待使用者確認**）。

## 狀態摘要（詳見 validation.md）

- WORKFLOW (API)：PASS（2026-11-22，`workflow_api.json` 實跑 1 次）
- WORKFLOW (GUI)：PASS — 離線結構檢查、UI->API 轉換與 `workflow_api.json` 完全一致、轉換後 API 實跑、前端首頁與工作流清單皆 PASS；瀏覽器實際載入並執行為**使用者回報 PASS（2026-11-22，未由 Agent 驗證，無截圖留存）**
- 獨立 Reviewer 已於 2026-11-22 核對（判定 PASS WITH CONDITIONS，條件為更新本文件狀態，已完成）；未重跑 selftest 與 check_workflow，未目視 GLB
- OUTPUT QUALITY：NOT VERIFIED（需使用者指定評定者目視）
- PRINT READINESS：NOT VERIFIED（網格非流形邊 1051、25 個連通體、未封閉；無切片器、未切片、未試印）

## 環境（驗證時）

- ComfyUI 0.43.0（commit b5a4321），Python 3.12.15，torch 2.14.1+cu130，RTX 3060 12GB
- ComfyUI-GGUF 1.1.10（pyproject 版本，無 .git，複製自正式 custom_nodes）
- 使用原生節點：Pixal3DConditioning、Trellis2ShapeStage、Trellis2UpsampleStage、MoGe、RemoveBackground、RemeshMesh 等

## 權重來源

HF repo：`Aero-Ex/Pixal3D-GGUF`（下載時間 2026-11-22；授權請以該 repo 與 Pixal3D 上游為準）

| 檔案（repo 內路徑） | 合併前綴 | sha256 |
| --- | --- | --- |
| `Sparse/ss_flow_img_dit_1_3B_64_bf16_Q4_K_M.gguf` | `model.structure_model.` | 8227521189ef5a24f01afdf49b7673cfd06a50a592cd8943f620a963d9f1757c |
| `shape/slat_flow_img2shape_dit_1_3B_1024_bf16_Q4_K_M.gguf` | `model.img2shape.` | ba25d6d25568c9ae61c93fefea75d68a4235a9b6b7c34ec92064cb9edb002ae1 |
| `shape/slat_flow_img2shape_dit_1_3B_512_bf16_Q4_K_M.gguf` | `model.img2shape_512.` | 45affe7302ae4e6ff124c8bea9b21cfe8340b75dea5610a7f1369b302ffbd389 |
| `texture/slat_flow_imgshape2tex_dit_1_3B_1024_bf16_Q4_K_M.gguf` | `model.shape2txt.` | 5c7ef624c7f4a4874c0e4c775afa11907cad4084f133bf4218f4c558729fee1b |

合併結果 `pixal3d_Q4_K_M.gguf`（3,110,040,608 bytes，2800 張量）sha256：
`9a17ba696cc8df727c66657ce40daf65247cc05534b74c429455f68ba767f592`

shape2txt（紋理階段）本工作流不使用，但 ComfyUI 的模型偵測依賴 `shape2txt` 鍵，故一併合併。

## 為何要合併

原生 Pixal3D 為單一檔案（鍵為 `model.{structure_model,img2shape,img2shape_512,shape2txt}.*`）；GGUF repo 為每階段一檔且鍵無前綴，
無法直接給原生偵測使用。`merge_pixal3d_gguf.py` 加前綴合併、丟棄多餘的 `rope_phases` buffer、同步改名 `comfy.gguf.orig_shape.*`，
量化位元組原樣複製（不反量化）。

## 操作步驟

1. 安裝 ComfyUI-GGUF（1.1.10）到 `custom_nodes/`，確認環境有 `gguf` Python 套件。
2. 下載上表四個 GGUF（`curl -L -C -`），核對 sha256。
3. 合併：
   ```bash
   python merge_pixal3d_gguf.py --selftest
   python merge_pixal3d_gguf.py --out <models>/diffusion_models/pixal3d_Q4_K_M.gguf \
     --part structure_model=<ss_flow...Q4_K_M.gguf> --part img2shape=<slat_flow_img2shape...1024...gguf> \
     --part img2shape_512=<slat_flow_img2shape...512...gguf> --part shape2txt=<slat_flow_imgshape2tex...gguf>
   ```
4. 其他模型（與原生 int8 POC 相同）：`vae/trellis_2_shape_vae_bf16.safetensors`、`clip_vision/dino_v3_L_naf_fp32.safetensors`、
   `geometry_estimation/moge_2_vitl_normal_fp16.safetensors`、`background_removal/birefnet.safetensors`。
5. 輸入圖放 `input/dragon_front.png`（或改 `workflow_api.json` 節點 1），以 `POST /prompt` 提交 `workflow_api.json`
   （`{"prompt": <檔案內容>}`），由 `/history/<prompt_id>` 確認完成，輸出在 `output/3d/`。
6. GUI：把 `workflow.json` 放進 ComfyUI 的 `user/default/workflows/` 或在畫面拖入；Note 節點有簡述。
   重新產生：`python build_workflow_json.py --object-info <GET /object_info 存檔> --out workflow.json`；
   離線檢查與轉換比對：`python check_workflow.py --workflow workflow.json --object-info <同上> --api workflow_api.json`。
7. 網格檢查：`python mesh_check.py <glb> [輸出.stl]`（需 trimesh）。

## 限制

- 僅 Q4_K_M 實測；Q8_0 未測；`trellis2` 相關 validated=false（未納入本驗證）。
- `workflow.json` 由 `build_workflow_json.py` 依 `/object_info` 產生；RemeshMesh 的 `sign_mode`（DynamicCombo）子欄位在 widgets_values 的排列依前端 1.57.6 慣例推定，使用者 GUI 載入回報 PASS，但未留存 sign_mode 數值證據；如重新載入請核對 sign_mode=solid、fill=20、qef=false。
- 同 seed 重跑結果不位元組相同（GPU 非確定性；GLB sha256 不同，面數差約 80）。
- 網格未封閉、有非流形邊與多個連通體，不可直接視為可列印；人物／造型品質未經評定。
- 輸出 GLB 約 120 MB、STL 約 340 MB，不入庫。
