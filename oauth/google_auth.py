"""
Google Analytics auth helpers.

In HTTP transport mode the server is protected by FastMCP's GoogleProvider
(see server.py), which runs the full web OAuth flow against Google, stores
the resulting credentials, and transparently refreshes them. Each incoming
request carries a token that FastMCP has already validated and refreshed;
this module just reads that token out of the current request context and
turns it into headers for calling the Google Analytics APIs.
"""

from typing import Dict

from fastmcp.server.dependencies import get_access_token


def get_headers_with_auto_token() -> Dict[str, str]:
    """Get API headers using the caller's authenticated Google access token.

    Relies on FastMCP's auth layer (GoogleProvider) having already validated
    the incoming request and made a live, auto-refreshed Google OAuth access
    token available via get_access_token(). Raises if no authenticated
    session is present (e.g. server started without auth configured).
    """
    access_token = get_access_token()

    if access_token is None or not access_token.token:
        raise RuntimeError(
            "No authenticated Google Analytics session for this request. "
            "Re-authorize the connector in claude.ai and try again."
        )

    return {
        'Authorization': f'Bearer {access_token.token}',
        'Content-Type': 'application/json',
    }
