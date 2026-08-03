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


def sanitize_css_family(name: str, idx: int) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_")
    slug = slug or "font"
    return f"f{idx}_{slug}"


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


def extract_fonts(zip_files, fonts_dir: Path):
    manifest = []
    errors = []
    idx = 0

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
                    dest_path = unique_dest_path(fonts_dir, orig_name)
                    dest_path.write_bytes(data)

                    stem = dest_path.stem
                    display_name = clean_display_name(stem)
                    weight, style = guess_weight_style(stem)
                    css_family = sanitize_css_family(display_name, idx)
                    idx += 1

                    manifest.append({
                        "id": idx,
                        "family": css_family,
                        "display_name": display_name,
                        "file": dest_path.name,
                        "ext": ext.lstrip("."),
                        "format": FORMAT_MAP[ext],
                        "weight": weight,
                        "style": style,
                        "source_zip": zpath.name,
                        "source_path": inner_path,
                        "size_kb": round(len(data) / 1024, 1),
                    })
        except zipfile.BadZipFile:
            errors.append(f"{zpath.name}: not a valid zip file, skipped")
        except Exception as e:
            errors.append(f"{zpath.name}: unexpected error ({e})")

    manifest.sort(key=lambda m: m["display_name"].lower())
    return manifest, errors


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Font Preview — {count} fonts</title>
<style>
  :root {{
    --bg: #f5f4f1;
    --panel: #ffffff;
    --ink: #1a1a1a;
    --ink-soft: #6b6b6b;
    --border: #e2e0da;
    --accent: #d1502f;
    --accent-ink: #ffffff;
    --chip: #efece5;
    --radius: 10px;
    --shadow: 0 1px 3px rgba(0,0,0,0.06), 0 8px 24px rgba(0,0,0,0.04);
  }}
  [data-theme="dark"] {{
    --bg: #16161a;
    --panel: #1f1f24;
    --ink: #f0efe9;
    --ink-soft: #9a9a9f;
    --border: #313138;
    --accent: #e2724a;
    --accent-ink: #16161a;
    --chip: #2a2a31;
    --shadow: 0 1px 3px rgba(0,0,0,0.3), 0 8px 24px rgba(0,0,0,0.25);
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    transition: background .2s ease, color .2s ease;
  }}
  header {{
    position: sticky;
    top: 0;
    z-index: 50;
    background: var(--panel);
    border-bottom: 1px solid var(--border);
    box-shadow: var(--shadow);
    padding: 14px 20px;
  }}
  .header-row {{
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    align-items: center;
    max-width: 1400px;
    margin: 0 auto;
  }}
  .header-title {{
    font-weight: 700;
    font-size: 15px;
    margin-right: 4px;
    white-space: nowrap;
  }}
  .header-title span {{
    color: var(--ink-soft);
    font-weight: 400;
    font-size: 12px;
    display: block;
  }}
  .field {{
    display: flex;
    flex-direction: column;
    gap: 3px;
  }}
  .field label {{
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--ink-soft);
  }}
  input[type="text"], select {{
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--ink);
    border-radius: 6px;
    padding: 7px 10px;
    font-size: 13px;
    font-family: inherit;
  }}
  #sampleText {{ min-width: 260px; flex: 1 1 260px; max-height: 2rem; }}
  input[type="range"] {{ width: 110px; }}
  .range-val {{ font-size: 11px; color: var(--ink-soft); min-width: 34px; }}
  .range-wrap {{ display: flex; align-items: center; gap: 6px; }}
  button {{
    border: 1px solid var(--border);
    background: var(--chip);
    color: var(--ink);
    border-radius: 6px;
    padding: 7px 12px;
    font-size: 12px;
    cursor: pointer;
    font-family: inherit;
  }}
  button:hover {{ filter: brightness(0.95); }}
  [data-theme="dark"] button:hover {{ filter: brightness(1.15); }}
  button.primary {{ background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }}
  button.toggle.active {{ background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }}
  .spacer {{ flex: 1 1 auto; }}
  .count-pill {{
    font-size: 11px;
    color: var(--ink-soft);
    white-space: nowrap;
  }}
  main {{
    max-width: 1400px;
    margin: 24px auto 80px;
    padding: 0 20px;
  }}
  #grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    gap: 16px;
  }}
  #grid.list-view {{
    grid-template-columns: 1fr;
  }}
  .card {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px 18px;
    box-shadow: var(--shadow);
    display: flex;
    flex-direction: column;
    gap: 10px;
  }}
  .card-meta {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 8px;
  }}
  .card-name-wrap {{ min-width: 0; }}
  .card-name {{
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .card-sub {{
    font-size: 10.5px;
    color: var(--ink-soft);
    margin-top: 2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .badges {{ display: flex; gap: 5px; flex-shrink: 0; }}
  .badge {{
    background: var(--chip);
    color: var(--ink-soft);
    font-size: 9.5px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 3px 6px;
    border-radius: 4px;
    white-space: nowrap;
  }}
  .specimen {{
    word-wrap: break-word;
    line-height: 1.25;
    min-height: 1.4em;
  }}
  .card-actions {{
    display: flex;
    gap: 6px;
    margin-top: 2px;
  }}
  .card-actions button {{ font-size: 11px; padding: 5px 9px; }}
  .pin-btn.pinned {{ background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }}
  footer {{
    text-align: center;
    font-size: 11px;
    color: var(--ink-soft);
    padding: 20px;
  }}
  .empty-state {{
    text-align: center;
    padding: 60px 20px;
    color: var(--ink-soft);
    font-size: 13px;
  }}
  .toast {{
    position: fixed;
    bottom: 20px;
    left: 50%;
    transform: translateX(-50%) translateY(20px);
    background: var(--ink);
    color: var(--bg);
    padding: 8px 16px;
    border-radius: 20px;
    font-size: 12px;
    opacity: 0;
    pointer-events: none;
    transition: opacity .2s ease, transform .2s ease;
    z-index: 100;
  }}
  .toast.show {{ opacity: 1; transform: translateX(-50%) translateY(0); }}
</style>
</head>
<body data-theme="light">

<header>
  <div class="header-row">
    <div class="header-title">Font Preview<span>{count} fonts loaded</span></div>

    <div class="field">
      <label for="sampleText">Preview text</label>
      <input type="text" id="sampleText" value="The quick brown fox jumps over the lazy dog — 0123456789">
    </div>

    <div class="field">
      <label for="presetSelect">Preset</label>
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
      <input type="text" id="searchBox" placeholder="Search name, file, weight…" style="min-width:160px;">
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
</main>

<footer>Generated locally — {count} font files from {zip_count} zip file(s). Nothing here touches the network.</footer>
<div class="toast" id="toast"></div>

<style id="fontFaceStyles"></style>

<script>
const FONTS = {manifest_json};

const PRESETS = {{
  pangram: "The quick brown fox jumps over the lazy dog",
  alphabet: "ABCDEFGHIJKLM abcdefghijklm NOPQRSTUVWXYZ nopqrstuvwxyz",
  numbers: "0123456789 !@#$%^&*()_+-=[]{{}}",
  paragraph: "Typography is the craft of endowing human language with a durable visual form. Great type disappears into meaning while still carrying its own quiet character.",
  name: "__NAME__"
}};

// Build @font-face rules
const styleEl = document.getElementById('fontFaceStyles');
styleEl.textContent = FONTS.map(f => `
@font-face {{
  font-family: "${{f.family}}";
  src: url("fonts/${{encodeURIComponent(f.file)}}") format("${{f.format}}");
  font-weight: ${{f.weight}};
  font-style: ${{f.style}};
  font-display: swap;
}}`).join("\\n");

const grid = document.getElementById('grid');
const emptyState = document.getElementById('emptyState');
const sampleTextEl = document.getElementById('sampleText');
const fontSizeEl = document.getElementById('fontSize');
const letterSpacingEl = document.getElementById('letterSpacing');
const lineHeightEl = document.getElementById('lineHeight');
const searchBoxEl = document.getElementById('searchBox');
const sortSelectEl = document.getElementById('sortSelect');
const presetSelectEl = document.getElementById('presetSelect');
const boldToggleEl = document.getElementById('boldToggle');
const italicToggleEl = document.getElementById('italicToggle');
const pinnedOnlyEl = document.getElementById('pinnedOnlyToggle');
const viewToggleEl = document.getElementById('viewToggle');
const themeToggleEl = document.getElementById('themeToggle');
const visibleCountEl = document.getElementById('visibleCount');
const toastEl = document.getElementById('toast');

let pinned = new Set(JSON.parse(localStorage.getItem('fontPreview.pinned') || '[]'));
let forceBold = false;
let forceItalic = false;
let pinnedOnly = false;
let listView = false;

function savePinned() {{
  localStorage.setItem('fontPreview.pinned', JSON.stringify([...pinned]));
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

function buildCard(f) {{
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
  subEl.textContent = `${{f.file}} · ${{f.size_kb}} KB · from ${{f.source_zip}}`;
  subEl.title = subEl.textContent;
  nameWrap.appendChild(nameEl);
  nameWrap.appendChild(subEl);

  const badges = document.createElement('div');
  badges.className = 'badges';
  badges.innerHTML = `<span class="badge">${{f.ext}}</span><span class="badge">${{f.weight}}</span>` + (f.style === 'italic' ? '<span class="badge">italic</span>' : '');

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
  faceBtn.onclick = () => copyText(
    `@font-face {{\\n  font-family: "${{f.family}}";\\n  src: url("fonts/${{f.file}}") format("${{f.format}}");\\n  font-weight: ${{f.weight}};\\n  font-style: ${{f.style}};\\n}}`,
    'Copied @font-face rule'
  );
  actions.appendChild(pinBtn);
  actions.appendChild(faceBtn);

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
    if (pinnedOnly && !pinned.has(f.id)) return false;
    if (!query) return true;
    return f.display_name.toLowerCase().includes(query)
      || f.file.toLowerCase().includes(query)
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

  emptyState.style.display = list.length === 0 ? 'block' : 'none';
  visibleCountEl.textContent = `Showing ${{list.length}} of ${{FONTS.length}}`;
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
viewToggleEl.addEventListener('click', () => {{
  listView = !listView;
  grid.classList.toggle('list-view', listView);
}});
themeToggleEl.addEventListener('click', () => {{
  const body = document.body;
  const next = body.dataset.theme === 'dark' ? 'light' : 'dark';
  body.dataset.theme = next;
  localStorage.setItem('fontPreview.theme', next);
}});

// Restore theme
const savedTheme = localStorage.getItem('fontPreview.theme');
if (savedTheme) document.body.dataset.theme = savedTheme;

render();
</script>
</body>
</html>
"""


def build_html(manifest, zip_count: int) -> str:
    return HTML_TEMPLATE.format(
        count=len(manifest),
        zip_count=zip_count,
        manifest_json=json.dumps(manifest, ensure_ascii=False),
    )


def main():
    parser = argparse.ArgumentParser(description="Extract fonts from zip files and generate an HTML preview page.")
    parser.add_argument("--output", default="fonter-preview", help="Output folder name (default: fonter-preview)")
    parser.add_argument("--no-recurse-dirs", action="store_true",
                         help="Only look for .zip files directly in the current folder, not in subfolders")
    args = parser.parse_args()

    cwd = Path.cwd()
    out_dir = cwd / args.output
    fonts_dir = out_dir / "fonts"

    # avoid re-scanning our own output folder if run twice
    zip_files = [z for z in find_zip_files(cwd, recurse=not args.no_recurse_dirs)
                 if out_dir not in z.parents]

    if not zip_files:
        print("No .zip files found in the current folder (or subfolders).")
        sys.exit(1)

    print(f"Found {len(zip_files)} zip file(s):")
    for z in zip_files:
        print(f"  - {z.relative_to(cwd)}")

    out_dir.mkdir(exist_ok=True)
    fonts_dir.mkdir(exist_ok=True)

    manifest, errors = extract_fonts(zip_files, fonts_dir)

    if not manifest:
        print("\nNo font files (.ttf/.otf/.woff/.woff2) were found inside those zips.")
        sys.exit(1)

    html = build_html(manifest, len(zip_files))
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