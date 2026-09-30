"""Generate assets/icon.ico (Pillow). One-off; the output is kept in the repo."""

from pathlib import Path

from PIL import Image, ImageDraw

S = 1024  # draw at high resolution, then downscale (anti-aliasing)
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((0, 0, S - 1, S - 1), radius=int(S * 0.23), fill=(10, 132, 255, 255))
cx, cy = S / 2, S * 0.60
for r, w in ((S * 0.34, S * 0.075), (S * 0.21, S * 0.075)):  # broadcast waves
    d.arc((cx - r, cy - r, cx + r, cy + r), start=215, end=325, fill="white", width=int(w))
d.ellipse((cx - S * 0.075, cy - S * 0.075, cx + S * 0.075, cy + S * 0.075), fill="white")
d.polygon([(cx - S * 0.2, S * 0.86), (cx + S * 0.2, S * 0.86), (cx, S * 0.66)], fill="white")  # AirPlay triangle

out = Path(__file__).with_name("assets") / "icon.ico"
out.parent.mkdir(exist_ok=True)
img.resize((256, 256), Image.LANCZOS).save(out, sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
img.resize((256, 256), Image.LANCZOS).save(out.with_suffix(".png"))
print(out)
