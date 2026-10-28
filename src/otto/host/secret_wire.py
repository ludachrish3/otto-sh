"""Where a wrapped password becomes the text a transport writes.

Traceback locals stay on in the CLI and the logs, so a password otto holds as
a plain string in any frame is printed when that frame is on a failing stack.
otto therefore carries a password as a pydantic ``SecretStr`` down to the
transport, and the transport's write unwraps it inside the call expression:
``process.stdin.write(reveal(data).encode())``. The raw value then lives only
in the library's own frame, never in an otto local, parameter or attribute.

These helpers are that unwrap, decided once. Each takes the wrapped value and
returns the raw text as its result, so its own frame holds only the wrapper.
"""

from pydantic import SecretBytes, SecretStr

__all__ = ["reveal", "reveal_bytes", "shown"]


def reveal(text: str | SecretStr | None, newline: str = "\n") -> str:
    r"""Return what writing *text* puts on the wire.

    A ``str`` is written as it is. A ``SecretStr`` is an answer to a prompt:
    its value followed by *newline* (``"\r"`` on a telnet line), so a caller
    hands over the cred's own ``SecretStr`` rather than wrapping a formatted
    string, which would wrap the mask instead of the password. ``None``, a
    cred with no password, answers the prompt with an empty line.

    Call it only inside the transport's write expression.
    """
    if text is None:
        return newline
    if isinstance(text, SecretStr):
        return text.get_secret_value() + newline
    return text


def reveal_bytes(data: bytes | SecretBytes) -> bytes:
    """Return the raw bytes of *data*; call it only inside the transport's write expression."""
    if isinstance(data, SecretBytes):
        return data.get_secret_value()
    return data


def shown(text: str | SecretStr) -> str:
    """Return *text* as a log line may show it: a ``SecretStr`` as its mask."""
    return text if isinstance(text, str) else str(text)
