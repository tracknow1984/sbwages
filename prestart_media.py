"""Validate and normalise private prestart photos and drawn signatures."""
import io
import json
import math
from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError


def signature_png(raw):
    try:
        if len(raw) > 100000:
            raise ValueError()
        strokes = json.loads(raw)
        if not isinstance(strokes, list) or not 1 <= len(strokes) <= 100:
            raise ValueError()
        points = []
        length = 0
        for stroke in strokes:
            if not isinstance(stroke, list) or not 2 <= len(stroke) <= 3000:
                raise ValueError()
            for point in stroke:
                if not isinstance(point, list) or len(point) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) for v in point):
                    raise ValueError()
                if not 0 <= point[0] <= 600 or not 0 <= point[1] <= 200:
                    raise ValueError()
            points.extend(stroke)
            length += sum(math.dist(a,b) for a,b in zip(stroke,stroke[1:]))
        if len(points) > 6000 or length < 40 or max(p[0] for p in points)-min(p[0] for p in points) < 10 or max(p[1] for p in points)-min(p[1] for p in points) < 5:
            raise ValueError()
        image = Image.new('RGB', (600,200), 'white')
        draw = ImageDraw.Draw(image)
        for stroke in strokes:
            draw.line([tuple(p) for p in stroke], fill='#111827', width=3, joint='curve')
        output=io.BytesIO(); image.save(output,format='PNG')
        return output.getvalue()
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ValueError('Draw your signature in the signature box, then sign the declaration.') from None


def photo_jpeg(upload):
    data=upload.read(5*1024*1024+1)
    if not data or len(data)>5*1024*1024:
        raise ValueError('Each photo must be no larger than 5 MB.')
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ('JPEG','PNG','WEBP') or image.width*image.height>20000000:
                raise ValueError()
            image.load()
            image=ImageOps.exif_transpose(image)
            image.thumbnail((1600,1600))
            image=image.convert('RGB')
            output=io.BytesIO();image.save(output,format='JPEG',quality=85)
            return output.getvalue()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        raise ValueError('Upload a valid JPG, PNG or WebP photo, up to 20 megapixels.') from None
