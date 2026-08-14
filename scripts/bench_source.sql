-- Bảng nguồn cho ĐO 5 (`scripts/measure_ingest_rss.py`), dựng trong một Postgres
-- DÙNG-MỘT-LẦN chạy trong cụm. KHÔNG BAO GIỜ chạy file này lên Aiven.
--
-- Vì sao nó tồn tại: phép đo RSS của đường nạp cần một bảng nguồn THẬT, và bảng
-- bench trên Aiven không còn (ĐO 3 đã seed 1,2 triệu dòng lên service gói 1 GB
-- đĩa của chủ dự án và lật cả service sang CHỈ-ĐỌC trong lúc control plane đang
-- sống; `measure_ingest_path.py` từ đó bị gỡ hẳn đường ghi và ĐÒI bảng có sẵn).
-- Một Postgres trong cụm không có ràng buộc đó: đĩa của nó là đĩa của node k3d,
-- và nó bị xoá cùng deployment.
--
-- BẢY CỘT y hệt bảng bench của ĐO 3/ĐO 4, và sự giống nhau đó là điều kiện để con
-- số RSS so được với 587 MiB của ĐO 3: RSS phía client là số object Python sống
-- cùng lúc cho MỘT lô, nên nó phụ thuộc SỐ CỘT và KIỂU của chúng, không phụ thuộc
-- gói tin đã đi bao xa. Đổi hình dạng dòng là đo một bài toán khác.
--
--   id bigint, event_time timestamptz, region text (16 giá trị),
--   status text (5 giá trị), amount float8, customer_id text,
--   payload text ĐÚNG 220 ký tự
--
-- => ~298 byte/dòng byte Arrow (ĐO 3: 500.000 dòng = 0,149 GB).
--
-- Cả bảy cột đi đường NHANH của `PostgresConnector` (không cột nào bị `::text` —
-- xem `_needs_text_cast`), đúng như bảng của ĐO 3. Một cột `numeric` ở đây sẽ đổi
-- đường đi và làm phép so mất nghĩa.
--
-- Một BẢNG THẬT chứ không một VIEW trên `generate_series`: `PostgresConnector`
-- chạy được trên cả hai, nhưng một view bắt Postgres sinh lại từng dòng trong lúc
-- client đọc, tức là trộn CPU sinh dòng vào đồng hồ tường — `probe_read_path_cost.py`
-- phải dựng cả hai lần `EXPLAIN` chỉ để định lượng nhiễu đó. Bảng thật thì không
-- có nhiễu để định lượng.
--
-- `md5()`/`rpad()` lấy nguyên từ `probe_read_path_cost.py`: chuỗi phải KHÁC NHAU
-- từng dòng, nếu không nén từ điển của Parquet đưa cột đó về gần 0 byte và phép đo
-- thành phép đo của một bài toán khác (Giai đoạn 2a đã dính đúng bẫy đó với
-- `repeat('x', 512)`).

DROP SCHEMA IF EXISTS bench CASCADE;
CREATE SCHEMA bench;

CREATE TABLE bench.ingest_bench AS
SELECT
    i::bigint AS id,
    TIMESTAMPTZ '2024-01-01 00:00:00+00' + (i * INTERVAL '1 second') AS event_time,
    'region-' || lpad((i % 16)::text, 2, '0') AS region,
    (ARRAY['pending', 'processing', 'completed', 'failed', 'refunded'])[1 + (i % 5)] AS status,
    ((i % 2500000)::float8) / 100.0 AS amount,
    'cust-' || substr(md5(i::text), 1, 16) AS customer_id,
    rpad(md5(i::text), 220, md5((i * 7 + 13)::text)) AS payload
FROM generate_series(0, 499999) AS s(i);

-- PRIMARY KEY vì bảng bench của ĐO 3 có một cái, và index đó là phần đáng kể của
-- 175 MB đã đo. Nó cũng là thứ làm `WHERE id >= <watermark>` của
-- `PostgresConnector` chạy như ở nguồn thật thay vì một seq scan.
ALTER TABLE bench.ingest_bench ADD PRIMARY KEY (id);

ANALYZE bench.ingest_bench;

SELECT
    count(*) AS rows,
    pg_size_pretty(pg_total_relation_size('bench.ingest_bench')) AS on_disk
FROM bench.ingest_bench;
