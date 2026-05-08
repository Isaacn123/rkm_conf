import os
import re
import shutil
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ImgRef:
    raw: str
    content_path: str  # like content/v1/<siteid>/<uuid>/<filename>


CONTENT_RE = re.compile(
    r"""
    (?P<prefix>
        (?:\.\./(?:assets/)?images\.squarespace-cdn\.com/) |
        (?:https?://images\.squarespace-cdn\.com/) |
        (?:https?://images\.squarespace-cdn\.com/) |
        (?:images\.squarespace-cdn\.com/)
    )
    (?P<content>content/(?:v1/)?[^"'()\s<>]+?)
    (?P<suffix>
        (?=[\"'\s<>])  # stop before delimiter
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)


def extract_refs(html: str) -> list[ImgRef]:
    refs: list[ImgRef] = []
    for m in CONTENT_RE.finditer(html):
        content = m.group("content")
        # strip any query string / HTML entities that leak in
        content_no_q = content.split("?", 1)[0]
        content_no_q = content_no_q.replace("&quot;", "").replace("&#34;", "")
        refs.append(ImgRef(raw=m.group(0), content_path=content_no_q))
    return refs


def guess_local_source(images_root: Path, content_path: str) -> Path | None:
    """
    Map a content path like:
      content/v1/<site>/<uuid>/Logo%2bSeal.png
    to a local file under images_root, accounting for URL-encoding and '+'.
    """
    rel = Path(content_path)
    parts = list(rel.parts)
    if not parts:
        return None

    # Decode only the filename portion; keep directories as-is.
    filename = parts[-1]
    decoded = urllib.parse.unquote(filename)
    # Squarespace local mirror tends to use '+' instead of '%2b'
    candidates = [
        filename,
        decoded,
        decoded.replace("%2b", "+"),
        decoded.replace("+", " "),
    ]

    # If it's a Logo+Seal.png-style reference, the local mirror may have a suffix
    # like Logo+Seal26f7.png. Try prefix match in the same folder.
    folder = images_root / Path(*parts[:-1])
    if folder.exists() and folder.is_dir():
        base_no_ext = Path(decoded).stem
        ext = Path(decoded).suffix
        if base_no_ext and ext:
            for child in folder.iterdir():
                if not child.is_file():
                    continue
                if child.suffix.lower() != ext.lower():
                    continue
                if child.stem.startswith(base_no_ext):
                    return child

    for cand in candidates:
        probe = images_root / Path(*parts[:-1]) / cand
        if probe.exists() and probe.is_file():
            return probe
    # Fallback: if decoded has '+' and local has '+', try that too
    probe = images_root / Path(*parts[:-1]) / decoded.replace(" ", "+")
    if probe.exists() and probe.is_file():
        return probe
    return None


def make_clean_name(content_path: str) -> str:
    rel = Path(content_path)
    parts = list(rel.parts)
    filename = urllib.parse.unquote(parts[-1]).replace(" ", "_")
    # Try to include a stable unique folder (uuid-ish) if present
    # Typical: content/v1/<siteid>/<uuid>/<filename>
    prefix = None
    if len(parts) >= 4:
        prefix = parts[-2]
    if prefix:
        clean = f"{prefix}_{filename}"
    else:
        clean = filename
    # sanitize a bit
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", clean)
    return clean


def rewrite_html(html: str, mapping: dict[str, str]) -> str:
    # Replace any occurrence of the original content path inside larger URLs.
    for content_path, new_rel in mapping.items():
        # replace both encoded and decoded variants
        encoded_filename = Path(content_path).name
        decoded_filename = urllib.parse.unquote(encoded_filename)

        variants = set()
        variants.add(content_path)
        variants.add(str(Path(*Path(content_path).parts[:-1]) / decoded_filename))
        variants.add(str(Path(*Path(content_path).parts[:-1]) / decoded_filename.replace("+", "%2b")))
        variants.add(str(Path(*Path(content_path).parts[:-1]) / decoded_filename.replace("%2b", "+")))

        for v in sorted(variants, key=len, reverse=True):
            html = html.replace(v, new_rel)

    # Also remove srcset to avoid loading external resized variants
    html = re.sub(r'\s+srcset="[^"]*"', "", html, flags=re.IGNORECASE)

    # Normalize any remaining "images.squarespace-cdn.com/images/<localname>" URLs
    html = html.replace("https://images.squarespace-cdn.com/images/", "images/")
    html = html.replace("http://images.squarespace-cdn.com/images/", "images/")
    html = html.replace("//images.squarespace-cdn.com/images/", "images/")
    html = html.replace("../assets/images.squarespace-cdn.com/images/", "images/")
    html = html.replace("../images.squarespace-cdn.com/images/", "images/")

    # Make assets relative to site root folder (so you can upload `www.rkmconf.com/` alone)
    html = html.replace("../assets/", "assets/")

    # Drop preconnect to Squarespace images CDN (keep page self-contained)
    html = re.sub(
        r'<link\s+rel="preconnect"\s+href="https://images\.squarespace-cdn\.com/?"\s*/?>',
        "",
        html,
        flags=re.IGNORECASE,
    )

    # If this specific remote asset isn't mirrored, blank it out to avoid external fetches.
    html = html.replace(
        "https://images.squarespace-cdn.com/content/v1/63b7280f1480f03182fa6b95/34057b75-9832-4b08-903a-6f156bbcd605/shutterstock_593374547_Gradient.png",
        "",
    )
    return html


def main() -> int:
    repo_root = Path.cwd()
    images_root = repo_root / "images.squarespace-cdn.com"
    site_root = repo_root / "www.rkmconf.com"
    out_images = site_root / "images"
    out_images.mkdir(parents=True, exist_ok=True)

    html_files = [site_root / "index.html", site_root / "cart.html"]
    missing: list[str] = []

    # Build mapping from content_path -> new relative path (images/<clean>)
    content_to_new: dict[str, str] = {}
    copied: dict[Path, Path] = {}

    for html_path in html_files:
        html = html_path.read_text(encoding="utf-8", errors="ignore")
        refs = extract_refs(html)
        for r in refs:
            if r.content_path in content_to_new:
                continue

            src = guess_local_source(images_root, r.content_path)
            if not src:
                missing.append(r.content_path)
                continue

            clean_name = make_clean_name(r.content_path)
            dst = out_images / clean_name
            # handle collisions: append counter before extension
            if dst.exists() and not dst.samefile(src):
                stem, ext = dst.stem, dst.suffix
                i = 2
                while True:
                    cand = out_images / f"{stem}_{i}{ext}"
                    if not cand.exists():
                        dst = cand
                        break
                    i += 1
            if not dst.exists():
                shutil.copy2(src, dst)
            content_to_new[r.content_path] = f"images/{dst.name}"
            copied[src] = dst

    # Rewrite HTML files
    for html_path in html_files:
        html = html_path.read_text(encoding="utf-8", errors="ignore")
        new_html = rewrite_html(html, content_to_new)
        html_path.write_text(new_html, encoding="utf-8")

    # Report
    print(f"Copied {len(copied)} images into {out_images}")
    if missing:
        print(f"WARNING: {len(missing)} image references had no local file.")
        for m in missing[:50]:
            print("  missing:", m)
        if len(missing) > 50:
            print("  ...")
    print("Rewrote:", ", ".join(str(p) for p in html_files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

