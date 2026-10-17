// Design image editor (Design tab), ported from jonah-image-editor's
// static/js/editor.js: a canvas with Draw / Box / Text / Crop tools, undo
// and reset, plus Seedream image-to-image that uses the edited canvas as the
// reference. Edits one design-folder image at a time and saves it back over
// itself, in its own timestamp folder. Images are loaded through this site (/api/design/<p>/file/<name>,
// /generated/...) because TOS sends no CORS headers, and a canvas that drew
// a cross-origin image can't be exported.
//
// Usage: DesignEditor.open({ project, name, label, onSaved(listing) })
(function () {
  "use strict";

  var dialog = document.getElementById("editor");
  if (!dialog) return;

  var canvas = document.getElementById("edCanvas");
  var ctx = canvas.getContext("2d");
  var stage = document.getElementById("edStage");
  var loadingEl = document.getElementById("edLoading");
  var colorEl = document.getElementById("edColor");
  var lineEl = document.getElementById("edLine");
  var fontEl = document.getElementById("edFont");
  var undoBtn = document.getElementById("edUndo");
  var resetBtn = document.getElementById("edReset");
  var saveBtn = document.getElementById("edSave");
  var saveStatus = document.getElementById("edSaveStatus");
  var promptEl = document.getElementById("edPrompt");
  var refInput = document.getElementById("edRef");
  var refPreview = document.getElementById("edRefPreview");
  var generateBtn = document.getElementById("edGenerate");
  var genStatus = document.getElementById("edGenStatus");
  var resultsEl = document.getElementById("edResults");
  var modeButtons = dialog.querySelectorAll(".ed-mode");
  var cropBar = document.getElementById("edCropBar");
  var cropRatioEl = document.getElementById("edCropRatio");
  var cropSizeEl = document.getElementById("edCropSize");

  var MIME_BY_EXT = { jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png", webp: "image/webp" };

  // Offscreen canvas holding the committed pixels (baked drawings, boxes, text).
  var baseCanvas = document.createElement("canvas");
  var baseCtx = baseCanvas.getContext("2d");

  // Overlay canvas over the visible one: live box preview and handles.
  var overlay = document.createElement("canvas");
  overlay.className = "ed-overlay";
  stage.appendChild(overlay);
  var overlayCtx = overlay.getContext("2d");

  // Floating input for typing text straight onto the image.
  var textInput = document.createElement("input");
  textInput.type = "text";
  textInput.className = "ed-text-input";
  textInput.hidden = true;
  textInput.autocomplete = "off";
  textInput.spellcheck = false;
  stage.appendChild(textInput);

  var current = null;      // { project, name, label, mime, onSaved }
  var originalUrl = null;
  var undoStack = [];
  var dirty = false;
  var busy = false;
  var mode = "none";       // none | draw | rect | text | crop
  var rectRect = null;     // axis-aligned rect while the first box drag is in progress
  var activeBox = null;    // { cx, cy, w, h, rotation, color, lineWidth }, not yet baked in
  var boxDrag = null;      // create | move | resize | rotate
  var pendingText = null;  // { x, y } where the text being typed will be baked
  var crop = null;         // { x, y, w, h } crop area while the Crop tool is on, not yet applied
  var cropDrag = null;     // create | move | resize
  var dragStart = null;
  var isDrawing = false;
  var activePointerId = null;
  var refFile = null;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  // Design file names are "<timestamp folder>/<file>": keep the slash.
  function encodePath(name) {
    return name.split("/").map(encodeURIComponent).join("/");
  }

  function resizeCanvases(w, h) {
    canvas.width = overlay.width = w;
    canvas.height = overlay.height = h;
    fitDisplay();
  }

  // Shows the canvas as large as fits in the wrap (never upscaled), with its
  // own aspect ratio: CSS max-width/height would letterbox the pixels inside
  // the element and throw off the overlay and pointer coordinates.
  var canvasWrap = stage.parentNode;
  function fitDisplay() {
    var aw = canvasWrap.clientWidth - 2, ah = canvasWrap.clientHeight - 2;
    if (aw <= 0 || ah <= 0 || !canvas.width || !canvas.height) return;
    var s = Math.min(aw / canvas.width, ah / canvas.height, 1);
    canvas.style.width = Math.floor(canvas.width * s) + "px";
    canvas.style.height = Math.floor(canvas.height * s) + "px";
  }
  // Toolbar wrapping (e.g. the crop bar appearing) or a window resize changes
  // the room; handle sizes on the overlay depend on the display scale.
  new ResizeObserver(function () {
    fitDisplay();
    if (crop) drawCropOverlay();
    else if (activeBox) drawActiveBoxOverlay();
  }).observe(canvasWrap);

  function render() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(baseCanvas, 0, 0);
  }

  // A crop area that's been drawn but not applied yet counts as a change:
  // Save applies it.
  function isDirty() {
    return dirty || cropIsReal();
  }

  function refreshSave() {
    saveBtn.disabled = busy || !isDirty();
  }

  function setDirty(on) {
    dirty = on;
    refreshSave();
  }

  function pushUndoSnapshot() {
    undoStack.push(baseCanvas.toDataURL("image/png"));
    undoBtn.disabled = false;
    setDirty(true);
  }

  function loadBase(src) {
    return new Promise(function (resolve, reject) {
      var img = new Image();
      img.onload = function () {
        resizeCanvases(img.naturalWidth, img.naturalHeight);
        baseCanvas.width = img.naturalWidth;
        baseCanvas.height = img.naturalHeight;
        baseCtx.clearRect(0, 0, baseCanvas.width, baseCanvas.height);
        baseCtx.drawImage(img, 0, 0);
        render();
        resolve();
      };
      img.onerror = function () { reject(new Error("Could not load the image.")); };
      img.src = src;
    });
  }

  // ---------- Open / close ----------
  function open(opts) {
    var ext = (opts.name.split(".").pop() || "").toLowerCase();
    current = {
      project: opts.project, name: opts.name, label: opts.label || opts.name,
      mime: MIME_BY_EXT[ext] || "image/png", onSaved: opts.onSaved
    };
    document.getElementById("edTitle").textContent = "Edit · " + current.label;
    document.getElementById("edFile").textContent = opts.name;
    discardActiveBox();
    discardTextEdit();
    undoStack = [];
    undoBtn.disabled = true;
    setDirty(false);
    setMode("none");
    saveStatus.textContent = "";
    genStatus.textContent = "";
    resultsEl.replaceChildren();
    promptEl.value = "";
    setRef(null);
    originalUrl = "/api/design/" + encodeURIComponent(opts.project) + "/file/" +
      encodePath(opts.name) + "?t=" + Date.now();
    loadingEl.hidden = false;
    loadingEl.textContent = "Loading image...";
    dialog.showModal();
    loadBase(originalUrl)
      .then(function () { loadingEl.hidden = true; })
      .catch(function (err) { loadingEl.textContent = err.message; });
  }

  function close() {
    if (busy) return;
    if (isDirty() && !confirm("Discard your unsaved changes to " + current.label + "?")) return;
    dirty = false;
    leaveCrop();
    dialog.close();
  }

  dialog.addEventListener("cancel", function (e) {  // Esc
    if (mode === "crop") { e.preventDefault(); leaveCrop(); return; }  // Esc only cancels the crop
    if (pendingText) { e.preventDefault(); return; }  // Esc only cancels the text being typed
    e.preventDefault();
    close();
  });
  document.getElementById("edClose").addEventListener("click", close);
  document.getElementById("edCancel").addEventListener("click", close);

  // ---------- Tools ----------
  function setMode(newMode) {
    if (activeBox && newMode !== "rect") commitActiveBox();
    if (pendingText && newMode !== "text") commitTextEdit();
    var prev = mode;
    mode = mode === newMode ? "none" : newMode;  // clicking the active tool turns it off
    if (prev === "crop" && mode !== "crop") discardCrop();
    if (mode === "crop" && prev !== "crop") fitCropToRatio();
    cropBar.hidden = mode !== "crop";
    modeButtons.forEach(function (b) { b.classList.toggle("active", b.dataset.mode === mode); });
    canvas.style.cursor = mode === "draw" || mode === "rect" || mode === "crop" ? "crosshair" : mode === "text" ? "text" : "default";
  }

  function leaveCrop() {
    if (mode === "crop") setMode("none");
  }
  modeButtons.forEach(function (b) {
    b.addEventListener("click", function () { setMode(b.dataset.mode); });
  });

  function getCanvasCoords(evt) {
    var rect = canvas.getBoundingClientRect();
    return {
      x: (evt.clientX - rect.left) * (canvas.width / rect.width),
      y: (evt.clientY - rect.top) * (canvas.height / rect.height)
    };
  }

  // Pointer capture, ignoring extra simultaneous touches and preventing the
  // default touch behaviour keep a stroke from being hijacked on tablets.
  canvas.addEventListener("pointerdown", function (evt) {
    if (activePointerId !== null && evt.pointerId !== activePointerId) return;
    if (busy || (mode !== "draw" && mode !== "text" && mode !== "rect" && mode !== "crop")) return;

    if (mode === "crop") {
      activePointerId = evt.pointerId;
      canvas.setPointerCapture(evt.pointerId);
      evt.preventDefault();
      var c = clampToImage(getCanvasCoords(evt));
      var cropHit = crop && hitTestCrop(c);
      if (cropHit && cropHit.type === "resize") cropDrag = { type: "resize", fixed: cropHit.fixed };
      // Inside the area moves it, unless it's still the whole image (nothing to move).
      else if (cropHit && cropIsReal()) cropDrag = { type: "move", start: Object.assign({}, crop), pointerStart: c };
      else cropDrag = { type: "create", fixed: c };  // otherwise draw a new area
      return;
    }
    if (mode === "rect") {
      activePointerId = evt.pointerId;
      canvas.setPointerCapture(evt.pointerId);
      evt.preventDefault();
      var p = getCanvasCoords(evt);
      if (activeBox) {
        var hit = hitTestBox(p, activeBox);
        if (hit) {
          boxDrag = { type: hit.type, corner: hit.corner, start: Object.assign({}, activeBox), pointerStart: p };
          return;
        }
        commitActiveBox();  // tapped outside the box being edited: bake it, start a new one
      }
      boxDrag = { type: "create" };
      dragStart = p;
      rectRect = null;
      return;
    }
    if (mode === "draw") {
      activePointerId = evt.pointerId;
      canvas.setPointerCapture(evt.pointerId);
      evt.preventDefault();
      pushUndoSnapshot();
      isDrawing = true;
      var q = getCanvasCoords(evt);
      baseCtx.beginPath();
      baseCtx.moveTo(q.x, q.y);
      baseCtx.lineCap = "round";
      baseCtx.lineJoin = "round";
      baseCtx.strokeStyle = colorEl.value;
      baseCtx.lineWidth = lineEl.value;
    } else if (mode === "text") {
      evt.preventDefault();
      commitTextEdit();
      startTextEdit(evt, getCanvasCoords(evt));
    }
  });

  canvas.addEventListener("pointermove", function (evt) {
    if (activePointerId !== null && evt.pointerId !== activePointerId) return;
    if (mode === "crop") {
      var c = clampToImage(getCanvasCoords(evt));
      if (!cropDrag) {  // hover: show what a drag here would do
        var hit = crop && hitTestCrop(c);
        canvas.style.cursor = !hit ? "crosshair" : hit.type === "move" ? (cropIsReal() ? "move" : "crosshair") :
          hit.corner === "nw" || hit.corner === "se" ? "nwse-resize" : "nesw-resize";
        return;
      }
      evt.preventDefault();
      if (cropDrag.type === "move") {
        crop.x = Math.min(Math.max(0, cropDrag.start.x + c.x - cropDrag.pointerStart.x), baseCanvas.width - crop.w);
        crop.y = Math.min(Math.max(0, cropDrag.start.y + c.y - cropDrag.pointerStart.y), baseCanvas.height - crop.h);
      } else {
        // A tiny drag outside the area is a stray tap, not a new crop.
        if (cropDrag.type === "create" && Math.hypot(c.x - cropDrag.fixed.x, c.y - cropDrag.fixed.y) < 4 * displayScale()) return;
        setCropFromCorner(cropDrag.fixed, c);
      }
      drawCropOverlay();
      refreshSave();
      return;
    }
    if (mode === "rect" && boxDrag) {
      evt.preventDefault();
      var p = getCanvasCoords(evt);
      if (boxDrag.type === "create") {
        if (!dragStart) return;
        rectRect = {
          x: Math.min(dragStart.x, p.x), y: Math.min(dragStart.y, p.y),
          w: Math.abs(p.x - dragStart.x), h: Math.abs(p.y - dragStart.y)
        };
        drawRectCreatePreview();
      } else if (boxDrag.type === "move") {
        activeBox.cx = boxDrag.start.cx + (p.x - boxDrag.pointerStart.x);
        activeBox.cy = boxDrag.start.cy + (p.y - boxDrag.pointerStart.y);
        drawActiveBoxOverlay();
      } else if (boxDrag.type === "rotate") {
        activeBox.rotation = Math.atan2(p.y - activeBox.cy, p.x - activeBox.cx) + Math.PI / 2;
        drawActiveBoxOverlay();
      } else if (boxDrag.type === "resize") {
        resizeActiveBox(boxDrag, p);
        drawActiveBoxOverlay();
      }
      return;
    }
    if (isDrawing && mode === "draw") {
      evt.preventDefault();
      var q = getCanvasCoords(evt);
      baseCtx.lineTo(q.x, q.y);
      baseCtx.stroke();
      render();
    }
  });

  function endActiveGesture(evt) {
    if (activePointerId !== null && evt.pointerId !== activePointerId) return;
    if (canvas.hasPointerCapture && canvas.hasPointerCapture(evt.pointerId)) canvas.releasePointerCapture(evt.pointerId);
    activePointerId = null;
    cropDrag = null;
    if (isDrawing) {
      isDrawing = false;
      baseCtx.closePath();
    }
    if (mode === "rect" && boxDrag) {
      var dragType = boxDrag.type;
      boxDrag = null;
      if (dragType === "create") {
        dragStart = null;
        if (evt.type === "pointerup" && rectRect && rectRect.w >= 4 && rectRect.h >= 4) {
          activeBox = {
            cx: rectRect.x + rectRect.w / 2, cy: rectRect.y + rectRect.h / 2,
            w: rectRect.w, h: rectRect.h, rotation: 0,
            color: colorEl.value, lineWidth: Number(lineEl.value)
          };
          setDirty(true);
          drawActiveBoxOverlay();
        } else {
          overlayCtx.clearRect(0, 0, overlay.width, overlay.height);
        }
        rectRect = null;
      } else {
        drawActiveBoxOverlay();  // keep the box live for further edits
      }
    }
  }
  canvas.addEventListener("pointerup", endActiveGesture);
  canvas.addEventListener("pointercancel", endActiveGesture);

  // ---------- Box ----------
  // Kept as a live object (center/size/rotation) on the overlay until it's
  // committed, so it can be moved, resized and rotated after being placed.
  var HANDLE_VISUAL_SIZE = 12;   // on-screen px
  var HANDLE_HIT_SIZE = 26;      // on-screen px, generous for touch
  var ROTATE_HANDLE_OFFSET = 28; // on-screen px above the box

  function displayScale() {
    var rect = canvas.getBoundingClientRect();
    return rect.width ? canvas.width / rect.width : 1;
  }

  function drawRectCreatePreview() {
    overlayCtx.clearRect(0, 0, overlay.width, overlay.height);
    if (!rectRect) return;
    overlayCtx.strokeStyle = colorEl.value;
    overlayCtx.lineWidth = Number(lineEl.value);
    overlayCtx.strokeRect(rectRect.x, rectRect.y, rectRect.w, rectRect.h);
  }

  // Canvas point -> the box's unrotated, center-origin space.
  function toLocalPoint(px, py, box) {
    var dx = px - box.cx, dy = py - box.cy;
    var cos = Math.cos(-box.rotation), sin = Math.sin(-box.rotation);
    return { x: dx * cos - dy * sin, y: dx * sin + dy * cos };
  }

  function hitTestBox(p, box) {
    var scale = displayScale();
    var hitR = (HANDLE_HIT_SIZE / 2) * scale;
    var local = toLocalPoint(p.x, p.y, box);
    var hw = box.w / 2, hh = box.h / 2;
    if (Math.hypot(local.x, local.y + hh + ROTATE_HANDLE_OFFSET * scale) <= hitR) return { type: "rotate" };
    var corners = { nw: [-hw, -hh], ne: [hw, -hh], se: [hw, hh], sw: [-hw, hh] };
    for (var corner in corners) {
      if (Math.hypot(local.x - corners[corner][0], local.y - corners[corner][1]) <= hitR) return { type: "resize", corner: corner };
    }
    if (Math.abs(local.x) <= hw && Math.abs(local.y) <= hh) return { type: "move" };
    return null;
  }

  // Resize from the drag-start state, keeping the opposite corner fixed and
  // the rotation unchanged.
  function resizeActiveBox(drag, p) {
    var start = drag.start;
    var local = toLocalPoint(p.x, p.y, start);
    var hw = start.w / 2, hh = start.h / 2;
    var fixed = { nw: [hw, hh], ne: [-hw, hh], se: [-hw, -hh], sw: [hw, -hh] }[drag.corner];
    var MIN_SIZE = 8;
    var centerLocal = { x: (local.x + fixed[0]) / 2, y: (local.y + fixed[1]) / 2 };
    var cos = Math.cos(start.rotation), sin = Math.sin(start.rotation);
    activeBox.cx = start.cx + centerLocal.x * cos - centerLocal.y * sin;
    activeBox.cy = start.cy + centerLocal.x * sin + centerLocal.y * cos;
    activeBox.w = Math.max(MIN_SIZE, Math.abs(local.x - fixed[0]));
    activeBox.h = Math.max(MIN_SIZE, Math.abs(local.y - fixed[1]));
  }

  function drawActiveBoxOverlay() {
    overlayCtx.clearRect(0, 0, overlay.width, overlay.height);
    if (!activeBox) return;
    var b = activeBox, hw = b.w / 2, hh = b.h / 2;
    var scale = displayScale();
    var handleSize = HANDLE_VISUAL_SIZE * scale;
    var rotateOffset = ROTATE_HANDLE_OFFSET * scale;
    overlayCtx.save();
    overlayCtx.translate(b.cx, b.cy);
    overlayCtx.rotate(b.rotation);
    overlayCtx.strokeStyle = b.color;
    overlayCtx.lineWidth = b.lineWidth;
    overlayCtx.strokeRect(-hw, -hh, b.w, b.h);
    overlayCtx.strokeStyle = overlayCtx.fillStyle = "#4f8cff";
    overlayCtx.lineWidth = Math.max(1, scale);
    overlayCtx.beginPath();
    overlayCtx.moveTo(0, -hh);
    overlayCtx.lineTo(0, -hh - rotateOffset);
    overlayCtx.stroke();
    [[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]].forEach(function (c) {
      overlayCtx.fillRect(c[0] - handleSize / 2, c[1] - handleSize / 2, handleSize, handleSize);
    });
    overlayCtx.beginPath();
    overlayCtx.arc(0, -hh - rotateOffset, handleSize / 2 + 2 * scale, 0, Math.PI * 2);
    overlayCtx.fill();
    overlayCtx.restore();
  }

  function commitActiveBox() {
    if (!activeBox) return;
    pushUndoSnapshot();
    var b = activeBox;
    baseCtx.save();
    baseCtx.translate(b.cx, b.cy);
    baseCtx.rotate(b.rotation);
    baseCtx.strokeStyle = b.color;
    baseCtx.lineWidth = b.lineWidth;
    baseCtx.strokeRect(-b.w / 2, -b.h / 2, b.w, b.h);
    baseCtx.restore();
    render();
    activeBox = null;
    overlayCtx.clearRect(0, 0, overlay.width, overlay.height);
  }

  function discardActiveBox() {
    activeBox = null;
    boxDrag = null;
    overlayCtx.clearRect(0, 0, overlay.width, overlay.height);
  }

  // ---------- Crop ----------
  // The area (image px) shows on the overlay with the outside dimmed. Drag
  // a corner to resize it, inside to move it, outside to draw a new one;
  // Apply crop (or Enter) cuts the image to it, Cancel / Esc drops it. The
  // ratio list locks the area's shape; Free leaves it open.
  var CROP_MIN = 16;

  function cropRatio() {
    var v = cropRatioEl.value;
    if (v === "free") return 0;
    if (v === "orig") return baseCanvas.width / baseCanvas.height;
    var parts = v.split(":");
    return Number(parts[0]) / Number(parts[1]);
  }

  function clampToImage(p) {
    return {
      x: Math.min(Math.max(0, p.x), baseCanvas.width),
      y: Math.min(Math.max(0, p.y), baseCanvas.height)
    };
  }

  function cropPixels() {
    var x = Math.round(crop.x), y = Math.round(crop.y);
    return {
      x: x, y: y,
      w: Math.min(Math.round(crop.w), baseCanvas.width - x),
      h: Math.min(Math.round(crop.h), baseCanvas.height - y)
    };
  }

  function cropIsReal() {
    if (!crop) return false;
    var c = cropPixels();
    return c.x > 0 || c.y > 0 || c.w < baseCanvas.width || c.h < baseCanvas.height;
  }

  // The largest area of the chosen ratio, centred (the whole image for Free).
  function fitCropToRatio() {
    var W = baseCanvas.width, H = baseCanvas.height, r = cropRatio();
    var w = r ? Math.min(W, H * r) : W, h = r ? w / r : H;
    crop = { x: (W - w) / 2, y: (H - h) / 2, w: w, h: h };
    drawCropOverlay();
    refreshSave();
  }
  cropRatioEl.addEventListener("change", function () { if (mode === "crop") fitCropToRatio(); });

  // Area spanned by a fixed corner and the pointer, kept to the ratio and
  // inside the image.
  function setCropFromCorner(fixed, p) {
    var W = baseCanvas.width, H = baseCanvas.height, r = cropRatio();
    var sx = p.x >= fixed.x ? 1 : -1, sy = p.y >= fixed.y ? 1 : -1;
    var maxW = sx > 0 ? W - fixed.x : fixed.x, maxH = sy > 0 ? H - fixed.y : fixed.y;
    var w = Math.min(Math.abs(p.x - fixed.x), maxW), h = Math.min(Math.abs(p.y - fixed.y), maxH);
    if (r) {
      w = Math.max(w, h * r);
      h = w / r;
      if (w > maxW) { w = maxW; h = w / r; }
      if (h > maxH) { h = maxH; w = h * r; }
      if (w < CROP_MIN && h < CROP_MIN) { w = Math.min(CROP_MIN, maxW, maxH * r); h = w / r; }
    } else {
      w = Math.max(w, Math.min(CROP_MIN, maxW));
      h = Math.max(h, Math.min(CROP_MIN, maxH));
    }
    crop = { x: sx > 0 ? fixed.x : fixed.x - w, y: sy > 0 ? fixed.y : fixed.y - h, w: w, h: h };
  }

  function hitTestCrop(p) {
    var hitR = (HANDLE_HIT_SIZE / 2) * displayScale();
    var x1 = crop.x, y1 = crop.y, x2 = crop.x + crop.w, y2 = crop.y + crop.h;
    var corners = {
      nw: [x1, y1, x2, y2], ne: [x2, y1, x1, y2],
      se: [x2, y2, x1, y1], sw: [x1, y2, x2, y1]
    };
    for (var corner in corners) {
      var k = corners[corner];
      if (Math.hypot(p.x - k[0], p.y - k[1]) <= hitR) return { type: "resize", corner: corner, fixed: { x: k[2], y: k[3] } };
    }
    if (p.x >= x1 && p.x <= x2 && p.y >= y1 && p.y <= y2) return { type: "move" };
    return null;
  }

  function drawCropOverlay() {
    overlayCtx.clearRect(0, 0, overlay.width, overlay.height);
    cropSizeEl.textContent = "";
    if (!crop) return;
    var c = crop, scale = displayScale();
    overlayCtx.fillStyle = "rgba(0,0,0,.55)";
    overlayCtx.fillRect(0, 0, overlay.width, overlay.height);
    overlayCtx.clearRect(c.x, c.y, c.w, c.h);
    overlayCtx.strokeStyle = "rgba(255,255,255,.45)";  // rule of thirds
    overlayCtx.lineWidth = Math.max(1, scale);
    overlayCtx.beginPath();
    for (var i = 1; i < 3; i++) {
      overlayCtx.moveTo(c.x + c.w * i / 3, c.y);
      overlayCtx.lineTo(c.x + c.w * i / 3, c.y + c.h);
      overlayCtx.moveTo(c.x, c.y + c.h * i / 3);
      overlayCtx.lineTo(c.x + c.w, c.y + c.h * i / 3);
    }
    overlayCtx.stroke();
    overlayCtx.strokeStyle = "#fff";
    overlayCtx.lineWidth = 2 * scale;
    overlayCtx.strokeRect(c.x, c.y, c.w, c.h);
    var hs = HANDLE_VISUAL_SIZE * 1.2 * scale;
    overlayCtx.fillStyle = "#fff";
    [[c.x, c.y], [c.x + c.w, c.y], [c.x + c.w, c.y + c.h], [c.x, c.y + c.h]].forEach(function (k) {
      overlayCtx.fillRect(k[0] - hs / 2, k[1] - hs / 2, hs, hs);
    });
    var px = cropPixels();
    cropSizeEl.textContent = px.w + " × " + px.h;
  }

  function discardCrop() {
    crop = null;
    cropDrag = null;
    drawCropOverlay();
    refreshSave();
  }

  function applyCrop() {
    if (!cropIsReal()) { leaveCrop(); return; }
    var c = cropPixels();
    pushUndoSnapshot();  // Undo goes back to the uncropped image
    var copy = document.createElement("canvas");
    copy.width = c.w;
    copy.height = c.h;
    copy.getContext("2d").drawImage(baseCanvas, c.x, c.y, c.w, c.h, 0, 0, c.w, c.h);
    baseCanvas.width = c.w;
    baseCanvas.height = c.h;
    baseCtx.drawImage(copy, 0, 0);
    resizeCanvases(c.w, c.h);
    render();
    crop = null;
    leaveCrop();
    saveStatus.textContent = "Cropped to " + c.w + " × " + c.h + ".";
  }

  document.getElementById("edCropApply").addEventListener("click", applyCrop);
  document.getElementById("edCropCancel").addEventListener("click", leaveCrop);
  dialog.addEventListener("keydown", function (evt) {
    if (mode !== "crop" || evt.key !== "Enter" || /^(INPUT|TEXTAREA|SELECT)$/.test(evt.target.tagName)) return;
    evt.preventDefault();  // also keeps Enter from clicking the focused tool button
    applyCrop();
  });

  // ---------- Text ----------
  // A floating input at the click point; baked into the image on Enter or
  // on any committing action, discarded on Esc / Undo / Reset.
  function startTextEdit(evt, point) {
    var stageRect = stage.getBoundingClientRect();
    pendingText = point;
    textInput.value = "";
    textInput.style.fontSize = Number(fontEl.value) / displayScale() + "px";
    textInput.style.color = colorEl.value;
    textInput.style.left = evt.clientX - stageRect.left + "px";
    textInput.style.top = evt.clientY - stageRect.top + "px";
    textInput.hidden = false;
    textInput.focus();
  }

  function commitTextEdit() {
    if (!pendingText) return;
    var p = pendingText, text = textInput.value.trim();
    pendingText = null;
    textInput.value = "";
    textInput.hidden = true;
    if (!text) return;
    pushUndoSnapshot();
    baseCtx.fillStyle = colorEl.value;
    baseCtx.font = fontEl.value + "px sans-serif";
    baseCtx.textBaseline = "top";
    baseCtx.fillText(text, p.x, p.y);
    render();
  }

  function discardTextEdit() {
    pendingText = null;
    textInput.value = "";
    textInput.hidden = true;
  }

  textInput.addEventListener("keydown", function (evt) {
    if (evt.key === "Enter") { evt.preventDefault(); commitTextEdit(); }
    else if (evt.key === "Escape") { evt.preventDefault(); discardTextEdit(); }
  });
  // Safety net for focus loss the explicit handlers don't cover.
  textInput.addEventListener("blur", commitTextEdit);

  function commitPending() {
    commitActiveBox();
    commitTextEdit();
    if (mode === "crop") applyCrop();
  }

  // ---------- Undo / reset ----------
  undoBtn.addEventListener("click", function () {
    discardActiveBox();
    discardTextEdit();
    leaveCrop();
    var snap = undoStack.pop();
    if (!snap) return;
    undoBtn.disabled = undoStack.length === 0;
    if (!undoStack.length) setDirty(false);
    loadBase(snap);
  });

  resetBtn.addEventListener("click", function () {
    discardActiveBox();
    discardTextEdit();
    leaveCrop();
    if (!originalUrl || (dirty && !confirm("Drop all changes and go back to the saved image?"))) return;
    undoStack = [];
    undoBtn.disabled = true;
    setDirty(false);
    loadBase(originalUrl);
  });

  // ---------- Seedream ----------
  function setRef(file) {
    refFile = file;
    refPreview.replaceChildren();
    refPreview.hidden = !file;
    if (!file) return;
    var img = el("img");
    img.src = URL.createObjectURL(file);
    img.alt = "Extra reference";
    var remove = el("button", null, "×");
    remove.type = "button";
    remove.title = "Remove";
    remove.addEventListener("click", function () {
      URL.revokeObjectURL(img.src);
      setRef(null);
    });
    refPreview.appendChild(img);
    refPreview.appendChild(remove);
  }

  refInput.addEventListener("change", function () {
    var file = refInput.files[0];
    refInput.value = "";
    if (file) setRef(file);
  });

  function setBusy(on) {
    busy = on;
    generateBtn.disabled = on;
    refreshSave();
    resetBtn.disabled = on;
  }

  function addResult(img) {
    var card = el("div", "ed-result");
    var pic = el("img");
    pic.src = img.url;
    pic.alt = "Generated image";
    var use = el("button", "btn-small", "Use this");
    use.type = "button";
    use.addEventListener("click", function () {
      discardActiveBox();
      discardTextEdit();
      leaveCrop();
      pushUndoSnapshot();  // Undo goes back to the image before this result
      loadBase(img.url).then(function () {
        saveStatus.textContent = "Result loaded. Keep editing, or save it.";
      });
    });
    card.appendChild(pic);
    card.appendChild(use);
    resultsEl.prepend(card);
  }

  generateBtn.addEventListener("click", function () {
    var prompt = promptEl.value.trim();
    if (!prompt) { genStatus.textContent = "Enter a prompt first."; promptEl.focus(); return; }
    commitPending();
    setBusy(true);
    genStatus.textContent = "Generating with Seedream... this can take up to a minute.";
    new Promise(function (resolve) { baseCanvas.toBlob(resolve, "image/png"); })
      .then(function (blob) {
        if (!blob) throw new Error("Could not read the edited image.");
        var form = new FormData();
        form.append("prompt", prompt);
        form.append("image", blob, "edited.png");
        form.append("width", baseCanvas.width);
        form.append("height", baseCanvas.height);
        if (refFile) form.append("reference", refFile);
        return fetch("/api/design/" + encodeURIComponent(current.project) + "/generate", { method: "POST", body: form });
      })
      .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || "Generation failed"); return d; }); })
      .then(function (data) {
        data.images.forEach(addResult);
        genStatus.textContent = "Done in " + data.elapsed_s + " s (" + data.size + "). Click Use this to load a result.";
      })
      .catch(function (err) { genStatus.textContent = err.message; })
      .then(function () { setBusy(false); });
  });

  // ---------- Save ----------
  saveBtn.addEventListener("click", function () {
    commitPending();
    if (!dirty) return;
    setBusy(true);
    saveStatus.textContent = "Saving " + current.name + "...";
    // In the file's own format, so it replaces it under the same name.
    var image = baseCanvas.toDataURL(current.mime, 0.92);
    fetch("/api/design/" + encodeURIComponent(current.project) + "/file/" + encodePath(current.name), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image: image })
    })
      .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || "Save failed"); return d; }); })
      .then(function (data) {
        undoStack = [];
        undoBtn.disabled = true;
        setDirty(false);
        saveStatus.textContent = "Saved to the design folder.";
        if (current.onSaved) current.onSaved(data);
      })
      .catch(function (err) { saveStatus.textContent = err.message; })
      .then(function () { setBusy(false); });
  });

  window.DesignEditor = { open: open };
})();
