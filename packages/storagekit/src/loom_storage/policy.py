"""Sinh policy IAM giới hạn một credential vào đúng prefix của một workspace.

Thuần hàm, không I/O. Đây là văn bản quyết định "credential này đọc được gì",
nên nó test được đầy đủ mà không cần MinIO — và nó ĐƯỢC test đầy đủ, vì contract
test trên container chỉ chạy được vài trường hợp còn ở đây thì chạy được mọi
trường hợp.
"""

import json
import uuid

from loom_storage.credentials import prefix_for_workspace


def workspace_policy(bucket: str, workspace_id: uuid.UUID) -> str:
    """JSON policy cho phép đọc/ghi TRONG prefix của workspace, và không hơn.

    Hai câu lệnh, không phải một, và đó là điểm dễ sai nhất của cả package:

    - Thao tác trên OBJECT (`GetObject`/`PutObject`/`DeleteObject`) nhận Resource
      hẹp `arn:aws:s3:::<bucket>/<prefix>*`.
    - `ListBucket` là quyền trên chính BUCKET. Resource của nó *buộc phải* là
      `arn:aws:s3:::<bucket>` — viết hẹp hơn thì lệnh liệt kê hỏng hoàn toàn.
      Thứ duy nhất thu hẹp được nó là `Condition` trên `s3:prefix`. Bỏ Condition
      đi thì credential vẫn không đọc nổi object của người khác, nhưng nó LIỆT KÊ
      được tên mọi bảng của mọi workspace.
    """
    prefix = prefix_for_workspace(workspace_id)
    document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                "Resource": [f"arn:aws:s3:::{bucket}/{prefix}*"],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{bucket}"],
                "Condition": {"StringLike": {"s3:prefix": [f"{prefix}*"]}},
            },
        ],
    }
    # separators compact: policy đi qua tham số truy vấn HTTP và MinIO giới hạn
    # 2048 ký tự.
    return json.dumps(document, separators=(",", ":"))
