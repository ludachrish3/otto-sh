"""Unit tests for the host-source (LabRepository) error contract."""

from otto.labs import LabNotFoundError, LabRepositoryError


def test_lab_not_found_is_a_lab_repository_error():
    assert issubclass(LabNotFoundError, LabRepositoryError)


def test_lab_repository_error_is_an_exception():
    assert issubclass(LabRepositoryError, Exception)


def test_errors_are_raisable_with_a_message():
    err = LabNotFoundError("lab 'x' not found")
    assert "not found" in str(err)
    assert isinstance(err, LabRepositoryError)
