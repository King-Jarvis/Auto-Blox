---
name: auto-blox-theme
description: Design a theme for the Auto-Blox dashboard from inspiration (photos, a mood, a brand, a place, a palette) and hand back one JSON theme file the dashboard imports. Use whenever someone wants to restyle, re-colour or theme their Auto-Blox console.
---

# Design an Auto-Blox theme

Auto-Blox is a dashboard for a small single-board computer and the devices
around it. It has live charts, a flow editor, device tables, cameras and a terminal.
A theme restyles all of it. The output is **one JSON file**, never CSS. The
person imports it from the dashboard's **Theme** menu (**Import a theme…**).

This folder has:

| File | What it is |
| --- | --- |
| `template.json` | The person's current theme. Start from it. |
| `TOKENS.md` | Every colour, what it is for, and the rules it must pass. |
| `check_theme.py` | The dashboard's own checker. If it prints `ok`, the import will be accepted. |
| `preview.html` | A mock of the dashboard that shows any theme. |
| `example.json` | A finished theme ("Asphalt and gold", made from a photo of a toy car on a dark car park) that passes the checker, for reference. |

## How to work

1. **Take in the inspiration.** Photos, a mood, a room, a brand, a film, a season,
   or anything else counts. If nothing was given, ask for it, and ask whether
   they want a dark or light theme. In a sentence or two, say what you take from
   it: the ground colour, the accent, and the temperature.

2. **Choose the base.** Use `"dark"` or `"light"`. It decides the direction of the
   scrollbars and form controls, and which shadows are used.

3. **Fill every colour** in a copy of `template.json`, using `TOKENS.md` for what
   each one is for. The rules that make it read well:
   - **Surfaces are one family.** `surface-000` is the page. `surface-100` is a
     widget on it, `surface-200` is a widget's header band, and `surface-300` is a
     recessed well (terminal, inputs). Keep the steps between them small but
     visible. Tint all four from the inspiration's ground, not from pure grey.
   - **`ink`, `ink-muted` and `ink-faint`** are three text weights. Each must
     read on every surface.
   - **`signal` is the one accent.** It marks the active tab, the primary button
     and the number a screen is about. Take it from the inspiration's most
     characteristic colour. `on-signal` is the text on a solid `signal` button.
     `signal-wash` is a quiet tint of it that sits close to the surfaces.
   - **Status colours keep their meaning.** `ok` is green-ish, `warn` is
     amber or yellow, `serious` is orange and `critical` is red. You may lean
     their hue towards the palette, but a person must still read them as OK,
     warning and failure at a glance. The three `-wash` colours are their quiet
     backgrounds.
   - **`series-1` to `series-4`** are chart lines: CPU, GPU, video engine and
     memory temperatures. They need four clearly different hues.
   - **`hairline`** divides and **`hairline-strong`** outlines controls. **`grid`**
     is chart gridlines and the flow canvas's dots. **`link`** and
     **`focus-ring`** are usually the same colour. **`scrim`** dims the page
     behind a drawer or a full-size picture. It stays dark even in a light theme.
   - **No glaring white.** In a light theme, keep the brightest surface at or
     below about `#eeeeee`.

4. **Choose fonts, radius and shadow.**
   - `fonts.ui` and `fonts.mono` must each be a name from the lists in
     `TOKENS.md`. Pick ones that suit the mood.
   - `radius` is `sm` (buttons), `md` (widgets) and `lg` (menus), in pixels
     from 0 to 16. Square reads as an instrument; round reads as soft.
   - `shadow` is `none`, `soft` or `deep`.

5. **Check it.** Save the file and run:

   ```
   python3 check_theme.py my-theme.json
   ```

   Fix everything it lists and run it again until it prints `ok`.
   - Most fixes are moving a text colour further from its background, or a
     surface further from its neighbour.
   - If you cannot run code, check contrast by hand. Relative luminance is
     L = 0.2126 R + 0.7152 G + 0.0722 B, where each channel c (0 to 1) becomes
     c/12.92 when c ≤ 0.03928, and ((c + 0.055)/1.055)^2.4 otherwise.
     Contrast is (L1 + 0.05)/(L2 + 0.05), with L1 the lighter. Say that you
     checked by hand.

6. **Show it.** Make a copy of `preview.html` and replace the JSON inside
   `<script id="theme" type="application/json">` with the theme. Show it as a
   rendered HTML page or artifact, so the person sees their dashboard in it
   before importing. Offer to adjust anything: warmer, less contrast, a
   different accent. Check again after every change.

7. **Hand it over.** Give the final JSON as a downloadable file named after the
   theme (for example `harbour-at-dusk.json`), and tell them how to use it:
   - On the dashboard, open **Theme → Import a theme…** and choose the file.
     It switches to the theme at once, on every screen and device.
   - Importing a theme with the same name replaces the earlier version, so they
     can come back and iterate.
   - If the dashboard lists problems instead, bring those lines back here.

## The file

```json
{
  "format": "auto-blox-theme/1",
  "name": "Harbour at dusk",
  "base": "dark",
  "colors": { "surface-000": "#0e1419", "...": "all 27, see TOKENS.md" },
  "fonts": { "ui": "IBM Plex Sans", "mono": "IBM Plex Mono" },
  "radius": { "sm": 3, "md": 6, "lg": 10 },
  "shadow": "soft"
}
```

Rules for the file:
- Colours are `#rrggbb` only: no names, no alpha, no `rgb()`.
- The name is plain words, up to 40 characters.
- Anything not listed here is refused. That is deliberate: the dashboard
  accepts theme files from anywhere, so they carry values, never code.
