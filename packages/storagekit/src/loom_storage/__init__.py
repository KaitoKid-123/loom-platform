"""Cấp credential S3 hẹp theo prefix của một workspace.

Tách khỏi `icebergkit` có chủ đích: Giai đoạn 2b đọc Parquet thô trong `Files/`
bằng DuckDB httpfs, không đi qua Iceberg chút nào. Gộp vào icebergkit sẽ buộc
đường đó import cả PyIceberg chỉ để lấy một cặp access key.
"""
