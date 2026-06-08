#!/usr/bin/env python3
"""Build a single-slide PPTX for AProver — high-level pitch + the BMC-Agent demo.

  python3 presentation/build_ppt.py

The slide is deliberately HIGH-LEVEL: it states the one idea (AI writes the
code; AProver proves it correct), plays the demo video, and points to the
project via a link + QR code. All the *mechanism* (spec synthesis, BMC, CEGAR,
ASan replay, confidence tiers) lives in the video — the slide does not repeat it.

- If presentation/bmc-agent-demo.mp4 exists it is EMBEDDED as a playable movie
  (poster = the real title frame bmc-agent-poster.jpg when available).
- Otherwise a generated poster is placed and click-linked to the live HTML demo.
"""
import os
import qrcode
from PIL import Image, ImageDraw, ImageFont

from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE

HERE = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(HERE, "aprover-icon.png")          # iceberg-A brand mark
MP4 = os.path.join(HERE, "bmc-agent-demo.mp4")
POSTER_REAL = os.path.join(HERE, "bmc-agent-poster.jpg")   # real title frame
POSTER_GEN = os.path.join(HERE, "_demo_poster.png")        # synthetic fallback
QR_PNG = os.path.join(HERE, "_qr.png")
OUT = os.path.join(HERE, "agentic-prover.pptx")

REPO_URL = "https://github.com/agentic-prover/aprover"
REPO_SHORT = "github.com/agentic-prover/aprover"
PAPER = "Agentic Model Checking — arXiv:2605.21434"

# ---- palette ----
def C(h): return RGBColor.from_string(h)
BG, BG2 = "0d1117", "05070b"
PANEL, PANEL2, LINE = "161b22", "1c2230", "30363d"
TEXT, MUTED = "e6edf3", "8b949e"
GREEN, BLUE, VIOLET, RED, AMBER, GREY = "3fb950", "58a6ff", "a371f7", "f85149", "d29922", "888888"

# ===================================================================
# 0) QR code → the project repo (black-on-white for reliable scanning)
# ===================================================================
def make_qr():
    qr = qrcode.QRCode(border=2, box_size=12,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(REPO_URL)
    qr.make(fit=True)
    qr.make_image(fill_color="black", back_color="white").save(QR_PNG)

make_qr()

# ===================================================================
# 1) synthetic poster (only used if the mp4 is missing)
# ===================================================================
def make_poster():
    W, H = 1600, 900
    img = Image.new("RGB", (W, H), (5, 7, 11))
    d = ImageDraw.Draw(img, "RGBA")
    def rgb(h): return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    COLS, ROWS = 8, 5
    aw, ah = 1180, 520
    ax, ay = (W - aw) // 2, (H - ah) // 2 + 20
    gap = 14
    cw = (aw - (COLS - 1) * gap) / COLS
    ch = (ah - (ROWS - 1) * gap) / ROWS
    def cell(i):
        c, r = i % COLS, i // COLS
        x = ax + c * (cw + gap); y = ay + r * (ch + gap)
        return x, y, x + cw, y + ch
    def center(i):
        x0, y0, x1, y1 = cell(i); return ((x0 + x1) / 2, (y0 + y1) / 2)
    bug = 19
    for i in range(COLS * ROWS):
        x0, y0, x1, y1 = cell(i)
        d.rounded_rectangle([x0, y0, x1, y1], radius=10, fill=rgb(PANEL), outline=rgb(LINE), width=2)
        for j in range(2 + (i % 3)):
            ly = y0 + 16 + j * 14
            col = rgb(BLUE) if (i + j) % 4 == 1 else (rgb(VIOLET) if (i + j) % 4 == 2 else (43, 51, 64))
            d.rounded_rectangle([x0 + 12, ly, x0 + 12 + cw * (0.45 + ((i*13+j*7) % 40)/100), ly + 6],
                                radius=3, fill=col)
    path = [2, 18, bug]
    pts = [center(i) for i in path]
    d.line(pts, fill=rgb(RED), width=5, joint="curve")
    for p in pts[:-1]:
        d.ellipse([p[0]-7, p[1]-7, p[0]+7, p[1]+7], fill=rgb(RED))
    bx0, by0, bx1, by1 = cell(bug)
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=10, outline=rgb(RED), width=4)
    cx, cy, rr = W // 2, H // 2 + 20, 92
    d.ellipse([cx-rr, cy-rr, cx+rr, cy+rr], fill=(5, 7, 11, 200), outline=(255, 255, 255, 230), width=4)
    tw = 46
    d.polygon([(cx-tw//2+6, cy-tw), (cx-tw//2+6, cy+tw), (cx+tw, cy)], fill=(255, 255, 255, 235))
    img.save(POSTER_GEN)

if not os.path.exists(MP4):
    make_poster()

# ===================================================================
# 2) build the slide
# ===================================================================
prs = Presentation()
prs.slide_width = Inches(13.333)
prs.slide_height = Inches(7.5)
slide = prs.slides.add_slide(prs.slide_layouts[6])
slide.background.fill.solid()
slide.background.fill.fore_color.rgb = C(BG)

def no_shadow(shp): shp.shadow.inherit = False

def box(x, y, w, h, fill=None, line=None, line_w=1.0, radius=True):
    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE,
        Inches(x), Inches(y), Inches(w), Inches(h))
    no_shadow(shape)
    if fill is None: shape.fill.background()
    else: shape.fill.solid(); shape.fill.fore_color.rgb = C(fill)
    if line is None: shape.line.fill.background()
    else: shape.line.color.rgb = C(line); shape.line.width = Pt(line_w)
    return shape

def text(x, y, w, h, runs, size=14, color=TEXT, bold=False, align=PP_ALIGN.LEFT,
         anchor=MSO_ANCHOR.TOP, font="Calibri", spacing=1.0):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame; tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = Pt(2); tf.margin_top = tf.margin_bottom = Pt(1)
    if isinstance(runs, str): runs = [(runs, color, bold, size)]
    p = tf.paragraphs[0]; p.alignment = align; p.line_spacing = spacing
    for seg in runs:
        if seg == "\n":
            p = tf.add_paragraph(); p.alignment = align; p.line_spacing = spacing
            continue
        t, c, b, s = (seg + (None,)*4)[:4]
        r = p.add_run(); r.text = t
        r.font.size = Pt(s or size); r.font.bold = bool(b)
        r.font.color.rgb = C(c or color); r.font.name = font
    return tb

# ---- brand (iceberg-A logo mark + wordmark) ----
if os.path.exists(ICON):
    slide.shapes.add_picture(ICON, Inches(0.58), Inches(0.42), height=Inches(0.36))
else:
    dot = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(0.6), Inches(0.5), Inches(0.18), Inches(0.18))
    no_shadow(dot); dot.fill.solid(); dot.fill.fore_color.rgb = C(VIOLET); dot.line.fill.background()
text(0.98, 0.4, 9, 0.4, [("AProver", TEXT, True, 19),
                         ("   ·  agentic prover for AI-generated code", MUTED, False, 13)],
     font="Consolas")

# ===================================================================
#  LEFT COLUMN — the one idea, high level
# ===================================================================
LX, LW = 0.6, 5.75

# thesis
text(LX, 1.6, LW, 0.7, [("AI writes the code.", TEXT, True, 29)], spacing=1.0)
text(LX, 2.3, LW, 0.7, [("AProver proves it ", TEXT, True, 29), ("correct.", BLUE, True, 29)], spacing=1.0)

# one high-level paragraph (the design principle — no mechanism detail)
text(LX, 3.35, LW, 1.3,
     [("Agents propose", VIOLET, True, 15.5),
      (" — specifications, counterexample verdicts, refinements.  ", MUTED, False, 15.5),
      ("Conventional tools dispose", BLUE, True, 15.5),
      (" — every soundness-relevant claim passes a formal check before it counts.", MUTED, False, 15.5)],
     spacing=1.12)
text(LX, 4.78, LW, 0.4,
     [("Bugs are ", MUTED, False, 15.5), ("confirmed, not guessed", GREEN, True, 15.5), (".", MUTED, False, 15.5)])

# ---- project link + QR (bottom-left) ----
QY = 5.55
# white quiet-zone card first (drawn behind), then the QR on top for scannability
card = box(LX-0.06, QY-0.06, 1.67, 1.67, fill="ffffff", line=LINE, line_w=1.0)
slide.shapes.add_picture(QR_PNG, Inches(LX), Inches(QY), Inches(1.55), Inches(1.55))
tX = LX + 1.78
text(tX, QY + 0.04, LW-1.8, 0.3, [("EXPLORE THE PROJECT", MUTED, True, 10.5)], font="Consolas")
text(tX, QY + 0.34, LW-1.8, 0.4, [(REPO_SHORT, BLUE, True, 14.5)], font="Consolas")
text(tX, QY + 0.78, LW-1.8, 0.5, [("Paper · ", MUTED, False, 11.5), (PAPER, TEXT, False, 11.5)], spacing=1.05)
text(tX, QY + 1.28, LW-1.8, 0.3, [("↗ scan to open", MUTED, False, 10.5)], font="Consolas")

# ===================================================================
#  RIGHT — the demo video (the hero; carries all the detail)
# ===================================================================
DX, DW = 6.62, 6.18
DH_TITLE = 0.32
vw = DW
vh = DW * 9/16
DY = 2.35   # vertically centred against the taller left column
# outer frame
box(DX-0.07, DY-0.07, DW+0.14, DH_TITLE+vh+0.14, fill=BG2, line=LINE, line_w=1.4)
# titlebar
box(DX, DY, DW, DH_TITLE, fill=PANEL2, line=None)
for k, dc in enumerate(["ff5f56", "ffbd2e", "27c93f"]):
    od = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(DX+0.16+k*0.19), Inches(DY+0.11), Inches(0.11), Inches(0.11))
    no_shadow(od); od.fill.solid(); od.fill.fore_color.rgb = C(dc); od.line.fill.background()
text(DX+0.78, DY, DW-1.6, DH_TITLE, [("AProver — verifying AI-generated software", MUTED, False, 10.5)],
     anchor=MSO_ANCHOR.MIDDLE, font="Consolas")
text(DX+DW-1.2, DY, 1.1, DH_TITLE, [("● looping", GREEN, False, 9.5)], anchor=MSO_ANCHOR.MIDDLE, align=PP_ALIGN.RIGHT)

vx, vy = DX, DY + DH_TITLE
poster = POSTER_REAL if os.path.exists(POSTER_REAL) else POSTER_GEN
if os.path.exists(MP4):
    kw = dict(mime_type="video/mp4")
    if os.path.exists(poster): kw["poster_frame_image"] = poster
    slide.shapes.add_movie(MP4, Inches(vx), Inches(vy), Inches(vw), Inches(vh), **kw)
    cap = "▶  embedded — plays in the slideshow"
else:
    pic = slide.shapes.add_picture(POSTER_GEN, Inches(vx), Inches(vy), Inches(vw), Inches(vh))
    pic.click_action.hyperlink.address = "bmc-agent.html"
    cap = "▶  click to play the live demo (bmc-agent.html)"
# ---- real-bugs results strip (under the video) ----
ry = vy + vh + 0.12
text(DX, ry, DW, 0.3,
     [("59", GREEN, True, 18), ("  confirmed memory-safety bugs", TEXT, True, 13.5),
      ("   ·   every one ASan-verified", MUTED, False, 11)],
     align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
text(DX, ry + 0.40, DW, 0.22,
     [("OSS-Fuzz 50   ", BLUE, True, 9.5),
      ("u-boot 32 · gpac 10 · libbej 4 · libredwg 2 · libheif 1 · libarchive 1", MUTED, False, 9.5)],
     align=PP_ALIGN.CENTER, font="Consolas")
text(DX, ry + 0.64, DW, 0.22,
     [("open-source 9   ", VIOLET, True, 9.5),
      ("libmikmod 4 · adplug 3 · libmodplug 2", MUTED, False, 9.5)],
     align=PP_ALIGN.CENTER, font="Consolas")
text(DX, ry + 0.90, DW, 0.2,
     [(cap.split("—")[0].strip() + "  ·  embargoed, coordinated disclosure", "5a6573", False, 8.5)],
     align=PP_ALIGN.CENTER)

prs.save(OUT)
print("wrote", OUT, "(movie embedded)" if os.path.exists(MP4) else "(poster + hyperlink to HTML demo)")
