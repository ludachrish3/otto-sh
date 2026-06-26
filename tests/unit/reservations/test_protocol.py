"""Structural tests for the optional username-completion capability."""

from otto.reservations import SupportsUsernameCompletion


def test_class_with_list_usernames_satisfies():
    class B:
        def list_usernames(self):
            return ["alice"]

    assert isinstance(B(), SupportsUsernameCompletion)


def test_class_without_list_usernames_does_not():
    class B:
        pass

    assert not isinstance(B(), SupportsUsernameCompletion)
