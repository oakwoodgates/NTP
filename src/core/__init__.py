"""
Core module — TIGHT SCOPE.

This module contains ONLY:
- Type aliases and newtypes (wrapping NT types, Decimal, etc.)
- Constants (exchange names, fee tiers, shared enums)
- Interface protocols (typing.Protocol ABCs for cross-module contracts)
- Pure utility functions (timestamp conversion, decimal formatting)

This module NEVER imports from other src/ modules.
If something doesn't fit the above list, it belongs in a more specific module.
"""
