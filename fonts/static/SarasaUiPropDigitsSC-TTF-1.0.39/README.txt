Sarasa Ui PropDigits SC TTF 1.0.39

This directory contains static TrueType instances generated from the corrected
Sarasa Ui VF PropDigits SC build.

Weights:

- ExtraLight 250
- Light 300
- Normal 350
- Regular 400
- Medium 500
- Bold 700
- Heavy 900

Each weight has an upright and Italic file. ASCII digits are proportional by
default; OpenType tnum restores tabular digits, and pnum maps tabular digits
back to proportional digits. The contextual digit-colon rule raises ':' only
when it appears between digits.

Static instances are passed through ttfautohint when the tool is available.
They keep a static STAT table for modern weight/italic style recognition; this
does not make the static TTFs variable fonts.
Glyph counts are not padded to match upstream; cmap glyphs and layout-reachable
unencoded glyphs are preserved, while unreachable glyph count differences are
left as build artifacts.
These fonts are modified derivatives and are not official Sarasa Gothic,
Source Han Sans, or Inter releases.
