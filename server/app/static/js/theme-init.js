// Pré-applique le thème mémorisé AVANT le rendu (évite le flash clair→sombre).
// Chargé en <head>, en bloquant, avant la feuille de style. Externalisé pour
// permettre une Content-Security-Policy stricte (script-src 'self', sans inline).
try {
  if (localStorage.getItem("ts-theme") === "light") {
    document.documentElement.classList.add("theme-light");
  }
} catch (e) {}
