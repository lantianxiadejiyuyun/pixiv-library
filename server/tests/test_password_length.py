import pytest

from app.config import Settings


def production(password):
    return Settings(environment="production", admin_password=password,
                    database_url="mysql+pymysql://test:test@localhost/test",
                    secret_key="test-key-for-validation-" * 2)


@pytest.mark.parametrize("length", [8, 11, 20])
def test_password_length_accepts_inclusive_boundaries(length):
    production("x" * length).validate()


@pytest.mark.parametrize("length", [0, 7, 21, 100])
def test_password_length_rejects_outside_boundaries(length):
    with pytest.raises(RuntimeError, match="8 to 20 characters"):
        production("x" * length).validate()


def test_development_default_is_still_rejected_in_production():
    with pytest.raises(RuntimeError, match="8 to 20 characters"):
        production("local-library").validate()
