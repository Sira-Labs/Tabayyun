"""Generates the Tabayyun logo assets.

The mark is a lens (ring) holding a signal: a noisy trace on the left, one flagged point,
and the trace settling into a check mark on the right. Verify the report before acting on
it. Hand-tuned geometry on a 400×400 grid; run from the repository root:

    python3 docs/assets/genlogo.py          # SVGs, plus PNGs when a Chromium is found
    python3 docs/assets/genlogo.py --no-png # SVGs only

Writes into docs/assets/ and web/public/. PNGs (social preview, touch and manifest icons)
are rendered with headless Chromium: set TABAYYUN_CHROMIUM to the binary, or install
Playwright's chromium (`npx playwright install chromium`), or have `chromium`,
`chromium-browser` or `google-chrome` on PATH. Without one the PNG step is skipped with a
notice and the committed PNGs stay as they are.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

NAVY = "#0f172a"  # ink, matches the web theme colour
TEAL = "#0e9f8f"
TEAL_LIGHT = "#5eead4"
AMBER = "#f59e0b"
WHITE = "#ffffff"
SLATE = "#94a3b8"

FONT = "Inter, 'Segoe UI', 'Helvetica Neue', Arial, sans-serif"

ASSETS = Path(__file__).resolve().parent
ROOT = ASSETS.parent.parent
WEB_PUBLIC = ROOT / "web" / "public"

# Signal geometry: noisy left part, then the check mark. Coordinates in the 400 grid.
NOISE = [(72, 232), (98, 196), (120, 256), (142, 186), (164, 246), (186, 214)]
CHECK = [(186, 214), (232, 282), (338, 138)]
FLAG = (142, 186)

# Executable names Playwright's Chromium bundles use across versions and platforms. The
# headless shell has no "new" headless mode and its window already equals the viewport.
HEADLESS_SHELL_NAMES = {"headless_shell", "chrome-headless-shell", "chrome-headless-shell.exe"}
CHROMIUM_NAMES = HEADLESS_SHELL_NAMES | {"chrome", "chrome.exe", "Chromium", "Google Chrome for Testing"}

# PNG renders: (source svg, output path, width, height, background css colour or None).
PNG_JOBS = [
    ("social-preview.svg", ASSETS / "social-preview.png", 1280, 640, None),
    ("icon.svg", WEB_PUBLIC / "icon-180.png", 180, 180, None),
    ("icon.svg", WEB_PUBLIC / "icon-192.png", 192, 192, None),
    ("icon.svg", WEB_PUBLIC / "icon-512.png", 512, 512, None),
]


def _poly(points: list[tuple[int, int]]) -> str:
    """Format points as an SVG polyline `points` attribute."""
    return " ".join(f"{x},{y}" for x, y in points)


def mark(x: float = 0, y: float = 0, scale: float = 1.0, *, ring: str, signal: str, check: str,
         flag: str = AMBER, with_ring: bool = True, stroke: float = 24) -> str:
    """Mark on a 400×400 grid, translated and scaled."""
    parts = [f'<g transform="translate({x},{y}) scale({scale})" fill="none" '
             f'stroke-linecap="round" stroke-linejoin="round">']
    if with_ring:
        parts.append(f'<circle cx="200" cy="200" r="172" stroke="{ring}" stroke-width="{stroke}"/>')
    parts.append(f'<polyline points="{_poly(NOISE)}" stroke="{signal}" stroke-width="{stroke}"/>')
    parts.append(f'<polyline points="{_poly(CHECK)}" stroke="{check}" stroke-width="{stroke + 4}"/>')
    parts.append(f'<circle cx="{FLAG[0]}" cy="{FLAG[1]}" r="{stroke * 0.8:.0f}" fill="{flag}" stroke="none"/>')
    parts.append("</g>")
    return "\n".join(parts)


def svg(width: int, height: int, body: str, *, background: str | None = None) -> str:
    """Wrap a body in an SVG document of the given size, optionally on a solid background."""
    bg = f'<rect width="{width}" height="{height}" fill="{background}"/>' if background else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" role="img" aria-label="Tabayyun">\n{bg}\n{body}\n</svg>\n')


def wordmark(x: float, y: float, colour: str, size: int = 112) -> str:
    """The word Tabayyun in the system sans-serif stack, baseline at (x, y)."""
    return (f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" font-weight="600" '
            f'letter-spacing="-2" fill="{colour}">Tabayyun</text>')


def tagline(x: float, y: float, colour: str, size: int = 34) -> str:
    """The tagline, baseline at (x, y)."""
    return (f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" font-weight="400" '
            f'fill="{colour}">Verify before you act.</text>')


def build_svgs() -> None:
    """Write every SVG variant into docs/assets and copy the icon and mark into web/public."""
    files: dict[str, str] = {}

    # Mark only, on transparent.
    files["mark.svg"] = svg(400, 400, mark(ring=NAVY, signal=NAVY, check=TEAL))

    # Icon: simplified (no ring) on a navy tile with rounded corners; reads at 16 px.
    tile = '<rect width="400" height="400" rx="72" fill="' + NAVY + '"/>'
    files["icon.svg"] = svg(400, 400, tile + mark(20, 20, 0.9, ring=WHITE, signal=WHITE,
                                                  check=TEAL_LIGHT, with_ring=False, stroke=34))

    # Mark + wordmark, light and dark backgrounds.
    for name, text, ring, signal, check in (
        ("logo-light.svg", NAVY, NAVY, NAVY, TEAL),
        ("logo-dark.svg", WHITE, WHITE, WHITE, TEAL_LIGHT),
    ):
        body = mark(0, 0, 0.5, ring=ring, signal=signal, check=check) + wordmark(232, 140, text)
        files[name] = svg(820, 200, body)

    # With tagline, dark, for banners.
    body = (mark(0, 10, 0.5, ring=WHITE, signal=WHITE, check=TEAL_LIGHT)
            + wordmark(232, 118, WHITE) + tagline(236, 172, SLATE))
    files["logo-tagline-dark.svg"] = svg(820, 220, body)

    # Social preview 1280×640.
    body = (mark(96, 120, 1.0, ring=WHITE, signal=WHITE, check=TEAL_LIGHT)
            + wordmark(560, 300, WHITE, 132) + tagline(566, 372, SLATE, 44)
            + f'<text x="566" y="436" font-family="{FONT}" font-size="30" fill="{SLATE}">'
              'Self-hosted data quality for industrial time series</text>')
    files["social-preview.svg"] = svg(1280, 640, body, background=NAVY)

    for name, content in files.items():
        (ASSETS / name).write_text(content)
    if WEB_PUBLIC.is_dir():
        shutil.copy(ASSETS / "icon.svg", WEB_PUBLIC / "favicon.svg")
        shutil.copy(ASSETS / "mark.svg", WEB_PUBLIC / "logo.svg")
    print("wrote", ", ".join(files))


def find_chromium() -> str | None:
    """Locate a headless-capable Chromium: env override, PATH, then Playwright's cache."""
    env = os.environ.get("TABAYYUN_CHROMIUM")
    if env and Path(env).is_file():
        return env
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome", "headless_shell"):
        found = shutil.which(name)
        if found:
            return found
    caches = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH"), Path.home() / ".cache" / "ms-playwright",
              Path.home() / "Library" / "Caches" / "ms-playwright"]
    for cache in caches:
        if not cache:
            continue
        # Bundle layouts differ by version (chrome-linux vs chrome-linux64, headless_shell vs
        # chrome-headless-shell), so search every chromium* bundle for a known executable.
        hits = sorted(
            candidate for bundle in Path(cache).glob("chromium*") for candidate in bundle.rglob("*")
            if candidate.name in CHROMIUM_NAMES and candidate.is_file() and os.access(candidate, os.X_OK)
        )
        if hits:
            shells = [h for h in hits if h.name in HEADLESS_SHELL_NAMES]
            return str((shells or hits)[-1])
    return None


def render_pngs(chromium: str) -> None:
    """Render PNG_JOBS with headless Chromium through a margin-free HTML wrapper."""
    with tempfile.TemporaryDirectory() as tmp:
        for source, out, width, height, background in PNG_JOBS:
            page = Path(tmp) / f"{out.stem}.html"
            bg = background or "transparent"
            page.write_text(
                "<!doctype html><html><head><style>"
                f"html,body{{margin:0;padding:0;background:{bg}}}"
                f"img{{display:block;width:{width}px;height:{height}px}}"
                f'</style></head><body><img src="{(ASSETS / source).as_uri()}"></body></html>'
            )
            out.parent.mkdir(parents=True, exist_ok=True)
            cmd = [chromium, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                   "--default-background-color=00000000", f"--window-size={width},{height}",
                   f"--screenshot={out}", page.as_uri()]
            if Path(chromium).name in HEADLESS_SHELL_NAMES:
                cmd.remove("--headless=new")
            subprocess.run(cmd, check=True, capture_output=True)
            print("rendered", out.relative_to(ROOT))


def main(argv: list[str]) -> int:
    """Entry point: SVGs always, PNGs unless --no-png or no Chromium is available."""
    build_svgs()
    if "--no-png" in argv:
        return 0
    chromium = find_chromium()
    if chromium is None:
        print("no Chromium found; PNGs not regenerated (set TABAYYUN_CHROMIUM)", file=sys.stderr)
        return 0
    render_pngs(chromium)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
