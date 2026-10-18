"""Indexed task identities for statistics, independent of permission checks."""
from sqlalchemy import String, case, column, func, or_

ACCOUNT_FIELDS = ('download_account_id', 'details_account_id', 'check_account_id', 'execution_account_id')


def account_value(payload, field):
    value = payload[field].as_string()
    # Account IDs are at most 36 characters. Invalid legacy payload values must
    # not make a migration fail or be truncated into another account's ID.
    return case((func.length(value) <= 36, value), else_=None)


def unique_account_value(field):
    """Keep each account once per job, comparing with the database's ID collation."""
    value = column(field, String(36))
    previous = ACCOUNT_FIELDS[:ACCOUNT_FIELDS.index(field)]
    return case((or_(*(value == column(key, String(36)) for key in previous)), None), else_=value)
