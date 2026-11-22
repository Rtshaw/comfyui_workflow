#!/usr/bin/env python3
"""由 workflow_api.json + 實例 GET /object_info 產生 ComfyUI UI 格式的 workflow.json。

輸入：--api workflow_api.json、--object-info object_info.json（從測試實例 GET /object_info 存檔）
輸出：--out workflow.json（LiteGraph 格式：nodes / links / groups / last_node_id / last_link_id）
規則：widget 順序與輸入/輸出槽位完全取自 object_info；seed 的 control_after_generate 一律 "fixed"。
"""
import argparse
import json

WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO", "COLOR", "COMFY_DYNAMICCOMBO_V3"}

# 版面：節點 id -> (群組, 欄, 列)；群組 -> (標題, 顏色, 起始 y)
LAYOUT = {
    1: ("pre", 0, 0), 2: ("pre", 0, 1), 3: ("pre", 1, 0), 4: ("pre", 2, 0),
    5: ("cam", 0, 0), 6: ("cam", 1, 0), 7: ("cam", 2, 0), 8: ("cam", 0, 1), 9: ("cam", 3, 0),
    10: ("mdl", 0, 0), 11: ("mdl", 1, 0), 14: ("mdl", 1, 1), 12: ("mdl", 2, 0), 15: ("mdl", 2, 1), 13: ("mdl", 3, 0),
    16: ("smp", 0, 1), 18: ("smp", 0, 0), 17: ("smp", 1, 0), 19: ("smp", 2, 0), 20: ("smp", 3, 0),
    21: ("smp", 4, 0), 22: ("smp", 5, 0), 23: ("smp", 6, 0),
    24: ("msh", 0, 0), 25: ("msh", 1, 0), 26: ("msh", 2, 0), 27: ("msh", 3, 0), 28: ("msh", 4, 0), 29: ("msh", 5, 0),
}
GROUPS = {"pre": ("1 前處理（去背 / 裁切）", 0), "cam": ("2 相機 FoV / 條件", 520),
          "mdl": ("3 GGUF 模型與 CFG 分段", 1000), "smp": ("4 三階段取樣（結構 / 形狀 / 升採樣）", 1500),
          "msh": ("5 網格後處理與輸出", 2100)}
TITLES = {17: "KSampler（結構 seed 56）", 21: "KSampler（形狀 512 階段）", 23: "KSampler（形狀 1024 階段）",
          11: "CFGOverride（結構後段）", 14: "CFGOverride（形狀）", 12: "RescaleCFG 0.7", 15: "RescaleCFG 0.5"}
NOTE = ("Pixal3D GGUF Q4_K_M（原生 Pixal3D 節點 + ComfyUI-GGUF UnetLoaderGGUF）。\n"
        "模型 pixal3d_Q4_K_M.gguf 需先以 merge_pixal3d_gguf.py 合併四階段 GGUF；輸入圖放 input/。\n"
        "三個 KSampler seed 皆為 fixed（56 / 42 / 42）。輸出 GLB 在 output/3d/。詳見 README.md。")


def widget_specs(info):
    """依 object_info 順序回傳 [(名稱, 規格)]（widget 輸入）與 [(名稱, 型別)]（連線槽）。"""
    widgets, slots = [], []
    for sect in ("required", "optional"):
        for name, spec in info["input"].get(sect, {}).items():
            t = spec[0]
            (widgets if isinstance(t, list) or t in WIDGET_TYPES else slots).append((name, spec))
    return widgets, slots


def widget_values(info, ins):
    """產生 widgets_values：含 control_after_generate、LoadImage 上傳鈕、DynamicCombo 子欄位。"""
    vals = []
    for name, spec in widget_specs(info)[0]:
        opts = spec[1] if len(spec) > 1 else {}
        # 已連線的 widget 輸入：widgets_values 放預設值（實際值由連線提供）
        vals.append(opts.get("default") if isinstance(ins[name], list) else ins[name])
        if opts.get("control_after_generate"):
            vals.append("fixed")
        if opts.get("image_upload"):
            vals.append("image")
        if spec[0] == "COMFY_DYNAMICCOMBO_V3":  # 子欄位 key 為 "<名稱>.<子名稱>"，順序依所選 option 定義
            sel = next(o for o in opts["options"] if o["key"] == ins[name])
            for sub in sel["inputs"]["required"]:
                vals.append(ins[f"{name}.{sub}"])
    return vals


def build(api, oi):
    nodes, links, link_id = [], [], 0
    pos, outs = {}, {}  # 節點 id -> [x, y]；(節點 id, 槽) -> 連線 id 清單
    for nid in sorted(api, key=int):
        g, c, r = LAYOUT[int(nid)]
        pos[nid] = [40 + c * 400, 120 + GROUPS[g][1] + r * 300]
    for order, nid in enumerate(sorted(api, key=int)):
        n = api[nid]; info = oi[n["class_type"]]
        wdg, slots = widget_specs(info)
        inputs = []
        for name, spec in slots + [w for w in wdg if isinstance(n["inputs"][w[0]], list)]:
            v = n["inputs"].get(name)
            ent = {"name": name, "type": spec[0] if isinstance(spec[0], str) else "COMBO", "link": None}
            if (name, spec) in wdg:
                ent["widget"] = {"name": name}
            if isinstance(v, list):
                link_id += 1
                src, sslot = v
                stype = oi[api[src]["class_type"]]["output"][sslot]
                links.append([link_id, int(src), sslot, int(nid), None, stype])
                ent["link"] = link_id
                outs.setdefault((src, sslot), []).append(link_id)
            inputs.append(ent)
        # 記錄每條 link 的目標槽位（依 inputs 清單索引）
        for idx, ent in enumerate(inputs):
            if ent["link"]:
                next(l for l in links if l[0] == ent["link"])[4] = idx
        nodes.append({"id": int(nid), "type": n["class_type"], "pos": pos[nid],
                      "size": [330, 60 + 26 * (len(inputs) + len(widget_values(info, n["inputs"])))],
                      "flags": {}, "order": order, "mode": 0, "inputs": inputs, "outputs": [],
                      "properties": {"Node name for S&R": n["class_type"]},
                      "widgets_values": widget_values(info, n["inputs"])})
        if int(nid) in TITLES:
            nodes[-1]["title"] = TITLES[int(nid)]
        nodes[-1]["_out"] = (nid, info)
    for node in nodes:
        nid, info = node.pop("_out")
        node["outputs"] = [{"name": nm, "type": t, "links": outs.get((nid, i)) or None, "slot_index": i}
                           for i, (nm, t) in enumerate(zip(info["output_name"], info["output"]))]
    # 群組外框：涵蓋成員節點
    groups = []
    for gi, (g, (title, _)) in enumerate(GROUPS.items(), 1):
        mem = [nd for nd in nodes if LAYOUT[nd["id"]][0] == g]
        x0 = min(m["pos"][0] for m in mem) - 20; y0 = min(m["pos"][1] for m in mem) - 70
        x1 = max(m["pos"][0] + m["size"][0] for m in mem) + 20; y1 = max(m["pos"][1] + m["size"][1] for m in mem) + 20
        groups.append({"id": gi, "title": title, "bounding": [x0, y0, x1 - x0, y1 - y0], "color": "#3f789e", "flags": {}})
    last_id = max(n["id"] for n in nodes) + 1
    nodes.append({"id": last_id, "type": "Note", "title": "說明", "pos": [40, -140], "size": [520, 120], "flags": {},
                  "order": len(nodes), "mode": 0, "inputs": [], "outputs": [], "properties": {}, "widgets_values": [NOTE]})
    return {"id": "pixal3d-gguf-q4km", "revision": 0, "last_node_id": last_id, "last_link_id": link_id,
            "nodes": nodes, "links": links, "groups": groups, "config": {}, "extra": {"ds": {"scale": 0.5, "offset": [0, 200]}},
            "version": 0.4}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="workflow_api.json")
    ap.add_argument("--object-info", required=True)
    ap.add_argument("--out", default="workflow.json")
    a = ap.parse_args()
    wf = build(json.load(open(a.api)), json.load(open(a.object_info)))
    json.dump(wf, open(a.out, "w"), ensure_ascii=False, indent=1)
    print("nodes", len(wf["nodes"]), "links", len(wf["links"]))
