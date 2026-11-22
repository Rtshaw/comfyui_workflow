#!/usr/bin/env python3
"""把 Pixal3D 各階段的 GGUF 合併成一個 GGUF，讓 ComfyUI 原生 Pixal3D 節點可經 UnetLoaderGGUF 載入。

用途：原生模型是單一檔案，張量鍵為 model.{structure_model,img2shape,img2shape_512,shape2txt}.<name>；
      Aero-Ex/Pixal3D-GGUF 則每個階段一個 GGUF（張量名不含前綴）。本腳本加前綴後合併。
輸入：--part <前綴>=<gguf 路徑>（可重複），例如 --part structure_model=ss_flow_....gguf
輸出：--out 單一 GGUF（架構沿用來源的 general.architecture，量化張量位元組原樣複製，不反量化）
處理：
  * 丟棄 rope_phases（原生模型不含此 buffer，由程式即時計算）
  * comfy.gguf.orig_shape.<name> 中繼資料同步加上相同前綴
  * 各來源架構不一致、張量名重複、前綴缺漏時直接報錯
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


def _arch(reader):
    f = reader.get_field("general.architecture")
    return str(bytes(f.parts[f.data[-1]]), "utf-8")


def merge(parts, out_path):
    """parts: [(prefix, gguf_path)]；回傳寫入的張量數。"""
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
    """合成兩個小 GGUF（Q8_0 + F32 + rope_phases），合併後驗證名稱、位元組、orig_shape。"""
    d = tempfile.mkdtemp()
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
    out = os.path.join(d, "m.gguf")
    assert merge(list(srcs.items()), out) == 4
    r = GGUFReader(out)
    names = {t.name: t for t in r.tensors}
    assert set(names) == {f"model.{p}.{k}" for p in "ab" for k in ("blk.w", "blk.b")}, names.keys()
    src = {t.name: t for t in GGUFReader(srcs["a"]).tensors}
    t = names["model.a.blk.w"]
    assert t.tensor_type == gguf.GGMLQuantizationType.Q8_0 and bytes(t.data) == bytes(src["blk.w"].data)
    f = r.get_field(ORIG_SHAPE + "model.a.blk.w")
    assert [int(f.parts[i][0]) for i in f.data] == [4, 64] and f.types[1] == gguf.GGUFValueType.INT32
    assert _arch(r) == "flux"
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
