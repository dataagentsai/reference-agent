"""The customer portal, rendering this shop's signed-in page.

The portal — login through the identity provider, the signed session cookie,
logout, the widget's identity hash — is the harness's (`agent_harness.portal`).
The page a signed-in customer sees is this shop's (`ui.portal_page`). `build`
here is the harness's with that page filled in; every other name is the
harness's.
"""

from __future__ import annotations

from typing import Any

from starlette.applications import Starlette

from agent_harness import portal as _portal
from agent_harness.portal import (
    FLOW_COOKIE,
    SESSION_COOKIE,
    Portal,
    PortalPage,
    Widget,
)
from support_agent.ui import portal_page


def build(portal: Portal) -> Starlette:
    """The portal app, ready to mount at `portal.base_path`, with this shop's page."""
    return _portal.build(portal, signed_in=portal_page)


def __getattr__(name: str) -> Any:
    """Every other name — private ones included — is the harness's."""
    return getattr(_portal, name)


__all__ = ["FLOW_COOKIE", "SESSION_COOKIE", "Portal", "PortalPage", "Widget", "build"]
