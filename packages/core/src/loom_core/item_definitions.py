"""Schema `definition` cho từng loại item — mỏng, có version, không nhận trường lạ.

`schema_version` có mặt từ Giai đoạn 1 để Giai đoạn 2/3 mở rộng được mà không
phải đoán bản ghi cũ theo hình dạng.
"""

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from loom_core.cron import next_tick

# Các lớp ký tự dùng chung cho HAI regex dưới đây. Khai một lần vì bản `k8s://`
# có nhóm bắt (`K8S_SECRET_REF_RE`) phải là đúng nhánh `k8s://` của bản chung —
# xem lý do ở ngay trên nó.
_K8S_NS = r"[a-z0-9-]+"
_K8S_NAME = r"[a-z0-9.-]+"
_SECRET_KEY = r"[A-Za-z0-9._-]+"  # noqa: S105 — lớp ký tự cho TÊN khoá, không phải khoá

# vault://<path>#<key>  hoặc  k8s://<namespace>/<name>#<key>
# Chặt có chủ đích: ô này là ĐƯỜNG DẪN, và mục đích chính của regex là chặn
# người dùng dán mật khẩu thật vào đây.
#
# `\Z` chứ KHÔNG phải `$`. Trong Python `$` khớp cả ở vị trí ngay trước một `\n`
# cuối chuỗi, nên `^...$` nhận "vault://a/b#c\n" — tức là nhận một chuỗi mà mắt
# người đọc là hai dòng. Đây là đúng lớp lỗi đã làm thủng bộ lọc URL của không
# ít dự án; `\Z` không có ngoại lệ đó.
SECRET_REF_RE = re.compile(
    rf"\A(?:vault://[A-Za-z0-9._\-/]+|k8s://{_K8S_NS}/{_K8S_NAME})#{_SECRET_KEY}\Z"
)

# Nhánh `k8s://` của `SECRET_REF_RE`, có nhóm bắt — dạng DUY NHẤT mà đường nạp
# (Giai đoạn 3a, xem `loom_api.ingest_service`) dùng được, vì cụm local không
# tới được Vault. `\Z` vì đúng lý do đã ghi ở trên.
#
# Dùng CÙNG các lớp ký tự ở trên chứ không viết lại: hai bản chép sẽ trôi, và
# trôi ở đây có hướng và người dùng thấy được — nới `SECRET_REF_RE` rộng ra
# (thêm ký tự vào namespace hay tên) mà quên bản kia thì một `secret_ref` mà
# `POST /items` đã NHẬN và đã LƯU bị đường nạp từ chối bằng 400 "not usable",
# trỏ vào chính dữ liệu mà API đã chấp thuận.
K8S_SECRET_REF_RE = re.compile(
    rf"\Ak8s://(?P<ns>{_K8S_NS})/(?P<name>{_K8S_NAME})#(?P<key>{_SECRET_KEY})\Z"
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


# --- PipelineStep & ScheduleDefinition (Task 2) -----------------------------


class IngestStepConfig(BaseModel):
    """Khối `ingest` bên trong một `PipelineStep`."""

    model_config = ConfigDict(extra="forbid")
    lakehouse_id: uuid.UUID
    connection_id: uuid.UUID
    stream: str = Field(min_length=1, max_length=255)
    mode: Literal["full", "incremental"]


class SqlStepConfig(BaseModel):
    """Khối `sql` bên trong một `PipelineStep`."""

    model_config = ConfigDict(extra="forbid")
    lakehouse_id: uuid.UUID
    sql: str = Field(min_length=1)


class PipelineStep(BaseModel):
    """Một bước trong chuỗi TUYẾN TÍNH. 3b không có rẽ nhánh."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["ingest", "sql"]
    ingest: IngestStepConfig | None = None
    sql: SqlStepConfig | None = None

    @model_validator(mode="after")
    def _config_matches_type(self) -> "PipelineStep":
        if self.type == "ingest" and self.ingest is None:
            raise ValueError("bước type='ingest' phải có khối `ingest`")
        if self.type == "sql" and self.sql is None:
            raise ValueError("bước type='sql' phải có khối `sql`")
        if self.type == "ingest" and self.sql is not None:
            raise ValueError("bước type='ingest' không được mang khối `sql`")
        if self.type == "sql" and self.ingest is not None:
            raise ValueError("bước type='sql' không được mang khối `ingest`")
        return self


class ScheduleDefinition(BaseModel):
    """Định nghĩa lịch cho một pipeline tự động."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    cron: str
    timezone: str = "UTC"
    run_as_user_id: uuid.UUID | None = None

    @field_validator("cron")
    @classmethod
    def _cron_parses(cls, value: str) -> str:
        next_tick(value, "UTC", datetime(2026, 1, 1, tzinfo=UTC))
        return value

    @field_validator("timezone")
    @classmethod
    def _timezone_exists(cls, value: str) -> str:
        next_tick("0 0 * * *", value, datetime(2026, 1, 1, tzinfo=UTC))
        return value

    @model_validator(mode="after")
    def _enabled_names_its_principal(self) -> "ScheduleDefinition":
        if self.enabled and self.run_as_user_id is None:
            raise ValueError("lịch đã bật phải có run_as_user_id")
        return self


class PipelineDefinition(_Base):
    """`steps` TUYẾN TÍNH — không còn `nodes`/`edges`."""

    steps: list[PipelineStep] = Field(default_factory=list)
    schedule: ScheduleDefinition | None = None


DEFINITION_BY_TYPE: dict[ItemType, type[_Base]] = {
    ItemType.lakehouse: LakehouseDefinition,
    ItemType.connection: ConnectionDefinition,
    ItemType.pipeline: PipelineDefinition,
    ItemType.sql_script: SqlScriptDefinition,
}

DEFAULT_DEFINITION: dict[ItemType, dict[str, Any]] = {
    ItemType.lakehouse: {"schema_version": 1},
    ItemType.pipeline: {"schema_version": 1, "steps": []},
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
