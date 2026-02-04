#!/usr/bin/env python3
"""
svg_pixel_rect_optimizer.py

Optimize "pixel art as SVG rects" by:
- normalizing style/opacity
- merging adjacent same-color pixels:
  - horizontal runs
  - + optional vertical stacking

Important behavior:
- Handles input rects with width/height > 1 by expanding them into covered pixels per-row.
  This prevents "breaking" images when re-optimizing already-merged SVGs.

CLI usage:
  python svg_pixel_rect_optimizer.py input.svg
  python svg_pixel_rect_optimizer.py input.svg -o output.svg
  python svg_pixel_rect_optimizer.py input.svg --no-vertical
  python svg_pixel_rect_optimizer.py input.svg --svgz
  python svg_pixel_rect_optimizer.py input.svg --svgz-only

Batch:
  python svg_pixel_rect_optimizer.py *.svg
"""

from __future__ import annotations

import argparse
import gzip
from collections import defaultdict
from pathlib import Path
import xml.etree.ElementTree as ET

SVG_NS = "http://www.w3.org/2000/svg"
NS = {"svg": SVG_NS}


def parse_style(style: str | None) -> dict[str, str]:
    d: dict[str, str] = {}
    if not style:
        return d
    for part in style.split(";"):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            k, v = part.split(":", 1)
            d[k.strip()] = v.strip()
    return d


def norm_opacity(op_str: str | None) -> float:
    """Opacity in SVG is 0..1. Some exporters write 0..255; normalize."""
    if not op_str:
        return 1.0
    try:
        op = float(op_str)
    except ValueError:
        return 1.0
    if op > 1.0:
        op = op / 255.0
    if op < 0.0:
        op = 0.0
    if op > 1.0:
        op = 1.0
    return op


def fmt_opacity(op: float) -> str:
    s = f"{op:.4f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _as_int(attr: str | None, default: int) -> int:
    if attr is None or attr == "":
        return default
    try:
        return int(float(attr))
    except Exception:
        return default


def _build_rect_list(svg_in: Path, vertical_merge: bool = True) -> tuple[list[tuple[int, int, int, int, tuple[str, float]]], dict[str, str]]:
    """
    Returns: (rect_list_sorted, out_root_attrs)

    rect_list_sorted items: (x, y, w, h, (fill, opacity))
    out_root_attrs: subset of attrs to copy to new root.
    """
    tree = ET.parse(str(svg_in))
    root = tree.getroot()

    rects = root.findall(".//svg:rect", NS)
    if not rects:
        raise ValueError("No <rect> elements found. This script expects pixel-rect SVGs.")

    rows: dict[int, dict[tuple[str, float], list[int]]] = defaultdict(lambda: defaultdict(list))

    # Expand each input rect into covered pixels (per row), so width/height>1 inputs are handled.
    for r in rects:
        x0 = _as_int(r.get("x"), 0)
        y0 = _as_int(r.get("y"), 0)
        w0 = _as_int(r.get("width"), 1)
        h0 = _as_int(r.get("height"), 1)
        if w0 <= 0:
            w0 = 1
        if h0 <= 0:
            h0 = 1

        st = parse_style(r.get("style"))
        fill = st.get("fill", r.get("fill", "#000000"))
        op = round(norm_opacity(st.get("opacity", r.get("opacity"))), 6)

        for yy in range(y0, y0 + h0):
            xs = rows[yy][(fill, op)]
            for xx in range(x0, x0 + w0):
                xs.append(xx)

    # 1) Horizontal merge runs -> list of (x,y,w,h,stylekey) with h=1
    merged_h: list[tuple[int, int, int, int, tuple[str, float]]] = []
    for y, style_map in rows.items():
        for stylekey, xs in style_map.items():
            if not xs:
                continue
            xs = sorted(xs)
            start = prev = xs[0]
            for x in xs[1:]:
                if x == prev + 1:
                    prev = x
                else:
                    merged_h.append((start, y, prev - start + 1, 1, stylekey))
                    start = prev = x
            merged_h.append((start, y, prev - start + 1, 1, stylekey))

    # 2) Optional vertical merge (stack identical x+width+style across y)
    if vertical_merge:
        cols: dict[tuple[int, int, tuple[str, float]], list[int]] = defaultdict(list)
        for x, y, w, _h, stylekey in merged_h:
            cols[(x, w, stylekey)].append(y)

        rect_list: list[tuple[int, int, int, int, tuple[str, float]]] = []
        for (x, w, stylekey), ys in cols.items():
            ys = sorted(ys)
            start = prev = ys[0]
            for y in ys[1:]:
                if y == prev + 1:
                    prev = y
                else:
                    rect_list.append((x, start, w, prev - start + 1, stylekey))
                    start = prev = y
            rect_list.append((x, start, w, prev - start + 1, stylekey))
    else:
        rect_list = merged_h

    out_attrs: dict[str, str] = {}
    for k in ("width", "height", "viewBox", "preserveAspectRatio"):
        v = root.get(k)
        if v:
            out_attrs[k] = v
    out_attrs["shape-rendering"] = "crispEdges"

    rect_list_sorted = sorted(rect_list, key=lambda t: (t[1], t[0], t[2], t[3]))
    return rect_list_sorted, out_attrs


def optimize_svg_rects_bytes(svg_in: Path, vertical_merge: bool = True) -> tuple[bytes, int]:
    """Return optimized SVG bytes and rect count, without writing to disk."""
    rect_list_sorted, out_attrs = _build_rect_list(svg_in, vertical_merge=vertical_merge)

    ET.register_namespace("", SVG_NS)
    new_root = ET.Element(f"{{{SVG_NS}}}svg", out_attrs)
    g = ET.SubElement(new_root, f"{{{SVG_NS}}}g")

    for x, y, w, h, (fill, op) in rect_list_sorted:
        r_attrs = {"x": str(x), "y": str(y), "width": str(w), "height": str(h), "fill": fill}
        if abs(op - 1.0) > 1e-6:
            r_attrs["opacity"] = fmt_opacity(op)
        ET.SubElement(g, f"{{{SVG_NS}}}rect", r_attrs)

    data = ET.tostring(new_root, encoding="utf-8", xml_declaration=True)
    return data, len(rect_list_sorted)


def optimize_svg_rects(svg_in: Path, svg_out: Path, vertical_merge: bool = True) -> tuple[int, int]:
    """Write optimized SVG to disk. Returns: (rect_count_out, bytes_out)."""
    data, rect_count = optimize_svg_rects_bytes(svg_in, vertical_merge=vertical_merge)
    svg_out.parent.mkdir(parents=True, exist_ok=True)
    svg_out.write_bytes(data)
    return rect_count, len(data)


def write_svgz(svg_bytes: bytes, svgz_out: Path, compresslevel: int = 9) -> int:
    """Write gzipped SVG (.svgz). Returns bytes written."""
    svgz_out.parent.mkdir(parents=True, exist_ok=True)
    level = max(1, min(9, int(compresslevel)))
    with gzip.open(svgz_out, "wb", compresslevel=level) as f:
        f.write(svg_bytes)
    return svgz_out.stat().st_size


def default_output_path(inp: Path, vertical_merge: bool) -> Path:
    suffix = "_optimized_hv" if vertical_merge else "_optimized_h"
    return inp.with_name(inp.stem + suffix + inp.suffix)


def main() -> None:
    ap = argparse.ArgumentParser(description="Merge/optimize pixel-rect SVGs.")
    ap.add_argument("input", nargs="+", help="Input SVG file(s). Supports wildcards via shell.")
    ap.add_argument("-o", "--output", help="Output path (only valid with a single input).")
    ap.add_argument("--no-vertical", action="store_true", help="Disable vertical stacking merge.")
    ap.add_argument("--svgz", action="store_true", help="Also write a .svgz (gzipped) alongside the .svg output.")
    ap.add_argument("--svgz-only", action="store_true", help="Write only .svgz output (implies --svgz).")
    ap.add_argument("--svgz-level", type=int, default=9, help="GZip level for .svgz (1-9, default 9).")
    args = ap.parse_args()

    vertical = not args.no_vertical
    inputs = [Path(p) for p in args.input]

    if args.output and len(inputs) != 1:
        raise SystemExit("Error: -o/--output can only be used with a single input file.")

    for inp in inputs:
        if not inp.exists():
            print(f"Skip (not found): {inp}")
            continue

        out_svg = Path(args.output) if args.output else default_output_path(inp, vertical)
        try:
            svg_bytes, rect_count = optimize_svg_rects_bytes(inp, vertical_merge=vertical)
            out_svg.parent.mkdir(parents=True, exist_ok=True)

            if not args.svgz_only:
                out_svg.write_bytes(svg_bytes)
                print(f"Wrote: {out_svg} | rects: {rect_count:,} | bytes: {len(svg_bytes):,}")

            if args.svgz or args.svgz_only:
                svgz_out = out_svg.with_suffix(out_svg.suffix + "z")
                bytes_svgz = write_svgz(svg_bytes, svgz_out, compresslevel=args.svgz_level)
                print(f"Wrote: {svgz_out} | bytes: {bytes_svgz:,}")

        except Exception as e:
            print(f"Failed: {inp} | {e}")


if __name__ == "__main__":
    main()
