"""One physical label per page for the 100 mm thermal roll."""
from PIL import Image as PillowImage
from reportlab.graphics import renderPDF, renderSVG
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing, Image, Line, String
from reportlab.lib.textsplit import wordSplit
from reportlab.lib.units import mm
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth


def thermal_label(bale, qr_url, logo, date, weight, *, preview=False, training=False):
    drawing = Drawing(100 * mm, 150 * mm)

    def text(x, y, value, size, *, bold=False, anchor='middle'):
        drawing.add(String(x * mm, y * mm, value, fontSize=size,
                           fontName='Helvetica-Bold' if bold else 'Helvetica', textAnchor=anchor))

    # Flatten the transparent logo onto the white stock before embedding it in the PDF.
    with PillowImage.open(logo) as source:
        rgba = source.convert('RGBA')
    rgba.thumbnail((640, 640), PillowImage.Resampling.LANCZOS)
    brand = PillowImage.new('RGB', rgba.size, 'white')
    brand.paste(rgba, mask=rgba.getchannel('A'))
    drawing.add(Image(24 * mm, 129 * mm, 52 * mm, 52 * mm * brand.height / brand.width, brand))
    if training:
        text(50, 126, 'SAMPLE BALE - TRAINING', 6.5, bold=True)
    drawing.add(Line(4 * mm, 123 * mm, 96 * mm, 123 * mm, strokeWidth=0.7))
    text(50, 117.5, 'MATERIAL CLASSIFICATION', 9, bold=True)

    # Fit the complete classification, including a single unbroken 100-character name.
    font, width = 'Helvetica-Bold', 92 * mm
    for size in (23, 19, 16, 13, 11, 9):
        lines = []
        for line in simpleSplit(bale['material_name'], font, size, width):
            if stringWidth(line, font, size) > width:
                lines.extend(part for _, part in wordSplit(line, [width], font, size))
            else:
                lines.append(line)
        if len(lines) * size * 1.12 <= 18 * mm:
            break
    baseline = 106 * mm + (len(lines) - 1) * size * .56 - size * .3
    for i, line in enumerate(lines):
        drawing.add(String(50 * mm, baseline - i * size * 1.12, line, fontSize=size,
                           fontName=font, textAnchor='middle'))

    drawing.add(QrCodeWidget(qr_url, x=21 * mm, y=38 * mm, barWidth=58 * mm,
                             barHeight=58 * mm, barBorder=4))
    text(50, 32, bale['code'], 19, bold=True)
    drawing.add(Line(4 * mm, 26 * mm, 96 * mm, 26 * mm, strokeWidth=0.7))
    text(4, 21, 'DATE CREATED', 8, bold=True, anchor='start')
    text(96, 21, 'NET WEIGHT', 8, bold=True, anchor='end')
    text(4, 13, date, 14, bold=True, anchor='start')
    text(96, 13, weight + ' kg', 16, bold=True, anchor='end')
    text(50, 5, 'BinFlow - Label v' + str(bale['version']), 7)
    if preview:
        return renderSVG.drawToString(drawing)
    return renderPDF.drawToString(drawing)
