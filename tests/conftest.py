import os

import psycopg
import pytest


@pytest.fixture
def core_concurrency_without_fraud():
    """Keep the row-lock acceptance test focused on concurrency, not risk policy."""
    database_url = os.getenv("TEST_DATABASE_URL", "postgresql://wallet:wallet@localhost:5432/wallet")
    with psycopg.connect(database_url) as connection:
        connection.execute("UPDATE risk_rules SET enabled=FALSE")
        connection.commit()
    try:
        yield
    finally:
        with psycopg.connect(database_url) as connection:
            connection.execute("UPDATE risk_rules SET enabled=TRUE")
            connection.commit()
