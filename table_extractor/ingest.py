from PIL import Image

def load_and_prep(image_path: str) -> list[Image.Image]:
    img = Image.open(image_path)
    w, h = img.size
    if w / h > 1.8:
        mid = w // 2
        return [img.crop((0, 0, mid, h)), img.crop((mid, 0, w, h))]
    return [img]

def crop_with_padding(img: Image.Image, bbox: list[float], pad_pct: float = 0.03) -> Image.Image:
    w, h = img.size
    # Convert 0-1000 normalized to absolute pixel coordinates
    x0 = (bbox[0] / 1000.0) * w
    y0 = (bbox[1] / 1000.0) * h
    x1 = (bbox[2] / 1000.0) * w
    y1 = (bbox[3] / 1000.0) * h

    bw = x1 - x0
    bh = y1 - y0
    pad_w = bw * pad_pct
    pad_h = bh * pad_pct

    # Apply padding and clamp to image boundaries
    x0_pad = max(0, int(x0 - pad_w))
    y0_pad = max(0, int(y0 - pad_h))
    x1_pad = min(w, int(x1 + pad_w))
    y1_pad = min(h, int(y1 + pad_h))

    return img.crop((x0_pad, y0_pad, x1_pad, y1_pad))
