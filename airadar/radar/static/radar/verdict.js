(() => {
  const btn = document.getElementById("verdict-btn");
  const out = document.getElementById("verdict-out");
  if (!btn || !out) return;
  const token = document.querySelector('meta[name="csrf-token"]').content;

  const show = (text, kind) => {
    out.textContent = text;     // textContent: відповідь моделі не парситься як HTML
    out.className = "mt-3 alert " + (kind === "ok" ? "alert-light border" : "alert-warning");
  };

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    out.setAttribute("aria-busy", "true");
    show("Аналізую… це може зайняти до 45 секунд.", "wait");
    try {
      const res = await fetch(btn.dataset.url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": token },
        body: JSON.stringify({ domains: btn.dataset.domains.split(",") }),
      });
      let data = null;
      try { data = await res.json(); } catch (e) { /* відповідь не JSON: таймаут сервера, CSRF тощо */ }
      if (res.ok && data && data.text) show(data.text, "ok");
      else show((data && data.error) || `Сервер повернув помилку (HTTP ${res.status}). Спробуйте пізніше.`, "error");
    } catch (e) {
      show("Помилка мережі. Перевірте з'єднання й повторіть.", "error");
    } finally {
      out.removeAttribute("aria-busy");
      btn.disabled = false;
    }
  });
})();
