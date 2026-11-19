"""Permanent bin QR labels on PPS78169 four-up A4 stock."""
from io import BytesIO

import qrcode
from qrcode.image.pil import PilImage
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import registerFont, stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas


def bin_qr(url):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_Q,
                       box_size=8, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    return qr.make_image(image_factory=PilImage).get_image()


# Match the print grid and content proportions in binflow-bin-labels.css.
LABEL_WIDTH, LABEL_HEIGHT = 99.1 * mm, 139 * mm
LEFT, TOP, GUTTER = 4.63 * mm, 9.5 * mm, 2.54 * mm
PADDING_X, PADDING_Y = 6 * mm, 5 * mm
CONTENT_WIDTH = LABEL_WIDTH - 2 * PADDING_X
QR_SIZE = CONTENT_WIDTH * .74
LOGO_WIDTH = CONTENT_WIDTH * .76
FONT = 'BinFlow-Label-Bold'
registerFont(TTFont(FONT, 'VeraBd.ttf'))


def bulk_label_pdf(stock, qr_url, logo_path):
    output = BytesIO()
    pdf = Canvas(output, pagesize=A4, pageCompression=1)
    pdf.setTitle('BinFlow - bulk bin QR labels')
    pdf.setViewerPreference('PrintScaling', 'None')
    logo = ImageReader(str(logo_path))
    width, height = logo.getSize()
    logo_height = LOGO_WIDTH * height / width

    scan_font, small_font, code_font = (CONTENT_WIDTH * n for n in (.08, .05, .10))
    scan_height = QR_SIZE + 1.1 * scan_font + .01 * CONTENT_WIDTH + 1.15 * small_font
    details_height = .02 * CONTENT_WIDTH + 1.15 * (code_font + small_font)
    gap = (LABEL_HEIGHT - 2 * PADDING_Y - logo_height - scan_height - details_height) / 2
    qr_bottom = LABEL_HEIGHT - PADDING_Y - logo_height - gap - QR_SIZE
    # Reuse static artwork and text across every sticker in the register.
    pdf.beginForm('sticker', 0, 0, LABEL_WIDTH, LABEL_HEIGHT)
    pdf.drawImage(logo, (LABEL_WIDTH - LOGO_WIDTH)/2, LABEL_HEIGHT-PADDING_Y-logo_height,
                  LOGO_WIDTH, logo_height, mask='auto')
    pdf.setFont(FONT, scan_font)
    pdf.drawCentredString(LABEL_WIDTH/2, qr_bottom - .88 * scan_font, 'SCAN HERE')
    pdf.setFont(FONT, small_font)
    pdf.drawCentredString(LABEL_WIDTH/2, qr_bottom - 1.1 * scan_font - .01 * CONTENT_WIDTH
                          - .92 * small_font, '1800 672 274')
    pdf.endForm()

    for bin_row in stock:
        # All four stickers reference one lossless QR image at the existing print size.
        qr = ImageReader(bin_qr(qr_url(bin_row['token'])).convert('L'))
        code = bin_row['code']
        fitted = code_font * min(1, CONTENT_WIDTH/stringWidth(code, FONT, code_font))
        for row in range(2):
            for column in range(2):
                pdf.saveState()
                pdf.translate(LEFT + column * (LABEL_WIDTH + GUTTER),
                              A4[1] - TOP - (row+1) * LABEL_HEIGHT)
                pdf.doForm('sticker')
                pdf.drawImage(qr, (LABEL_WIDTH-QR_SIZE)/2, qr_bottom, QR_SIZE, QR_SIZE)
                pdf.setFont(FONT, fitted)
                pdf.drawString(PADDING_X, PADDING_Y + 1.15 * small_font + .23 * code_font, code)
                pdf.setFont(FONT, small_font)
                pdf.drawString(PADDING_X, PADDING_Y + .23 * small_font, f"SIZE - {bin_row['litres']}L")
                pdf.restoreState()
        pdf.showPage()
    pdf.save()
    output.seek(0)
    return output
