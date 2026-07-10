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
    var timeSpan = document.createElement("span");
    timeSpan.className = "log-time";
    timeSpan.textContent = "[" + getTimestamp() + "] ";
    var msgSpan = document.createElement("span");
    msgSpan.textContent = message;
    entry.appendChild(timeSpan);
    entry.appendChild(msgSpan);
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

  function showRetry(message, url) {
    progressArea.hidden = true;
    var retryArea = document.getElementById("retry-area");
    var retryMsg = document.getElementById("retry-msg");
    var retryBtn = document.getElementById("retry-btn");
    retryMsg.textContent = message;
    retryBtn.disabled = false;
    retryBtn.textContent = "Retry";
    retryBtn.onclick = function () {
      retryBtn.disabled = true;
      retryBtn.textContent = "Retrying...";
      fetch(url, { method: "POST" })
        .then(function () { window.location.reload(); })
        .catch(function () {
          retryBtn.disabled = false;
          retryBtn.textContent = "Retry";
        });
    };
    retryArea.hidden = false;
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
      var total = data.total || data.totalPages || 1;
      var done = data.progress || data.page || 0;
      var pct = 10 + (done / Math.max(1, total)) * 80;
      setProgress(Math.min(pct, 90));
      statusText.textContent = "Extracted " + done + " of " + total + " regions...";
      if (data.log) appendLog(data.log);

    } else if (data.status === "done") {
      setProgress(100);
      pctText.textContent = "100%";
      statusText.textContent = "Complete";
      appendLog("HTML document generated successfully.");
      setTimeout(showResult, 700);
      source.close();

    } else if (data.status === "cancelled") {
      setProgress(100);
      pctText.textContent = "✗";
      statusText.textContent = "Cancelled";
      appendLog("Extraction cancelled.");
      source.close();

    } else if (data.status === "paused") {
      appendLog("Interrupted — click Resume to continue.");
      showRetry("Extraction interrupted. Resume from where it left off?", "/extract-html/" + sessionId);

    } else if (data.status === "idle") {
      appendLog("No extraction started yet.");
      showRetry("Start extraction?", "/extract-html/" + sessionId);

    } else if (data.status === "error") {
      setProgress(100);
      pctText.textContent = "✗";
      statusText.textContent = "Failed";
      appendLog("ERROR: " + (data.message || "unknown"));
      var isAuth = ["auth", "credits"].includes(data.error_type);
      if (isAuth) {
        showRetry(
          (data.message || "Authentication/credit failure.") + " Fix it, then click Retry.",
          "/extract-html/" + sessionId + "?retry_nonretryable=true"
        );
      } else {
        showError(data.message);
      }
      source.close();
    }
  };

  source.onerror = function () {
    appendLog("EventSource error. Retrying...");
  };
});
