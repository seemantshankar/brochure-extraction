from PIL import Image, ImageDraw, ImageFont
from table_extractor.schemas import Region

# Colors for different depth levels
DEPTH_COLORS = {
    0: (240, 128, 128),  # Coral
    1: (30, 144, 255),   # Blue
    2: (0, 128, 128)     # Teal
}

def draw_overlay(img: Image.Image, regions: list[Region]) -> Image.Image:
    overlay = img.copy()
    draw = ImageDraw.Draw(overlay)
    w, h = overlay.size

    # Simple fallback font
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    def draw_box(region: Region):
        # Scale coordinates from 0-1000 back to pixels
        x0 = (region.bbox[0] / 1000.0) * w
        y0 = (region.bbox[1] / 1000.0) * h
        x1 = (region.bbox[2] / 1000.0) * w
        y1 = (region.bbox[3] / 1000.0) * h

        color = DEPTH_COLORS.get(region.depth, (128, 128, 128))
        
        # Draw rectangle outline
        draw.rectangle([x0, y0, x1, y1], outline=color, width=4)
        
        # Draw label at top-left
        label_str = f"{region.id}: {region.label} ({region.region_type.value})"
        if font:
            # Draw label background
            draw.rectangle([x0, y0 - 15, x0 + len(label_str) * 6, y0], fill=color)
            draw.text((x0 + 2, y0 - 13), label_str, fill=(255, 255, 255), font=font)
        else:
            draw.text((x0 + 2, y0 - 13), label_str, fill=color)

        for child in region.children:
            draw_box(child)

    for region in regions:
        draw_box(region)

    return overlay
