"""Generates the Tabayyun logo SVGs.

The mark is a lens (ring) holding a signal: a noisy trace on the left, one flagged point,
and the trace settling into a check mark on the right. Verify the report before acting on
it. Hand-tuned geometry on a 400×400 grid; run from the repository root:

    python3 docs/assets/genlogo.py

Outputs into docs/assets/ and copies the favicon and header logo into web/public/.
"""

from __future__ import annotations

import shutil
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

# Signal geometry: noisy left part, then the check mark. Coordinates in the 400 grid.
NOISE = [(72, 232), (98, 196), (120, 256), (142, 186), (164, 246), (186, 214)]
CHECK = [(186, 214), (232, 282), (338, 138)]
FLAG = (142, 186)


def _poly(points: list[tuple[int, int]]) -> str:
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
    bg = f'<rect width="{width}" height="{height}" fill="{background}"/>' if background else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" role="img" aria-label="Tabayyun">\n{bg}\n{body}\n</svg>\n')


def wordmark(x: float, y: float, colour: str, size: int = 112) -> str:
    return (f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" font-weight="600" '
            f'letter-spacing="-2" fill="{colour}">Tabayyun</text>')


def tagline(x: float, y: float, colour: str, size: int = 34) -> str:
    return (f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" font-weight="400" '
            f'fill="{colour}">Verify before you act.</text>')


def build() -> None:
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
    web_public = ROOT / "web" / "public"
    if web_public.is_dir():
        shutil.copy(ASSETS / "icon.svg", web_public / "favicon.svg")
        shutil.copy(ASSETS / "mark.svg", web_public / "logo.svg")
    print("wrote", ", ".join(files))


if __name__ == "__main__":
    build()
