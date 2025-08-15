from __future__ import annotations
import sys, os
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parents[1]))  # 加專案根到 sys.path

from services.conversion_service import precise_convert, write_outputs

def log(msg: str):
    print(msg, flush=True)

def main():
    if len(sys.argv) < 3:
        print("用法: python -m tools.cli_convert <input.dxf> <out_dir> [src_epsg] [tgt_epsg]", file=sys.stderr)
        sys.exit(2)
    dxf = sys.argv[1]
    outdir = sys.argv[2]
    src = int(sys.argv[3]) if len(sys.argv)>3 else 3826
    tgt = int(sys.argv[4]) if len(sys.argv)>4 else None

    log(f"[cli] 轉換開始 dxf={dxf} out={outdir} src={src} tgt={tgt}")
    buckets = precise_convert([dxf], source_epsg=src, target_epsg=tgt, on_progress=log)
    written = write_outputs(buckets, out_path=outdir, driver="ESRI Shapefile", overwrite=True, on_progress=log)
    if not written:
        log("[cli] 沒有寫出任何檔案")
    else:
        for w in written:
            log(f"[cli] 寫出：{w['layer']} ({w['count']}) -> {w['path']}")
    log("[cli] 完成")

if __name__ == "__main__":
    main()