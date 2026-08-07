import json
import uuid
from typing import Any

from loom_storage.policy import workspace_policy

WS = uuid.UUID("11111111-2222-3333-4444-555555555555")
PREFIX = "workspaces/11111111-2222-3333-4444-555555555555/"


def _statements(bucket: str = "loom-local") -> list[dict[str, Any]]:
    return json.loads(workspace_policy(bucket, WS))["Statement"]


def test_object_actions_are_scoped_to_the_workspace_prefix() -> None:
    objects = [s for s in _statements() if "s3:GetObject" in s["Action"]]
    assert len(objects) == 1
    assert objects[0]["Resource"] == [f"arn:aws:s3:::loom-local/{PREFIX}*"]


def test_listbucket_is_restricted_by_condition_not_by_resource() -> None:
    """`s3:ListBucket` là quyền trên BUCKET. Resource của nó buộc phải là chính
    bucket, nên thứ duy nhất thu hẹp được nó là `Condition` trên `s3:prefix`.
    Thiếu Condition thì credential của một workspace liệt kê được tên bảng của
    MỌI workspace — không đọc được nội dung, nhưng vẫn là rò rỉ."""
    listing = [s for s in _statements() if "s3:ListBucket" in s["Action"]]
    assert len(listing) == 1
    assert listing[0]["Resource"] == ["arn:aws:s3:::loom-local"]
    assert listing[0]["Condition"]["StringLike"]["s3:prefix"] == [f"{PREFIX}*"]


def test_no_statement_grants_a_wildcard_resource() -> None:
    """Phép canh trực tiếp cho chính cái đột biến mà contract test ở Task 6 dùng
    để tự chứng minh mình đỏ được."""
    for statement in _statements():
        for resource in statement["Resource"]:
            assert not resource.endswith(":::*")
            assert resource != "*"


def test_two_workspaces_never_produce_the_same_policy() -> None:
    other = uuid.uuid4()
    assert workspace_policy("loom-local", WS) != workspace_policy("loom-local", other)


def test_policy_is_compact_json() -> None:
    """MinIO STS nhận policy qua tham số truy vấn của một request HTTP, và giới
    hạn 2048 ký tự. Khoảng trắng thừa ăn vào ngân sách đó mà không đổi ý nghĩa."""
    rendered = workspace_policy("loom-local", WS)
    assert ", " not in rendered
    assert len(rendered) < 2048
