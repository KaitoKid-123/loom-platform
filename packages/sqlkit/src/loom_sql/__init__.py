"""Đọc AST của SQL. Nhận chuỗi, trả dữ liệu. **KHÔNG I/O.**

Ràng buộc không-I/O là lý do package này tách riêng, và nó KIỂM ĐƯỢC —
`tests/test_no_io.py` đọc AST của chính các module ở đây và bác mọi import ngoài
allowlist. Cùng khuôn với `loom_core.roles` không import SQLAlchemy.

Không có ràng buộc đó, logic phân tích SQL sẽ lẻn xuống tầng lưu trữ và `sqlkit`
hết test được độc lập — mà nó là chỗ RBAC gặp SQL, nên nó phải test được cho mọi
trường hợp chứ không chỉ những trường hợp dựng nổi một database.
"""
