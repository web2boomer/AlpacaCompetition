from money_machine.persistence.database import normalize_database_url


def test_normalize_render_postgres_url_for_psycopg() -> None:
    assert (
        normalize_database_url("postgresql://user:pass@host/db")
        == "postgresql+psycopg://user:pass@host/db"
    )
    assert (
        normalize_database_url("postgres://user:pass@host/db")
        == "postgresql+psycopg://user:pass@host/db"
    )


def test_normalize_database_url_preserves_explicit_driver_and_sqlite() -> None:
    psycopg_url = "postgresql+psycopg://user:pass@host/db"
    sqlite_url = "sqlite:///test.db"

    assert normalize_database_url(psycopg_url) == psycopg_url
    assert normalize_database_url(sqlite_url) == sqlite_url
