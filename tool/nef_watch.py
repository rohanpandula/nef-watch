#!/usr/bin/env python3
"""
nef-watch — watch a folder for Nikon NEF files and convert each to a TIFF with
Nikon's native in-camera rendering, via the Nikon Image SDK.

Render core: the compiled `nef_render` helper (next to this script). Each NEF is
~6 s of (lightly-threaded) SDK development, so conversions run in a small worker
pool to use the otherwise-idle cores. TIFF encode, watching, and routing live here.

Examples:
  # watch ~/Incoming, write TIFFs to ~/Exports (Ctrl-C to stop)
  ./nef_watch.py ~/Incoming --out ~/Exports

  # one-shot: convert everything already in a folder, then exit
  ./nef_watch.py ~/Shoot --out ~/Shoot/tiff --once -j 6
"""
import argparse
import concurrent.futures as cf
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

HERE = Path(__file__).resolve().parent
DEFAULT_RENDER_BIN = HERE / "nef_render"
DEFAULT_SDK = Path("/Users/rohan/Downloads/nx-tiffexport/Image SDK/Library/Mac")
DEFAULT_PROFILE = DEFAULT_SDK / "Profiles" / "NKsRGB.icm"


def log(msg):
    print(msg, flush=True)


def read_nkraw(path):
    with open(path, "rb") as f:
        header = bytearray()
        while not header.endswith(b"\n"):
            b = f.read(1)
            if not b:
                raise ValueError("truncated raw (no header)")
            header += b
        parts = header.decode("ascii").split()
        if parts[0] != "NKRAW1":
            raise ValueError(f"bad raw magic: {parts[:1]}")
        w, h, ch, depth, orient = (int(x) for x in parts[1:6])
        data = f.read()
    if len(data) != w * h * ch * depth:
        raise ValueError(f"raw payload {len(data)} != {w*h*ch*depth}")
    dt = "<u2" if depth == 2 else "u1"
    arr = np.frombuffer(data, dtype=dt).reshape(h, w, ch)
    # GetImageData returns display-oriented pixels (validated in spike 001), so the
    # array is already correctly oriented — no rotation from `orient` needed.
    return arr, dict(w=w, h=h, ch=ch, depth=depth, orient=orient)


def encode_tiff(arr, out_path, icc_bytes):
    """8-bit -> Pillow (validated format); 16-bit -> tifffile. Both LZW + ICC."""
    if arr.dtype == np.uint8:
        Image.fromarray(arr).save(
            out_path, format="TIFF", compression="tiff_lzw", icc_profile=icc_bytes
        )
    else:  # uint16
        import tifffile
        extratags = []
        if icc_bytes:
            extratags = [(34675, 7, len(icc_bytes), icc_bytes, True)]  # ICCProfile
        tifffile.imwrite(
            out_path, arr, photometric="rgb", compression="lzw", extratags=extratags
        )


def render_tiff(nef, out_path, args, icc):
    """SDK develop -> 8/16-bit LZW Nikon-sRGB TIFF (atomic). This is the path that
    bakes in Nikon's in-camera look."""
    with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as tf:
        raw = tf.name
    try:
        proc = subprocess.run(
            [str(args.render_bin), str(nef), raw, str(args.profile), str(args.bits)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
            raise RuntimeError(f"render exit {proc.returncode}: {tail[0]}")
        arr, _ = read_nkraw(raw)
        # temp file + atomic rename, so a partial TIFF never appears at the final path
        tmp_out = out_path.with_name(out_path.name + ".partial")
        encode_tiff(arr, tmp_out, icc)
        os.replace(tmp_out, out_path)
    finally:
        try:
            os.unlink(raw)
        except OSError:
            pass


def render_dng(nef, out_path, args):
    """Raw transcode NEF -> DNG via dnglab (default) or Adobe DNG Converter (atomic).
    A DNG preserves the raw sensor data — it does NOT bake in the Nikon look."""
    tmp = out_path.with_name(out_path.stem + ".partial.dng")
    if tmp.exists():
        tmp.unlink()
    if args.dng_engine == "dnglab":
        cmd = [str(args.dng_bin), "convert", "-c", "lossless",
               "--embed-raw", "true" if args.dng_embed_original else "false",
               str(nef), str(tmp)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not tmp.exists():
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["failed"]
            raise RuntimeError(f"dnglab: {tail[0]}")
    else:  # adobe — drives the app's CLI; not verified on this machine
        with tempfile.TemporaryDirectory() as td:
            cmd = [str(args.dng_bin), "-c"]
            if args.dng_embed_original:
                cmd += ["-e"]
            cmd += ["-d", td, "-o", tmp.name, str(nef)]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            produced = Path(td) / tmp.name
            if proc.returncode != 0 or not produced.exists():
                raise RuntimeError(f"Adobe DNG Converter failed (rc={proc.returncode})")
            os.replace(produced, tmp)
    os.replace(tmp, out_path)


def convert_one(nef, out_dir, args, icc):
    """Produce the requested output(s) for one NEF. Returns (status, detail) with
    status in {ok,skip,error}. Safe to run concurrently."""
    wanted = []
    if args.format in ("tiff", "both"):
        wanted.append(("tif", out_dir / (nef.stem + ".tif")))
    if args.format in ("dng", "both"):
        wanted.append(("dng", out_dir / (nef.stem + ".dng")))
    todo = [(kind, p) for kind, p in wanted if args.overwrite or not p.exists()]
    if not todo:
        return "skip", "exists"
    t0 = time.time()
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    try:
        for kind, outp in todo:
            if kind == "tif":
                render_tiff(nef, outp, args, icc)
            else:
                render_dng(nef, outp, args)
            made.append("." + kind)
    except RuntimeError as e:
        return "error", str(e)
    return "ok", f"{'+'.join(made)}  ({time.time()-t0:.1f}s)"


def find_nefs(folder, recursive):
    globber = folder.rglob if recursive else folder.glob
    seen = {}
    for p in globber("*"):
        if p.is_file() and p.suffix.lower() == ".nef":
            seen[p] = p
    return sorted(seen)


def _submit(ex, nef, args, out_dir, icc):
    return ex.submit(convert_one, nef, out_dir, args, icc)


def run_once(args, out_dir, icc):
    nefs = find_nefs(args.input, args.recursive)
    if not nefs:
        log(f"no NEFs found in {args.input}")
        return 0
    log(f"converting {len(nefs)} NEF(s) -> {out_dir}  ({args.jobs} parallel)")
    total = len(nefs)
    ok = skip = err = 0
    done = 0
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = {_submit(ex, nef, args, out_dir, icc): nef for nef in nefs}
        for fut in cf.as_completed(futs):
            nef = futs[fut]
            done += 1
            try:
                status, detail = fut.result()
            except Exception as e:
                status, detail = "error", str(e)
            if status == "ok":
                ok += 1;   log(f"  [{done}/{total}] {nef.name} -> {detail}")
            elif status == "skip":
                skip += 1; log(f"  [{done}/{total}] {nef.name} -> skip (exists)")
            else:
                err += 1;  log(f"  [{done}/{total}] {nef.name} -> ERROR: {detail}")
    log(f"done: {ok} converted, {skip} skipped, {err} errors")
    return 1 if err else 0


def run_watch(args, out_dir, icc):
    log(f"watching {args.input}  ->  {out_dir}   "
        f"(every {args.interval}s, {args.jobs} parallel, Ctrl-C to stop)")
    sizes = {}          # path -> last-seen size, for copy-completion stability
    processed = set()
    inflight = {}       # future -> nef
    n = 0
    ex = cf.ThreadPoolExecutor(max_workers=args.jobs)
    try:
        while True:
            for nef in find_nefs(args.input, args.recursive):
                if nef in processed or nef in inflight.values():
                    continue
                out_path = out_dir / (nef.stem + ".tif")
                if out_path.exists() and not args.overwrite:
                    processed.add(nef)
                    continue
                try:
                    size = nef.stat().st_size
                except OSError:
                    continue
                # submit only once the file size has settled (done copying)
                if sizes.get(nef) == size and size > 0:
                    inflight[_submit(ex, nef, args, out_dir, icc)] = nef
                sizes[nef] = size
            for fut in [f for f in inflight if f.done()]:
                nef = inflight.pop(fut)
                processed.add(nef)
                n += 1
                try:
                    status, detail = fut.result()
                except Exception as e:
                    status, detail = "error", str(e)
                if status == "ok":
                    log(f"  [{n}] {nef.name} -> {detail}")
                elif status == "error":
                    log(f"  [{n}] {nef.name} -> ERROR: {detail}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("\nstopping, finishing in-flight conversions…")
    finally:
        ex.shutdown(wait=True)
    log(f"stopped. {len(processed)} file(s) handled this session.")
    return 0


def resolve_dng_engine(args):
    """Return the path to the chosen DNG transcoder, or exit with install hint."""
    if args.dng_engine == "dnglab":
        p = shutil.which("dnglab")
        if not p:
            sys.exit("dnglab not found — install: brew install dnglab   (or --dng-engine adobe)")
        return p
    cand = Path("/Applications/Adobe DNG Converter.app/Contents/MacOS/Adobe DNG Converter")
    if not cand.exists():
        sys.exit("Adobe DNG Converter not found — install: "
                 "brew install --cask adobe-dng-converter   (or --dng-engine dnglab)")
    return str(cand)


def main():
    ap = argparse.ArgumentParser(
        prog="nef-watch",
        description="Watch a folder for Nikon NEFs and convert them to TIFF (Nikon look, via "
                    "the Image SDK) and/or DNG (raw transcode, no baked look).",
    )
    ap.add_argument("input", type=Path, help="folder to watch / scan for .NEF files")
    ap.add_argument("--out", "-o", type=Path, required=True, help="output folder")
    ap.add_argument("--format", choices=("tiff", "dng", "both"), default="tiff",
                    help="output format (default tiff). dng is a raw transcode — no Nikon look")
    ap.add_argument("--once", action="store_true", help="convert existing NEFs once, then exit (default: keep watching)")
    ap.add_argument("--jobs", "-j", type=int, default=4, help="parallel workers (default 4)")
    ap.add_argument("--bits", type=int, choices=(8, 16), default=8, help="TIFF bit depth (default 8)")
    ap.add_argument("--dng-engine", choices=("dnglab", "adobe"), default="dnglab",
                    help="DNG backend (default dnglab; adobe needs the app installed)")
    ap.add_argument("--dng-embed-original", action="store_true",
                    help="embed the original NEF inside the DNG (much larger files)")
    ap.add_argument("--recursive", "-r", action="store_true", help="scan subfolders too")
    ap.add_argument("--overwrite", action="store_true", help="re-convert even if the output already exists")
    ap.add_argument("--interval", type=float, default=3.0, help="watch poll interval in seconds (default 3)")
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="output ICC profile (default: Nikon sRGB)")
    ap.add_argument("--render-bin", type=Path, default=DEFAULT_RENDER_BIN, help="path to the nef_render helper")
    args = ap.parse_args()

    if not args.input.is_dir():
        sys.exit(f"input folder not found: {args.input}")
    if args.jobs < 1:
        args.jobs = 1

    # Validate only what the chosen format needs.
    icc = b""
    if args.format in ("tiff", "both"):
        if not args.render_bin.exists():
            sys.exit(f"render helper not found: {args.render_bin}\n  build it: bash {HERE/'build.sh'}")
        if not args.profile.exists():
            sys.exit(f"ICC profile not found: {args.profile}  (set --profile)")
        icc = args.profile.read_bytes()
    args.dng_bin = resolve_dng_engine(args) if args.format in ("dng", "both") else None

    if args.once:
        return run_once(args, out_dir=args.out, icc=icc)
    return run_watch(args, out_dir=args.out, icc=icc)


if __name__ == "__main__":
    sys.exit(main())
