"""Transpile SQL giữa hai phương ngữ qua sqlglot — mỏng có chủ đích.

Tồn tại để `loom-query` không import `sqlglot` trực tiếp: khi Giai đoạn 4 đổi
engine sang Trino, chỗ gọi chỉ đổi tham số `to`, không đổi import.
"""

from __future__ import annotations

import sqlglot


def transpile(sql: str, frm: str, to: str) -> str:
    statements = sqlglot.transpile(sql, read=frm, write=to)
    return ";\n".join(statements)
