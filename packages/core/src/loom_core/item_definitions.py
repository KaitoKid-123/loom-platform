"""Schema `definition` cho từng loại item — mỏng, có version, không nhận trường lạ.

`schema_version` có mặt từ Giai đoạn 1 để Giai đoạn 2/3 mở rộng được mà không
phải đoán bản ghi cũ theo hình dạng.
"""

import hashlib
import json
import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# vault://<path>#<key>  hoặc  k8s://<namespace>/<name>#<key>
# Chặt có chủ đích: ô này là ĐƯỜNG DẪN, và mục đích chính của regex là chặn
# người dùng dán mật khẩu thật vào đây.
#
# `\Z` chứ KHÔNG phải `$`. Trong Python `$` khớp cả ở vị trí ngay trước một `\n`
# cuối chuỗi, nên `^...$` nhận "vault://a/b#c\n" — tức là nhận một chuỗi mà mắt
# người đọc là hai dòng. Đây là đúng lớp lỗi đã làm thủng bộ lọc URL của không
# ít dự án; `\Z` không có ngoại lệ đó.
SECRET_REF_RE = re.compile(
    r"\A(?:vault://[A-Za-z0-9._\-/]+|k8s://[a-z0-9-]+/[a-z0-9.-]+)#[A-Za-z0-9._-]+\Z"
)


class ItemType(StrEnum):
    lakehouse = "lakehouse"
    connection = "connection"
    pipeline = "pipeline"
    sql_script = "sql_script"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1


class LakehouseDefinition(_Base):
    """Giai đoạn 2 thêm `tables`."""


class ConnectionDefinition(_Base):
    kind: Literal["postgres", "mysql", "sqlserver", "rest"]
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    database: str | None = Field(default=None, max_length=128)
    # KHÔNG BAO GIỜ là mật khẩu. Control plane không đọc secret — xem spec mục 5.2.
    secret_ref: str

    @field_validator("secret_ref")
    @classmethod
    def _check_ref(cls, value: str) -> str:
        if not SECRET_REF_RE.match(value):
            raise ValueError(
                "secret_ref must be vault://path#key or k8s://namespace/name#key "
                "— this is a reference, not a password"
            )
        return value


class SqlScriptDefinition(_Base):
    sql: str = ""
    visualization: dict[str, Any] | None = None


class PipelineDefinition(_Base):
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)


DEFINITION_BY_TYPE: dict[ItemType, type[_Base]] = {
    ItemType.lakehouse: LakehouseDefinition,
    ItemType.connection: ConnectionDefinition,
    ItemType.pipeline: PipelineDefinition,
    ItemType.sql_script: SqlScriptDefinition,
}

DEFAULT_DEFINITION: dict[ItemType, dict[str, Any]] = {
    ItemType.lakehouse: {"schema_version": 1},
    ItemType.pipeline: {"schema_version": 1, "nodes": [], "edges": []},
    ItemType.sql_script: {"schema_version": 1, "sql": ""},
    # connection KHÔNG có mặc định: không đoán được host/secret_ref của ai.
}


def parse_definition(item_type: ItemType, raw: dict[str, Any]) -> _Base:
    return DEFINITION_BY_TYPE[item_type].model_validate(raw)


def canonical_hash(definition: dict[str, Any]) -> str:
    """sha256 của JSON đã chuẩn hoá.

    `sort_keys` là bắt buộc: không có nó, cùng một nội dung với thứ tự khoá khác
    cho ra hash khác, nên mỗi lần lưu lại sinh một version mới dù người dùng
    không đổi gì — và lịch sử version đầy bản ghi trùng.

    Chuẩn hoá dừng ở thứ tự KHOÁ. Thứ tự phần tử trong list là nội dung thật
    (`nodes` của một pipeline), nên sắp xếp cả list sẽ làm hai định nghĩa khác
    nhau ra cùng một hash — hỏng theo hướng ngược lại và tệ hơn.
    """
    payload = json.dumps(definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()
