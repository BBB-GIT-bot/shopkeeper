"""Initialize default configurations and accounts, preserving existing values."""
import os

from app.auth import hash_password
from app.db import init_db, insert_if_absent, transaction


def seed_accounts():
    with transaction() as connection:
        for username, role, password in (
            ('operator', 'operator', os.getenv('DEMO_OPERATOR_PASSWORD', os.getenv('OPERATOR_PASSWORD', 'operator123'))),
            ('manager', 'manager', os.getenv('DEMO_MANAGER_PASSWORD', os.getenv('MANAGER_PASSWORD', 'manager123'))),
        ):
            insert_if_absent(connection, 'users', {'username': username, 'password_hash': hash_password(password), 'role': role})


if __name__ == '__main__':
    init_db()
    seed_accounts()
    print('Default configurations and accounts ready; existing values preserved.')
