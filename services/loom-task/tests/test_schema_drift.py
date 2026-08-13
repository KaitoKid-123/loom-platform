"""Schema nguồn lệch khỏi bảng bronze phải hỏng TO TIẾNG, không tự chữa.

Giai đoạn 3a KHÔNG tiến hoá schema — xem spec mục 7. Thêm một cột ở nguồn là
chuyện rất thường và Iceberg thừa sức làm, nhưng "tự động theo schema nguồn" có
nhiều cách sai âm thầm (cột bị xoá rồi thêm lại với kiểu khác, một cột đổi
nghĩa, thứ tự đọc ngược) và 3a chưa có gì canh chúng cho tử tế. Hỏng to tiếng,
rồi để 3b làm đàng hoàng.

Thuần hàm: không Docker, không Lakekeeper, không nguồn thật — nên nó chạy trong
`make test`. Điều đang được canh là một QUYẾT ĐỊNH (hai tập tên cột có khớp
không), và quyết định đó không cần bảng nào để kiểm.
"""

from __future__ import annotations

import pytest

from loom_task.runner import SchemaDrift, check_schema


def test_a_new_column_at_the_source_fails_the_run_with_a_diff() -> None:
    """Thông báo phải NÊU TÊN cột lệch, không chỉ nói "schema khác".

    Nó đi vào `ingest_run.error` và là artifact duy nhất còn lại khi có người
    đọc tới (pod đã bị TTL dọn), nên "schema drift" trần buộc người vận hành tự
    đi so hai danh sách cột bằng tay — đúng việc mà máy vừa làm xong.
    """
    with pytest.raises(SchemaDrift) as exc:
        check_schema(source=["id", "name", "email"], target=["id", "name"])

    assert "email" in str(exc.value)


def test_a_removed_column_fails_too() -> None:
    """Cột MẤT ở nguồn cũng là drift, và nó KHÔNG đối xứng với cột thêm.

    Iceberg vẫn nối được một lô thiếu cột (cột đó thành NULL), nên đây đúng là
    hình dạng sai ÂM THẦM: bảng bronze tiếp tục lớn lên với một cột toàn NULL từ
    một ngày nào đó trở đi, và không ai biết ngày nào cho tới khi đi tìm.
    """
    with pytest.raises(SchemaDrift) as exc:
        check_schema(source=["id"], target=["id", "name"])

    assert "name" in str(exc.value)


def test_the_bronze_metadata_columns_are_not_drift() -> None:
    """`_ingested_at`/`_source`/`_batch_id` do CHÚNG TA thêm; nguồn không bao giờ
    có chúng. Không loại trừ ba cột này thì MỌI lần nạp thứ hai đều báo drift, và
    phép canh trở thành thứ người ta tắt đi."""
    check_schema(source=["id"], target=["id", "_ingested_at", "_source", "_batch_id"])


def test_column_order_is_not_drift() -> None:
    """So theo TẬP HỢP, không theo thứ tự.

    Thứ tự cột ở nguồn đổi được mà không đổi ý nghĩa gì (một `ALTER` rồi
    `SELECT *` trả thứ tự khác), nên báo drift cho một thay đổi vô hại là báo
    động giả — và một phép canh báo động giả là một phép canh sắp bị tắt.
    """
    check_schema(source=["b", "a"], target=["a", "b"])
