(function () {
  var data = window.REVIEW_DATA;
  var sessionId = data.sessionId;
  var pages = data.pages;
  var initialPage = data.initialPage;

  var canvas = document.getElementById("review-canvas");
  var container = document.getElementById("review-canvas-container");
  var zoomDisplay = document.getElementById("review-zoom-level");
  var frame = document.getElementById("extracted-frame");
  var split = document.getElementById("review-split");
  var divider = document.getElementById("review-divider");
  var thumbsNav = document.getElementById("review-thumbnails");

  var viewer = window.ImageViewer.create({
    canvas: canvas,
    container: container,
    mode: "review",
    zoomDisplay: zoomDisplay,
  });

  var errPanel = document.getElementById("extraction-error");
  var currentPageIndex = initialPage;
  var loadToken = 0;

  function showExtracted() {
    errPanel.hidden = true;
    frame.style.display = "";
  }

  function showExtractedError() {
    errPanel.hidden = false;
    frame.style.display = "none";
  }

  function loadExtractedPage(index) {
    currentPageIndex = index;
    var token = ++loadToken;
    var url = "/extracted/" + encodeURIComponent(sessionId) + "/page-" + index + ".html?embed=1";
    fetch(url)
      .then(function (resp) {
        if (token !== loadToken) return;
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        frame.src = url;
        showExtracted();
      })
      .catch(function () {
        if (token !== loadToken) return;
        showExtractedError();
      });
  }

  function selectPage(index, push) {
    if (push === undefined) push = true;
    viewer.setImage("/pages/" + encodeURIComponent(sessionId) + "/" + pages[index].path);
    loadExtractedPage(index);
    if (push) history.pushState({ page: index }, "", "?page=" + index);
    updateThumbnails(index);
  }

  function updateThumbnails(activeIndex) {
    var buttons = thumbsNav.querySelectorAll("button");
    for (var i = 0; i < buttons.length; i++) {
      if (i === activeIndex) {
        buttons[i].setAttribute("aria-current", "page");
      } else {
        buttons[i].removeAttribute("aria-current");
      }
    }
  }

  function setSplit(percent) {
    var value = Math.max(25, Math.min(75, percent));
    split.style.setProperty("--source-width", value + "%");
    divider.setAttribute("aria-valuenow", String(Math.round(value)));
    localStorage.setItem("extraction-review-split", String(value));
    viewer.resize({ refit: false });
  }

  function buildThumbnails() {
    for (var i = 0; i < pages.length; i++) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.setAttribute("aria-label", "Page " + (i + 1));
      var img = document.createElement("img");
      img.src = "/pages/" + encodeURIComponent(sessionId) + "/" + pages[i].path;
      img.alt = "";
      btn.appendChild(img);
      btn.addEventListener("click", (function (idx) {
        return function () { selectPage(idx); };
      })(i));
      thumbsNav.appendChild(btn);
    }
  }

  divider.addEventListener("pointerdown", function (e) {
    e.preventDefault();
    divider.setPointerCapture(e.pointerId);
    var startX = e.clientX;
    var startWidth = split.getBoundingClientRect().width * (parseFloat(getComputedStyle(split).getPropertyValue("--source-width")) || 50) / 100;
    var totalWidth = split.getBoundingClientRect().width;

    function onMove(ev) {
      var dx = ev.clientX - startX;
      var newPercent = ((startWidth + dx) / totalWidth) * 100;
      setSplit(newPercent);
    }
    function onUp(ev) {
      divider.releasePointerCapture(ev.pointerId);
      divider.removeEventListener("pointermove", onMove);
      divider.removeEventListener("pointerup", onUp);
    }
    divider.addEventListener("pointermove", onMove);
    divider.addEventListener("pointerup", onUp);
  });

  divider.addEventListener("keydown", function (e) {
    var current = Number(divider.getAttribute("aria-valuenow")) || 50;
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      setSplit(current - 5);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      setSplit(current + 5);
    }
  });

  window.addEventListener("popstate", function () {
    var params = new URLSearchParams(location.search);
    var p = params.get("page");
    if (p !== null) {
      var index = Number(p);
      if (index >= 0 && index < pages.length) {
        selectPage(index, false);
      }
    }
  });

  document.getElementById("review-zoom-in").addEventListener("click", function () { viewer.zoomIn(); });
  document.getElementById("review-zoom-out").addEventListener("click", function () { viewer.zoomOut(); });
  document.getElementById("review-reset").addEventListener("click", function () { viewer.resize({ refit: true }); });
  document.getElementById("extraction-retry-btn").addEventListener("click", function () { loadExtractedPage(currentPageIndex); });

  var savedSplit = localStorage.getItem("extraction-review-split");
  if (savedSplit !== null) {
    setSplit(Number(savedSplit));
  }

  buildThumbnails();
  selectPage(initialPage, false);
  history.replaceState({ page: initialPage }, "", "?page=" + initialPage);
})();
