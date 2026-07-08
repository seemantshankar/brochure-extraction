document.addEventListener("DOMContentLoaded", function () {
  var tooltip = null;

  function getTooltip() {
    if (!tooltip) {
      tooltip = document.createElement("div");
      tooltip.className = "footnote-tooltip";
      tooltip.style.display = "none";
      document.body.appendChild(tooltip);
    }
    return tooltip;
  }

  function positionTooltip(ref) {
    if (!tooltip) return;
    var rect = ref.getBoundingClientRect();
    var ttRect = tooltip.getBoundingClientRect();
    var top = rect.bottom + 6;
    var left = rect.left;
    if (left + ttRect.width > window.innerWidth - 16) {
      left = Math.max(8, window.innerWidth - ttRect.width - 16);
    }
    if (top + ttRect.height > window.innerHeight - 16) {
      top = rect.top - ttRect.height - 6;
    }
    tooltip.style.top = top + "px";
    tooltip.style.left = left + "px";
  }

  document.addEventListener("mouseover", function (e) {
    var ref = e.target.closest(".footnote-ref");
    if (!ref) return;
    var fnText = ref.getAttribute("data-footnote");
    if (!fnText) return;
    var tt = getTooltip();
    tt.textContent = fnText;
    tt.style.display = "block";
    positionTooltip(ref);
  });

  document.addEventListener("mouseout", function (e) {
    var ref = e.target.closest(".footnote-ref");
    if (!ref) return;
    if (tooltip) tooltip.style.display = "none";
  });

  document.addEventListener("mousemove", function (e) {
    if (tooltip && tooltip.style.display !== "none") {
      var ref = e.target.closest(".footnote-ref");
      if (ref) positionTooltip(ref);
    }
  });

  document.addEventListener("click", function (e) {
    var ref = e.target.closest(".footnote-ref");
    if (ref) {
      e.preventDefault();
      var targetId = ref.getAttribute("href");
      if (targetId && targetId.startsWith("#")) {
        var target = document.getElementById(targetId.slice(1));
        if (target) {
          target.scrollIntoView({ behavior: "smooth", block: "center" });
          target.classList.remove("fn-flash");
          void target.offsetWidth;
          target.classList.add("fn-flash");
          setTimeout(function () { target.classList.remove("fn-flash"); }, 1000);
        }
      }
      if (tooltip) tooltip.style.display = "none";
      return;
    }

    var back = e.target.closest(".footnote-back");
    if (back) {
      e.preventDefault();
      var href = back.getAttribute("href");
      if (href && href.startsWith("#")) {
        var srcRef = document.getElementById(href.slice(1));
        if (srcRef) {
          srcRef.scrollIntoView({ behavior: "smooth", block: "center" });
          srcRef.style.transition = "background 0.3s";
          srcRef.style.background = "#fef9c3";
          setTimeout(function () { srcRef.style.background = ""; }, 1000);
        }
      }
      return;
    }
  });

  var tables = document.querySelectorAll("table");
  tables.forEach(function (table) {
    var wrap = document.createElement("div");
    wrap.className = "table-scroll-wrap";
    var container = document.createElement("div");
    container.className = "table-container";

    var btn = document.createElement("button");
    btn.className = "copy-table-btn";
    btn.textContent = "Copy";
    btn.type = "button";
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      navigator.clipboard.writeText(table.outerHTML).then(function () {
        btn.textContent = "Copied!";
        setTimeout(function () { btn.textContent = "Copy"; }, 1500);
      }).catch(function () {
        var ta = document.createElement("textarea");
        ta.value = table.outerHTML;
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); } catch (err) {}
        document.body.removeChild(ta);
        btn.textContent = "Copied!";
        setTimeout(function () { btn.textContent = "Copy"; }, 1500);
      });
    });

    table.parentNode.insertBefore(wrap, table);
    wrap.appendChild(container);
    container.appendChild(btn);
    container.appendChild(table);
  });

  var tocLinks = document.querySelectorAll(".toc-sidebar a");
  tocLinks.forEach(function (link) {
    link.addEventListener("click", function () {
      tocLinks.forEach(function (l) { l.classList.remove("active"); });
      link.classList.add("active");
    });
  });

  var pageObserver = null;
  if (window.IntersectionObserver) {
    var pages = document.querySelectorAll(".page");
    pageObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          var href = "#" + entry.target.id;
          var link = document.querySelector('.toc-sidebar a[href="' + href + '"]');
          if (link) {
            tocLinks.forEach(function (l) { l.classList.remove("active"); });
            link.classList.add("active");
          }
        }
      });
    }, { threshold: 0.4 });
    pages.forEach(function (p) { pageObserver.observe(p); });
  }
});
