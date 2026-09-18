#!/usr/bin/env python3
"""
fonter.py

Scans the current folder for .zip files and loose font files — by default,
the current folder plus up to 4 levels of subfolders (see --max-depth) —
pulls every font file out of them (no matter how deeply nested inside a
zip), copies them into a flat fonts/ folder, and generates a single
self-contained HTML page to preview every font at once.

The HTML page itself lives in template.html (next to this script) and is
loaded + token-substituted at runtime, so you can edit the design/markup
there without touching any Python string escaping.

Each font is also tagged with a best-effort classification (sans-serif,
serif, monospace, script, display, symbol) so the preview page can filter
by type. This is read from the font's own OS/2 (sFamilyClass, PANOSE) and
post (isFixedPitch) tables when the optional `fontTools` package is
installed, falling back to a keyword guess on the font's family name
otherwise. Many free/open-source/amateur fonts leave sFamilyClass and
PANOSE blank, so this is a strong hint, not a guarantee — see
detect_font_type() below.

Usage:
    python3 fonter.py
    python3 fonter.py --output my-preview
    python3 fonter.py --no-recurse-dirs   # current folder only, no subfolders
    python3 fonter.py --max-depth 8       # go deeper than the default 4 levels
    python3 fonter.py --folders-only      # skip files sitting directly in the current folder
    python3 fonter.py --fonts-only        # skip zip files, only look for loose font files

Optional dependency for metadata-based font-type detection:
    pip install fonttools
    pip install brotli   # also needed to read .woff2 metadata

Without fontTools installed, everything else still works exactly the
same — font-type detection just falls back to guessing from filenames.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile
from collections import Counter
from pathlib import Path

try:
    from fontTools.ttLib import TTFont

    HAVE_FONTTOOLS = True
except ImportError:
    HAVE_FONTTOOLS = False

FONT_EXTS = {".ttf", ".otf", ".woff", ".woff2"}

# How many levels of subfolders to search by default. 0 = current folder
# only; 1 = current folder + its immediate subfolders; etc. Overridable
# with --max-depth, or forced to 0 with --no-recurse-dirs.
DEFAULT_MAX_DEPTH = 4

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

# sFamilyClass high byte -> our font-type buckets. See the OpenType OS/2
# spec's "IBM font class" table for the full list of class IDs.
FAMILY_CLASS_MAP = {
    1: "serif",  # Oldstyle Serifs
    2: "serif",  # Transitional Serifs
    3: "serif",  # Modern Serifs
    4: "serif",  # Clarendon Serifs
    5: "serif",  # Slab Serifs
    7: "serif",  # Freeform Serifs
    8: "sans-serif",
    9: "display",  # Ornamentals
    10: "script",
    12: "symbol",
}

# PANOSE bSerifStyle values 11-13 are the three "sans" styles; 2-10/14/15
# are various serif styles; 0/1 mean "Any"/"No Fit" (unclassified).
PANOSE_SANS_SERIF_STYLES = {11, 12, 13}
PANOSE_UNCLASSIFIED_SERIF_STYLES = {0, 1}

# Path to the external HTML template, next to this script.
TEMPLATE_PATH = Path(__file__).resolve().parent / "template.html"


def iter_files_by_depth(root: Path, max_depth: int, exclude_dir: Path = None):
    """Yield (path, depth) for every file under root, where depth 0 means
    the file sits directly inside root, depth 1 means one subfolder down,
    and so on. Never descends past max_depth, and never enters exclude_dir
    (the tool's own output folder), so a previous run's output is never
    rescanned as input."""
    root = root.resolve()
    exclude_resolved = exclude_dir.resolve() if exclude_dir is not None else None

    for dirpath, dirnames, filenames in os.walk(root):
        dirpath_p = Path(dirpath)
        depth = len(dirpath_p.relative_to(root).parts)

        if exclude_resolved is not None:
            dirnames[:] = [
                d for d in dirnames if (dirpath_p / d).resolve() != exclude_resolved
            ]

        if depth >= max_depth:
            dirnames[:] = []  # don't descend any further

        for fname in filenames:
            yield dirpath_p / fname, depth


def find_zip_files(
    root: Path, max_depth: int, folders_only: bool, exclude_dir: Path = None
):
    results = []
    for path, depth in iter_files_by_depth(root, max_depth, exclude_dir):
        if folders_only and depth == 0:
            continue
        if path.suffix.lower() == ".zip":
            results.append(path)
    return sorted(results)


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


def guess_font_type_from_name(display_name: str) -> str:
    """Fallback classification when there's no usable OS/2/PANOSE data:
    a keyword match against the font's own family name."""
    name = display_name.lower()
    if any(t in name for t in ("mono", "code", "console", "typewriter", "terminal")):
        return "monospace"
    if any(t in name for t in ("script", "hand", "brush", "calli", "signature")):
        return "script"
    if any(t in name for t in ("display", "deco", "poster", "headline")):
        return "display"
    if "sans" in name:
        return "sans-serif"
    if any(
        t in name for t in ("serif", "slab", "roman", "times", "georgia", "garamond")
    ):
        return "serif"
    return "unknown"


def detect_font_type(path: Path, display_name: str):
    """Best-effort font classification. Returns (font_type, source) where
    source is:
      - "metadata"   read from the font's own OS/2/PANOSE/post tables
      - "name-guess" fell back to keyword-matching the family name
      - "unknown"    neither approach found anything

    fontTools is optional; without it (or if the font fails to parse —
    e.g. a .woff2 with no 'brotli' package installed) this always falls
    back to the name guess."""
    result = None

    if HAVE_FONTTOOLS:
        try:
            font = TTFont(str(path), lazy=True, fontNumber=0)
            try:
                try:
                    post = font["post"]
                    if getattr(post, "isFixedPitch", 0):
                        result = "monospace"
                except Exception:
                    pass

                if result is None and "OS/2" in font:
                    os2 = font["OS/2"]

                    family_class = getattr(os2, "sFamilyClass", 0) or 0
                    class_id = (family_class >> 8) & 0xFF
                    result = FAMILY_CLASS_MAP.get(class_id)

                    if result is None:
                        panose = getattr(os2, "panose", None)
                        if panose is not None:
                            family_type = getattr(panose, "bFamilyType", 0)
                            serif_style = getattr(panose, "bSerifStyle", 0)
                            if family_type == 3:  # Latin Script
                                result = "script"
                            elif family_type == 4:  # Latin Decorative
                                result = "display"
                            elif family_type == 2:  # Latin Text
                                if serif_style in PANOSE_SANS_SERIF_STYLES:
                                    result = "sans-serif"
                                elif (
                                    serif_style not in PANOSE_UNCLASSIFIED_SERIF_STYLES
                                ):
                                    result = "serif"
            finally:
                font.close()
        except Exception:
            result = None  # corrupt/unsupported font, e.g. woff2 w/o brotli

    if result:
        return result, "metadata"

    guess = guess_font_type_from_name(display_name)
    if guess != "unknown":
        return guess, "name-guess"
    return "unknown", "unknown"


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


def _best_font_type(group):
    """When the same font ships in multiple file formats, different files
    can yield different confidence levels (e.g. a .woff2 that fails to
    parse without 'brotli' falls back to a name guess while its .ttf
    sibling reads real OS/2 metadata) — prefer the most reliable result
    found across the group."""
    for source_pref in ("metadata", "name-guess", "unknown"):
        for g in group:
            if g.get("font_type_source") == source_pref:
                return g.get("font_type", "unknown"), source_pref
    return "unknown", "unknown"


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

        font_type, font_type_source = _best_font_type(group)

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
                "font_type": font_type,
                "font_type_source": font_type_source,
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
    font_type, font_type_source = detect_font_type(dest_path, display_name)

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
        "font_type": font_type,
        "font_type_source": font_type_source,
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


def find_loose_font_files(
    root: Path, max_depth: int, folders_only: bool, exclude_dir: Path = None
):
    """Find font files sitting directly on disk (not inside a zip).
    Respects max_depth and excludes anything under exclude_dir."""
    results = []
    for path, depth in iter_files_by_depth(root, max_depth, exclude_dir):
        if folders_only and depth == 0:
            continue
        if path.suffix.lower() not in FONT_EXTS:
            continue
        if "__MACOSX" in path.parts or path.name.startswith("."):
            continue
        results.append(path)
    return sorted(results)


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
        help="Current folder only, no subfolders",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=DEFAULT_MAX_DEPTH,
        help=f"How many levels of subfolders to search (default: {DEFAULT_MAX_DEPTH})",
    )
    parser.add_argument(
        "--folders-only",
        action="store_true",
        help="Skip files sitting directly in the current folder",
    )
    parser.add_argument(
        "--fonts-only",
        action="store_true",
        help="Skip zip files, only look for loose font files",
    )
    args = parser.parse_args()

    cwd = Path.cwd()
    out_dir = cwd / args.output
    fonts_dir = out_dir / "fonts"

    # Create a stable ID for this specific output folder
    page_id = hashlib.md5(out_dir.resolve().as_posix().encode("utf-8")).hexdigest()[:12]

    # Determine actual max depth
    actual_max_depth = 0 if args.no_recurse_dirs else args.max_depth

    # Find Zip Files
    if args.fonts_only:
        zip_files = []
    else:
        zip_files = find_zip_files(
            cwd, actual_max_depth, args.folders_only, exclude_dir=out_dir
        )

    # Find Loose Fonts
    loose_files = find_loose_font_files(
        cwd, actual_max_depth, args.folders_only, exclude_dir=out_dir
    )

    if not zip_files and not loose_files:
        print(
            "No .zip files or loose font files found in the current folder"
            + ("." if args.no_recurse_dirs else " (or subfolders).")
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

    type_counts = Counter(m.get("font_type_source", "unknown") for m in manifest)
    print(
        f"\nFont type detection: {type_counts.get('metadata', 0)} from font metadata, "
        f"{type_counts.get('name-guess', 0)} guessed from filename, "
        f"{type_counts.get('unknown', 0)} unknown."
    )
    if not HAVE_FONTTOOLS:
        print(
            "(fontTools isn't installed, so detection relied only on filename "
            "guessing. For metadata-based detection: pip install fonttools\n"
            " — add 'brotli' too if you have .woff2 files: pip install brotli)"
        )

    print(f"\nDone. Open this in your browser:\n  {index_path.resolve()}")


if __name__ == "__main__":
    main()
