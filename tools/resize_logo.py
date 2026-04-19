#!/usr/bin/env python
"""Resize a logo image for the Nishro TFTP web UI.

Usage::

    python tools/resize_logo.py path/to/nishro_logo.png

Outputs:
    web/static/img/logo.png     - header logo (height 36px, auto width)
    web/static/img/favicon.ico  - 16/32/48px multi-size favicon
"""
import os
import sys

from PIL import Image


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    src = sys.argv[1]
    if not os.path.isfile(src):
        print(f"ERROR: file not found: {src}")
        sys.exit(1)

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(base, "web", "static", "img")
    os.makedirs(out_dir, exist_ok=True)

    img = Image.open(src).convert("RGBA")
    w, h = img.size
    print(f"source: {w}x{h}")

    # Header logo: height 36px, proportional width
    logo_h = 36
    logo_w = int(w * (logo_h / h))
    logo = img.resize((logo_w, logo_h), Image.LANCZOS)
    logo_path = os.path.join(out_dir, "logo.png")
    logo.save(logo_path, optimize=True)
    print(f"logo:    {logo_path}  ({logo_w}x{logo_h}, {os.path.getsize(logo_path)} bytes)")

    # Favicon: 16, 32, 48px square crops
    ico_sizes = [16, 32, 48]
    ico_images = []
    for sz in ico_sizes:
        # Center-crop to square, then resize
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        sq = img.crop((left, top, left + side, top + side))
        sq = sq.resize((sz, sz), Image.LANCZOS)
        ico_images.append(sq)

    ico_path = os.path.join(out_dir, "favicon.ico")
    ico_images[0].save(
        ico_path,
        format="ICO",
        sizes=[(sz, sz) for sz in ico_sizes],
        append_images=ico_images[1:],
    )
    print(f"favicon: {ico_path}  ({os.path.getsize(ico_path)} bytes)")
    print("done - refresh your browser to see the changes")


if __name__ == "__main__":
    main()
