// Botão "Copiar": copia o texto da resposta para colar no formulário da empresa.
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const el = document.querySelector(btn.dataset.copy);
  if (!el) return;
  navigator.clipboard.writeText(el.value ?? el.textContent).then(() => {
    const old = btn.textContent;
    btn.textContent = "Copiado ✓";
    setTimeout(() => (btn.textContent = old), 1200);
  });
});
