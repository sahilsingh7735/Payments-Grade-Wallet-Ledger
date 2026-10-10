import time

from .db import pool
from .jobs import reconcile


def main():
    pool.open()
    pool.wait()
    try:
        while True:
            reconcile()
            time.sleep(24 * 60 * 60)
    finally:
        pool.close()


if __name__ == "__main__":
    main()
