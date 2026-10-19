"""数据库连接。只读进程环境变量，不加载任何配置文件。

必需：SCOPED_KB_DATABASE_URL —— libpq 连接串或 URI，原样交给 psycopg2.connect。
"""
import os
import sys

import psycopg2

DATABASE_URL_ENV = "SCOPED_KB_DATABASE_URL"


def connect():
    dsn = os.environ.get(DATABASE_URL_ENV)
    if not dsn:
        sys.exit(
            f"缺少环境变量 {DATABASE_URL_ENV}。\n"
            f"示例（URI）：postgresql://scoped_kb:密码@127.0.0.1:5432/scoped_kb\n"
            f"示例（libpq 键值）：host=127.0.0.1 port=5432 dbname=scoped_kb "
            f"user=scoped_kb password=密码")
    return psycopg2.connect(dsn)
