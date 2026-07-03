"""Hatchling build hooks for the nullrun SDK.

``authors`` / ``maintainers`` injection
---------------------------------------
PEP 621 maps the ``authors`` array to PKG-INFO's ``Author-email:``
line but does NOT populate the legacy single ``Author:`` line, and
``pip show`` only renders ``Author:`` (it does not render
``Maintainer:`` at all). As a result a project whose ``authors`` is
``[{name=..., email=...}]`` ships with an empty ``Author:`` field and
the maintainer's name never appears in ``pip show``.

Hatchling makes this worse: in its ``authors`` property parser
(``hatchling/metadata/core.py``), an inline-table only contributes to
the legacy ``Author:`` field when it has a ``name`` and NO ``email``.
If both are set, the name is folded into the ``Author-email:``
display_name and the ``Author:`` line is suppressed entirely.

This hook splits the primary author into two inline-table entries so
hatchling populates both ``authors_data["name"]`` (``Author:``) and
``authors_data["email"]`` (``Author-email:``)::

    Author: Anatolii Maltsev
    Author-email: support@nullrun.io

It also sets ``maintainers`` to the publishing org for the PyPI
sidebar (pip does not display ``Maintainer:``).

Why ``authors`` / ``maintainers`` are listed in ``project.dynamic``:
hatchling only invokes ``MetadataHookInterface.update()`` when at
least one field is marked dynamic. Removing the static arrays and
keeping the hook as the single source of truth is what actually wires
the update call.
"""

from __future__ import annotations

from hatchling.metadata.plugin.interface import MetadataHookInterface


class CustomMetadataHook(MetadataHookInterface):
    PLUGIN_NAME = "custom"

    def update(self, metadata: dict) -> None:
        # See module docstring for the full rationale.
        metadata["authors"] = [
            {"name": "Anatolii Maltsev"},
            {"email": "support@nullrun.io"},
        ]
        metadata["maintainers"] = [
            {"name": "nullrun.io", "email": "support@nullrun.io"},
        ]