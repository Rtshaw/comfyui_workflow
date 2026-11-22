# Pixal3D（GGUF Q8_0）圖生 3D 網格工作流

與 `../pixal3d_gguf/`（Q4_K_M）同一流程、同一輸入與參數（seed 56 / 42 / 42），只把 `UnetLoaderGGUF` 的模型換成合併後的 `pixal3d_Q8_0.gguf`，
輸出前綴改為 `3d/pixal3d_gguf_Q8_0`。節點、參數、腳本（`merge_pixal3d_gguf.py`、`build_workflow_json.py`、`check_workflow.py`、`mesh_check.py`）皆沿用 Q4 目錄，不在此重複。

## 狀態摘要（詳見 validation.md）

- WORKFLOW (API)：PASS（2026-11-22，在正式服務 8188 實跑 1 次，history success，190 s，未 OOM）
- WORKFLOW (GUI)：僅離線結構檢查 PASS；`workflow.json` 未在瀏覽器載入（NOT VERIFIED）
- OUTPUT QUALITY：NOT VERIFIED（未目視、未產生預覽圖，需使用者評定）
- PRINT READINESS：NOT VERIFIED（未封閉、非流形邊 2051、29 個連通體；未切片、未試印）
- 審查類型：Implementer 自我檢查；尚無獨立 Reviewer

## 重點對照（同機同條件，8188，先 /free、佇列空）

| 項目 | Q4_K_M（重跑） | Q8_0 |
| --- | --- | --- |
| 耗時 | 174 s | 190 s |
| 整卡 VRAM 峰值 | 9519 MiB | 10191 MiB（RTX 3060 共 12288 MiB） |
| 面數 | 6,762,408 | 7,025,680 |
| 非流形邊 | 1055 | 2051 |
| 連通體 | 27 | 29 |
| GLB 大小 | 121,721,836 B | 126,461,112 B |

Q8_0 比 Q4_K_M 多約 0.7 GB 顯存峰值、約 16 s，在 12GB 卡上可跑完，餘裕約 2 GB。網格指標並未較佳（非流形邊較多）；品質差異需使用者目視評定。

## 權重來源

HF repo：`Aero-Ex/Pixal3D-GGUF`（下載 2026-11-22；授權以該 repo 與 Pixal3D 上游為準）

| 檔案（repo 內路徑） | 合併前綴 | bytes | sha256 |
| --- | --- | --- | --- |
| `Sparse/ss_flow_img_dit_1_3B_64_bf16_Q8_0.gguf` | `model.structure_model.` | 1427084032 | 6551ab0381262db6b2a6a2e351f18f7462583d2b064ca9d0b89e4410ef73f16d |
| `shape/slat_flow_img2shape_dit_1_3B_1024_bf16_Q8_0.gguf` | `model.img2shape.` | 1476761184 | 7ce2303bb03fcbb8ec435d76be34dcbe21f1b3d12a673a818855342b6d5652c8 |
| `shape/slat_flow_img2shape_dit_1_3B_512_bf16_Q8_0.gguf` | `model.img2shape_512.` | 1476761184 | 651dc94f65bf8af0ddd1e6c5c266c496f1cd61c03f965acaaf53d92bc6f4912f |
| `texture/slat_flow_imgshape2tex_dit_1_3B_1024_bf16_Q8_0.gguf` | `model.shape2txt.` | 1476813408 | c827e6f5e4b6078434d8c2d054e37424706de1e5587830d5943121cd4ce2dcd8 |

合併結果 `pixal3d_Q8_0.gguf`：5,856,949,280 bytes、2800 張量（F32 1340 / Q8_0 980 / F16 480），sha256 `bc2dd6494be4d2fd6943194e8330ccc5849147244b02c2f423117ef051146acb`。

## 操作步驟

與 Q4 相同（見 `../pixal3d_gguf/README.md`），差別：

```bash
python merge_pixal3d_gguf.py --selftest
python merge_pixal3d_gguf.py --out <models>/diffusion_models/pixal3d_Q8_0.gguf \
  --part structure_model=<ss_flow...Q8_0.gguf> --part img2shape=<slat_flow_img2shape...1024...Q8_0.gguf> \
  --part img2shape_512=<slat_flow_img2shape...512...Q8_0.gguf> --part shape2txt=<slat_flow_imgshape2tex...Q8_0.gguf>
```

其餘模型、輸入圖 `input/dragon_front.png`、`POST /prompt` 提交 `workflow_api.json` 皆同 Q4。

## 限制

- 單次實跑；GPU 非確定性，同 seed 重跑面數與網格指標會有小幅差異（Q4 重跑即與先前 run 不同）。
- VRAM 峰值為整卡（nvidia-smi）在正式服務 8188 上量到，含該服務本身的常駐占用，與 Q4 目錄在隔離實例 8190 量到的 6871 MiB 不可直接比較。
- 網格未封閉，有非流形邊與多連通體，不可視為可列印；造型品質未評定。
- 輸出 GLB 約 126 MB 不入庫。
