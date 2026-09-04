# SPDX-License-Identifier: MIT
# Pons Family - module entry point for pons.family
"""Allows ``python -m pons_pal`` as an alias for the ``pons-pal`` console script."""

from __future__ import annotations

from pons_pal.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
