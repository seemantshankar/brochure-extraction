document.addEventListener("DOMContentLoaded", () => {
  const canvas = document.getElementById("annotation-canvas");
  const ctx = canvas.getContext("2d");
  const container = document.getElementById("canvas-container");
  const pageThumbs = document.getElementById("page-thumbs");
  const zoomInBtn = document.getElementById("zoom-in-btn");
  const zoomOutBtn = document.getElementById("zoom-out-btn");
  const zoomLevel = document.getElementById("zoom-level");

  const { sessionId, pageData, allPages } = window.APP_DATA;
  const pageIndex = parseInt(new URLSearchParams(window.location.search).get("page")) || 0;

  const ZOOM_LEVELS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0];
  const PADDING = 20;
  const DPR = window.devicePixelRatio || 1;

  const state = {
    image: null,
    imgW: 0,
    imgH: 0,
    canvasW: 0,
    canvasH: 0,
    zoom: 1.0,
    panX: 0,
    panY: 0,
    isPanning: false,
    panStartX: 0,
    panStartY: 0,
    panOriginX: 0,
    panOriginY: 0,
    spaceHeld: false,
    boxes: [],
    canvasFocused: false,
    mode: "idle",
    nextBoxId: 1,
    interaction: null,
  };

  function screenToNormalized(sx, sy, imgW, imgH, zoom, panX, panY) {
    const x = (sx - panX) / zoom / imgW;
    const y = (sy - panY) / zoom / imgH;
    return [x, y];
  }

  function normalizedToScreen(nx, ny, imgW, imgH, zoom, panX, panY) {
    const x = nx * imgW * zoom + panX;
    const y = ny * imgH * zoom + panY;
    return [x, y];
  }

  window.canvasState = state;
  window.screenToNormalized = screenToNormalized;
  window.normalizedToScreen = normalizedToScreen;

  function getCursorZone(nx, ny, boxes, zoom, panX, panY, imgW, imgH) {
    const HANDLE_GAP = 12, HANDLE_W = 24, HANDLE_H = 10;
    const CORNER_HIT = 14, EDGE_HIT = 8;
    const BTN_SIZE = 14, BTN_MARGIN = 4;

    for (let i = boxes.length - 1; i >= 0; i--) {
      const b = boxes[i];
      const [sx0, sy0] = normalizedToScreen(b.x0, b.y0, imgW, imgH, zoom, panX, panY);
      const [sx1, sy1] = normalizedToScreen(b.x1, b.y1, imgW, imgH, zoom, panX, panY);
      const [csx, csy] = normalizedToScreen(nx, ny, imgW, imgH, zoom, panX, panY);

      if (b.selected) {
        const dBtnX0 = sx1 - BTN_SIZE - BTN_MARGIN;
        const dBtnY0 = sy0 + BTN_MARGIN;
        if (
          csx >= dBtnX0 && csx <= dBtnX0 + BTN_SIZE &&
          csy >= dBtnY0 && csy <= dBtnY0 + BTN_SIZE
        ) {
          return { boxIndex: i, zone: "delete" };
        }
      }

      const handleCX = (sx0 + sx1) / 2;
      const handleCY = sy0 - HANDLE_GAP - HANDLE_H / 2;

      if (
        csx >= handleCX - HANDLE_W / 2 - 6 &&
        csx <= handleCX + HANDLE_W / 2 + 6 &&
        csy >= handleCY - HANDLE_H / 2 - 6 &&
        csy <= handleCY + HANDLE_H / 2 + 6
      ) {
        return { boxIndex: i, zone: "handle" };
      }

      const corners = [
        { name: "NW", x: sx0, y: sy0 },
        { name: "NE", x: sx1, y: sy0 },
        { name: "SW", x: sx0, y: sy1 },
        { name: "SE", x: sx1, y: sy1 },
      ];
      for (const c of corners) {
        if (Math.abs(csx - c.x) <= CORNER_HIT / 2 && Math.abs(csy - c.y) <= CORNER_HIT / 2) {
          return { boxIndex: i, zone: "corner", corner: c.name };
        }
      }

      const inCornerZone = corners.some(c =>
        Math.abs(csx - c.x) <= CORNER_HIT / 2 && Math.abs(csy - c.y) <= CORNER_HIT / 2
      );
      if (!inCornerZone) {
        if (csx >= sx0 && csx <= sx1) {
          if (Math.abs(csy - sy0) <= EDGE_HIT / 2) return { boxIndex: i, zone: "edge", edge: "N" };
          if (Math.abs(csy - sy1) <= EDGE_HIT / 2) return { boxIndex: i, zone: "edge", edge: "S" };
        }
        if (csy >= sy0 && csy <= sy1) {
          if (Math.abs(csx - sx0) <= EDGE_HIT / 2) return { boxIndex: i, zone: "edge", edge: "W" };
          if (Math.abs(csx - sx1) <= EDGE_HIT / 2) return { boxIndex: i, zone: "edge", edge: "E" };
        }
      }
    }
    return null;
  }

  function getCursorStyle(zone) {
    if (!zone) return "crosshair";
    if (zone.zone === "delete") return "pointer";
    if (zone.zone === "handle") return "grab";
    if (zone.zone === "corner") {
      if (zone.corner === "NW" || zone.corner === "SE") return "nwse-resize";
      return "nesw-resize";
    }
    if (zone.zone === "edge") {
      if (zone.edge === "N" || zone.edge === "S") return "ns-resize";
      return "ew-resize";
    }
    return "crosshair";
  }

  function resizeCanvas() {
    const rect = container.getBoundingClientRect();
    state.canvasW = rect.width;
    state.canvasH = rect.height;
    canvas.width = rect.width * DPR;
    canvas.height = rect.height * DPR;
    canvas.style.width = rect.width + "px";
    canvas.style.height = rect.height + "px";
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  }

  function fitToViewport() {
    if (!state.image) return;
    const maxW = state.canvasW - PADDING * 2;
    const maxH = state.canvasH - PADDING * 2;
    const scaleX = maxW / state.imgW;
    const scaleY = maxH / state.imgH;
    state.zoom = Math.min(scaleX, scaleY);
    state.panX = (state.canvasW - state.imgW * state.zoom) / 2;
    state.panY = (state.canvasH - state.imgH * state.zoom) / 2;
  }

  function render() {
    ctx.clearRect(0, 0, state.canvasW, state.canvasH);
    if (!state.image) return;

    ctx.save();
    ctx.translate(state.panX, state.panY);
    ctx.scale(state.zoom, state.zoom);
    ctx.drawImage(state.image, 0, 0, state.imgW, state.imgH);

    const invZ = 1 / state.zoom;

    for (let i = 0; i < state.boxes.length; i++) {
      const b = state.boxes[i];
      const bx = b.x0 * state.imgW;
      const by = b.y0 * state.imgH;
      const bw = (b.x1 - b.x0) * state.imgW;
      const bh = (b.y1 - b.y0) * state.imgH;

      ctx.strokeStyle = b.selected ? "rgba(233,69,96,0.9)" : "rgba(79,195,247,0.7)";
      ctx.lineWidth = (b.selected ? 2.5 : 2) * invZ;
      ctx.strokeRect(bx, by, bw, bh);

      if (b.selected || b.hovered) {
        drawDragHandle(ctx, bx, by, bw, invZ);
      }

      if (b.selected) {
        drawDeleteBtn(ctx, bx, by, bw, invZ);
      }
    }

    ctx.restore();
  }

  function drawDragHandle(ctx, bx, by, bw, invZ) {
    const HANDLE_GAP = 12 * invZ;
    const HANDLE_W = 24 * invZ;
    const HANDLE_H = 10 * invZ;
    const cx = bx + bw / 2;
    const hy = by - HANDLE_GAP - HANDLE_H;

    ctx.save();
    ctx.fillStyle = b_selected_fill();
    ctx.strokeStyle = "rgba(233,69,96,0.9)";
    ctx.lineWidth = 1.5 * invZ;

    ctx.beginPath();
    ctx.roundRect(cx - HANDLE_W / 2, hy, HANDLE_W, HANDLE_H, 3 * invZ);
    ctx.fill();
    ctx.stroke();

    ctx.strokeStyle = "rgba(255,255,255,0.8)";
    ctx.lineWidth = 1 * invZ;
    const lineY1 = hy + HANDLE_H * 0.25;
    const lineY2 = hy + HANDLE_H * 0.5;
    const lineY3 = hy + HANDLE_H * 0.75;
    const lx = cx - HANDLE_W * 0.3;
    const lw = HANDLE_W * 0.6;
    for (const ly of [lineY1, lineY2, lineY3]) {
      ctx.beginPath();
      ctx.moveTo(lx, ly);
      ctx.lineTo(lx + lw, ly);
      ctx.stroke();
    }
    ctx.restore();
  }

  function b_selected_fill() {
    if (state.boxes.some(b => b.selected)) return "rgba(233,69,96,0.3)";
    return "rgba(79,195,247,0.3)";
  }

  function drawDeleteBtn(ctx, bx, by, bw, invZ) {
    const SIZE = 14 * invZ;
    const MARGIN = 4 * invZ;
    const dx = bx + bw - SIZE - MARGIN;
    const dy = by + MARGIN;

    ctx.save();
    ctx.fillStyle = "rgba(233,69,96,0.85)";
    ctx.beginPath();
    ctx.roundRect(dx, dy, SIZE, SIZE, 2 * invZ);
    ctx.fill();

    ctx.strokeStyle = "#fff";
    ctx.lineWidth = 1.5 * invZ;
    const pad = SIZE * 0.25;
    ctx.beginPath();
    ctx.moveTo(dx + pad, dy + pad);
    ctx.lineTo(dx + SIZE - pad, dy + SIZE - pad);
    ctx.moveTo(dx + SIZE - pad, dy + pad);
    ctx.lineTo(dx + pad, dy + SIZE - pad);
    ctx.stroke();
    ctx.restore();
  }

  function updateZoomDisplay() {
    zoomLevel.textContent = Math.round(state.zoom * 100) + "%";
  }

  function getZoomIndex(zoom) {
    let closest = 0;
    let minDist = Infinity;
    for (let i = 0; i < ZOOM_LEVELS.length; i++) {
      const dist = Math.abs(ZOOM_LEVELS[i] - zoom);
      if (dist < minDist) {
        minDist = dist;
        closest = i;
      }
    }
    return closest;
  }

  function zoomTo(newZoom, centerX, centerY) {
    newZoom = Math.max(ZOOM_LEVELS[0], Math.min(ZOOM_LEVELS[ZOOM_LEVELS.length - 1], newZoom));
    if (centerX === undefined) {
      centerX = state.canvasW / 2;
      centerY = state.canvasH / 2;
    }

    const imgX = (centerX - state.panX) / state.zoom;
    const imgY = (centerY - state.panY) / state.zoom;

    state.zoom = newZoom;

    state.panX = centerX - imgX * state.zoom;
    state.panY = centerY - imgY * state.zoom;

    updateZoomDisplay();
    render();
  }

  function zoomIn() {
    const idx = getZoomIndex(state.zoom);
    const next = idx + 1;
    if (next < ZOOM_LEVELS.length) {
      zoomTo(ZOOM_LEVELS[next]);
    }
  }

  function zoomOut() {
    const idx = getZoomIndex(state.zoom);
    const prev = idx - 1;
    if (prev >= 0) {
      zoomTo(ZOOM_LEVELS[prev]);
    }
  }

  function getCanvasPos(e) {
    const rect = canvas.getBoundingClientRect();
    return [e.clientX - rect.left, e.clientY - rect.top];
  }

  function showToast(msg, kind) {
    var container = document.getElementById("toast-container");
    if (!container) return;
    var el = document.createElement("div");
    el.className = "toast " + (kind || "info");
    el.textContent = msg;
    container.appendChild(el);
    var duration = kind === "success" ? 4500 : kind === "error" ? 5000 : 2600;
    setTimeout(function () {
      if (!el.parentNode) return;
      el.classList.add("hiding");
      setTimeout(function () {
        if (el.parentNode) el.parentNode.removeChild(el);
      }, 300);
    }, duration);
  }

  function countUncommitted(boxes) {
    var n = 0;
    for (var i = 0; i < boxes.length; i++) { if (!boxes[i].committed) n++; }
    return n;
  }

  function focusBox(b) {
    if (!state.image) return;

    var bx0 = Math.min(b.x0, b.x1);
    var by0 = Math.min(b.y0, b.y1);
    var bx1 = Math.max(b.x0, b.x1);
    var by1 = Math.max(b.y0, b.y1);

    var boxW = (bx1 - bx0) * state.imgW;
    var boxH = (by1 - by0) * state.imgH;
    if (boxW < 1 || boxH < 1) return;

    var availW = state.canvasW - PADDING * 2;
    var availH = state.canvasH - PADDING * 2;

    var padding = availW * 0.15;
    var scaleW = (availW - padding * 2) / boxW;
    var scaleH = (availH - padding * 2) / boxH;
    var idealZoom = Math.min(scaleW, scaleH);

    var zoom = ZOOM_LEVELS[0];
    for (var i = 0; i < ZOOM_LEVELS.length; i++) {
      if (ZOOM_LEVELS[i] <= idealZoom) zoom = ZOOM_LEVELS[i];
    }

    var cx = ((bx0 + bx1) / 2) * state.imgW;
    var cy = ((by0 + by1) / 2) * state.imgH;
    state.zoom = zoom;
    state.panX = (state.canvasW / 2) - cx * zoom;
    state.panY = (state.canvasH / 2) - cy * zoom;

    updateZoomDisplay();
    render();
  }

  function updateExtractButton(boxes) {
    var extractBtn = document.getElementById("extract-btn");
    if (!extractBtn) return;
    var hasCommitted = boxes.some(function (b) { return b.committed; });
    var hasUncommitted = countUncommitted(boxes) > 0;
    if (hasCommitted && !hasUncommitted) {
      extractBtn.disabled = false;
      extractBtn.title = "";
    } else {
      extractBtn.disabled = true;
      extractBtn.title = hasUncommitted
        ? "Commit your changes to enable HTML extraction."
        : "Commit at least one crop region first.";
    }
  }

  function updateCropPanel(boxes) {
    const cropList = document.getElementById("crop-list");
    const commitBtn = document.getElementById("commit-btn");
    updateExtractButton(boxes);
    cropList.innerHTML = "";

    const pending = countUncommitted(boxes);

    if (boxes.length === 0) {
      commitBtn.disabled = true;
      commitBtn.textContent = "Commit All Crops";
      const hint = document.createElement("p");
      hint.className = "empty-hint";
      hint.textContent = "Draw bounding boxes to create crops";
      cropList.appendChild(hint);
      return;
    }

    commitBtn.disabled = pending === 0;
    commitBtn.textContent = pending === 0
      ? "All Crops Committed"
      : "Commit " + pending + " Crop" + (pending !== 1 ? "s" : "");

    boxes.forEach(function (b, index) {
      const card = document.createElement("div");
      card.className = "crop-card" + (b.selected ? " selected" : "");

      if (b.committed && b.cropFilename) {
        const img = document.createElement("img");
        img.src = "/crops/" + sessionId + "/" + b.cropFilename + "?t=" + Date.now();
        img.alt = "Crop " + (index + 1);
        img.className = "crop-thumb";

        card.classList.add("committed");

        const check = document.createElement("span");
        check.className = "committed-badge";
        check.textContent = "\u2713";
        card.appendChild(img);
        card.appendChild(check);

        card.style.cursor = "pointer";
        card.addEventListener("click", function () {
          state.boxes.forEach(function (box) {
            box.selected = (box === b);
          });
          focusBox(b);
          updateCropPanel(boxes);
        });
      } else if (state.image) {
        const thumbCanvas = document.createElement("canvas");
        thumbCanvas.className = "crop-thumb";
        const thumbW = 120;
        const thumbH = 80;
        thumbCanvas.width = thumbW;
        thumbCanvas.height = thumbH;

        const sx = Math.min(b.x0, b.x1) * state.imgW;
        const sy = Math.min(b.y0, b.y1) * state.imgH;
        const sw = Math.abs(b.x1 - b.x0) * state.imgW;
        const sh = Math.abs(b.y1 - b.y0) * state.imgH;

        if (sw > 0 && sh > 0) {
          const tCtx = thumbCanvas.getContext("2d");
          tCtx.drawImage(state.image, sx, sy, sw, sh, 0, 0, thumbW, thumbH);
        }

        card.appendChild(thumbCanvas);

        card.style.cursor = "pointer";
        card.addEventListener("click", function () {
          state.boxes.forEach(function (box) {
            box.selected = (box === b);
          });
          focusBox(b);
          updateCropPanel(boxes);
        });
      }

      const label = document.createElement("span");
      label.className = "crop-label";
      label.textContent = "Crop " + (index + 1);
      card.appendChild(label);

      cropList.appendChild(card);
    });
  }

  async function commitCrops(sessionId, pageIndex, boxes) {
    const uncommitted = boxes.filter(function (b) { return !b.committed; });
    if (uncommitted.length === 0) {
      showToast("All crops are already committed", "info");
      return;
    }

    const commitBtn = document.getElementById("commit-btn");
    const n = uncommitted.length;
    const originalText = commitBtn.textContent;

    commitBtn.disabled = true;
    commitBtn.textContent = "Committing " + n + "...";

    const crops = uncommitted.map(function (b) {
      return {
        bbox: [b.x0, b.y0, b.x1, b.y1],
        filename: b.cropFilename || null
      };
    });
    let data;
    try {
      const resp = await fetch("/commit/" + sessionId, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ page_index: pageIndex, crops: crops })
      });
      if (!resp.ok) {
        showToast("Commit failed (HTTP " + resp.status + ") — try again", "error");
        commitBtn.disabled = false;
        commitBtn.textContent = originalText;
        return;
      }
      data = await resp.json();
    } catch (err) {
      console.error("Commit failed:", err);
      showToast("Commit failed: " + err.message + " — try again", "error");
      commitBtn.disabled = false;
      commitBtn.textContent = originalText;
      return;
    }
    uncommitted.forEach(function (b, i) {
      b.committed = true;
      b.cropFilename = data.crops[i].filename;
      b.cropPath = data.crops[i].path;
    });
    fetch("/clear-draft/" + sessionId, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ page_index: pageIndex }),
    }).catch(function (e) { console.error("Clear draft failed:", e); });
    updateCropPanel(boxes);
    showToast("Committed " + n + " crop" + (n !== 1 ? "s" : "") + " successfully", "success");
  }

  var draftSaveTimer = null;

  function scheduleSaveDraft() {
    if (draftSaveTimer) clearTimeout(draftSaveTimer);
    draftSaveTimer = setTimeout(function () {
      draftSaveTimer = null;
      doSaveDraft();
    }, 800);
  }

  async function doSaveDraft() {
    const pi = pageIndex;
    const boxes = state.boxes.filter(function (b) { return !b.committed; }).map(function (b) {
      return { x0: b.x0, y0: b.y0, x1: b.x1, y1: b.y1 };
    });
    if (boxes.length === 0) return;
    try {
      await fetch("/save-draft/" + sessionId, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ page_index: pi, boxes: boxes }),
      });
    } catch (err) {
      console.error("Draft save failed:", err);
    }
  }

  loadPageImage();
  buildThumbnails();
  initResizeObserver();
  initEventListeners();

  function loadPageImage() {
    const img = new Image();
    img.src = "/pages/" + sessionId + "/" + pageData.path;
    img.onload = function () {
      state.image = img;
      state.imgW = img.naturalWidth;
      state.imgH = img.naturalHeight;
      resizeCanvas();
      fitToViewport();
      updateZoomDisplay();

      if (pageData.crops && Array.isArray(pageData.crops)) {
        pageData.crops.forEach(function (crop) {
          state.boxes.push({
            id: state.nextBoxId++,
            x0: crop.bbox[0],
            y0: crop.bbox[1],
            x1: crop.bbox[2],
            y1: crop.bbox[3],
            selected: false,
            committed: true,
            cropFilename: crop.filename,
            cropPath: crop.path,
          });
        });
      }

      if (pageData.draft && Array.isArray(pageData.draft)) {
        pageData.draft.forEach(function (box) {
          state.boxes.push({
            id: state.nextBoxId++,
            x0: box.x0, y0: box.y0, x1: box.x1, y1: box.y1,
            selected: false,
            committed: false,
            cropFilename: null,
            cropPath: null,
          });
        });
      }

      render();
      updateCropPanel(state.boxes);
    };
  }

  function buildThumbnails() {
    pageThumbs.innerHTML = "";
    const currentPageIndex = parseInt(new URLSearchParams(window.location.search).get("page")) || 0;

    allPages.forEach(function (page, index) {
      const thumb = document.createElement("div");
      thumb.className = "thumb-item" + (index === currentPageIndex ? " active" : "");

      if (page.has_draft) {
        thumb.classList.add("has-draft");
      }

      const img = document.createElement("img");
      img.src = "/pages/" + sessionId + "/" + page.path;
      img.alt = "Page " + (index + 1);
      thumb.appendChild(img);

      thumb.addEventListener("click", function () {
        window.location = "/annotate/" + sessionId + "?page=" + index;
      });

      pageThumbs.appendChild(thumb);
    });
  }

  function initResizeObserver() {
    const ro = new ResizeObserver(function () {
      resizeCanvas();
      if (state.image) {
        fitToViewport();
        updateZoomDisplay();
        render();
      }
    });
    ro.observe(container);
  }

  function initEventListeners() {
    zoomInBtn.addEventListener("click", zoomIn);
    zoomOutBtn.addEventListener("click", zoomOut);

    canvas.addEventListener("mousedown", onMouseDown);
    canvas.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    canvas.addEventListener("wheel", onWheel, { passive: false });

    canvas.addEventListener("mouseenter", function () { state.canvasFocused = true; });
    canvas.addEventListener("mouseleave", function () { state.canvasFocused = false; });

    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);

    const commitBtn = document.getElementById("commit-btn");
    commitBtn.addEventListener("click", function () {
      if (state.boxes.length === 0) return;
      const pageIndex = parseInt(new URLSearchParams(window.location.search).get("page")) || 0;
      commitCrops(sessionId, pageIndex, state.boxes);
    });

    var extractBtn = document.getElementById("extract-btn");
    if (extractBtn) {
      extractBtn.addEventListener("click", function () {
        if (extractBtn.disabled) return;
        fetch("/session/" + sessionId)
          .then(function (r) { return r.json(); })
          .then(function (meta) {
            var hasDraft = (meta.pages || []).some(function (p) { return p.draft && p.draft.length > 0; });
            if (hasDraft) {
              showToast("You have uncommitted changes. Please commit first.", "error");
              updateExtractButton(state.boxes);
              return;
            }
            window.location = "/extract-html/" + sessionId;
          })
          .catch(function (err) {
            showToast("Could not verify commit state.", "error");
          });
      });
    }

    window.addEventListener("beforeunload", function (e) {
      const hasUncommitted = state.boxes.some(function (b) { return !b.committed; });
      if (hasUncommitted) {
        e.preventDefault();
        e.returnValue = "";
        if (draftSaveTimer) clearTimeout(draftSaveTimer);
        draftSaveTimer = null;
        var xhr = new XMLHttpRequest();
        xhr.open("POST", "/save-draft/" + sessionId, false);
        xhr.setRequestHeader("Content-Type", "application/json");
        var pi = pageIndex;
        var uncommitted = state.boxes.filter(function (b) { return !b.committed; }).map(function (b) {
          return { x0: b.x0, y0: b.y0, x1: b.x1, y1: b.y1 };
        });
        xhr.send(JSON.stringify({ page_index: pi, boxes: uncommitted }));
        return xhr.status < 500;
      }
    });

    window.addEventListener("crop-trimmed", function (e) {
      updateCropPanel(state.boxes);
    });
  }

  function onMouseDown(e) {
    if (e.button !== 0) {
      if (e.button === 1) {
        e.preventDefault();
        const [mx, my] = getCanvasPos(e);
        state.isPanning = true;
        state.panStartX = mx;
        state.panStartY = my;
        state.panOriginX = state.panX;
        state.panOriginY = state.panY;
        canvas.classList.add("cursor-grab");
      }
      return;
    }

    if (state.spaceHeld || e.altKey) {
      e.preventDefault();
      const [mx, my] = getCanvasPos(e);
      state.isPanning = true;
      state.panStartX = mx;
      state.panStartY = my;
      state.panOriginX = state.panX;
      state.panOriginY = state.panY;
      canvas.classList.add("cursor-grab");
      return;
    }

    const [mx, my] = getCanvasPos(e);
    const [nx, ny] = screenToNormalized(mx, my, state.imgW, state.imgH, state.zoom, state.panX, state.panY);

    if (nx < 0 || nx > 1 || ny < 0 || ny > 1) {
      e.preventDefault();
      state.isPanning = true;
      state.panStartX = mx;
      state.panStartY = my;
      state.panOriginX = state.panX;
      state.panOriginY = state.panY;
      canvas.classList.add("cursor-grab");
      return;
    }

    const zone = getCursorZone(nx, ny, state.boxes, state.zoom, state.panX, state.panY, state.imgW, state.imgH);

    if (zone) {
      e.preventDefault();
      const b = state.boxes[zone.boxIndex];

      state.boxes.forEach((box, idx) => { box.selected = idx === zone.boxIndex; });

      if (zone.zone === "delete") {
        const b = state.boxes[zone.boxIndex];
        if (b.committed && b.cropFilename) {
          fetch("/delete-crop/" + sessionId, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ page_index: pageIndex, filename: b.cropFilename }),
          }).then(function (resp) {
            if (!resp.ok) throw new Error("HTTP " + resp.status);
            showToast("Crop deleted", "success");
          }).catch(function (err) {
            console.error("Delete crop failed:", err);
            showToast("Failed to delete crop — try again", "error");
            state.boxes.splice(zone.boxIndex, 0, b);
            render();
            updateCropPanel(state.boxes);
          });
        }
        state.boxes.splice(zone.boxIndex, 1);
        state.mode = "idle";
        state.interaction = null;
        render();
        updateCropPanel(state.boxes);
        scheduleSaveDraft();
        return;
      } else if (zone.zone === "handle") {
        state.mode = "move";
        state.interaction = {
          boxIndex: zone.boxIndex,
          startNX: nx,
          startNY: ny,
          origX0: b.x0, origY0: b.y0,
          origX1: b.x1, origY1: b.y1,
        };
        canvas.classList.add("cursor-grab");
      } else if (zone.zone === "corner") {
        state.mode = "resize";
        state.interaction = {
          boxIndex: zone.boxIndex,
          corner: zone.corner,
          startNX: nx,
          startNY: ny,
          origX0: b.x0, origY0: b.y0,
          origX1: b.x1, origY1: b.y1,
        };
      } else if (zone.zone === "edge") {
        state.mode = "resize-edge";
        state.interaction = {
          boxIndex: zone.boxIndex,
          edge: zone.edge,
          startNX: nx,
          startNY: ny,
          origX0: b.x0, origY0: b.y0,
          origX1: b.x1, origY1: b.y1,
        };
      }
      render();
      return;
    }

    for (const box of state.boxes) { box.selected = false; }

    state.mode = "draw";
    state.interaction = {
      boxIndex: -1,
      anchorNX: nx,
      anchorNY: ny,
    };
    state.boxes.push({
      id: state.nextBoxId++,
      x0: nx, y0: ny, x1: nx, y1: ny,
      selected: true, committed: false,
      cropFilename: null, cropPath: null,
    });
    canvas.style.cursor = "crosshair";
    render();
  }

  function onMouseMove(e) {
    const [mx, my] = getCanvasPos(e);

    if (state.isPanning) {
      const dx = mx - state.panStartX;
      const dy = my - state.panStartY;
      state.panX = state.panOriginX + dx;
      state.panY = state.panOriginY + dy;
      canvas.classList.add("cursor-grab");
      render();
      return;
    }

    if (state.mode === "idle") {
      const [nx, ny] = screenToNormalized(mx, my, state.imgW, state.imgH, state.zoom, state.panX, state.panY);
      const zone = getCursorZone(nx, ny, state.boxes, state.zoom, state.panX, state.panY, state.imgW, state.imgH);
      canvas.style.cursor = getCursorStyle(zone);
      return;
    }

    const [nx, ny] = screenToNormalized(mx, my, state.imgW, state.imgH, state.zoom, state.panX, state.panY);
    const cNx = Math.max(0, Math.min(1, nx));
    const cNy = Math.max(0, Math.min(1, ny));

    if (state.mode === "move") {
      const ia = state.interaction;
      const b = state.boxes[ia.boxIndex];
      const dnx = cNx - ia.startNX;
      const dny = cNy - ia.startNY;
      b.x0 = ia.origX0 + dnx;
      b.y0 = ia.origY0 + dny;
      b.x1 = ia.origX1 + dnx;
      b.y1 = ia.origY1 + dny;
    } else if (state.mode === "resize") {
      const ia = state.interaction;
      const b = state.boxes[ia.boxIndex];
      const c = ia.corner;
      const minX = ia.origX0, minY = ia.origY0;
      const maxX = ia.origX1, maxY = ia.origY1;

      if (c === "NW") { b.x0 = Math.min(cNx, maxX - 0.005); b.y0 = Math.min(cNy, maxY - 0.005); }
      else if (c === "NE") { b.x1 = Math.max(cNx, minX + 0.005); b.y0 = Math.min(cNy, maxY - 0.005); }
      else if (c === "SW") { b.x0 = Math.min(cNx, maxX - 0.005); b.y1 = Math.max(cNy, minY + 0.005); }
      else if (c === "SE") { b.x1 = Math.max(cNx, minX + 0.005); b.y1 = Math.max(cNy, minY + 0.005); }
    } else if (state.mode === "resize-edge") {
      const ia = state.interaction;
      const b = state.boxes[ia.boxIndex];
      const e = ia.edge;

      if (e === "N") { b.y0 = Math.min(cNy, ia.origY1 - 0.005); }
      else if (e === "S") { b.y1 = Math.max(cNy, ia.origY0 + 0.005); }
      else if (e === "W") { b.x0 = Math.min(cNx, ia.origX1 - 0.005); }
      else if (e === "E") { b.x1 = Math.max(cNx, ia.origX0 + 0.005); }
    } else if (state.mode === "draw") {
      const drawBox = state.boxes[state.boxes.length - 1];
      if (drawBox && !drawBox.committed) {
        drawBox.x1 = cNx;
        drawBox.y1 = cNy;
      }
    }

    render();
  }

  function onMouseUp(e) {
    if (state.isPanning) {
      state.isPanning = false;
      canvas.classList.remove("cursor-grab");
      return;
    }

    if (state.mode === "idle") {
      return;
    }

    if (state.mode === "move" || state.mode === "resize" || state.mode === "resize-edge") {
      const ia = state.interaction;
      if (ia && ia.boxIndex >= 0) {
        const b = state.boxes[ia.boxIndex];
        if (b) {
          const changed = b.x0 !== ia.origX0 || b.y0 !== ia.origY0 || b.x1 !== ia.origX1 || b.y1 !== ia.origY1;
          if (changed) {
            b.committed = false;
          }
        }
      }
    }

    if (state.mode === "draw") {
      const drawBox = state.boxes[state.boxes.length - 1];
      if (drawBox && !drawBox.committed) {
        const [sx0, sy0] = normalizedToScreen(drawBox.x0, drawBox.y0, state.imgW, state.imgH, state.zoom, state.panX, state.panY);
        const [sx1, sy1] = normalizedToScreen(drawBox.x1, drawBox.y1, state.imgW, state.imgH, state.zoom, state.panX, state.panY);
        const screenW = Math.abs(sx1 - sx0);
        const screenH = Math.abs(sy1 - sy0);

        if (screenW < 10 || screenH < 10) {
          state.boxes.pop();
        } else {
          if (drawBox.x0 > drawBox.x1) { const t = drawBox.x0; drawBox.x0 = drawBox.x1; drawBox.x1 = t; }
          if (drawBox.y0 > drawBox.y1) { const t = drawBox.y0; drawBox.y0 = drawBox.y1; drawBox.y1 = t; }
        }
      }
    }

    state.mode = "idle";
    state.interaction = null;
    canvas.style.cursor = "";
    render();
    updateCropPanel(state.boxes);
    scheduleSaveDraft();
  }

  const ZOOM_SPEED = 0.005;

  function onWheel(e) {
    e.preventDefault();

    if (e.ctrlKey) {
      const [mx, my] = getCanvasPos(e);
      const zoomFactor = Math.exp(-e.deltaY * ZOOM_SPEED);
      zoomTo(state.zoom * zoomFactor, mx, my);
      return;
    }

    state.panX -= e.deltaX;
    state.panY -= e.deltaY;
    render();
  }

  function onKeyDown(e) {
    if (e.code === "Space" && !state.spaceHeld) {
      e.preventDefault();
      state.spaceHeld = true;
      canvas.classList.add("cursor-grab");
    }

    if (!state.canvasFocused) return;

    if (e.key === "+" || e.key === "=") {
      e.preventDefault();
      zoomIn();
    } else if (e.key === "-") {
      e.preventDefault();
      zoomOut();
    } else     if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      const selected = state.boxes.filter(b => b.selected);
      if (selected.length > 0) {
        state.boxes = state.boxes.filter(b => !b.selected);
        state.mode = "idle";
        state.interaction = null;
        render();
        updateCropPanel(state.boxes);
        scheduleSaveDraft();
      }
    }
  }

  function onKeyUp(e) {
    if (e.code === "Space") {
      state.spaceHeld = false;
      if (!state.isPanning) {
        canvas.classList.remove("cursor-grab");
      }
    }
  }
});
