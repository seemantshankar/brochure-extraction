import json
import random
import re
from PIL import Image
from table_extractor.schemas import Region, RegionType, ExtractedContent
from table_extractor.extract import extract_content

def flatten_columns(columns: list) -> list[str]:
    if not columns:
        return []
    flat = []
    for col in columns:
        if isinstance(col, str):
            flat.append(col)
        elif isinstance(col, dict):
            # Check for explicit name/group structure
            if "group" in col or "name" in col or "sub_columns" in col or "columns" in col:
                group_name = col.get("group") or col.get("name") or ""
                sub_list = col.get("sub_columns") or col.get("columns") or []
                if not isinstance(sub_list, list):
                    if isinstance(sub_list, dict):
                        sub_list = [sub_list]
                    else:
                        sub_list = [str(sub_list)]
                flat_sub = flatten_columns(sub_list)
                for sub in flat_sub:
                    if group_name:
                        flat.append(f"{group_name} > {sub}")
                    else:
                        flat.append(sub)
            else:
                # If dict keys are groupings, recursively flatten their values
                for k, v in col.items():
                    group_name = k
                    # Normalize value to a list
                    if isinstance(v, list):
                        sub_list = v
                    elif isinstance(v, dict):
                        sub_list = [v]
                    else:
                        sub_list = [str(v)]
                    
                    flat_sub = flatten_columns(sub_list)
                    for sub in flat_sub:
                        if group_name:
                            flat.append(f"{group_name} > {sub}")
                        else:
                            flat.append(sub)
        elif isinstance(col, list):
            flat.extend(flatten_columns(col))
        else:
            flat.append(str(col))
    return flat


def _lookup_path(data, path_parts: list[str]):
    if not path_parts:
        return data
    if not isinstance(data, dict):
        return None
        
    part = path_parts[0]
    if part in data:
        res = _lookup_path(data[part], path_parts[1:])
        if res is not None:
            return res
            
    part_lower = part.lower()
    for k, v in data.items():
        if k.lower() == part_lower:
            res = _lookup_path(v, path_parts[1:])
            if res is not None:
                return res
    return None

def _get_row_value(row: dict, col: str) -> str:
    if not isinstance(row, dict):
        return ""
    
    parts = [p.strip() for p in col.split(">")]
    if not parts:
        return ""
        
    # 1. Try matching suffixes of the column path (longest to shortest)
    for i in range(len(parts)):
        subpath = parts[i:]
        res = _lookup_path(row, subpath)
        if res is not None and not isinstance(res, dict):
            return str(res)

    # 2. Try looking up using elements of parts from right to left
    for p in reversed(parts):
        if p in row:
            return str(row[p])
        # Case insensitive match
        p_lower = p.lower()
        for k, v in row.items():
            if k.lower() == p_lower:
                return str(v)

    # 3. Check for sub-variant keys matching sub-tokens of the column header (e.g., LXi MT matching LXi)
    # Split the column header on commas, slashes, spaces to find base tokens
    col_text = col.lower()
    matches = []
    # Ignore common structural keys like 'section', 'features & specifications'
    for k, v in row.items():
        if k.lower() in ('section', 'features & specifications', 'features and specifications'):
            continue
        # Split key like 'LXi MT' -> ['lxi', 'mt']
        k_tokens = [t.strip().lower() for t in re.split(r'[\s/,\-\+]+', k) if t.strip()]
        if not k_tokens:
            continue
        # Check if the main variant identifier (e.g., LXi, VXi, ZXi) is present in the column path text
        # (typically the first token of the key is the variant label)
        main_token = k_tokens[0]
        # Match word boundaries or substring
        if re.search(r'\b' + re.escape(main_token) + r'\b', col_text):
            matches.append(f"{k}: {v}")
    
    if matches:
        return " / ".join(matches)

    # 4. Recursive leaf lookup as final fallback
    leaf = parts[-1]
    def _find_leaf(d, key: str):
        if not isinstance(d, dict):
            return None
        if key in d:
            return d[key]
        key_lower = key.lower()
        for k, v in d.items():
            if k.lower() == key_lower:
                return v
        for k, v in d.items():
            res = _find_leaf(v, key)
            if res is not None:
                return res
        return None
        
    res = _find_leaf(row, leaf)
    if res is not None and not isinstance(res, dict):
        return str(res)
        
    return ""

def _extract_markers_from_value(val: str) -> list[str]:
    # Look for typical footnote markers like *, **, ^, ^^, #, ##, etc.
    if not isinstance(val, str):
        return []
    
    # Strip markdown bolding **, italics *, and links [...] before matching
    # 1. Links [text](url) -> text, and [text] -> text
    val = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", val)
    val = re.sub(r"\[([^\]]*)\]", r"\1", val)
    # 2. Bolding **text** -> text
    val = re.sub(r"\*\*([^*]+)\*\*", r"\1", val)
    # 3. Italics *text* -> text
    val = re.sub(r"\*([^*]+)\*", r"\1", val)
    
    # Match markers attached to words/numbers
    matches = re.findall(r"(?<=[a-zA-Z0-9])([\*\^\#]+)(?![a-zA-Z0-9])", val)
    return matches

def resolve_footnotes(regions: list[Region]) -> None:
    # Find footnote blocks and construct marker registry
    footnote_registry = {}
    for region in regions:
        if region.extracted and region.region_type == RegionType.FOOTNOTE_BLOCK:
            items = region.extracted.items_json or []
            for item in items:
                marker = item.get("marker")
                text = item.get("text")
                if marker:
                    footnote_registry[marker.strip()] = text.strip()

    # Iterate over all regions and attempt to parse and resolve footnote markers
    for region in regions:
        if not region.extracted or region.region_type == RegionType.FOOTNOTE_BLOCK:
            continue

        found_markers = set()
        # Check in table row/column text
        if region.extracted.table_json:
            cols = region.extracted.table_json.get("columns", [])
            flat_cols = flatten_columns(cols)
            rows = region.extracted.table_json.get("rows", [])
            for col in flat_cols:
                found_markers.update(_extract_markers_from_value(col))
            
            # Recursively find all string values in rows to find footnote markers
            def get_all_str_values(obj) -> list[str]:
                res = []
                if isinstance(obj, str):
                    res.append(obj)
                elif isinstance(obj, dict):
                    for v in obj.values():
                        res.extend(get_all_str_values(v))
                elif isinstance(obj, list):
                    for v in obj:
                        res.extend(get_all_str_values(v))
                return res

            for row in rows:
                for val in get_all_str_values(row):
                    found_markers.update(_extract_markers_from_value(val))

        # Check in markdown text
        if region.extracted.markdown:
            found_markers.update(_extract_markers_from_value(region.extracted.markdown))

        # Attach found markers
        region.extracted.footnote_markers = list(found_markers)
        
        # Compute resolutions
        resolutions = {}
        for marker in found_markers:
            m_clean = marker.strip()
            if m_clean in footnote_registry:
                resolutions[marker] = footnote_registry[m_clean]
            else:
                resolutions[marker] = "WARNING: Unresolved footnote text"

        # Store footnote resolution inside usage details dict or custom print maps
        region.extracted.usage["footnote_resolutions"] = resolutions

def self_consistency_check(regions: list[Region], img: Image.Image, verification_model: str, sample_rate: float = 0.2, force: bool = False) -> None:
    # Filter atomic regions with extracted content
    eligible = [r for r in regions if r.extracted and not r.may_contain_subregions]
    if not eligible:
        return

    sample_size = max(1, int(len(eligible) * sample_rate))
    # Use deterministic local random instance
    sample = random.Random(42).sample(eligible, sample_size)

    # Import ingest helper here to avoid circular imports
    from table_extractor.ingest import crop_with_padding

    for region in sample:
        crop = crop_with_padding(img, region.bbox, pad_pct=0.03)
        # Call API under the 'extract_verify' caching key using the VERIFICATION_MODEL
        try:
            verify_content = extract_content(
                crop=crop,
                region=region,
                model=verification_model,
                stage_name="extract_verify",
                force=force
            )
            
            # Simple diff verification comparison (comparing schemas field-by-field, including drawing_json)
            primary_data = (
                region.extracted.table_json 
                or region.extracted.items_json 
                or region.extracted.markdown 
                or region.extracted.drawing_json
            )
            verify_data = (
                verify_content.table_json 
                or verify_content.items_json 
                or verify_content.markdown 
                or verify_content.drawing_json
            )
            
            if json_dumps_hash(primary_data) != json_dumps_hash(verify_data):
                region.extracted.confidence_flag = True
        except Exception:
            # Fallback mark confidence false if API verification call failed
            region.extracted.confidence_flag = True

def json_dumps_hash(data) -> str:
    if not data:
        return ""
    try:
        return json.dumps(data, sort_keys=True)
    except Exception:
        return str(data)

def _table_to_pipe_table(table_dict: dict) -> str:
    cols = table_dict.get("columns", [])
    rows = table_dict.get("rows", [])
    if not cols:
        return ""
    
    flat_cols = flatten_columns(cols)
    if not flat_cols:
        return ""
    
    lines = []
    # Column headers
    lines.append("| " + " | ".join(flat_cols) + " |")
    # Divider
    lines.append("| " + " | ".join("---" for _ in flat_cols) + " |")
    
    # Rows
    for row in rows:
        row_vals = []
        for col in flat_cols:
            val = _get_row_value(row, col)
            row_vals.append(str(val))
        lines.append("| " + " | ".join(row_vals) + " |")
        
    return "\n".join(lines)

def to_markdown(regions: list[Region]) -> str:
    md_blocks = []

    def walk(region: Region, path_prefix: str = ""):
        current_path = f"{path_prefix} > {region.label}" if path_prefix else region.label
        
        if region.region_type == RegionType.SECTION_HEADING:
            md_blocks.append(f"\n## {current_path} (ID: {region.id}, Type: {region.region_type.value})")
        elif region.extracted:
            md_blocks.append(f"\n## {current_path} (ID: {region.id}, Type: {region.region_type.value})")
            
            content = region.extracted
            # 1. Ruled/Section Grouped Tables
            if content.table_json:
                md_blocks.append(_table_to_pipe_table(content.table_json))
            
            # 2. Markdown text (bullets)
            elif content.markdown:
                md_blocks.append(content.markdown)
                
            # 3. Item List (Swatches, Stat cards, Footnotes)
            elif content.items_json:
                for idx, item in enumerate(content.items_json):
                    item_str = ", ".join(f"{k}: {v}" for k, v in item.items())
                    md_blocks.append(f"- Item {idx+1}: {item_str}")

            # 4. Technical Drawings
            elif content.drawing_json:
                draw = content.drawing_json
                md_blocks.append(f"Technical Drawing view: **{draw.get('view')}**")
                for m in draw.get("measurements", []):
                    md_blocks.append(f"- {m.get('label')}: {m.get('value')} {m.get('unit')}")

            # Append resolved footnotes details if present
            resolutions = content.usage.get("footnote_resolutions")
            if resolutions:
                md_blocks.append("\n**Footnotes:**")
                for marker, text in resolutions.items():
                    md_blocks.append(f"* **{marker}**: {text}")

            # Confidence flag warning
            if content.confidence_flag:
                md_blocks.append("\n> [!WARNING]\n> Self-consistency verification check disagreed on this region extraction. Human QA suggested.")

        # Sort children by y0, then x0
        sorted_children = sorted(region.children, key=lambda r: (r.bbox[1], r.bbox[0]))
        for child in sorted_children:
            walk(child, current_path)

    # Sort top-level elements by y0, then x0
    sorted_top_regions = sorted(
        [r for r in regions if r.parent_id is None],
        key=lambda r: (r.bbox[1], r.bbox[0])
    )
    for region in sorted_top_regions:
        walk(region)

    return "\n".join(md_blocks)

