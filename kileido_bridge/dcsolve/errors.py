# Vendored from Fill Resistance 1.4.2, fill_resistance/errors.py
# (https://git.b4l.co.th/B4L/kicad-zone-resistance, release 1.4.2).
# Copyright (C) 2026 Janik Oltmanns / B4L and the Fill Resistance contributors.
# SPDX-License-Identifier: GPL-3.0-or-later
# Unchanged apart from this header.
"""User-facing error hierarchy.

Every UserFacingError message is shown both on stdout (KiCad status bar)
and in a matplotlib error figure, so keep messages self-contained and
actionable.
"""


class UserFacingError(Exception):
    pass


class ApiVersionError(UserFacingError):
    pass


class SelectionError(UserFacingError):
    pass


class ConfigError(UserFacingError):
    """fill_res_config.json is present but unreadable or invalid. Always
    fatal - silently ignoring a config (and running a default setup the
    user did not ask for) would be worse than stopping."""


class CandidateError(UserFacingError):
    pass


class ElectrodeError(UserFacingError):
    pass


class ConnectivityError(UserFacingError):
    pass


class GridSizeError(UserFacingError):
    pass


class SolverError(UserFacingError):
    pass
