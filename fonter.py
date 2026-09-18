#!/usr/bin/env python3
"""
fonter.py

Scans the current folder (and subfolders) for .zip files, pulls every font
file out of them (no matter how deeply nested inside the zip), copies them
into a flat fonts/ folder, and generates a single self-contained HTML page
to preview every font at once.

The HTML page itself lives in template.html (next to this script) and is
loaded + token-substituted at runtime, so you can edit the design/markup
there without touching any Python string escaping.

Usage:
    python3 fonter.py
    python3 fonter.py --output my-preview --no-recurse-dirs

Requires only the Python standard library.
"""

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

FONT_EXTS = {".ttf", ".otf", ".woff", ".woff2"}

FORMAT_MAP = {
    ".ttf": "truetype",
    ".otf": "opentype",
    ".woff": "woff",
    ".woff2": "woff2",
}

WEIGHT_TOKENS = [
    ("thin", 100),
    ("hairline", 100),
    ("extralight", 200),
    ("ultralight", 200),
    ("light", 300),
    ("regular", 400),
    ("normal", 400),
    ("book", 400),
    ("medium", 500),
    ("semibold", 600),
    ("demibold", 600),
    ("bold", 700),
    ("extrabold", 800),
    ("ultrabold", 800),
    ("heavy", 800),
    ("black", 900),
]

# Path to the external HTML template, next to this script.
TEMPLATE_PATH = Path(__file__).resolve().parent / "template.html"


def find_zip_files(root: Path, recurse: bool):
    if recurse:
        return sorted(p for p in root.rglob("*.zip") if p.is_file())
    return sorted(p for p in root.glob("*.zip") if p.is_file())


def clean_display_name(stem: str) -> str:
    name = re.sub(r"[_\-]+", " ", stem)
    name = re.sub(r"\s+", " ", name).strip()
    return name or stem


def guess_weight_style(stem: str):
    lower = stem.lower()
    weight = 400
    # check longest tokens first so "extrabold" wins over "bold"
    for token, value in sorted(WEIGHT_TOKENS, key=lambda t: -len(t[0])):
        if token in lower:
            weight = value
            break
    style = "italic" if ("italic" in lower or "oblique" in lower) else "normal"
    return weight, style


def generate_stable_id(name: str, weight: int, style: str) -> str:
    """Creates a stable slug based on font identity rather than sequential order."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name.lower()).strip("_")
    slug = slug or "font"
    return f"{slug}_{weight}_{style}"


def unique_dest_path(dest_dir: Path, filename: str) -> Path:
    dest = dest_dir / filename
    if not dest.exists():
        return dest
    stem, ext = Path(filename).stem, Path(filename).suffix
    n = 1
    while True:
        candidate = dest_dir / f"{stem}_{n}{ext}"
        if not candidate.exists():
            return candidate
        n += 1


def merge_duplicate_formats(entries):
    """Merge entries that are the same font shipped in multiple file formats
    (e.g. Roboto-Bold.otf + Roboto-Bold.ttf) into a single manifest item with
    a `formats` list, instead of showing duplicate preview cards."""
    groups = {}
    order = []
    for e in entries:
        key = (e["display_name"].lower(), e["weight"], e["style"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(e)

    merged = []
    for key in order:
        group = groups[key]
        primary = group[0]
        formats = [
            {
                "file": g["file"],
                "ext": g["ext"],
                "format": g["format"],
                "size_kb": g["size_kb"],
                "source_zip": g["source_zip"],
                "source_path": g["source_path"],
            }
            for g in group
        ]

        merged.append(
            {
                "id": primary["family"],  # Use the stable string as the JS identifier
                "family": primary["family"],
                "display_name": primary["display_name"],
                "weight": primary["weight"],
                "style": primary["style"],
                "source_zip": primary["source_zip"],
                "size_kb": primary["size_kb"],
                "formats": formats,
            }
        )
    return merged


def build_manifest_entry(
    dest_path: Path,
    orig_stem: str,
    ext: str,
    data_len: int,
    source_zip: str,
    source_path: str,
):
    display_name = clean_display_name(orig_stem)
    weight, style = guess_weight_style(orig_stem)
    stable_id = generate_stable_id(display_name, weight, style)

    return {
        "family": stable_id,
        "display_name": display_name,
        "file": dest_path.name,
        "ext": ext.lstrip("."),
        "format": FORMAT_MAP[ext],
        "weight": weight,
        "style": style,
        "source_zip": source_zip,
        "source_path": source_path,
        "size_kb": round(data_len / 1024, 1),
    }


def extract_fonts(zip_files, fonts_dir: Path):
    manifest = []
    errors = []

    for zpath in zip_files:
        try:
            with zipfile.ZipFile(zpath) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    inner_path = info.filename
                    if "__MACOSX" in inner_path or Path(inner_path).name.startswith(
                        "."
                    ):
                        continue
                    ext = Path(inner_path).suffix.lower()
                    if ext not in FONT_EXTS:
                        continue

                    try:
                        data = zf.read(info)
                    except Exception as e:
                        errors.append(
                            f"{zpath.name} -> {inner_path}: could not read ({e})"
                        )
                        continue

                    if not data:
                        errors.append(
                            f"{zpath.name} -> {inner_path}: empty file, skipped"
                        )
                        continue

                    orig_name = Path(inner_path).name
                    orig_stem = Path(orig_name).stem
                    dest_path = unique_dest_path(fonts_dir, orig_name)
                    dest_path.write_bytes(data)

                    manifest.append(
                        build_manifest_entry(
                            dest_path, orig_stem, ext, len(data), zpath.name, inner_path
                        )
                    )
        except zipfile.BadZipFile:
            errors.append(f"{zpath.name}: not a valid zip file, skipped")
        except Exception as e:
            errors.append(f"{zpath.name}: unexpected error ({e})")

    return manifest, errors


def find_loose_font_files(root: Path, recurse: bool, exclude_dir: Path):
    """Find font files sitting directly on disk (not inside a zip). Always
    excludes anything under exclude_dir (the tool's own output folder)."""
    pattern_iter = root.rglob("*") if recurse else root.glob("*")
    files = []
    for p in pattern_iter:
        if not p.is_file():
            continue
        if exclude_dir in p.parents:
            continue
        if p.suffix.lower() not in FONT_EXTS:
            continue
        if "__MACOSX" in p.parts or p.name.startswith("."):
            continue
        files.append(p)
    return sorted(files)


def extract_loose_fonts(files, fonts_dir: Path, cwd: Path):
    manifest = []
    errors = []

    for fpath in files:
        try:
            data = fpath.read_bytes()
        except Exception as e:
            errors.append(f"{fpath.name}: could not read ({e})")
            continue

        if not data:
            errors.append(f"{fpath.name}: empty file, skipped")
            continue

        ext = fpath.suffix.lower()
        orig_stem = fpath.stem
        dest_path = unique_dest_path(fonts_dir, fpath.name)
        dest_path.write_bytes(data)

        try:
            rel = str(fpath.relative_to(cwd))
        except ValueError:
            rel = str(fpath)

        manifest.append(
            build_manifest_entry(
                dest_path, orig_stem, ext, len(data), "(unzipped)", rel
            )
        )

    return manifest, errors


def build_html(manifest, zip_count: int, page_id: str) -> str:
    """Load template.html and substitute the runtime values in via plain
    string replacement (no str.format — the template's own CSS/JS use curly
    braces freely, so a token-based approach avoids escaping headaches)."""
    if not TEMPLATE_PATH.exists():
        sys.exit(
            f"Error: template.html not found next to fonter.py "
            f"(expected at {TEMPLATE_PATH})."
        )

    html = TEMPLATE_PATH.read_text(encoding="utf-8")

    manifest_json = json.dumps(manifest, ensure_ascii=False)

    html = html.replace("__COUNT__", str(len(manifest)))
    html = html.replace("__ZIP_COUNT__", str(zip_count))
    html = html.replace("__PAGE_ID__", page_id)
    html = html.replace("__MANIFEST_JSON__", manifest_json)

    return html


def main():
    parser = argparse.ArgumentParser(
        description="Extract fonts from zip files (and loose font files) and generate an HTML preview page."
    )
    parser.add_argument(
        "--output",
        default="fonter-preview",
        help="Output folder name (default: fonter-preview)",
    )
    parser.add_argument(
        "--no-recurse-dirs",
        action="store_true",
        help="Only look for .zip files directly in the current folder, not in subfolders",
    )
    parser.add_argument(
        "--scan-folders",
        action="store_true",
        help="Also scan subfolders for loose (already-unzipped) font files, not just the current folder. "
        "The output folder is always ignored.",
    )
    args = parser.parse_args()

    cwd = Path.cwd()
    out_dir = cwd / args.output
    fonts_dir = out_dir / "fonts"

    # Create a stable ID for this specific output folder so localStorage doesn't bleed across different preview pages.
    page_id = hashlib.md5(out_dir.resolve().as_posix().encode("utf-8")).hexdigest()[:12]

    # avoid re-scanning our own output folder if run twice
    zip_files = [
        z
        for z in find_zip_files(cwd, recurse=not args.no_recurse_dirs)
        if out_dir not in z.parents
    ]

    # loose (already-unzipped) font files sitting next to the zips.
    # Top-level always scanned; --scan-folders extends this into subfolders too.
    # The output folder itself is always excluded, even if it already exists from a prior run.
    loose_files = find_loose_font_files(
        cwd, recurse=args.scan_folders, exclude_dir=out_dir
    )

    if not zip_files and not loose_files:
        print(
            "No .zip files or loose font files found in the current folder"
            + (
                " (or subfolders)."
                if not args.no_recurse_dirs or args.scan_folders
                else "."
            )
        )
        sys.exit(1)

    if zip_files:
        print(f"Found {len(zip_files)} zip file(s):")
        for z in zip_files:
            print(f"  - {z.relative_to(cwd)}")

    if loose_files:
        print(f"Found {len(loose_files)} loose font file(s):")
        for f in loose_files:
            print(f"  - {f.relative_to(cwd)}")

    out_dir.mkdir(exist_ok=True)
    fonts_dir.mkdir(exist_ok=True)

    zip_manifest, zip_errors = extract_fonts(zip_files, fonts_dir)
    loose_manifest, loose_errors = extract_loose_fonts(loose_files, fonts_dir, cwd)

    raw_manifest = zip_manifest + loose_manifest
    errors = zip_errors + loose_errors

    if not raw_manifest:
        print("\nNo font files (.ttf/.otf/.woff/.woff2) were found.")
        sys.exit(1)

    raw_manifest.sort(key=lambda m: m["display_name"].lower())
    manifest = merge_duplicate_formats(raw_manifest)

    html = build_html(manifest, len(zip_files), page_id)
    index_path = out_dir / "index.html"
    index_path.write_text(html, encoding="utf-8")

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Small metadata sidecar so dev_server.py can rebuild index.html from
    # template.html without re-running extraction (zip_count/page_id aren't
    # otherwise recoverable from manifest.json alone).
    meta_path = out_dir / "meta.json"
    meta_path.write_text(
        json.dumps({"zip_count": len(zip_files), "page_id": page_id}, indent=2),
        encoding="utf-8",
    )

    try:
        fonts_dir_display = fonts_dir.relative_to(cwd)
    except ValueError:
        fonts_dir_display = fonts_dir
    print(f"\nExtracted {len(manifest)} font file(s) into {fonts_dir_display}/")
    if errors:
        print(f"\n{len(errors)} issue(s) encountered:")
        for e in errors:
            print(f"  ! {e}")

    print(f"\nDone. Open this in your browser:\n  {index_path.resolve()}")


if __name__ == "__main__":
    main()
