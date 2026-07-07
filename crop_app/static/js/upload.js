document.addEventListener("DOMContentLoaded", () => {
  const uploadForm = document.getElementById("upload-form");
  const dropZone = document.getElementById("drop-zone");
  const fileInput = document.getElementById("file-input");
  const filePreview = document.getElementById("file-preview");
  const submitBtn = document.getElementById("submit-btn");
  const btnText = submitBtn.querySelector(".btn-text");
  const btnLoader = submitBtn.querySelector(".btn-loader");
  const spinner = submitBtn.querySelector(".spinner");
  const resultsSection = document.getElementById("results-section");
  const pageGrid = document.getElementById("page-grid");

  const ACCEPTED_TYPES = [".pdf", ".png", ".jpg", ".jpeg"];
  const MAX_SIZE = 200 * 1024 * 1024;

  function formatSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  function showError(msg) {
    const el = document.createElement("div");
    el.className = "error-message";
    el.textContent = msg;
    filePreview.appendChild(el);
  }

  function validateFiles(files) {
    const list = Array.from(files);
    if (!list.length) return "Please select at least one file.";

    for (const f of list) {
      const ext = "." + f.name.split(".").pop().toLowerCase();
      if (!ACCEPTED_TYPES.includes(ext)) {
        return `File "${f.name}" has an unsupported format. Accepted: PDF, PNG, JPG, JPEG.`;
      }
    }

    const totalSize = list.reduce((sum, f) => sum + f.size, 0);
    if (totalSize > MAX_SIZE) {
      return `Total file size (${formatSize(totalSize)}) exceeds the 200 MB limit.`;
    }

    return null;
  }

  function renderPreview(files) {
    filePreview.innerHTML = "";
    if (!files.length) return;

    const ul = document.createElement("ul");
    ul.className = "file-list";

    Array.from(files).forEach((f) => {
      const li = document.createElement("li");
      li.textContent = `${f.name} (${formatSize(f.size)})`;
      ul.appendChild(li);
    });

    filePreview.appendChild(ul);
  }

  function setLoading(loading) {
    submitBtn.disabled = loading;
    btnText.hidden = loading;
    btnLoader.hidden = !loading;
  }

  dropZone.addEventListener("click", () => fileInput.click());

  dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropZone.classList.add("drag-over");
  });

  dropZone.addEventListener("dragenter", (e) => {
    e.preventDefault();
    dropZone.classList.add("drag-over");
  });

  dropZone.addEventListener("dragleave", () => {
    dropZone.classList.remove("drag-over");
  });

  dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("drag-over");
    fileInput.files = e.dataTransfer.files;
    fileInput.dispatchEvent(new Event("change"));
  });

  fileInput.addEventListener("change", () => {
    const err = validateFiles(fileInput.files);
    filePreview.innerHTML = "";

    if (err) {
      showError(err);
      submitBtn.disabled = true;
      return;
    }

    renderPreview(fileInput.files);
    submitBtn.disabled = false;
  });

  uploadForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    filePreview.innerHTML = "";
    setLoading(true);

    const formData = new FormData(uploadForm);

    try {
      const uploadRes = await fetch("/upload", { method: "POST", body: formData });

      if (!uploadRes.ok) throw new Error("Upload failed");

      const { session_id, page_count } = await uploadRes.json();

      filePreview.textContent = `Uploaded ${page_count} page${page_count !== 1 ? "s" : ""}. Analyzing…`;

      const analyzeRes = await fetch(`/analyze/${session_id}`, { method: "POST" });

      if (!analyzeRes.ok) throw new Error("Analysis failed");

      const analysis = await analyzeRes.json();
      renderPageGrid(analysis, session_id);
      setLoading(false);
      btnText.textContent = "Analysis Complete";
      submitBtn.disabled = true;
    } catch (err) {
      showError(err.message);
      setLoading(false);
    }
  });

  function renderPageGrid(analysis, sessionId) {
    pageGrid.innerHTML = "";
    resultsSection.hidden = false;

    analysis.pages.forEach((page, index) => {
      const card = document.createElement("div");
      card.className = "page-card";

      const img = document.createElement("img");
      img.src = `/pages/${sessionId}/${page.path}`;
      card.appendChild(img);

      const badge = document.createElement("span");
      badge.className = "badge";

      if (page.classification === "Complex") {
        badge.classList.add("badge-complex");
        badge.textContent = "Complex";
        card.classList.add("page-complex");
        card.addEventListener("click", () => {
          window.location = `/annotate/${sessionId}?page=${index}`;
        });
      } else if (page.classification === "Simple") {
        badge.classList.add("badge-simple");
        badge.textContent = "Simple";
      } else {
        badge.classList.add("badge-pending");
        badge.textContent = "Pending";
      }

      card.appendChild(badge);
      pageGrid.appendChild(card);
    });
  }
});
