"""Access-key gate through a real browser (spec 2026-07-16).

The keyed-URL happy path is implicitly exercised by every other browser spec
in this directory (their page.goto(<dash>.url) now carries ?key=…); this
module pins the refusal side: the BARE origin — what a port-scanner or a
teammate guessing the address gets — must render the 403 hint page, not the
dashboard shell.
"""

from urllib.parse import urlsplit, urlunsplit

import pytest

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
]


def test_bare_url_renders_403_hint_not_dashboard(shell_dash, page) -> None:
    parts = urlsplit(shell_dash.url)
    bare = urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
    response = page.goto(bare)
    assert response is not None
    assert response.status == 403
    content = page.content()
    assert "otto monitor" in content  # the hint names the command that prints the key
    assert "Access key required" in content


def test_keyed_url_boots_the_shell_and_cookie_covers_reload(shell_dash, page) -> None:
    """One keyed navigation, then a BARE reload must still work via the cookie.

    Waits on the Import front door's own testids (the idiom
    ``test_review_shell.py::test_live_mode_still_boots_empty`` uses against
    this same ``shell_dash`` fixture) rather than the static ``#root`` mount
    point — that proves the React app actually mounted, not merely that the
    HTML skeleton arrived.
    """
    page.goto(shell_dash.url)
    page.locator('[data-testid="empty-review"]').wait_for()
    page.locator('[data-testid="import-input"]').wait_for(state="attached")
    parts = urlsplit(shell_dash.url)
    response = page.goto(urlunsplit((parts.scheme, parts.netloc, "/", "", "")))
    assert response is not None
    assert response.status == 200
    page.locator('[data-testid="empty-review"]').wait_for()
    page.locator('[data-testid="import-input"]').wait_for(state="attached")
