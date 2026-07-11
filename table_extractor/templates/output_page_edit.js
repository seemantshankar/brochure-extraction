document.addEventListener("DOMContentLoaded", function () {
  var searchParams = new URLSearchParams(window.location.search);
  if (searchParams.get("embed") === "1") {
    document.documentElement.classList.add("embedded-review");
    document.body.classList.add("embedded-review");
  }

  var EDITABLE_SELECTORS = [
    "td", "th", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "dd", "dt", "span.field",
  ];
  var editedElements = new Set();
  var saveButton = null;
  var toast = null;

  function getTextNodes(el) {
    var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
    var nodes = [];
    var node;
    while ((node = walker.nextNode())) {
      var text = node.nodeValue.trim();
      if (text.length > 0 && !node.parentNode.closest("a, sup, script, style")) {
        nodes.push(node);
      }
    }
    return nodes;
  }

  function markEdited(el) {
    if (!el.classList.contains("edited")) {
      el.classList.add("edited");
      editedElements.add(el);
    }
    showSaveButton();
  }

  function getSaveUrl() {
    var match = window.location.pathname.match(/\/extracted\/([^/]+)\/page-(\d+)\.html$/);
    if (!match) return "";
    return "/save-page/" + encodeURIComponent(match[1]) + "/" + match[2];
  }

  function showSaveButton() {
    if (saveButton) return;
    saveButton = document.createElement("button");
    saveButton.className = "save-btn";
    saveButton.textContent = "Save Changes";
    saveButton.type = "button";
    saveButton.addEventListener("click", function () {
      var saveUrl = getSaveUrl();
      if (!saveUrl) return;
      saveButton.disabled = true;
      saveButton.textContent = "Saving...";
      var saveRoot = document.documentElement.cloneNode(true);
      saveRoot.querySelectorAll(".save-btn, .save-toast, .save-error").forEach(function (n) { n.remove(); });
      saveRoot.querySelectorAll("input.inline-edit-input").forEach(function (inp) {
        inp.parentNode.replaceChild(document.createTextNode(inp.value != null ? inp.value : ""), inp);
      });
      saveRoot.querySelectorAll("[contenteditable]").forEach(function (el) {
        el.removeAttribute("contenteditable");
      });
      saveRoot.querySelectorAll(".inline-edit-input, .edited").forEach(function (el) {
        el.classList.remove("inline-edit-input", "edited");
      });
      var doctype = document.doctype
        ? new XMLSerializer().serializeToString(document.doctype)
        : "<!DOCTYPE html>";
      var fullHtml = doctype + "\n" + saveRoot.outerHTML;
      fetch(saveUrl, {
        method: "POST",
        headers: { "Content-Type": "text/html" },
        body: fullHtml,
      })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.status === "ok") {
          editedElements.clear();
          document.querySelectorAll(".edited").forEach(function (el) {
            el.classList.remove("edited");
          });
          if (toast) toast.remove();
          toast = document.createElement("div");
          toast.className = "save-toast";
          toast.textContent = "Saved";
          document.body.appendChild(toast);
          setTimeout(function () { if (toast) toast.remove(); }, 2000);
          saveButton.remove();
          saveButton = null;
        } else {
          saveButton.disabled = false;
          saveButton.textContent = "Save Changes";
          var prevErr = saveButton.parentNode.querySelector(".save-error");
          if (prevErr) prevErr.remove();
          var err = document.createElement("span");
          err.className = "save-error";
          err.textContent = data.message || "Save failed";
          saveButton.parentNode.insertBefore(err, saveButton.nextSibling);
        }
      })
      .catch(function () {
        saveButton.disabled = false;
        saveButton.textContent = "Save Changes";
      });
    });
    document.body.appendChild(saveButton);
  }

  EDITABLE_SELECTORS.forEach(function (sel) {
    document.querySelectorAll(sel).forEach(function (el) {
      if (el.querySelector("input, textarea, select")) return;
      var textNodes = getTextNodes(el);
      if (textNodes.length === 0) return;

      if (textNodes.length === 1 && el.children.length === 0) {
        var input = document.createElement("input");
        input.type = "text";
        input.setAttribute("value", textNodes[0].nodeValue);
        input.value = textNodes[0].nodeValue;
        input.className = "inline-edit-input";
        textNodes[0].parentNode.replaceChild(input, textNodes[0]);
        input.addEventListener("input", function () { input.setAttribute("value", input.value); markEdited(input); });
        input.addEventListener("focus", function () { input.select(); });
      } else {
        el.setAttribute("contenteditable", "true");
        el.addEventListener("input", function () { markEdited(el); });
      }
    });
  });

  var pageNav = document.querySelector(".page-nav");
  if (pageNav) {
    pageNav.setAttribute("data-nav", "true");
  }

  var observer = new MutationObserver(function (mutations) {
    mutations.forEach(function (mutation) {
      if (mutation.type === "childList" || mutation.type === "characterData") {
        var target = mutation.target;
        if (target.nodeType === Node.TEXT_NODE) {
          target = target.parentNode;
        }
        if (target && target.matches) {
          EDITABLE_SELECTORS.forEach(function (sel) {
            if (target.matches(sel)) {
              markEdited(target);
            }
          });
        }
      }
    });
  });

  document.querySelectorAll(EDITABLE_SELECTORS.join(", ")).forEach(function (el) {
    observer.observe(el, { childList: true, characterData: true, subtree: true });
  });
});
