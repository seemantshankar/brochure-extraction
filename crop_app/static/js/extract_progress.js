document.addEventListener("DOMContentLoaded", function () {
  var sessionId = window.SESSION_ID;
  var R = 65;
  var CIRCUMFERENCE = 2 * Math.PI * R;

  var gaugeCircle = document.getElementById("gauge-circle");
  var pctText = document.getElementById("pct-text");
  var statusText = document.getElementById("status-text");
  var actionLog = document.getElementById("action-log");
  var progressArea = document.getElementById("progress-area");
  var resultArea = document.getElementById("result-area");
  var errorArea = document.getElementById("error-area");
  var errorMsg = document.getElementById("error-msg");
  var openBtn = document.getElementById("open-btn");

  openBtn.href = "/extracted/" + sessionId + "/extraction.html";

  function setProgress(pct) {
    pct = Math.max(0, Math.min(100, pct));
    var offset = CIRCUMFERENCE - (pct / 100) * CIRCUMFERENCE;
    gaugeCircle.style.strokeDasharray = CIRCUMFERENCE;
    gaugeCircle.style.strokeDashoffset = offset;
    pctText.textContent = Math.round(pct) + "%";
  }

  function getTimestamp() {
    var d = new Date();
    function pad(n) { return n < 10 ? "0" + n : n; }
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  function appendLog(message) {
    var entry = document.createElement("div");
    entry.className = "log-entry";
    entry.innerHTML = '<span class="log-time">[' + getTimestamp() + ']</span> ' + message;
    actionLog.appendChild(entry);
    actionLog.scrollTop = actionLog.scrollHeight;
  }

  function showResult() {
    progressArea.hidden = true;
    resultArea.hidden = false;
  }

  function showError(message) {
    progressArea.hidden = true;
    errorArea.hidden = false;
    errorMsg.textContent = message || "Extraction failed. Please check server logs.";
  }

  setProgress(3);
  appendLog("Starting extraction pipeline...");

  var source = new EventSource("/extract-progress/" + sessionId);

  source.onmessage = function (e) {
    var data;
    try { data = JSON.parse(e.data); } catch (_) { return; }

    if (data.status === "starting") {
      setProgress(10);
      statusText.textContent = "Processing pages...";
      appendLog("Extraction server connected. Working...");

    } else if (data.status === "progress") {
      var pct = 10 + ((data.page || 0) / Math.max(1, data.totalPages || 1)) * 80;
      setProgress(Math.min(pct, 90));
      statusText.textContent = "Page " + ((data.page || 0) + 1) + " of " + (data.totalPages || "?");
      if (data.log) appendLog(data.log);

    } else if (data.status === "done") {
      setProgress(100);
      pctText.textContent = "100%";
      statusText.textContent = "Complete";
      appendLog("HTML document generated successfully.");
      setTimeout(showResult, 700);
      source.close();

    } else if (data.status === "error") {
      setProgress(100);
      pctText.textContent = "✗";
      statusText.textContent = "Failed";
      appendLog("ERROR: " + (data.message || "unknown"));
      setTimeout(function () { showError(data.message); }, 500);
      source.close();
    }
  };

  source.onerror = function () {
    appendLog("EventSource error. Retrying...");
  };
});
