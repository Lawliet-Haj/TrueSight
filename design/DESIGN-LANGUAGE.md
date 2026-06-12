# ParcVue — système de design (« poste de pilotage »)

> Identité visuelle de référence. Le prototype `design/prototype.html` en est l'implémentation.
> Toutes les pages du dashboard doivent appliquer ces tokens. Thème **sombre**.

## Concept
Cockpit / instrument de précision, pas un dashboard SaaS générique. Fond encre profond, grille
en pointillés, accent **signal cyan-phosphore** réservé au « vivant » (présence, live, sparklines),
filets ultra-fins, halos discrets, données en chiffres monospace. Sobre, dense, premium.

## Couleurs (CSS variables)
```css
--bg:#080B0F;        /* fond app */
--bg-2:#0C1116;      /* panneaux */
--bg-3:#10171E;      /* surfaces surélevées / hover */
--raised:#131C24;
--line:rgba(255,255,255,.06);   /* filets par défaut */
--line-2:rgba(255,255,255,.11); /* filets emphase */
--line-3:rgba(255,255,255,.18);
--text:#E8EEF4;  --dim:#93A1B0;  --faint:#5A6773;   /* texte 3 niveaux */
--signal:#2BE3C6;  --signal-dim:#1B9C8A;  /* accent principal (cyan-phosphore) */
--ok:#34E2B0;   /* en ligne / sain */
--warn:#F5B83D; /* à surveiller */
--danger:#FF5D63; /* critique */
--info:#5AB6FF;
--glow:0 0 0 1px rgba(43,227,198,.25),0 0 22px -6px rgba(43,227,198,.55); /* halo accent */
```
Règle : l'accent `--signal` ne sert qu'au vivant/interactif. Le reste est neutre. 1 accent, 3 sémantiques (ok/warn/danger).

## Typographie
- UI / titres : **Hanken Grotesk** (400/500/600/700/800).
- Chiffres, identifiants, hostnames, libellés techniques, wordmark : **JetBrains Mono** (400/500/700).
- Micro-libellés : mono, UPPERCASE, `letter-spacing:.12–.16em`, couleur `--faint` → effet « étiquette d'instrument ».
- Grands nombres (KPI) : mono 34px/700, `letter-spacing:-.02em`.

## Formes & espacement
- Rayons : `--r:7px` (éléments), `--r-lg:11px` (panneaux). Pas d'arrondis sur bordures à un seul côté.
- Filets 1px `--line`. Panneaux = `--bg-2` + filet + `--r-lg`.
- Densité d'instrument : padding interne 12–17px ; rythme vertical 14–18px.

## Composants clés
- **Pastille d'état** (`.dot`) : 9px ; en ligne = `--ok` + halo `box-shadow:0 0 9px` + pulsation lente ; alerte = `--warn` + halo ; hors ligne = gris mat sans halo.
- **Jauge** CPU/RAM : track 5px `#1b2730` + remplissage coloré par seuil (vert <60, ambre 60–84, rouge ≥85) + valeur mono à droite.
- **KPI card** : dégradé `--bg-2→--bg-3`, sparkline SVG en bas-droite, micro-label mono en haut. La carte « phare » a `border` cyan + halo radial.
- **Ligne de tableau** : hover → fond `--bg-3` + liseré gauche `--signal` (inset box-shadow).
- **Pill LIVE** : mono, vert, point pulsant.
- **Fenêtre bureau à distance** : titlebar (REC/LIVE pulsant + utilisateur + fps/latence/écran), zone écran 16/10 avec scanlines subtiles, bouton « Prendre la main » en accent plein.
- **Barre d'état** (bas) : mono, télémétrie (relais, agents reportant, heartbeat, file commandes, version/build).
- **Rail gauche** : 62px, icônes ligne, item actif = liseré + accent.

## Motion
- Apparition au chargement : `rise` (opacity + translateY 10px), staggered via `animation-delay` (KPIs puis panneaux).
- Pulsation `beat` (1.3–2.4s) sur les éléments « vivants » (LIVE, pastilles en ligne, REC).
- Hover : transitions .15–.18s. Pas d'effets gratuits ailleurs.

## Icônes
Jeu d'icônes ligne maison en SVG `<symbol viewBox="0 0 24 24">` + `<use>`, `stroke-width:1.8`, `currentColor`.
(En prod, possibilité de passer à Lucide/Tabler ; garder le style ligne fin.)

## Accessibilité
Contrastes vérifiés sur fond sombre ; le texte courant utilise `--text`/`--dim`, jamais `--faint` pour du contenu lisible essentiel. Pastilles d'état toujours doublées d'un libellé/valeur (jamais la couleur seule).
