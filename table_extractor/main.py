import argparse
import json
import os

def load_env():
    # Find .env in project root (parent directory of table_extractor)
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    env_path = os.path.join(project_root, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    os.environ[k] = v
    
    # If OPENROUTER_API_KEY is active, and OPENAI_BASE_URL is pointing to a local loopback/proxy,
    # remove it so client defaults back to OpenRouter's production endpoint.
    if "OPENROUTER_API_KEY" in os.environ and "OPENAI_BASE_URL" in os.environ:
        url = os.environ["OPENAI_BASE_URL"]
        if "127.0.0.1" in url or "localhost" in url:
            del os.environ["OPENAI_BASE_URL"]

load_env()

from table_extractor import ingest, detect, extract, reconcile, render, snap  # noqa: E402

# Model selection — all loaded from .env; no hardcoded defaults
DEFAULT_DETECTION_MODEL = os.environ.get("STRUCTURE_DETECTION_MODEL_ID")
DEFAULT_EXTRACTION_MODEL = os.environ.get("DATA_EXTRACTION_MODEL_ID")
DEFAULT_VERIFICATION_MODEL = os.environ.get("VERIFICATION_MODEL_ID")

def _flatten(regions: list) -> list:
    flat = []
    for r in regions:
        flat.append(r)
        if r.children:
            flat.extend(_flatten(r.children))
    return flat

def run_pipeline(
    image_path: str,
    out_dir: str,
    detection_model: str,
    extraction_model: str,
    verification_model: str,
    force_from: str = None,
    detect_only: bool = False,
    refinement_method: str = "zoom"
):
    os.makedirs(out_dir, exist_ok=True)
    
    # Determine cache invalidation layers
    force_detect = force_from in ("detect",)
    force_extract = force_from in ("detect", "extract")
    force_reconcile = force_from in ("detect", "extract", "reconcile")

    # 1. Ingestion
    print(f"[*] Ingesting: {image_path}")
    page_images = ingest.load_and_prep(image_path)
    print(f"[*] Extracted {len(page_images)} page(s) from spreads.")

    for page_idx, img in enumerate(page_images):
        suffix = f"_page{page_idx}" if len(page_images) > 1 else ""
        print(f"[*] Processing page {page_idx}...")

        # 2. Coarse detection
        top_regions = detect.detect_regions(img, detection_model, force=force_detect)

        # 3. Recursive subregion detection and zoomed refinement
        # 3. Recursive subregion detection and zoomed refinement
        if refinement_method in ("zoom", "hybrid"):
            def refine_zoom_recursive(img, regions, model, force):
                for j, sr in enumerate(regions):
                    sr_should_refine = sr.region_type in (detect.RegionType.RULED_TABLE, detect.RegionType.SECTION_GROUPED_TABLE, detect.RegionType.TECHNICAL_DRAWING)
                    if sr_should_refine:
                        refined_sr = detect.refine_region_zoom(img, sr, model, force=force)
                        refined_sr.children = sr.children
                        regions[j] = refined_sr
                        sr = regions[j]
                    if sr.children:
                        refine_zoom_recursive(img, sr.children, model, force)

            for i, r in enumerate(top_regions):
                should_refine = r.region_type in (detect.RegionType.RULED_TABLE, detect.RegionType.SECTION_GROUPED_TABLE, detect.RegionType.TECHNICAL_DRAWING)
                if should_refine:
                    top_regions[i] = detect.refine_region_zoom(img, r, detection_model, force=force_detect)
                    r = top_regions[i]

                if r.may_contain_subregions or should_refine:
                    # Temporarily enable subregion flag so detect_subregions runs
                    r.may_contain_subregions = True
                    sub = detect.detect_subregions(img, r, detection_model, depth=1, max_depth=2, force=force_detect)
                    r.children = sub
                    if not sub:
                        r.may_contain_subregions = False
                    else:
                        refine_zoom_recursive(img, r.children, detection_model, force_detect)

        if refinement_method == "ocr":
            print("[*] Running OCR-based bounding box refinement...")
            snap.snap_regions(img, top_regions)
        elif refinement_method == "hybrid":
            print("[*] Running hybrid refinement (Zoom + OCR Snapping)...")
            snap.snap_regions(img, top_regions)

        flat_regions = _flatten(top_regions)

        # Save individual region crops for analysis/debugging
        crops_dir = os.path.join(out_dir, "crops")
        os.makedirs(crops_dir, exist_ok=True)
        for region in flat_regions:
            crop = ingest.crop_with_padding(img, region.bbox, pad_pct=0.03)
            crop_filename = f"page{page_idx}_{region.id}_{region.region_type.value}.png"
            crop_path = os.path.join(crops_dir, crop_filename)
            crop.save(crop_path)

        if not detect_only:
            # 4. Regional Extraction
            print(f"[*] Extracting content for {len(flat_regions)} regions...")
            for region in flat_regions:
                if region.may_contain_subregions:
                    print(f"[*] Skipping extraction for container region: {region.id} ({region.label})")
                    continue
                crop = ingest.crop_with_padding(img, region.bbox, pad_pct=0.03)
                content = extract.extract_content(crop, region, extraction_model, force=force_extract)
                region.extracted = content

            # 5. Reconciliation (Footnotes + Consistency sampling check)
            print("[*] Reconciling footnotes & performing self-consistency verify...")
            reconcile.resolve_footnotes(flat_regions)
            reconcile.self_consistency_check(
                regions=flat_regions,
                img=img,
                verification_model=verification_model,
                sample_rate=0.2,
                force=force_reconcile
            )
        else:
            print("[*] skipping extraction and reconciliation as --detect-only is enabled.")

        # 6. Render overlay & save
        overlay = render.draw_overlay(img, top_regions)
        overlay_path = os.path.join(out_dir, f"overlay{suffix}.png")
        overlay.save(overlay_path)
        print(f"[+] Overlay saved: {overlay_path}")

        # 7. Convert to Markdown and save (rendering whatever is available)
        markdown_output = reconcile.to_markdown(top_regions)
        md_path = os.path.join(out_dir, f"extraction{suffix}.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(markdown_output)
        print(f"[+] Extraction markdown saved: {md_path}")

        # 8. Save structured regions JSON tree
        regions_json = [r.model_dump() for r in top_regions]
        json_path = os.path.join(out_dir, f"regions{suffix}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(regions_json, f, indent=2)
        print(f"[+] Structured regions JSON saved: {json_path}")

    # Print total usage metrics at the end of the run
    det_usage = detect.get_total_usage()
    ext_usage = extract.get_total_usage()
    print("\n" + "="*40)
    print("TOTAL PIPELINE USAGE METRICS:")
    print("  Detection API Calls:")
    print(f"    Prompt Tokens:     {det_usage.get('prompt_tokens', 0)}")
    print(f"    Completion Tokens: {det_usage.get('completion_tokens', 0)}")
    print(f"    Estimated Cost:    ${det_usage.get('cost_usd', 0.0):.6f}")
    if not detect_only:
        print("  Extraction/Verification API Calls:")
        print(f"    Prompt Tokens:     {ext_usage.get('prompt_tokens', 0)}")
        print(f"    Completion Tokens: {ext_usage.get('completion_tokens', 0)}")
        print(f"    Estimated Cost:    ${ext_usage.get('cost_usd', 0.0):.6f}")
        print(f"  Combined Cost:       ${(det_usage.get('cost_usd', 0.0) + ext_usage.get('cost_usd', 0.0)):.6f}")
    else:
        print(f"  Combined Cost:       ${det_usage.get('cost_usd', 0.0):.6f}")
    print("="*40 + "\n")

def main():
    parser = argparse.ArgumentParser(description="Brochure Table/Region Extraction Pipeline (v0 Prototype)")
    parser.add_argument("--image", required=True, help="Path to input brochure sheet image")
    parser.add_argument("--out", required=True, help="Output directory to save artifacts")
    parser.add_argument("--detection-model", default=DEFAULT_DETECTION_MODEL, help="Model to use for region detection")
    parser.add_argument("--extraction-model", default=DEFAULT_EXTRACTION_MODEL, help="Model to use for extraction")
    parser.add_argument("--verification-model", default=DEFAULT_VERIFICATION_MODEL, help="Model to use for self-consistency verify check")
    parser.add_argument("--force-from", choices=["detect", "extract", "reconcile"], help="Nuke cache downstream of this stage")
    parser.add_argument("--detect-only", action="store_true", help="Only run ingestion, region detection and overlay rendering (skip text extraction)")
    parser.add_argument("--refinement-method", choices=["none", "zoom", "ocr", "hybrid"], default="hybrid", help="Bbox refinement method to use")
    
    args = parser.parse_args()

    if not os.environ.get("OPENROUTER_API_KEY"):
        raise ValueError("Missing environment variable: OPENROUTER_API_KEY. Please set it before running the pipeline.")
    
    run_pipeline(
        image_path=args.image,
        out_dir=args.out,
        detection_model=args.detection_model,
        extraction_model=args.extraction_model,
        verification_model=args.verification_model,
        force_from=args.force_from,
        detect_only=args.detect_only,
        refinement_method=args.refinement_method
    )

if __name__ == "__main__":
    main()
