"""Naive-UTC timestamps, as stored throughout the database.

datetime.utcnow() is deprecated since Python 3.12; this keeps the exact same
semantics (naive datetime in UTC) without the deprecation.
"""
import datetime


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
