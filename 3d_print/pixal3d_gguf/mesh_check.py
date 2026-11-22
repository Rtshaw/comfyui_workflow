# 網格基礎檢查（trimesh）：面數、封閉性、非流形邊、孔洞邊、連通體、體積、尺寸；並可輸出 STL
import sys, json, trimesh, numpy as np, hashlib
src=sys.argv[1]; out=sys.argv[2] if len(sys.argv)>2 else None
m=trimesh.load(src, force="mesh", process=False)
m.merge_vertices()
e=m.edges_sorted; u,c=np.unique(e,axis=0,return_counts=True)
r={"faces":int(len(m.faces)),"verts":int(len(m.vertices)),
 "watertight":bool(m.is_watertight),"winding_consistent":bool(m.is_winding_consistent),
 "boundary_edges":int((c==1).sum()),"nonmanifold_edges":int((c>2).sum()),
 "euler":int(m.euler_number),"bodies":len(m.split(only_watertight=False)),
 "bbox_extents":[round(float(x),4) for x in m.extents],
 "volume":round(float(m.volume),6) if m.is_watertight else None}
if out: m.export(out); r["stl"]=out
print(json.dumps(r))
