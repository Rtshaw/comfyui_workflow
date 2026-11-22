#!/usr/bin/env python3
"""把 Pixal3D 各階段的 GGUF 合併成一個 GGUF，讓 ComfyUI 原生 Pixal3D 節點可經 UnetLoaderGGUF 載入。

用途：原生模型是單一檔案，張量鍵為 model.{structure_model,img2shape,img2shape_512,shape2txt}.<name>；
      Aero-Ex/Pixal3D-GGUF 則每個階段一個 GGUF（張量名不含前綴）。本腳本加前綴後合併。
輸入：--part <前綴>=<gguf 路徑>（可重複），例如 --part structure_model=ss_flow_....gguf
輸出：--out 單一 GGUF（架構沿用來源的 general.architecture，量化張量位元組原樣複製，不反量化）
處理：
  * 丟棄 rope_phases（原生模型不含此 buffer，由程式即時計算）
  * comfy.gguf.orig_shape.<name> 中繼資料同步加上相同前綴
  * 各來源架構不一致、張量名重複、前綴重複／缺少必要階段時直接報錯（輸出為確定性：同輸入重跑位元組相同）
自我測試：python merge_pixal3d_gguf.py --selftest（離線，使用合成的小型 GGUF）
需求：gguf、numpy（正式 venv 已內建）
"""
import argparse
import os
import sys
import tempfile

import numpy as np
import gguf
from gguf import GGUFReader, GGUFWriter

DROP_SUFFIXES = ("rope_phases",)  # 原生模型不需要的額外 buffer
ORIG_SHAPE = "comfy.gguf.orig_shape."
# 原生 Pixal3D 偵測所需的四個階段前綴（shape2txt 本工作流不用，但偵測依賴它）
STAGES = ("structure_model", "img2shape", "img2shape_512", "shape2txt")


def _arch(reader):
    f = reader.get_field("general.architecture")
    return str(bytes(f.parts[f.data[-1]]), "utf-8")


def merge(parts, out_path, required=STAGES):
    """parts: [(prefix, gguf_path)]；required: 必須齊全的前綴（None 不檢查）；回傳寫入的張量數。"""
    prefixes = [p for p, _ in parts]
    dup = {p for p in prefixes if prefixes.count(p) > 1}
    if dup:
        raise ValueError(f"階段前綴重複: {sorted(dup)}")
    if required is not None and set(required) != set(prefixes):
        raise ValueError(f"階段不符: 缺少 {sorted(set(required) - set(prefixes))}, 多出 {sorted(set(prefixes) - set(required))}")
    readers = [(p, GGUFReader(path)) for p, path in parts]
    archs = {_arch(r) for _, r in readers}
    if len(archs) != 1:
        raise ValueError(f"來源 GGUF 架構不一致: {archs}")
    writer = GGUFWriter(out_path, arch=archs.pop())
    seen, n = set(), 0
    for prefix, r in readers:
        for t in r.tensors:
            if t.name.split(".")[-1] in DROP_SUFFIXES:
                continue
            name = f"model.{prefix}.{t.name}"
            if name in seen:
                raise ValueError(f"張量名重複: {name}")
            seen.add(name)
            # raw_dtype 讓 writer 以原量化型別直接寫入位元組（量化張量 data 為 uint8 位元組矩陣）
            writer.add_tensor(name, t.data, raw_dtype=t.tensor_type)
            f = r.get_field(ORIG_SHAPE + t.name)
            if f is not None:  # 與 ComfyUI-GGUF 讀法一致：INT32 陣列
                writer.add_array(ORIG_SHAPE + name, [int(f.parts[i][0]) for i in f.data])
            n += 1
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_tensors_to_file()
    writer.close()
    return n


def selftest():
    """合成兩個小 GGUF（Q8_0 + F32 + rope_phases），驗證改名、丟棄、KV 改名、位元組一致、對齊、確定性與錯誤情境。"""
    with tempfile.TemporaryDirectory() as d:
        srcs = {}
        for prefix in ("a", "b"):
            path = os.path.join(d, prefix + ".gguf")
            w = GGUFWriter(path, arch="flux")
            q = gguf.quants.quantize(np.random.rand(4, 64).astype(np.float32), gguf.GGMLQuantizationType.Q8_0)
            w.add_tensor("blk.w", q, raw_dtype=gguf.GGMLQuantizationType.Q8_0)
            w.add_array(ORIG_SHAPE + "blk.w", [4, 64])
            w.add_tensor("blk.b", np.arange(8, dtype=np.float32))
            w.add_tensor("rope_phases", np.zeros(4, dtype=np.float32))
            w.write_header_to_file(); w.write_kv_data_to_file(); w.write_tensors_to_file(); w.close()
            srcs[prefix] = path
        parts, out = list(srcs.items()), os.path.join(d, "m.gguf")
        assert merge(parts, out, required=("a", "b")) == 4
        r = GGUFReader(out)
        names = {t.name: t for t in r.tensors}
        # 丟棄規則：rope_phases 不得出現；其餘加前綴
        assert not any(n.endswith("rope_phases") for n in names), names.keys()
        assert set(names) == {f"model.{p}.{k}" for p in "ab" for k in ("blk.w", "blk.b")}, names.keys()
        for p in "ab":
            src = {t.name: t for t in GGUFReader(srcs[p]).tensors}
            for k in ("blk.w", "blk.b"):
                t, s0 = names[f"model.{p}.{k}"], src[k]
                # 量化型別、形狀與原始位元組皆原樣保留
                assert t.tensor_type == s0.tensor_type and list(t.shape) == list(s0.shape)
                assert bytes(t.data) == bytes(s0.data)
            f = r.get_field(ORIG_SHAPE + f"model.{p}.blk.w")  # KV 改名；與 ComfyUI-GGUF 一致為 INT32 陣列
            assert [int(f.parts[i][0]) for i in f.data] == [4, 64] and f.types[1] == gguf.GGUFValueType.INT32
            assert r.get_field(ORIG_SHAPE + "blk.w") is None  # 舊名不殘留
        assert all(t.data_offset % 32 == 0 for t in r.tensors)  # 預設對齊 32
        assert _arch(r) == "flux"
        # 確定性：同輸入再合併一次，檔案位元組相同
        out2 = os.path.join(d, "m2.gguf")
        merge(parts, out2, required=("a", "b"))
        assert open(out, "rb").read() == open(out2, "rb").read()
        # 錯誤情境：缺階段、前綴重複、架構不一致（張量名重複為前綴重複之後的第二道防線）
        for bad, why in ((parts[:1], "缺階段"), (parts + parts[:1], "前綴重複")):
            try:
                merge(bad, out2, required=("a", "b")); raise SystemExit(f"應報錯: {why}")
            except ValueError:
                pass
        try:
            merge([("a", srcs["a"]), ("a2", srcs["a"])], out2, required=None)  # 不同前綴不會撞名，應成功
        except ValueError:
            raise SystemExit("不同前綴不應報錯")
        other = os.path.join(d, "o.gguf")
        w = GGUFWriter(other, arch="llama"); w.add_tensor("x", np.zeros(4, dtype=np.float32))
        w.write_header_to_file(); w.write_kv_data_to_file(); w.write_tensors_to_file(); w.close()
        try:
            merge([("a", srcs["a"]), ("o", other)], out2, required=None); raise SystemExit("應報錯: 架構不一致")
        except ValueError:
            pass
    print("selftest OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", action="append", default=[], help="<前綴>=<gguf 路徑>")
    ap.add_argument("--out")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    else:
        if not a.part or not a.out:
            sys.exit("需要 --part 與 --out")
        pairs = [tuple(s.split("=", 1)) for s in a.part]
        print("merged tensors:", merge(pairs, a.out))
