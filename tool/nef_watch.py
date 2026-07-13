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
# /// script
# requires-python = ">=3.9"
# dependencies = ["pillow", "numpy", "tifffile", "imagecodecs"]
# ///
import argparse
import concurrent.futures as cf
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

HERE = Path(__file__).resolve().parent
DEFAULT_RENDER_BIN = HERE / "nef_render"
STAGED_PROFILE = HERE / "Contents" / "Resources" / "NKsRGB.icm"
DEFAULT_PROFILE = STAGED_PROFILE
RAW_SUFFIXES = {".nef", ".nrw"}
FORMAT_ORDER = ("tiff", "jpeg", "dng")
RASTER_FORMATS = {"tiff", "jpeg"}
EXTS = {"tiff": ".tif", "jpeg": ".jpg", "dng": ".dng"}
MAX_RENDER_PIXELS = 100_000_000
MAX_RENDER_BYTES = 800_000_000
MAX_NKRAW_HEADER = 256
LOG_FILE = None
LOG_LOCK = threading.Lock()


def log(msg):
    stamp = time.strftime("[%H:%M:%S] ")
    lines = str(msg).splitlines() or [""]
    with LOG_LOCK:
        for line in lines:
            out = stamp + line
            print(out, flush=True)
            if LOG_FILE:
                LOG_FILE.write(out + "\n")
                LOG_FILE.flush()


def configure_logging(path):
    global LOG_FILE
    if path:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            LOG_FILE = open(path, "a", encoding="utf-8")
        except OSError as e:
            sys.exit(f"cannot open log file {path}: {e}")


def is_raw_file(path):
    return path.suffix.lower() in RAW_SUFFIXES


def parse_formats(value):
    out = set()
    for part in value.split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name == "both":
            out.update(("tiff", "dng"))
        elif name in FORMAT_ORDER:
            out.add(name)
        else:
            raise argparse.ArgumentTypeError(
                "format must be a comma-separated set of tiff,jpeg,dng (or both)"
            )
    if not out:
        raise argparse.ArgumentTypeError("at least one output format is required")
    return frozenset(out)


def parse_quality(value):
    try:
        quality = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("quality must be an integer")
    if not 1 <= quality <= 100:
        raise argparse.ArgumentTypeError("quality must be between 1 and 100")
    return quality


def parse_exp_comp(value):
    try:
        ev = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("exp-comp must be a number")
    if not -5.0 <= ev <= 5.0:  # also rejects nan/inf before they reach the SDK
        raise argparse.ArgumentTypeError("exp-comp must be between -5 and 5 EV")
    return ev


def needs_raster(args):
    return bool(args.formats & RASTER_FORMATS)


def read_nkraw(path):
    with open(path, "rb") as f:
        header = f.readline(MAX_NKRAW_HEADER + 1)
        if not header.endswith(b"\n") or len(header) > MAX_NKRAW_HEADER:
            raise ValueError("invalid or oversized NKRAW1 header")
        try:
            parts = header.decode("ascii").split()
        except UnicodeDecodeError as e:
            raise ValueError("NKRAW1 header is not ASCII") from e
        if len(parts) != 6 or parts[0] != "NKRAW1":
            raise ValueError(f"bad raw magic: {parts[:1]}")
        try:
            w, h, ch, depth, orient = (int(x) for x in parts[1:6])
        except ValueError as e:
            raise ValueError("NKRAW1 header contains a non-integer field") from e
        if w <= 0 or h <= 0 or w * h > MAX_RENDER_PIXELS:
            raise ValueError(f"unsafe NKRAW1 dimensions: {w}x{h}")
        if ch not in (1, 3, 4) or depth not in (1, 2):
            raise ValueError(f"unsupported NKRAW1 format: channels={ch} depth={depth}")
        expected = w * h * ch * depth
        if expected > MAX_RENDER_BYTES:
            raise ValueError(f"NKRAW1 payload exceeds safety limit: {expected} bytes")
        data = f.read(expected + 1)
    if len(data) != expected:
        raise ValueError(f"raw payload {len(data)} != {expected}")
    dt = "<u2" if depth == 2 else "u1"
    arr = np.frombuffer(data, dtype=dt).reshape(h, w, ch)
    # GetImageData returns display-oriented pixels (validated in spike 001), so the
    # array is already correctly oriented — no rotation from `orient` needed.
    return arr, dict(w=w, h=h, ch=ch, depth=depth, orient=orient)


def unique_partial(out_path):
    return out_path.with_name(f"{out_path.stem}.{uuid.uuid4().hex}.partial{out_path.suffix}")


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


def jpeg_pixels(arr):
    if arr.dtype == np.uint8:
        return arr
    return ((arr.astype(np.uint32) * 255 + 32767) // 65535).astype(np.uint8)


def encode_jpeg(arr, out_path, icc_bytes, quality):
    Image.fromarray(jpeg_pixels(arr)).save(
        out_path, format="JPEG", quality=quality, icc_profile=icc_bytes
    )


def copy_exif(nef, tmp_out, args):
    if not args.exiftool:
        return
    proc = subprocess.run(
        [
            str(args.exiftool),
            "-q",
            "-overwrite_original",
            "-tagsFromFile",
            str(nef),
            "-EXIF:all",
            "-makernotes",
            "-Orientation#=1",
            "-ExifImageWidth=",
            "-ExifImageHeight=",
            str(tmp_out),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["failed"]
        raise RuntimeError(f"exiftool: {tail[0]}")


def warn_if_bad_magic(nef):
    try:
        with open(nef, "rb") as f:
            magic = f.read(4)
    except OSError:
        return
    if magic not in (b"II*\0", b"MM\0*"):
        log(f"warning: {nef.name} does not look like a NEF/NRW (JPEG?) — attempting anyway")


def render_raster(nef, todo, args, icc):
    """SDK develop -> TIFF/JPEG (atomic). TIFF/JPEG share one SDK develop."""
    with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as tf:
        raw = tf.name
    tmps = []
    try:
        develop_bits = args.bits if "tiff" in args.formats else 8
        proc = subprocess.run(
            [
                str(args.render_bin),
                str(nef),
                raw,
                str(args.profile),
                str(develop_bits),
                str(args.exp_comp),
            ],
            capture_output=True,
            text=True,
            env=args.render_env,  # None => inherit; set for --deterministic
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
            raise RuntimeError(f"render exit {proc.returncode}: {tail[0]}")
        arr, _ = read_nkraw(raw)
        for kind, out_path in todo:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_out = unique_partial(out_path)
            tmps.append(tmp_out)
            if kind == "tiff":
                encode_tiff(arr, tmp_out, icc)
            else:
                encode_jpeg(arr, tmp_out, icc, args.quality)
            try:
                copy_exif(nef, tmp_out, args)
            except RuntimeError as e:
                # a failed metadata copy shouldn't void a good render
                log(f"warning: EXIF copy failed for {nef.name}: {e} — writing without EXIF")
            os.replace(tmp_out, out_path)
    finally:
        try:
            os.unlink(raw)
        except OSError:
            pass
        for tmp_out in tmps:
            try:
                tmp_out.unlink()
            except OSError:
                pass


def render_dng(nef, out_path, args):
    """Raw transcode NEF -> DNG via dnglab (default) or Adobe DNG Converter (atomic).
    A DNG preserves the raw sensor data — it does NOT bake in the Nikon look."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = unique_partial(out_path)
    try:
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
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def expected_outputs(nef, args, out_dir):
    parent = out_dir
    if args.recursive and not args.input_is_file:
        try:
            rel = nef.relative_to(args.input_root)
            parent = out_dir / rel.parent
        except ValueError:
            parent = out_dir
    return [(kind, parent / (nef.stem + EXTS[kind]))
            for kind in FORMAT_ORDER if kind in args.formats]


def convert_one(nef, out_dir, args, icc, force=False):
    """Produce the requested output(s) for one NEF. Returns (status, detail) with
    status in {ok,skip,error}. Safe to run concurrently."""
    wanted = expected_outputs(nef, args, out_dir)
    todo = [(kind, p) for kind, p in wanted if force or args.overwrite or not p.exists()]
    if not todo:
        return "skip", "exists"
    t0 = time.time()
    made = []
    try:
        warn_if_bad_magic(nef)
        raster_todo = [(kind, p) for kind, p in todo if kind in RASTER_FORMATS]
        if raster_todo:
            input_bytes = nef.stat().st_size
            if input_bytes > args.max_input_bytes:
                raise RuntimeError(
                    f"input is {input_bytes / (1024 * 1024):.1f} MiB; "
                    f"limit is {args.max_input_mib} MiB (--max-input-mib to override)"
                )
            render_raster(nef, raster_todo, args, icc)
            made.extend(p.suffix for _, p in raster_todo)
        for kind, outp in todo:
            if kind == "dng":
                render_dng(nef, outp, args)
                made.append(outp.suffix)
    except RuntimeError as e:
        return "error", str(e)
    return "ok", f"{'+'.join(made)}  ({time.time()-t0:.1f}s)"


def find_nefs(path, recursive):
    if path.is_file():
        return [path] if is_raw_file(path) else []
    folder = path
    globber = folder.rglob if recursive else folder.glob
    seen = {}
    for p in globber("*"):
        if p.is_file() and is_raw_file(p):
            seen[p] = p
    return sorted(seen)


def _submit(ex, nef, args, out_dir, icc, force=False):
    return ex.submit(convert_one, nef, out_dir, args, icc, force)


def run_once(args, out_dir, icc):
    nefs = find_nefs(args.input, args.recursive)
    if not nefs:
        log(f"no NEF/NRW files found in {args.input}")
        return 0
    log(f"converting {len(nefs)} NEF/NRW file(s) -> {out_dir}  ({args.jobs} parallel)")
    total = len(nefs)
    ok = skip = err = 0
    done = 0
    handled = set()
    futs = {}

    def collect(fut):
        nonlocal ok, skip, err, done
        nef = futs[fut]
        handled.add(fut)
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

    ex = cf.ThreadPoolExecutor(max_workers=args.jobs)
    try:
        for nef in nefs:
            futs[_submit(ex, nef, args, out_dir, icc)] = nef
        for fut in cf.as_completed(futs):
            collect(fut)
    except KeyboardInterrupt:
        log("interrupted — finishing in-flight file(s); queued files cancelled")
        try:
            ex.shutdown(wait=True, cancel_futures=True)
        except KeyboardInterrupt:
            log("second interrupt — exiting immediately (outputs stay atomic)")
            os._exit(130)
        for fut in futs:
            if fut not in handled and fut.done() and not fut.cancelled():
                collect(fut)
        not_started = sum(1 for fut in futs if fut.cancelled())
        log(f"partial: {ok} converted, {skip} skipped, {err} errors, {not_started} not-started")
        return 130
    finally:
        try:
            ex.shutdown(wait=True, cancel_futures=True)
        except KeyboardInterrupt:
            log("second interrupt — exiting immediately (outputs stay atomic)")
            os._exit(130)
    log(f"done: {ok} converted, {skip} skipped, {err} errors")
    return 1 if err else 0


def stat_key(path):
    st = path.stat()
    return st.st_size, st.st_mtime_ns


def outputs_complete(nef, args, out_dir):
    return all(path.exists() for _, path in expected_outputs(nef, args, out_dir))


def processed_source_state(processed_key, current_key, complete):
    """Classify an already-seen source without conflating missing outputs with edits."""
    if processed_key is None:
        return "new"
    if processed_key != current_key:
        return "source-changed"
    return "complete" if complete else "output-missing"


def run_watch(args, out_dir, icc):
    log(f"watching {args.input}  ->  {out_dir}   "
        f"(every {args.interval}s, {args.jobs} parallel, Ctrl-C to stop)")
    stable_keys = {}    # path -> last-seen (size, mtime_ns), for upload stability
    processed = {}      # path -> (size, mtime_ns) that produced current outputs
    force = set()       # changed sources whose existing outputs must be replaced
    failures = {}       # path -> attempts/size/mtime/next_retry_at/error
    inflight = {}       # future -> (nef, submitted stat key, forced overwrite)
    n = 0
    unavailable = False
    ex = cf.ThreadPoolExecutor(max_workers=args.jobs)
    try:
        while True:
            if not args.input.exists() or not args.input.is_dir():
                if not unavailable:
                    log(f"warning: watched folder unavailable: {args.input} — waiting")
                    unavailable = True
                time.sleep(args.interval)
                continue
            if unavailable:
                log(f"watched folder available again: {args.input}")
                unavailable = False
            try:
                nefs = find_nefs(args.input, args.recursive)
            except OSError:
                if not unavailable:
                    log(f"warning: watched folder unavailable: {args.input} — waiting")
                    unavailable = True
                time.sleep(args.interval)
                continue

            now = time.time()
            for nef in nefs:
                if nef in {job[0] for job in inflight.values()}:
                    continue
                try:
                    key = stat_key(nef)
                except OSError:
                    continue
                size, mtime = key

                source_state = processed_source_state(
                    processed.get(nef), key, outputs_complete(nef, args, out_dir)
                )
                if source_state != "new":
                    if source_state == "complete":
                        continue
                    processed.pop(nef, None)
                    failures.pop(nef, None)
                    stable_keys[nef] = key
                    if source_state == "source-changed":
                        force.add(nef)
                        log(
                            f"source changed after conversion: {nef.name} — "
                            "waiting for it to settle"
                        )
                    else:
                        log(
                            f"output missing for unchanged source: {nef.name} — "
                            "regenerating only missing format(s)"
                        )
                    continue

                if (not args.overwrite and nef not in force
                        and outputs_complete(nef, args, out_dir)):
                    processed[nef] = key
                    failures.pop(nef, None)
                    continue

                state = failures.get(nef)
                if state:
                    if (size, mtime) != (state["size"], state["mtime"]):
                        failures.pop(nef, None)
                        stable_keys[nef] = key
                        log(f"retry 1/3 {nef.name} (file changed)")
                        continue
                    if state["attempts"] >= 3:
                        continue
                    if now < state["next_retry_at"]:
                        continue
                    log(f"retry {state['attempts'] + 1}/3 {nef.name}")

                # Submit only once both size and mtime have settled. Some upload
                # clients preallocate the final size while still writing.
                if stable_keys.get(nef) == key and size > 0:
                    forced = nef in force
                    future = _submit(ex, nef, args, out_dir, icc, force=forced)
                    inflight[future] = (nef, key, forced)
                stable_keys[nef] = key
            for fut in [f for f in inflight if f.done()]:
                nef, submitted_key, forced = inflight.pop(fut)
                n += 1
                try:
                    status, detail = fut.result()
                except Exception as e:
                    status, detail = "error", str(e)
                if status == "ok":
                    try:
                        current_key = stat_key(nef)
                    except OSError:
                        current_key = None
                    if current_key != submitted_key:
                        processed.pop(nef, None)
                        failures.pop(nef, None)
                        force.add(nef)
                        if current_key is not None:
                            stable_keys[nef] = current_key
                        log(f"  [{n}] {nef.name} -> source changed during conversion; rerender queued")
                    else:
                        processed[nef] = submitted_key
                        failures.pop(nef, None)
                        force.discard(nef)
                        log(f"  [{n}] {nef.name} -> {detail}")
                elif status == "skip":
                    processed[nef] = submitted_key
                    failures.pop(nef, None)
                    if forced:
                        force.discard(nef)
                    log(f"  [{n}] {nef.name} -> skip (exists)")
                elif status == "error":
                    try:
                        size, mtime = stat_key(nef)
                    except OSError:
                        size, mtime = 0, 0
                    prev = failures.get(nef)
                    same = prev and (size, mtime) == (prev["size"], prev["mtime"])
                    attempts = prev["attempts"] + 1 if same else 1
                    failures[nef] = {
                        "attempts": attempts,
                        "size": size,
                        "mtime": mtime,
                        "next_retry_at": time.time() + args.interval * (2 ** attempts),
                        "error": detail,
                    }
                    log(f"  [{n}] {nef.name} -> ERROR: {detail}")
                    if attempts >= 3:
                        log(f"giving up after 3 failures for {nef.name}: {detail} (will retry if file changes)")
            # prune bookkeeping for files that vanished from the watched folder,
            # so week-long sessions don't accumulate state for every file ever seen
            present = set(nefs)
            active = {job[0] for job in inflight.values()}
            for d in (stable_keys, failures, processed):
                for p in [p for p in d if p not in present and p not in active]:
                    del d[p]
            force &= present | active
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("stopping, finishing in-flight conversions…")
    finally:
        try:
            ex.shutdown(wait=True)
        except KeyboardInterrupt:
            log("second interrupt — exiting immediately (outputs stay atomic)")
            os._exit(130)
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
    ap.add_argument("input", type=Path, help="folder to watch/scan, or a single .NEF/.NRW file")
    ap.add_argument("--out", "-o", type=Path, required=True, help="output folder")
    ap.add_argument("--format", dest="formats", type=parse_formats, default=parse_formats("tiff"),
                    metavar="FORMATS", help="comma-separated output formats: tiff,jpeg,dng; both=tiff,dng")
    ap.add_argument("--quality", type=parse_quality, default=90, help="JPEG quality 1-100 (default 90)")
    ap.add_argument("--once", action="store_true", help="convert existing NEFs once, then exit (default: keep watching)")
    ap.add_argument("--jobs", "-j", type=int, default=4, help="parallel workers (default 4)")
    ap.add_argument("--bits", type=int, choices=(8, 16), default=8, help="TIFF bit depth (default 8)")
    ap.add_argument("--exp-comp", type=parse_exp_comp, default=0.0,
                    help="exposure compensation in EV for TIFF/JPEG, -5..5 (default 0.0)")
    ap.add_argument("--deterministic", action="store_true",
                    help="byte-reproducible TIFF/JPEG: pin the SDK's rand()-seeded dither "
                         "(native macOS renderer only; same look, stable bytes)")
    ap.add_argument("--dng-engine", choices=("dnglab", "adobe"), default="dnglab",
                    help="DNG backend (default dnglab; adobe needs the app installed)")
    ap.add_argument("--dng-embed-original", action="store_true",
                    help="embed the original NEF inside the DNG (much larger files)")
    ap.add_argument("--recursive", "-r", action="store_true", help="scan subfolders too")
    ap.add_argument("--overwrite", action="store_true", help="re-convert even if the output already exists")
    ap.add_argument("--interval", type=float, default=3.0, help="watch poll interval in seconds (default 3)")
    ap.add_argument("--max-input-mib", type=int, default=512,
                    help="reject a NEF/NRW larger than this before invoking the SDK (default 512)")
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="output ICC profile (default: Nikon sRGB)")
    ap.add_argument("--log-file", type=Path, help="also append timestamped log lines to this file")
    ap.add_argument("--render-bin", type=Path, default=DEFAULT_RENDER_BIN, help="path to the nef_render helper")
    args = ap.parse_args()

    if args.log_file:
        args.log_file = args.log_file.expanduser()
    configure_logging(args.log_file)

    args.input = args.input.expanduser()
    args.out = args.out.expanduser()
    args.render_bin = args.render_bin.expanduser()
    args.profile = args.profile.expanduser()
    args.input_is_file = args.input.is_file()
    if args.input_is_file:
        if not is_raw_file(args.input):
            sys.exit(f"input file is not a NEF/NRW: {args.input}")
        args.once = True
        args.recursive = False
        args.input_root = args.input.parent
    elif args.input.is_dir():
        args.input_root = args.input
    else:
        sys.exit(f"input folder not found: {args.input}")
    if args.jobs < 1:
        args.jobs = 1
    if args.max_input_mib < 1:
        sys.exit("--max-input-mib must be at least 1")
    args.max_input_bytes = args.max_input_mib * 1024 * 1024

    # Validate only what the chosen format needs.
    icc = b""
    args.render_env = None
    if needs_raster(args):
        if not args.render_bin.exists():
            sys.exit(f"render helper not found: {args.render_bin}\n  build it: bash {HERE/'build.sh'}")
        if not args.profile.exists():
            sys.exit(f"ICC profile not found: {args.profile}\n  re-run: bash {HERE/'build.sh'}  (or set --profile)")
        icc = args.profile.read_bytes()
        args.exiftool = shutil.which("exiftool")
        if not args.exiftool:
            log("warning: outputs will carry no EXIF — brew install exiftool")
        if args.deterministic:
            if sys.platform != "darwin":
                sys.exit("--deterministic is only supported by the native macOS renderer; "
                         "the unmodified Windows SDK under Wine has no supported interposer")
            lib = args.render_bin.with_name("rand_freeze.dylib")
            if not lib.exists():
                sys.exit(f"--deterministic needs {lib}\n  re-run: bash {HERE/'build.sh'}")
            args.render_env = {**os.environ, "DYLD_INSERT_LIBRARIES": str(lib)}
    else:
        args.exiftool = None
        if args.deterministic:
            log("note: --deterministic only affects TIFF/JPEG; DNG is already reproducible")
    args.dng_bin = resolve_dng_engine(args) if "dng" in args.formats else None

    if args.once:
        return run_once(args, out_dir=args.out, icc=icc)
    return run_watch(args, out_dir=args.out, icc=icc)


if __name__ == "__main__":
    sys.exit(main())
