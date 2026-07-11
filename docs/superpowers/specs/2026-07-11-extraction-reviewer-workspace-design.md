# Extraction Reviewer Workspace Design

## Purpose

Replace the extracted-HTML index and per-page reading flow with a review workspace for a human to compare a brochure page against its extraction, correct discrepancies, and save the corrected rendered HTML.

The workspace is for visual quality assurance, not for reproducing the source brochure design. The original page image remains the visual reference; the extracted content is presented as a readable, editable semantic document.

## User experience

### Workspace layout

- Extraction completion opens `/review/<session_id>?page=0`.
- A fixed-height application shell uses nearly the full viewport below the header.
- A horizontal thumbnail strip appears below the workspace title and above both panes. Selecting a thumbnail changes the page image and extracted HTML together.
- The main area contains a left source pane and right extraction pane separated by a draggable vertical divider.
- Each pane has a minimum width. At narrow viewport widths, panes stack vertically rather than clipping content.
- The divider is keyboard reachable, has `role="separator"`, reports its percentage via ARIA values, and responds to left/right arrow keys. The chosen width is retained in browser storage.

### Source pane

- The source pane uses the annotation page's existing canvas viewer, rather than a new zoom/pan implementation.
- It reuses fit-to-view initialization, pointer-anchored wheel/trackpad zoom, drag-to-pan, resize handling, and the existing zoom controls.
- Review mode is read-only: crop drawing, selection, and crop manipulation are not included.
- Resizing the outer pane triggers the viewer's existing sizing/fit calculation without changing the reviewer's current zoom/pan unnecessarily.

### Extracted-content pane

- The right pane displays the current page's existing rendered extraction in an iframe with an embedded-review mode.
- The embedded view hides standalone page navigation and removes the standalone document offset, but keeps the existing inline editing and save behavior.
- Content scrolls vertically inside the pane. Paragraphs, list items, table cells, headings, and long unbroken values wrap; the pane does not require horizontal scrolling to read text.
- Existing in-place editing remains the interaction model. Edited values are highlighted until saving succeeds.
- A save action remains visible while the page scrolls. On success, edited markers clear and a confirmation appears. On failure, edits remain in place and an error is announced.

## Architecture

### Shared source-image viewer

Extract the annotation canvas's image-viewing responsibilities into a reusable JavaScript module with two modes:

- `annotate`: current crop drawing/manipulation behavior;
- `review`: image loading, fit, zoom, pan, and controls only.

The module owns canvas state and exposes a layout-resize method. The annotation page becomes a consumer of `annotate` mode, and the reviewer page becomes a consumer of `review` mode. This prevents behavior drift between the two screens.

### Reviewer route and page selection

`crop_app.app` adds a protected reviewer route which verifies the session and completed extraction, loads session metadata, and passes page image paths and page count to a dedicated template.

The client keeps the selected page in the URL query string and updates both panes on thumbnail selection. Browser back/forward restores the corresponding page. The page image uses the existing `/pages/<session_id>/<filename>` endpoint; the right iframe targets the existing `/extracted/<session_id>/page-<index>.html` endpoint with embedded-review mode enabled.

### Embedded rendered page mode

The generated per-page HTML recognizes an `embed=1` query parameter. In that mode it:

- adds an embedded-mode class to the document;
- hides previous/next navigation;
- removes the standalone canvas margin and viewport layout restrictions;
- retains editable selectors, changed-state highlighting, and the existing save request.

The current page save endpoint continues to own authorization, page-index validation, and persistence. No HTML editor or new save API is introduced.

### Styling responsibilities

The reviewer template owns the shell, thumbnails, panels, divider, and responsive layout. The generated-page stylesheet owns readable extracted content: typography, tables, links, footnotes, and word wrapping. Embedded overrides are limited to iframe containment so standalone exports remain usable.

## Error handling and accessibility

- Missing sessions, incomplete extraction, unavailable page images, and unavailable extraction pages show a clear in-context error without breaking the other pane.
- Iframe load failure offers a retry action for the extraction pane.
- The divider follows WAI-ARIA interactive separator semantics; pointer resizing and keyboard resizing use the same bounds.
- Zoom controls, thumbnails, save action, and error/retry actions have visible focus states and accessible names.
- The source image viewer retains button controls so zoom does not depend exclusively on gesture input.

## Verification

Automated coverage will verify:

- reviewer access only for existing sessions with completed extraction;
- page index validation and initial page selection;
- page-image and embedded-page URL construction;
- embedded mode retains the editor and save URL while hiding standalone navigation;
- generated text/table wrapping rules and no horizontal overflow configuration;
- divider ARIA and keyboard/pointer resize behavior;
- annotation behavior continues to pass after viewer extraction.

Manual browser verification will cover trackpad/mouse zoom and pan, divider resizing and reflow, thumbnail switching, inline edits, save success/failure, and narrow-screen stacking.

## Out of scope

- Pixel-for-pixel recreation of brochure artwork in extracted HTML.
- A raw HTML/source-code editor.
- Automatic discrepancy detection or reviewer approval workflow.
- Changing the extraction model or extraction prompts.
