#!/usr/bin/env python3
"""離線檢查 workflow.json（UI 格式）並轉成 API 格式，與 workflow_api.json 比對。

用法：python check_workflow.py --workflow workflow.json --object-info object_info.json \
        --api workflow_api.json [--out-api converted_api.json]
檢查：節點型別存在、widget 數量、links 兩端一致與型別相容、last_*_id；轉換規則同前端：
      widget 依 object_info 順序取 widgets_values（跳過 control_after_generate / 上傳鈕），連線輸入 -> [來源id, 槽]。
結束碼 0 = 全部通過；任何錯誤會列出並以 1 結束。僅用標準函式庫。
"""
import argparse
import json
import sys

WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO", "COLOR", "COMFY_DYNAMICCOMBO_V3"}
FRONTEND_ONLY = {"Note", "MarkdownNote", "Reroute", "PrimitiveNode"}  # 不進 API 的前端節點


def widget_list(info):
    """object_info 中依序的 widget 輸入 [(名稱, 規格)]。"""
    out = []
    for sect in ("required", "optional"):
        for name, spec in info["input"].get(sect, {}).items():
            if isinstance(spec[0], list) or spec[0] in WIDGET_TYPES:
                out.append((name, spec))
    return out


def consume(info, vals, errs, nid):
    """依序消耗 widgets_values，回傳 {輸入名: 值}。"""
    res, i = {}, 0
    try:
        for name, spec in widget_list(info):
            res[name] = vals[i]; i += 1
            opts = spec[1] if len(spec) > 1 else {}
            if opts.get("control_after_generate"):
                if vals[i] != "fixed":
                    errs.append(f"node {nid}: {name} control_after_generate={vals[i]!r} 不是 fixed")
                i += 1
            if opts.get("image_upload"):
                i += 1
            if spec[0] == "COMFY_DYNAMICCOMBO_V3":
                sel = next(o for o in opts["options"] if o["key"] == res[name])
                for sub in sel["inputs"]["required"]:
                    res[f"{name}.{sub}"] = vals[i]; i += 1
    except (IndexError, StopIteration):
        errs.append(f"node {nid}: widgets_values 太短（{len(vals)}）")
    if i != len(vals):
        errs.append(f"node {nid}: widgets_values 數量 {len(vals)} != 預期 {i}")
    return res


def check_and_convert(wf, oi):
    errs, api = [], {}
    nodes = {n["id"]: n for n in wf["nodes"]}
    links = {l[0]: l for l in wf["links"]}
    if wf["last_node_id"] < max(nodes) or wf["last_link_id"] < max(links):
        errs.append("last_node_id / last_link_id 小於實際最大 id")
    for l in wf["links"]:
        lid, s, ss, d, ds, t = l
        if s not in nodes or d not in nodes:
            errs.append(f"link {lid}: 端點節點不存在"); continue
        so, di = nodes[s]["outputs"][ss], nodes[d]["inputs"][ds]
        if lid not in (so.get("links") or []): errs.append(f"link {lid}: 來源 outputs.links 缺")
        if di.get("link") != lid: errs.append(f"link {lid}: 目標 inputs.link 不符")
        if so["type"] != t: errs.append(f"link {lid}: 型別 {t} != 來源輸出 {so['type']}")
        if t not in di["type"].split(",") and di["type"] != "*": errs.append(f"link {lid}: 型別 {t} 不符目標 {di['type']}")
    for nid, n in nodes.items():
        if n["type"] in FRONTEND_ONLY:
            continue
        info = oi.get(n["type"])
        if info is None:
            errs.append(f"node {nid}: 型別 {n['type']} 不在 object_info"); continue
        if [o["type"] for o in n["outputs"]] != info["output"]:
            errs.append(f"node {nid}: outputs 與 object_info 不符")
        for o in n["outputs"]:
            for lid in o.get("links") or []:
                if lid not in links or links[lid][1] != nid: errs.append(f"node {nid}: outputs.links {lid} 無對應 link")
        ins = consume(info, n["widgets_values"], errs, nid)
        known = {k for sect in info["input"].values() for k in sect}
        for i in n["inputs"]:
            if i["name"] not in known: errs.append(f"node {nid}: 輸入 {i['name']} 不在 object_info")
            if i.get("link") is not None:
                if i["link"] not in links: errs.append(f"node {nid}: input link {i['link']} 不存在"); continue
                ins[i["name"]] = [str(links[i["link"]][1]), links[i["link"]][2]]
        api[str(nid)] = {"class_type": n["type"], "inputs": ins}
    return errs, api


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", required=True)
    ap.add_argument("--object-info", required=True)
    ap.add_argument("--api", required=True)
    ap.add_argument("--out-api")
    a = ap.parse_args()
    errs, api = check_and_convert(json.load(open(a.workflow, encoding="utf-8")), json.load(open(a.object_info)))
    ref = json.load(open(a.api))
    if api != ref:  # 逐節點比對 class_type 與 inputs（dict 比較不計順序）
        for k in sorted(set(api) | set(ref), key=int):
            if api.get(k) != ref.get(k): errs.append(f"API 差異 node {k}: {api.get(k)} != {ref.get(k)}")
    if a.out_api:
        json.dump(api, open(a.out_api, "w"), indent=1)
    print("\n".join(errs) or "OK: 結構檢查通過，轉換結果與 workflow_api.json 相同")
    sys.exit(1 if errs else 0)
