# Vendored from Fill Resistance 1.4.2 (https://git.b4l.co.th/B4L/kicad-zone-resistance).
# Copyright (C) 2026 Janik Oltmanns / B4L and the Fill Resistance contributors.
# SPDX-License-Identifier: GPL-3.0-or-later
"""The DC solver of Fill Resistance by Janik Oltmanns / B4L, vendored.

Fill Resistance (https://git.b4l.co.th/B4L/kicad-zone-resistance, GPL-3.0-or-later)
computes DC resistance and IR drop of copper fills and traces: a coupled
multi-layer finite-difference solver, a 5-point sheet per copper layer, via
and plated-hole barrels between them, an adaptive quadtree grid, and a PDN
mode with any number of supplies and loads. These modules are its release
1.4.2 files, kept as close to the originals as possible so this package can
later give way to a dependency on theirs, or changes can go upstream:

- config, errors, skin, geometry, quadtree, adaptive, solver: unchanged
  apart from a provenance header.
- raster: rasterizes with KiLeidoscope's numpy filler instead of PIL and
  matplotlib (the bridge has neither).
- progress: reports to the solve worker instead of a Qt window.

Everything KiLeidoscope-specific (board snapshots in, display frames out)
lives outside this package, in kileido_bridge/dc.py and dcworker.py.
"""

__version__ = "1.4.2"  # the Fill Resistance release these files come from
