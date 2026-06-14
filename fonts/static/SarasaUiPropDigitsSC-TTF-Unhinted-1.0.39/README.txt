Sarasa Ui PropDigits SC TTF Unhinted 1.0.39

This directory contains static TrueType fonts generated from static Source Han
Sans SC and Inter sources through Sarasa's pass1/kanji/hangul/pass2 build path,
then patched with the PropDigits derivative behavior.

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
back to proportional digits. Static TTFs reuse Inter/Sarasa's retained calt
data for contextual colon raising, so numeric-adjacent contexts such as 1:2,
1:a, a:2, and 1::2 may raise ':', while plain alphabetic text such as a:b is
left alone.

The name table includes Simplified Chinese display names, such as
更纱黑体 Ui PropDigits SC ExtraLight.
The unhinted set is built through the same static fragment route as
upstream Sarasa, but uses the unhinted pass1/kanji/hangul fragments
directly in pass2. It intentionally skips ttfautohint and Chlorophytum,
providing a formal static output without TrueType instructions.
They keep a static STAT table for modern weight/italic style recognition; this
does not make the static TTFs variable fonts.
GSUB/GPOS FeatureRecord order, Script/LangSys coverage, and the base lookup
structure are templated from the corresponding upstream Sarasa Ui SC static
font for each style; the static digit-colon behavior does not add its own
lookup.
Simple glyf overlap flags are cleared at final write-out for OTS/web-font
compatibility; this changes the raw glyf encoding flag only, not coordinates
or TrueType instructions.
Glyph counts are not padded to match upstream; cmap glyphs and layout-reachable
unencoded glyphs are preserved, while unreachable glyph count differences are
left as build artifacts.
These fonts are modified derivatives and are not official Sarasa Gothic,
Source Han Sans, or Inter releases.
