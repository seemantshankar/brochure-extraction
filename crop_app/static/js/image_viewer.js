window.ImageViewer = window.ImageViewer || {};
window.ImageViewer.create = function createImageViewer(options) {
  var canvas = options.canvas;
  var ctx = canvas.getContext("2d");
  var container = options.container;
  var mode = options.mode || "annotate";
  var zoomDisplay = options.zoomDisplay || null;

  var ZOOM_LEVELS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0];
  var PADDING = 20;
  var DPR = window.devicePixelRatio || 1;
  var ZOOM_SPEED = 0.005;

  var state = {
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
    canvasFocused: false,
  };

  function getCanvasPos(e) {
    var rect = canvas.getBoundingClientRect();
    return [e.clientX - rect.left, e.clientY - rect.top];
  }

  function screenToNormalized(sx, sy, imgW, imgH, zoom, panX, panY) {
    var x = (sx - panX) / zoom / imgW;
    var y = (sy - panY) / zoom / imgH;
    return [x, y];
  }

  function normalizedToScreen(nx, ny, imgW, imgH, zoom, panX, panY) {
    var x = nx * imgW * zoom + panX;
    var y = ny * imgH * zoom + panY;
    return [x, y];
  }

  function resizeCanvas() {
    var rect = container.getBoundingClientRect();
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
    var maxW = state.canvasW - PADDING * 2;
    var maxH = state.canvasH - PADDING * 2;
    var scaleX = maxW / state.imgW;
    var scaleY = maxH / state.imgH;
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
    ctx.restore();

    if (options.onAfterRender) {
      ctx.save();
      ctx.translate(state.panX, state.panY);
      ctx.scale(state.zoom, state.zoom);
      options.onAfterRender(ctx, state);
      ctx.restore();
    }
  }

  function updateZoomDisplay() {
    if (zoomDisplay) {
      zoomDisplay.textContent = Math.round(state.zoom * 100) + "%";
    }
  }

  function getZoomIndex(zoom) {
    var closest = 0;
    var minDist = Infinity;
    for (var i = 0; i < ZOOM_LEVELS.length; i++) {
      var dist = Math.abs(ZOOM_LEVELS[i] - zoom);
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

    var imgX = (centerX - state.panX) / state.zoom;
    var imgY = (centerY - state.panY) / state.zoom;

    state.zoom = newZoom;

    state.panX = centerX - imgX * state.zoom;
    state.panY = centerY - imgY * state.zoom;

    updateZoomDisplay();
    render();
  }

  function zoomIn() {
    var idx = getZoomIndex(state.zoom);
    var next = idx + 1;
    if (next < ZOOM_LEVELS.length) {
      zoomTo(ZOOM_LEVELS[next]);
    }
  }

  function zoomOut() {
    var idx = getZoomIndex(state.zoom);
    var prev = idx - 1;
    if (prev >= 0) {
      zoomTo(ZOOM_LEVELS[prev]);
    }
  }

  function startPan(e) {
    var pos = getCanvasPos(e);
    state.isPanning = true;
    state.panStartX = pos[0];
    state.panStartY = pos[1];
    state.panOriginX = state.panX;
    state.panOriginY = state.panY;
    canvas.classList.add("cursor-grab");
  }

  function onMouseDown(e) {
    if (e.button === 1) {
      e.preventDefault();
      startPan(e);
      return;
    }

    if (e.button !== 0) return;

    if (state.spaceHeld || e.altKey) {
      e.preventDefault();
      startPan(e);
      return;
    }

    if (mode === "review") {
      e.preventDefault();
      startPan(e);
      return;
    }

    var pos = getCanvasPos(e);
    var n = screenToNormalized(pos[0], pos[1], state.imgW, state.imgH, state.zoom, state.panX, state.panY);
    if (n[0] < 0 || n[0] > 1 || n[1] < 0 || n[1] > 1) {
      e.preventDefault();
      startPan(e);
      return;
    }
  }

  function onMouseMove(e) {
    if (!state.isPanning) return;
    var pos = getCanvasPos(e);
    var dx = pos[0] - state.panStartX;
    var dy = pos[1] - state.panStartY;
    state.panX = state.panOriginX + dx;
    state.panY = state.panOriginY + dy;
    canvas.classList.add("cursor-grab");
    render();
  }

  function onMouseUp(e) {
    if (!state.isPanning) return;
    state.isPanning = false;
    canvas.classList.remove("cursor-grab");
  }

  function onWheel(e) {
    e.preventDefault();

    if (e.ctrlKey) {
      var pos = getCanvasPos(e);
      var zoomFactor = Math.exp(-e.deltaY * ZOOM_SPEED);
      zoomTo(state.zoom * zoomFactor, pos[0], pos[1]);
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

  canvas.addEventListener("mousedown", onMouseDown);
  canvas.addEventListener("mousemove", onMouseMove);
  window.addEventListener("mouseup", onMouseUp);
  canvas.addEventListener("wheel", onWheel, { passive: false });
  window.addEventListener("keydown", onKeyDown);
  window.addEventListener("keyup", onKeyUp);
  canvas.addEventListener("mouseenter", function() { state.canvasFocused = true; });
  canvas.addEventListener("mouseleave", function() { state.canvasFocused = false; });

  var ro = new ResizeObserver(function() {
    resizeCanvas();
    if (state.image) {
      fitToViewport();
      updateZoomDisplay();
      render();
    }
  });
  ro.observe(container);

  function resize(opts) {
    var refit = opts && opts.refit;
    resizeCanvas();
    if (refit && state.image) {
      fitToViewport();
      updateZoomDisplay();
    }
    render();
  }

  function setImage(url) {
    var image = new Image();
    image.onload = function() {
      state.image = image;
      state.imgW = image.naturalWidth;
      state.imgH = image.naturalHeight;
      resize({ refit: true });
    };
    image.src = url;
  }

  function destroy() {
    canvas.removeEventListener("mousedown", onMouseDown);
    canvas.removeEventListener("mousemove", onMouseMove);
    window.removeEventListener("mouseup", onMouseUp);
    canvas.removeEventListener("wheel", onWheel);
    window.removeEventListener("keydown", onKeyDown);
    window.removeEventListener("keyup", onKeyUp);
    ro.disconnect();
  }

  if (options.imageUrl) setImage(options.imageUrl);

  return {
    state: state,
    ZOOM_LEVELS: ZOOM_LEVELS,
    PADDING: PADDING,
    DPR: DPR,
    ZOOM_SPEED: ZOOM_SPEED,
    canvas: canvas,
    ctx: ctx,
    container: container,
    setImage: setImage,
    resize: resize,
    destroy: destroy,
    render: render,
    zoomTo: zoomTo,
    zoomIn: zoomIn,
    zoomOut: zoomOut,
    updateZoomDisplay: updateZoomDisplay,
    getCanvasPos: getCanvasPos,
    screenToNormalized: screenToNormalized,
    normalizedToScreen: normalizedToScreen,
    fitToViewport: fitToViewport,
    resizeCanvas: resizeCanvas,
  };
};
