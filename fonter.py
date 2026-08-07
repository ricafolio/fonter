#!/usr/bin/env python3
"""
fonter.py

Scans the current folder (and subfolders) for .zip files, pulls every font
file out of them (no matter how deeply nested inside the zip), copies them
into a flat fonts/ folder, and generates a single self-contained HTML page
to preview every font at once.

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
    ("thin", 100), ("hairline", 100),
    ("extralight", 200), ("ultralight", 200),
    ("light", 300),
    ("regular", 400), ("normal", 400), ("book", 400),
    ("medium", 500),
    ("semibold", 600), ("demibold", 600),
    ("bold", 700),
    ("extrabold", 800), ("ultrabold", 800), ("heavy", 800),
    ("black", 900),
]


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
        formats = [{
            "file": g["file"],
            "ext": g["ext"],
            "format": g["format"],
            "size_kb": g["size_kb"],
            "source_zip": g["source_zip"],
            "source_path": g["source_path"],
        } for g in group]
        
        merged.append({
            "id": primary["family"], # Use the stable string as the JS identifier
            "family": primary["family"],
            "display_name": primary["display_name"],
            "weight": primary["weight"],
            "style": primary["style"],
            "source_zip": primary["source_zip"],
            "size_kb": primary["size_kb"],
            "formats": formats,
        })
    return merged


def build_manifest_entry(dest_path: Path, orig_stem: str, ext: str, data_len: int, source_zip: str, source_path: str):
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
                    if "__MACOSX" in inner_path or Path(inner_path).name.startswith("."):
                        continue
                    ext = Path(inner_path).suffix.lower()
                    if ext not in FONT_EXTS:
                        continue

                    try:
                        data = zf.read(info)
                    except Exception as e:
                        errors.append(f"{zpath.name} -> {inner_path}: could not read ({e})")
                        continue

                    if not data:
                        errors.append(f"{zpath.name} -> {inner_path}: empty file, skipped")
                        continue

                    orig_name = Path(inner_path).name
                    orig_stem = Path(orig_name).stem
                    dest_path = unique_dest_path(fonts_dir, orig_name)
                    dest_path.write_bytes(data)

                    manifest.append(build_manifest_entry(dest_path, orig_stem, ext, len(data), zpath.name, inner_path))
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

        manifest.append(build_manifest_entry(dest_path, orig_stem, ext, len(data), "(unzipped)", rel))

    return manifest, errors


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Font Preview — {count} fonts</title>
<style>:root{{--bg:#f2f5f6;--panel:#ffffff;--ink:#1a1a1a;--ink-soft:#6b6b6b;--border:#dadfe2;--accent:#2f65d1;--accent-ink:#ffffff;--chip:#f3f6f9;--radius:10px;--shadow:none}}[data-theme="dark"]{{--bg:#16161a;--panel:#1f1f24;--ink:#f0efe9;--ink-soft:#9a9a9f;--border:#313138;--accent:#2f65d1;--accent-ink:#16161a;--chip:#2a2a31;--shadow:none}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;transition:background .2s ease,color .2s ease}}header{{position:sticky;top:0;z-index:50;background:var(--panel);box-shadow: 0px 1px 20px 0px #ababab2b;padding:14px 20px}}.header-row{{display:flex;justify-content: space-between;flex-wrap:wrap;gap:12px;align-items:center;max-width:1400px;margin:0 auto}}.header-title{{font-weight:700;font-size:15px;margin-right:4px;white-space:nowrap}}.header-title span{{color:var(--ink-soft);font-weight:400;font-size:12px;display:block}}.field{{display:flex;flex-direction:column;gap:3px}}.field label{{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--ink-soft)}}input[type="text"],select{{background:var(--bg);border:1px solid var(--border);color:var(--ink);border-radius:6px;padding:7px 10px;font-size:13px;font-family:inherit}}#sampleText{{min-width:260px;flex:1 1 260px;max-height:2rem}}input[type="range"]{{width:110px;-webkit-appearance:none;appearance:none;height:4px;border-radius:999px;background:var(--border);outline:none;cursor:pointer}}input[type="range"]::-webkit-slider-thumb{{-webkit-appearance:none;appearance:none;width:14px;height:14px;border-radius:50%;background:var(--accent);border:2px solid var(--panel);cursor:pointer}}input[type="range"]::-moz-range-thumb{{width:14px;height:14px;border-radius:50%;background:var(--accent);border:2px solid var(--panel);cursor:pointer}}input[type="range"]::-moz-range-track{{height:4px;border-radius:999px;background:var(--border)}}.range-val{{font-size:11px;color:var(--ink-soft);min-width:34px}}.range-wrap{{display:flex;align-items:center;gap:6px}}button{{border:1px solid var(--border);background:var(--chip);color:var(--ink);border-radius:6px;padding:7px 12px;font-size:12px;cursor:pointer;font-family:inherit}}button:hover{{filter:brightness(.95)}}[data-theme="dark"] button:hover{{filter:brightness(1.15)}}button.primary{{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}}button.toggle.active{{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}}.spacer{{flex:1 1 auto}}.count-pill{{font-size:11px;color:var(--ink-soft);white-space:nowrap}}main{{max-width:1400px;margin:24px auto 80px}}#grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}}#grid.list-view{{grid-template-columns:1fr}}.card{{background:var(--panel);border-radius:var(--radius);padding:16px 18px;box-shadow:var(--shadow);display:flex;flex-direction:column;gap:10px}}.card-meta{{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}}.card-name-wrap{{min-width:0}}.card-name{{font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.card-sub{{font-size:10.5px;color:var(--ink-soft);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.badges{{display:flex;gap:5px;flex-shrink:0}}.badge{{background:var(--chip);color:var(--ink-soft);font-size:9.5px;text-transform:uppercase;letter-spacing:.04em;padding:3px 6px;border-radius:4px;white-space:nowrap}}.badge-active{{color:var(--accent)}}.specimen{{word-wrap:break-word;line-height:1.25;min-height:1.4em}}.card-actions{{display:flex;gap:6px;margin-top:2px}}.card-actions button{{font-size:11px;padding:5px 9px}}.pin-btn.pinned{{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}}footer{{text-align:center;font-size:11px;color:var(--ink-soft);padding:20px}}.empty-state{{text-align:center;padding:60px 20px;color:var(--ink-soft);font-size:13px}}.toast{{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(20px);background:var(--ink);color:var(--bg);padding:8px 16px;border-radius:20px;font-size:12px;opacity:0;pointer-events:none;transition:opacity .2s ease,transform .2s ease;z-index:100}}.toast.show{{opacity:1;transform:translateX(-50%) translateY(0)}}.focus-mode{{display:none;flex-direction:column;align-items:center;justify-content:center;gap:6rem;min-height:calc(100vh - 200px);text-align:center;padding:20px}}.focus-mode.active{{display:flex;justify-content:flex-start}}.focus-top{{display:flex;align-items:center;gap:14px;align-self:flex-start;flex-wrap:wrap}}.focus-meta{{display:flex;flex-direction:column;gap:2px;text-align:left}}.focus-sub{{font-size:10.5px;color:var(--ink-soft)}}.focus-actions{{display:flex;gap:6px;margin-left:auto}}.focus-label{{font-size:13px;color:var(--ink-soft);text-transform:uppercase;letter-spacing:.05em}}.focus-text{{font-size:clamp(40px,8vw,140px);line-height:1.15;outline:none;max-width:100%;word-wrap:break-word;cursor:text}}.focus-controls{{display:flex;gap:24px;flex-wrap:wrap;justify-content:center;padding-top:2rem;border-top:1px solid var(--border);width:100%;max-width:600px}}.focus-text:empty::before{{content:'Type something…';color:var(--ink-soft)}}</style>
</head>
<body data-theme="light">

<header>
  <div class="header-row">
    <div class="field">
      <label for="sampleText">Preview text</label>
      <input type="text" id="sampleText" value="The quick brown fox jumps over the lazy dog — 0123456789">
    </div>

    <div class="field">
      <label for="presetSelect">Text Preset</label>
      <select id="presetSelect">
        <option value="">Custom…</option>
        <option value="pangram">Pangram</option>
        <option value="alphabet">Alphabet</option>
        <option value="numbers">Numbers &amp; symbols</option>
        <option value="paragraph">Paragraph</option>
        <option value="name">Font name only</option>
      </select>
    </div>

    <div class="field">
      <label for="fontSize">Size</label>
      <div class="range-wrap">
        <input type="range" id="fontSize" min="12" max="120" value="32">
        <span class="range-val" id="fontSizeVal">32px</span>
      </div>
    </div>

    <div class="field">
      <label for="letterSpacing">Tracking</label>
      <div class="range-wrap">
        <input type="range" id="letterSpacing" min="-5" max="20" value="0">
        <span class="range-val" id="letterSpacingVal">0px</span>
      </div>
    </div>

    <div class="field">
      <label for="lineHeight">Leading</label>
      <div class="range-wrap">
        <input type="range" id="lineHeight" min="80" max="200" value="125">
        <span class="range-val" id="lineHeightVal">1.25</span>
      </div>
    </div>

    <div class="field">
      <label for="searchBox">Filter</label>
      <input type="text" id="searchBox" placeholder="Search name, file, weight…" style="min-width:160px;height:2rem;">
    </div>

    <div class="field">
      <label for="formatSelect">Font Format</label>
      <select id="formatSelect">
        <option value="auto">Auto</option>
        <option value="otf">Prefer OTF</option>
        <option value="ttf">Prefer TTF</option>
        <option value="woff2">Prefer WOFF2</option>
        <option value="woff">Prefer WOFF</option>
      </select>
    </div>

    <div class="field">
      <label for="sortSelect">Sort</label>
      <select id="sortSelect">
        <option value="name">Name A–Z</option>
        <option value="name-desc">Name Z–A</option>
        <option value="weight">Weight</option>
        <option value="size">File size</option>
        <option value="zip">Source zip</option>
      </select>
    </div>

    <div class="field">
      <label>&nbsp;</label>
      <div style="display:flex; gap:6px;">
        <button id="boldToggle" class="toggle" title="Force bold rendering">B</button>
        <button id="italicToggle" class="toggle" title="Force italic rendering"><i>I</i></button>
        <button id="pinnedOnlyToggle" class="toggle" title="Show pinned only">★</button>
        <button id="hiddenOnlyToggle" class="toggle" title="Show hidden fonts">🙈</button>
        <button id="viewToggle" title="Toggle grid/list view">☰</button>
        <button id="themeToggle" title="Toggle dark mode">◐</button>
      </div>
    </div>

    <div class="spacer"></div>
    <div class="count-pill" id="visibleCount"></div>
  </div>
</header>

<main>
  <div id="grid"></div>
  <div class="empty-state" id="emptyState" style="display:none;">No fonts match your filter.</div>

  <div id="focusMode" class="focus-mode">
    <div class="focus-top">
      <button id="focusBackBtn">← Back</button>
      <div class="focus-meta">
        <div class="focus-label" id="focusLabel"></div>
        <div class="focus-sub" id="focusSub"></div>
      </div>
      <div class="focus-actions">
        <button id="focusPinBtn"></button>
        <button id="focusFaceBtn">Copy @font-face</button>
      </div>
    </div>
    <div id="focusText" class="focus-text" contenteditable="true" spellcheck="false"></div>

    <div class="focus-controls">
      <div class="field">
        <label for="focusSize">Size</label>
        <div class="range-wrap">
          <input type="range" id="focusSize" min="12" max="300" value="96">
          <span class="range-val" id="focusSizeVal">96px</span>
        </div>
      </div>
      <div class="field">
        <label for="focusTracking">Tracking</label>
        <div class="range-wrap">
          <input type="range" id="focusTracking" min="-5" max="20" value="0">
          <span class="range-val" id="focusTrackingVal">0px</span>
        </div>
      </div>
      <div class="field">
        <label for="focusLeading">Leading</label>
        <div class="range-wrap">
          <input type="range" id="focusLeading" min="80" max="200" value="115">
          <span class="range-val" id="focusLeadingVal">1.15</span>
        </div>
      </div>
    </div>
  </div>
</main>

<footer>Generated locally {count} font files from {zip_count} zip file(s).</footer>
<div class="toast" id="toast"></div>

<style id="fontFaceStyles"></style>

<script>
const PAGE_ID = "{page_id}";
const PIN_KEY = `fontPreview.${{PAGE_ID}}.pinned`;
const HIDDEN_KEY = `fontPreview.${{PAGE_ID}}.hidden`;
const THEME_KEY = `fontPreview.theme`;

const FONTS = {manifest_json};

const PRESETS = {{
  pangram: "The quick brown fox jumps over the lazy dog",
  alphabet: "ABCDEFGHIJKLM abcdefghijklm NOPQRSTUVWXYZ nopqrstuvwxyz",
  numbers: "0123456789 !@#$%^&*()_+-=[]{{}}",
  paragraph: "Typography is the craft of endowing human language with a durable visual form. Great type disappears into meaning while still carrying its own quiet character.",
  name: "__NAME__"
}};

// Order a font's available formats according to the current preference,
// so the preferred format is first (and used as the src the browser loads).
function orderedFormats(f, preference) {{
  const formats = f.formats.slice();
  if (preference && preference !== 'auto') {{
    formats.sort((a, b) => (a.ext === preference ? -1 : 0) - (b.ext === preference ? -1 : 0));
  }}
  return formats;
}}

function buildFontFaceCSS(preference) {{
  return FONTS.map(f => {{
    const formats = orderedFormats(f, preference);
    const srcList = formats.map(fmt => `url("fonts/${{encodeURIComponent(fmt.file)}}") format("${{fmt.format}}")`).join(",\\n       ");
    return `
@font-face {{
  font-family: "${{f.family}}";
  src: ${{srcList}};
  font-weight: ${{f.weight}};
  font-style: ${{f.style}};
  font-display: swap;
}}`;
  }}).join("\\n");
}}

const styleEl = document.getElementById('fontFaceStyles');

const grid = document.getElementById('grid');
const emptyState = document.getElementById('emptyState');
const focusModeEl = document.getElementById('focusMode');
const focusTextEl = document.getElementById('focusText');
const focusLabelEl = document.getElementById('focusLabel');
const focusSubEl = document.getElementById('focusSub');
const focusPinBtn = document.getElementById('focusPinBtn');
const focusFaceBtn = document.getElementById('focusFaceBtn');
const focusBackBtn = document.getElementById('focusBackBtn');
const focusSizeEl = document.getElementById('focusSize');
const focusTrackingEl = document.getElementById('focusTracking');
const focusLeadingEl = document.getElementById('focusLeading');
const sampleTextEl = document.getElementById('sampleText');
const fontSizeEl = document.getElementById('fontSize');
const letterSpacingEl = document.getElementById('letterSpacing');
const lineHeightEl = document.getElementById('lineHeight');
const searchBoxEl = document.getElementById('searchBox');
const formatSelectEl = document.getElementById('formatSelect');
const sortSelectEl = document.getElementById('sortSelect');
const presetSelectEl = document.getElementById('presetSelect');
const boldToggleEl = document.getElementById('boldToggle');
const italicToggleEl = document.getElementById('italicToggle');
const pinnedOnlyEl = document.getElementById('pinnedOnlyToggle');
const viewToggleEl = document.getElementById('viewToggle');
const themeToggleEl = document.getElementById('themeToggle');
const hiddenOnlyEl = document.getElementById('hiddenOnlyToggle');
const visibleCountEl = document.getElementById('visibleCount');
const toastEl = document.getElementById('toast');

let pinned = new Set(JSON.parse(localStorage.getItem(PIN_KEY) || '[]'));
let hiddenFonts = new Set(JSON.parse(localStorage.getItem(HIDDEN_KEY) || '[]'));
let forceBold = false;
let forceItalic = false;
let pinnedOnly = false;
let hiddenOnly = false;
let listView = false;
let formatPreference = 'auto';

styleEl.textContent = buildFontFaceCSS(formatPreference);

function savePinned() {{
  localStorage.setItem(PIN_KEY, JSON.stringify([...pinned]));
}}

function saveHidden() {{
  localStorage.setItem(HIDDEN_KEY, JSON.stringify([...hiddenFonts]));
}}

function showToast(msg) {{
  toastEl.textContent = msg;
  toastEl.classList.add('show');
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => toastEl.classList.remove('show'), 1400);
}}

function copyText(text, msg) {{
  navigator.clipboard.writeText(text).then(() => showToast(msg)).catch(() => showToast('Could not copy'));
}}

function currentSampleText(fontDisplayName) {{
  const preset = presetSelectEl.value;
  if (preset === 'name') return fontDisplayName;
  return sampleTextEl.value || 'Type something above';
}}

function updateFocusPinBtn(f) {{
  focusPinBtn.className = 'pin-btn' + (pinned.has(f.id) ? ' pinned' : '');
  focusPinBtn.textContent = pinned.has(f.id) ? '★ Pinned' : '☆ Pin';
}}

function applyFocusStyles() {{
  focusTextEl.style.fontSize = focusSizeEl.value + 'px';
  focusTextEl.style.letterSpacing = focusTrackingEl.value + 'px';
  focusTextEl.style.lineHeight = (focusLeadingEl.value / 100);
}}

focusSizeEl.addEventListener('input', () => {{
  document.getElementById('focusSizeVal').textContent = focusSizeEl.value + 'px';
  applyFocusStyles();
}});
focusTrackingEl.addEventListener('input', () => {{
  document.getElementById('focusTrackingVal').textContent = focusTrackingEl.value + 'px';
  applyFocusStyles();
}});
focusLeadingEl.addEventListener('input', () => {{
  document.getElementById('focusLeadingVal').textContent = (focusLeadingEl.value / 100).toFixed(2);
  applyFocusStyles();
}});

function enterFocus(f) {{
  const formats = orderedFormats(f, formatPreference);
  const active = formats[0];

  focusModeEl.dataset.fontId = f.id;
  focusLabelEl.textContent = f.display_name;
  focusSubEl.textContent = `${{active.file}} · ${{active.size_kb}} KB · from ${{active.source_zip}}`;
  focusTextEl.style.fontFamily = `"${{f.family}}"`;
  if (!focusTextEl.textContent.trim()) {{
    focusTextEl.textContent = sampleTextEl.value || 'The quick brown fox jumps over the lazy dog';
  }}

  updateFocusPinBtn(f);
  focusPinBtn.onclick = () => {{
    if (pinned.has(f.id)) {{ pinned.delete(f.id); }} else {{ pinned.add(f.id); }}
    savePinned();
    updateFocusPinBtn(f);
  }};

  focusFaceBtn.onclick = () => {{
    const srcList = formats.map(fmt => `url("fonts/${{fmt.file}}") format("${{fmt.format}}")`).join(',\\n       ');
    copyText(
      `@font-face {{\\n  font-family: "${{f.family}}";\\n  src: ${{srcList}};\\n  font-weight: ${{f.weight}};\\n  font-style: ${{f.style}};\\n}}`,
      'Copied @font-face rule'
    );
  }};

  grid.style.display = 'none';
  emptyState.style.display = 'none';
  focusModeEl.classList.add('active');
  window.scrollTo({{ top: 0, behavior: 'smooth' }});
  applyFocusStyles();
  focusTextEl.focus();
}}

function exitFocus() {{
  focusModeEl.classList.remove('active');
  grid.style.display = '';
  render();
}}

focusBackBtn.addEventListener('click', exitFocus);

function buildCard(f) {{
  const formats = orderedFormats(f, formatPreference);
  const active = formats[0];

  const card = document.createElement('div');
  card.className = 'card';
  card.dataset.id = f.id;

  const meta = document.createElement('div');
  meta.className = 'card-meta';

  const nameWrap = document.createElement('div');
  nameWrap.className = 'card-name-wrap';
  const nameEl = document.createElement('div');
  nameEl.className = 'card-name';
  nameEl.textContent = f.display_name;
  nameEl.title = 'Click to copy CSS font-family';
  nameEl.onclick = () => copyText(`font-family: "${{f.family}}";`, `Copied CSS for ${{f.display_name}}`);
  const subEl = document.createElement('div');
  subEl.className = 'card-sub';
  subEl.textContent = `${{active.file}} · ${{active.size_kb}} KB · from ${{active.source_zip}}`;
  subEl.title = subEl.textContent;
  nameWrap.appendChild(nameEl);
  nameWrap.appendChild(subEl);

  const badges = document.createElement('div');
  badges.className = 'badges';
  const formatBadges = formats.map(fmt =>
    `<span class="badge${{fmt === active ? ' badge-active' : ''}}">${{fmt.ext}}</span>`
  ).join('');
  badges.innerHTML = formatBadges + `<span class="badge">${{f.weight}}</span>` + (f.style === 'italic' ? '<span class="badge">italic</span>' : '');

  meta.appendChild(nameWrap);
  meta.appendChild(badges);

  const specimen = document.createElement('div');
  specimen.className = 'specimen';
  specimen.style.fontFamily = `"${{f.family}}"`;
  specimen.textContent = currentSampleText(f.display_name);

  const actions = document.createElement('div');
  actions.className = 'card-actions';
  const pinBtn = document.createElement('button');
  pinBtn.className = 'pin-btn' + (pinned.has(f.id) ? ' pinned' : '');
  pinBtn.textContent = pinned.has(f.id) ? '★ Pinned' : '☆ Pin';
  pinBtn.onclick = () => {{
    if (pinned.has(f.id)) {{ pinned.delete(f.id); }} else {{ pinned.add(f.id); }}
    savePinned();
    render();
  }};
  const faceBtn = document.createElement('button');
  faceBtn.textContent = 'Copy @font-face';
  faceBtn.onclick = () => {{
    const srcList = formats.map(fmt => `url("fonts/${{fmt.file}}") format("${{fmt.format}}")`).join(',\\n       ');
    copyText(
      `@font-face {{\\n  font-family: "${{f.family}}";\\n  src: ${{srcList}};\\n  font-weight: ${{f.weight}};\\n  font-style: ${{f.style}};\\n}}`,
      'Copied @font-face rule'
    );
  }};
  actions.appendChild(pinBtn);
  actions.appendChild(faceBtn);

  const hideBtn = document.createElement('button');
  const isHidden = hiddenFonts.has(f.id);
  hideBtn.textContent = isHidden ? 'Unhide' : 'Hide';
  hideBtn.title = isHidden ? 'Bring this font back to the list' : '🙈 Hide this font from the list';
  hideBtn.onclick = () => {{
    if (isHidden) {{ hiddenFonts.delete(f.id); }} else {{ hiddenFonts.add(f.id); }}
    saveHidden();
    render();
  }};
  actions.appendChild(hideBtn);

  const playBtn = document.createElement('button');
  playBtn.textContent = '▶ Play';
  playBtn.title = 'Focus mode: big editable preview';
  playBtn.onclick = () => enterFocus(f);
  actions.appendChild(playBtn);

  card.appendChild(meta);
  card.appendChild(specimen);
  card.appendChild(actions);
  return card;
}}

function applySpecimenStyles() {{
  const size = fontSizeEl.value + 'px';
  const spacing = letterSpacingEl.value + 'px';
  const leading = (lineHeightEl.value / 100);
  document.querySelectorAll('.specimen').forEach(el => {{
    el.style.fontSize = size;
    el.style.letterSpacing = spacing;
    el.style.lineHeight = leading;
    el.style.fontWeight = forceBold ? '700' : '';
    el.style.fontStyle = forceItalic ? 'italic' : '';
  }});
}}

function updateSpecimenText() {{
  document.querySelectorAll('.card').forEach(card => {{
    const f = FONTS.find(x => x.id == card.dataset.id);
    const specimen = card.querySelector('.specimen');
    if (f && specimen) specimen.textContent = currentSampleText(f.display_name);
  }});
}}

function render() {{
  const query = searchBoxEl.value.trim().toLowerCase();
  let list = FONTS.filter(f => {{
    const isHidden = hiddenFonts.has(f.id);
    if (hiddenOnly) {{
      if (!isHidden) return false;
    }} else {{
      if (isHidden) return false;
    }}
    if (pinnedOnly && !pinned.has(f.id)) return false;
    if (!query) return true;
    return f.display_name.toLowerCase().includes(query)
      || f.formats.some(fmt => fmt.file.toLowerCase().includes(query) || fmt.ext.includes(query))
      || f.source_zip.toLowerCase().includes(query)
      || String(f.weight).includes(query)
      || f.style.includes(query);
  }});

  const sort = sortSelectEl.value;
  list = list.slice().sort((a, b) => {{
    if (sort === 'name') return a.display_name.localeCompare(b.display_name);
    if (sort === 'name-desc') return b.display_name.localeCompare(a.display_name);
    if (sort === 'weight') return a.weight - b.weight;
    if (sort === 'size') return b.size_kb - a.size_kb;
    if (sort === 'zip') return a.source_zip.localeCompare(b.source_zip);
    return 0;
  }});

  grid.innerHTML = '';
  list.forEach(f => grid.appendChild(buildCard(f)));
  applySpecimenStyles();

  emptyState.style.display = list.length === 0
    ? 'block'
    : 'none';
  emptyState.textContent = hiddenOnly ? 'No hidden fonts.' : 'No fonts match your filter.';

  const hiddenCount = hiddenFonts.size;
  visibleCountEl.textContent = hiddenOnly
    ? `Showing ${{list.length}} hidden`
    : `Showing ${{list.length}} of ${{FONTS.length}}${{hiddenCount ? ` (${{hiddenCount}} hidden)` : ''}}`;
}}

// Event wiring
sampleTextEl.addEventListener('input', () => {{ presetSelectEl.value=''; updateSpecimenText(); }});
fontSizeEl.addEventListener('input', () => {{
  document.getElementById('fontSizeVal').textContent = fontSizeEl.value + 'px';
  applySpecimenStyles();
}});
letterSpacingEl.addEventListener('input', () => {{
  document.getElementById('letterSpacingVal').textContent = letterSpacingEl.value + 'px';
  applySpecimenStyles();
}});
lineHeightEl.addEventListener('input', () => {{
  document.getElementById('lineHeightVal').textContent = (lineHeightEl.value/100).toFixed(2);
  applySpecimenStyles();
}});
searchBoxEl.addEventListener('input', render);
formatSelectEl.addEventListener('change', () => {{
  formatPreference = formatSelectEl.value;
  styleEl.textContent = buildFontFaceCSS(formatPreference);
  render();
}});
sortSelectEl.addEventListener('change', render);
presetSelectEl.addEventListener('change', () => {{
  const val = presetSelectEl.value;
  if (val && val !== 'name' && PRESETS[val]) sampleTextEl.value = PRESETS[val];
  updateSpecimenText();
}});
boldToggleEl.addEventListener('click', () => {{
  forceBold = !forceBold;
  boldToggleEl.classList.toggle('active', forceBold);
  applySpecimenStyles();
}});
italicToggleEl.addEventListener('click', () => {{
  forceItalic = !forceItalic;
  italicToggleEl.classList.toggle('active', forceItalic);
  applySpecimenStyles();
}});
pinnedOnlyEl.addEventListener('click', () => {{
  pinnedOnly = !pinnedOnly;
  pinnedOnlyEl.classList.toggle('active', pinnedOnly);
  render();
}});
hiddenOnlyEl.addEventListener('click', () => {{
  hiddenOnly = !hiddenOnly;
  hiddenOnlyEl.classList.toggle('active', hiddenOnly);
  render();
}});
viewToggleEl.addEventListener('click', () => {{
  listView = !listView;
  grid.classList.toggle('list-view', listView);
}});
themeToggleEl.addEventListener('click', () => {{
  const body = document.body;
  const next = body.dataset.theme === 'dark' ? 'light' : 'dark';
  body.dataset.theme = next;
  localStorage.setItem(THEME_KEY, next);
}});

// Restore theme
const savedTheme = localStorage.getItem(THEME_KEY);
if (savedTheme) document.body.dataset.theme = savedTheme;

render();
</script>
</body>
</html>
"""


def build_html(manifest, zip_count: int, page_id: str) -> str:
    return HTML_TEMPLATE.format(
        count=len(manifest),
        zip_count=zip_count,
        manifest_json=json.dumps(manifest, ensure_ascii=False),
        page_id=page_id,
    )


def main():
    parser = argparse.ArgumentParser(description="Extract fonts from zip files (and loose font files) and generate an HTML preview page.")
    parser.add_argument("--output", default="fonter-preview", help="Output folder name (default: fonter-preview)")
    parser.add_argument("--no-recurse-dirs", action="store_true",
                         help="Only look for .zip files directly in the current folder, not in subfolders")
    parser.add_argument("--scan-folders", action="store_true",
                         help="Also scan subfolders for loose (already-unzipped) font files, not just the current folder. "
                              "The output folder is always ignored.")
    args = parser.parse_args()

    cwd = Path.cwd()
    out_dir = cwd / args.output
    fonts_dir = out_dir / "fonts"

    # Create a stable ID for this specific output folder so localStorage doesn't bleed across different preview pages.
    page_id = hashlib.md5(out_dir.resolve().as_posix().encode('utf-8')).hexdigest()[:12]

    # avoid re-scanning our own output folder if run twice
    zip_files = [z for z in find_zip_files(cwd, recurse=not args.no_recurse_dirs)
                 if out_dir not in z.parents]

    # loose (already-unzipped) font files sitting next to the zips.
    # Top-level always scanned; --scan-folders extends this into subfolders too.
    # The output folder itself is always excluded, even if it already exists from a prior run.
    loose_files = find_loose_font_files(cwd, recurse=args.scan_folders, exclude_dir=out_dir)

    if not zip_files and not loose_files:
        print("No .zip files or loose font files found in the current folder"
              + (" (or subfolders)." if not args.no_recurse_dirs or args.scan_folders else "."))
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
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

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