(() => {
  const $ = (id) => document.getElementById(id);
  let token = "", current = null, history = [], busy = false, shownAt = 0;
  const elapsed = () => Date.now() - shownAt;
  function toast(message) { const node = $("toast"); node.textContent = message; node.classList.add("show"); setTimeout(() => node.classList.remove("show"), 2200); }
  async function api(url, options = {}) { const response = await fetch(url, options); const data = await response.json(); if (!response.ok) throw new Error(data.error || "Request failed"); return data; }
  function hud(stats) {
    $("xp").textContent = `${stats.xp} XP`;
    $("level").textContent = stats.level || "";
    $("streak").textContent = `🔥 ${stats.streak || 0}`;
    $("progress").value = stats.percent || 0;
    $("progress-label").textContent = `${stats.completed} / ${stats.total} lines`;
    $("pace").textContent = stats.lines_per_hour ? `${stats.lines_per_hour} lines/h` : "warming up…";
    const d = stats.decisions || {};
    $("verified-count").textContent = `verified ${stats.verified || 0} · flagged ${d.flag || 0} · skipped ${d.skip || 0}`;
  }
  function render(entry) {
    current = entry; shownAt = Date.now();
    $("empty").classList.add("hidden"); $("card").classList.remove("hidden");
    $("title").textContent = entry.title || "Untitled";
    $("entry-label").textContent = `p${entry.page} · conf ${entry.line_confidence ?? "?"}${entry.suspicious ? " · suspicious" : ""}`;
    $("cand-a").textContent = entry.tesseract || "";
    $("cand-b").textContent = entry.qwen || "";
    $("conf-b").textContent = entry.qwen_derived ? "(Qwen edit in this line)" : "(no Qwen edit here — A is the draft)";
    $("conf-a").textContent = "";
    $("ctx-a").textContent = entry.context_original || "—";
    $("ctx-b").textContent = entry.context_proposed || "—";
    $("verified").value = entry.review ? entry.review.verified_text : (entry.tesseract || "");
    const r = entry.review;
    $("saved").textContent = r ? `Saved · ${r.decision} · +${r.xp} XP` : "Not saved";
    $("crop").classList.remove("hidden"); $("crop-unavailable").classList.add("hidden");
    $("crop").src = `/api/task/${entry.id}/crop?cache=${entry.id}`;
    $("crop").onerror = () => { $("crop").classList.add("hidden"); $("crop-unavailable").classList.remove("hidden"); };
    $("back").disabled = history.length === 0;
    $("verified").focus();
  }
  async function load(entry) { if (!entry) { $("card").classList.add("hidden"); $("empty").classList.remove("hidden"); return; } render(entry); }
  async function refresh() {
    const data = await api("/api/session"); token = data.token;
    hud(data.stats || data.progress); await load(data.next);
  }
  async function save(kind) {
    if (!current || busy) return; busy = true;
    const before = $("verified").value;
    if (kind === "tesseract") $("verified").value = current.tesseract || "";
    if (kind === "qwen") $("verified").value = current.qwen || current.tesseract || "";
    try {
      const data = await api(`/api/task/${current.id}/review`, { method: "POST",
        headers: { "Content-Type": "application/json", "X-Gold-Token": token },
        body: JSON.stringify({ verified_text: $("verified").value, decision: kind,
          revision: current.review ? current.review.revision : 0, elapsed_ms: elapsed() }) });
      history.push(current.id); current = data.entry;
      hud(data.progress);
      toast(kind === "skip" ? "Skipped — no points" : `+${data.entry.review.xp} XP — next line ✨`);
      await load((await api("/api/session")).next);
    } catch (error) { $("verified").value = before; toast(error.message); } finally { busy = false; }
  }
  $("fill-a").addEventListener("click", () => { if (current) $("verified").value = current.tesseract || ""; });
  $("fill-b").addEventListener("click", () => { if (current) $("verified").value = current.qwen || current.tesseract || ""; });
  document.querySelectorAll("[data-decision]").forEach(button => button.addEventListener("click", () => save(button.dataset.decision)));
  $("back").addEventListener("click", async () => { const id = history.pop(); if (id) await load(await api(`/api/task/${id}`)); });
  document.addEventListener("keydown", (event) => {
    if (event.target.matches("textarea,input") && !["F1","F2","F3"].includes(event.key)) {
      if (!(event.ctrlKey && ["1","2","3"].includes(event.key))) return;
    }
    if (event.key === "1") save("tesseract");
    else if (event.key === "2") save("qwen");
    else if (event.key === "3") save("manual");
    else if (event.key === "f" || event.key === "F") save("flag");
    else if (event.key === "s" || event.key === "S" || event.key === "ArrowDown") save("skip");
  });
  let swipe = null;
  $("card").addEventListener("pointerdown", (event) => {
    if (event.target.closest("button,a,textarea,input,summary")) return;
    swipe = { x: event.clientX, y: event.clientY };
    $("card").classList.add("swiping");
    $("card").setPointerCapture?.(event.pointerId);
  });
  $("card").addEventListener("pointermove", (event) => {
    if (!swipe || busy) return;
    const dx = event.clientX - swipe.x;
    if (Math.abs(dx) < 8) return;
    $("card").style.transform = `translateX(${dx}px) rotate(${dx / 28}deg)`;
  });
  $("card").addEventListener("pointerup", (event) => {
    if (!swipe || busy) return;
    const dx = event.clientX - swipe.x;
    const dy = event.clientY - swipe.y;
    swipe = null;
    $("card").classList.remove("swiping");
    $("card").style.transform = "";
    if (Math.abs(dx) < 110 || Math.abs(dx) < Math.abs(dy)) return;
    save(dx < 0 ? "tesseract" : "qwen");
  });
  $("card").addEventListener("pointercancel", () => {
    swipe = null;
    $("card").classList.remove("swiping");
    $("card").style.transform = "";
  });
  refresh().catch(error => toast(error.message));
})();
