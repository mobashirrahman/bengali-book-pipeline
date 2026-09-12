(() => {
  const $ = (id) => document.getElementById(id);
  let token = "", current = null, history = [], busy = false;
  const decision = (value) => save(value);
  function toast(message) { const node = $("toast"); node.textContent = message; node.classList.add("show"); setTimeout(() => node.classList.remove("show"), 2200); }
  async function api(url, options = {}) { const response = await fetch(url, options); const data = await response.json(); if (!response.ok) throw new Error(data.error || "Request failed"); return data; }
  function render(entry) {
    current = entry; $("empty").classList.add("hidden"); $("card").classList.remove("hidden");
    $("title").textContent = entry.title || "Untitled"; $("entry-label").textContent = `Page ${entry.page || "?"} · ${entry.audit_id || entry.id.slice(0,8)}`;
    $("original").textContent = entry.original || ""; $("proposed").textContent = entry.proposed || entry.original || "";
    const decisions = entry.decisions || []; const reasons = decisions.map(item => item.reason).filter(Boolean); $("reason").textContent = reasons.length ? reasons.join(" · ") : (entry.reason || "");
    $("verified").value = entry.review ? entry.review.verified_text : (entry.original || ""); $("saved").textContent = entry.review ? `Saved · ${entry.review.decision}` : "Not saved";
    $("crop").classList.remove("hidden"); $("crop-unavailable").classList.add("hidden"); $("crop").src = `/api/entry/${entry.id}/crop?cache=${entry.id}`;
    $("crop").onerror = () => { $("crop").classList.add("hidden"); $("crop-unavailable").classList.remove("hidden"); };
    $("back").disabled = history.length === 0;
  }
  async function load(entry) { if (!entry) { $("card").classList.add("hidden"); $("empty").classList.remove("hidden"); return; } render(entry); }
  async function refresh() { const data = await api("/api/session"); token = data.token; const p = data.progress; $("progress").value = p.percent; $("progress-label").textContent = `${p.completed} / ${p.total} reviewed`; await load(data.next); }
  async function save(kind) {
    if (!current || busy) return; busy = true; const value = $("verified").value; if (kind === "keep") $("verified").value = current.original || ""; if (kind === "apply") $("verified").value = current.proposed || current.original || "";
    try { const acceptedEdits = kind === "apply" ? (current.decisions || []).filter(item => item && item.status === "accepted") : []; const data = await api(`/api/entry/${current.id}/review`, { method:"POST", headers:{"Content-Type":"application/json","X-Review-Token":token}, body:JSON.stringify({verified_text:$("verified").value, decision:kind, revision:current.review ? current.review.revision : 0, accepted_edits:acceptedEdits}) }); history.push(current.id); current = data.entry; toast("Saved — next card loading ✨"); const next = (await api("/api/session")).next; $("progress").value = data.progress.percent; $("progress-label").textContent = `${data.progress.completed} / ${data.progress.total} reviewed`; await load(next); }
    catch (error) { $("verified").value = value; toast(error.message); } finally { busy = false; }
  }
  $("apply").addEventListener("click", () => save("apply"));
  document.querySelectorAll("[data-decision]").forEach(button => button.addEventListener("click", () => decision(button.dataset.decision)));
  $("back").addEventListener("click", async () => { const id = history.pop(); if (id) await load(await api(`/api/entry/${id}`)); });
  document.addEventListener("keydown", (event) => { if (event.target.matches("textarea,input")) return; if (event.key === "ArrowLeft") decision("keep"); if (event.key === "ArrowRight") decision("apply"); if (event.key === "ArrowDown") decision("skip"); });
  let swipeStart = null;
  $("card").addEventListener("pointerdown", (event) => { if (event.target.closest("button,a,textarea,input")) return; swipeStart = {x:event.clientX, y:event.clientY}; });
  $("card").addEventListener("pointerup", (event) => { if (!swipeStart || busy) return; const dx = event.clientX - swipeStart.x; const dy = event.clientY - swipeStart.y; swipeStart = null; if (Math.abs(dx) < 100 || Math.abs(dx) < Math.abs(dy)) return; decision(dx < 0 ? "keep" : "apply"); });
  refresh().catch(error => toast(error.message));
})();
