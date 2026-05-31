#!/usr/bin/env python
"""Generate the README demo GIF for ``get_daily_briefing``.

Renders a terminal-style animation entirely with Pillow — no ffmpeg/ttyd/vhs —
so it is fully reproducible and uses **sample data only** (never real Garmin
numbers). Output: ``docs/demo.gif``.

Run:
    uv run --with pillow python scripts/make_demo_gif.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# --- geometry / palette (GitHub-dark-ish) ----------------------------------

FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
FS = 22
PAD_X, PAD_Y = 26, 22
LINE_H = int(FS * 1.45)

BG = (13, 17, 23)
CHROME = (22, 27, 34)
FG = (201, 209, 217)
DIM = (125, 133, 144)
BORDER = (70, 80, 92)
GREEN = (63, 185, 80)
CYAN = (56, 189, 248)
YELLOW = (210, 168, 80)
PROMPT = (88, 166, 255)
CURSOR = (201, 209, 217)

reg = ImageFont.truetype(FONT_REG, FS)
bld = ImageFont.truetype(FONT_BLD, FS)
CW = reg.getlength("M")  # monospace cell width

BOX = 56  # total box width in chars (incl. the two border columns)
INNER = BOX - 2  # content chars between the borders
COLS = 60  # canvas width in chars
ROWS = 15  # canvas height in rows (below the chrome bar)

W = int(PAD_X * 2 + CW * COLS)
CHROME_H = 38
H = int(CHROME_H + PAD_Y * 2 + LINE_H * ROWS)

# A "segment" is (text, color, bold). A "line" is a list of segments.
Seg = tuple
Line = list


def _card_row(label: str, value_segs: list[tuple[str, tuple[int, int, int], bool]]) -> list:
    """One bordered content row: ``│  label        value…        │``."""
    segs: list = [("│  ", BORDER, False), (label.ljust(14), DIM, False)]
    segs += value_segs
    used = 2 + 14 + sum(len(t) for t, _, _ in value_segs)
    segs.append((" " * max(INNER - used, 0), FG, False))
    segs.append(("│", BORDER, False))
    return segs


def _border(top: bool) -> list:
    left, right = ("╭", "╮") if top else ("╰", "╯")
    if top:
        title = "─ Morning briefing · 2026-05-31 "
        fill = "─" * (BOX - 2 - len(title))
        return [(left, BORDER, False), (title, CYAN, True), (fill + right, BORDER, False)]
    return [(left + "─" * (BOX - 2) + right, BORDER, False)]


# The fully-revealed screen (sample data — not real). Each entry is a Line.
CARD: list[list] = [
    _border(top=True),
    _card_row("Sleep", [("82", FG, True), ("  GOOD   7h32m  deep 1h12m", DIM, False)]),
    _card_row("HRV", [("BALANCED", GREEN, True), ("  54 ms   base 45–68", DIM, False)]),  # noqa: RUF001
    _card_row("Body Battery", [("78", FG, True), ("  ▲ charged 61 · drained 12", DIM, False)]),
    _card_row("Readiness", [("88 PRIME", GREEN, True), ("  ready to train", DIM, False)]),
    _card_row("Training", [("PRODUCTIVE", CYAN, True), ("  load 320", DIM, False)]),
    _card_row("Resting HR", [("52 bpm", FG, True), ("  ", DIM, False), ("−2.0 vs base ▼", GREEN, False)]),  # noqa: RUF001
    _border(top=False),
]

PROMPT_TEXT = "Give me my Garmin morning briefing"


def _draw_frame(lines: list[list]) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    # window chrome bar + traffic lights
    d.rectangle([0, 0, W, CHROME_H], fill=CHROME)
    for i, c in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        d.ellipse([PAD_X + i * 24, 13, PAD_X + i * 24 + 12, 25], fill=c)
    d.text((W // 2, CHROME_H // 2), "garmin-mcp", font=reg, fill=DIM, anchor="mm")
    # body
    y = CHROME_H + PAD_Y
    for line in lines:
        x = float(PAD_X)
        for text, color, is_bold in line:
            d.text((x, y), text, font=bld if is_bold else reg, fill=color)
            x += CW * len(text)
        y += LINE_H
    return img


def _screen(typed: int, *, calling: bool = False, card_rows: int = 0) -> list[list]:
    """Assemble the visible screen for a moment in the animation."""
    lines: list[list] = []
    cursor = [("█", CURSOR, False)] if typed <= len(PROMPT_TEXT) and card_rows == 0 else []
    lines.append([("❯ ", PROMPT, True), (PROMPT_TEXT[:typed], FG, False), *cursor])  # noqa: RUF001
    if calling or card_rows:
        lines.append([])
        lines.append([("  → calling ", DIM, False), ("get_daily_briefing", YELLOW, False), ("…", DIM, False)])
    if card_rows:
        lines.append([])
        lines.extend(CARD[:card_rows])
    return lines


def build_frames() -> tuple[list[Image.Image], list[int]]:
    frames: list[Image.Image] = []
    durations: list[int] = []

    def add(lines: list[list], ms: int) -> None:
        frames.append(_draw_frame(lines))
        durations.append(ms)

    add(_screen(0), 600)  # initial pause with cursor
    for i in range(2, len(PROMPT_TEXT) + 1, 2):  # typing
        add(_screen(i), 45)
    add(_screen(len(PROMPT_TEXT)), 450)  # finished typing
    for _ in range(3):  # "calling…" beat
        add(_screen(len(PROMPT_TEXT), calling=True), 160)
    for r in range(1, len(CARD) + 1):  # reveal card top→bottom
        add(_screen(len(PROMPT_TEXT), card_rows=r), 95)
    add(_screen(len(PROMPT_TEXT), card_rows=len(CARD)), 2800)  # hold
    return frames, durations


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "docs" / "demo.gif"
    frames, durations = build_frames()
    frames[0].save(
        out,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )
    kb = out.stat().st_size / 1024
    print(f"wrote {out} ({len(frames)} frames, {kb:.0f} KB, {W}x{H})")


if __name__ == "__main__":
    main()
