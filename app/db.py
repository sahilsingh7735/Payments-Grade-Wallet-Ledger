import os
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

pool = ConnectionPool(
    os.environ["DATABASE_URL"],
    min_size=2, max_size=20,
    kwargs={"row_factory": dict_row},
    open=False,
)
