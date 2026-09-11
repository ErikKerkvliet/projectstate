"""Tiny server-side SVG charts (no JS dependencies)."""
from __future__ import annotations

from html import escape


def bars(values: list[float], labels: list[str], width: int = 720, height: int = 160, color: str = "#2563eb", fmt=lambda v: f"{v:g}") -> str:
    n = max(len(values), 1)
    vmax = max(values) if values and max(values) > 0 else 1.0
    pad_l, pad_b, pad_t = 34, 22, 8
    bw = (width - pad_l - 4) / n
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img" font-family="system-ui" font-size="10">']
    for frac in (0.5, 1.0):
        y = pad_t + (height - pad_t - pad_b) * (1 - frac)
        parts.append(f'<line x1="{pad_l}" x2="{width}" y1="{y:.1f}" y2="{y:.1f}" stroke="#e5e7eb"/>')
        parts.append(f'<text x="{pad_l - 4}" y="{y + 3:.1f}" text-anchor="end" fill="#6b7280">{escape(fmt(vmax * frac))}</text>')
    for i, v in enumerate(values):
        h = (height - pad_t - pad_b) * (v / vmax)
        x = pad_l + i * bw
        y = height - pad_b - h
        parts.append(f'<rect x="{x + 1:.1f}" y="{y:.1f}" width="{max(bw - 2, 1):.1f}" height="{h:.1f}" fill="{color}" rx="1"><title>{escape(labels[i])}: {escape(fmt(v))}</title></rect>')
        if n <= 14 or i % max(1, n // 10) == 0:
            parts.append(f'<text x="{x + bw / 2:.1f}" y="{height - 6}" text-anchor="middle" fill="#6b7280">{escape(labels[i][-5:])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def lines(series: list[tuple[str, list[float], str]], labels: list[str], width: int = 720, height: int = 180, fmt=lambda v: f"{v:g}") -> str:
    n = max(len(labels), 1)
    vmax = max((max(s[1]) for s in series if s[1]), default=1.0) or 1.0
    pad_l, pad_b, pad_t = 40, 22, 10
    sx = (width - pad_l - 6) / max(n - 1, 1)
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img" font-family="system-ui" font-size="10">']
    for frac in (0.5, 1.0):
        y = pad_t + (height - pad_t - pad_b) * (1 - frac)
        parts.append(f'<line x1="{pad_l}" x2="{width}" y1="{y:.1f}" y2="{y:.1f}" stroke="#e5e7eb"/>')
        parts.append(f'<text x="{pad_l - 4}" y="{y + 3:.1f}" text-anchor="end" fill="#6b7280">{escape(fmt(vmax * frac))}</text>')
    for name, vals, color in series:
        pts = " ".join(f"{pad_l + i * sx:.1f},{height - pad_b - (height - pad_t - pad_b) * (v / vmax):.1f}" for i, v in enumerate(vals))
        parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{pts}"><title>{escape(name)}</title></polyline>')
    for i, lab in enumerate(labels):
        if n <= 14 or i % max(1, n // 10) == 0:
            parts.append(f'<text x="{pad_l + i * sx:.1f}" y="{height - 6}" text-anchor="middle" fill="#6b7280">{escape(lab[-5:])}</text>')
    lx = pad_l
    for name, _, color in series:
        parts.append(f'<rect x="{lx}" y="2" width="10" height="10" fill="{color}"/><text x="{lx + 13}" y="11" fill="#374151">{escape(name)}</text>')
        lx += 14 + 7 * len(name)
    parts.append("</svg>")
    return "".join(parts)


def hbars(items: list[tuple[str, float]], width: int = 420, color: str = "#2563eb", fmt=lambda v: f"{v:g}") -> str:
    # long labels (e.g. scanner-invented tool names) would push the bars out of the card
    items = [((label if len(label) <= 22 else label[:21] + "…"), v) for label, v in items]
    vmax = max((v for _, v in items), default=1.0) or 1.0
    rh = 18
    height = rh * len(items) + 4
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" font-family="system-ui" font-size="11">']
    for i, (label, v) in enumerate(items):
        y = 2 + i * rh
        w = (width - 190) * (v / vmax)
        parts.append(f'<text x="120" y="{y + 13}" text-anchor="end" fill="#374151">{escape(label)}</text>')
        parts.append(f'<rect x="126" y="{y + 3}" width="{w:.1f}" height="{rh - 6}" fill="{color}" rx="2"/>')
        parts.append(f'<text x="{130 + w:.1f}" y="{y + 13}" fill="#374151">{escape(fmt(v))}</text>')
    parts.append("</svg>")
    return "".join(parts)
