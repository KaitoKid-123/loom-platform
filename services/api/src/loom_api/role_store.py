"""Gán và thu vai trò, kèm hai quy tắc chống leo thang quyền (spec mục 4.4).

Cả hai là loại thiếu sót thành SỰ CỐ BẢO MẬT chứ không thành một lỗi hiển thị:

1. `role.grant` một mình cho phép member tự nâng mình lên admin trong đúng một
   lệnh. `GRANTABLE_BY` đặt trần: member gán được tới contributor, không hơn.
2. Thu mất admin cuối cùng của một phạm vi là tự khoá mình khỏi workspace của
   chính mình, và cách sửa duy nhất lúc đó là vào database bằng tay.

Quy tắc 2 phải chạy TRONG CÙNG transaction với lệnh xoá và phải khoá hàng lại.
Kiểm rồi xoá ở hai transaction khác nhau thì hai người thu đồng thời đều thấy
"còn hai admin" và cả hai đều xoá. Test tuần tự KHÔNG bắt được điều đó —
`test_role_store_concurrent.py` mới bắt được.
"""

import uuid

from fastapi import HTTPException, status
from sqlalchemy import ColumnElement, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.integrity import constraint_of
from loom_api.models import DEFAULT_TENANT_ID, RoleAssignment
from loom_api.permissions import Forbidden, PermissionService
from loom_core.roles import GRANTABLE_BY, Action, Role
from loom_core.schemas import Principal

Scope = tuple[str, uuid.UUID]


_PRINCIPAL_USER_FK = "fk_role_assignment_principal_user_id_app_user"


class LastAdminError(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status.HTTP_409_CONFLICT,
            "this is the last admin of this scope — grant another admin before removing it",
        )


class UnknownUser(HTTPException):
    def __init__(self, user_id: uuid.UUID) -> None:
        # 422 chứ không 500: `user_id` là dữ liệu client gửi lên, và một danh sách
        # người dùng đã cũ trên giao diện là cách bình thường nhất để nó sai.
        super().__init__(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"there is no user {user_id}",
        )


def _one_principal(user_id: uuid.UUID | None, group: str | None) -> ColumnElement[bool]:
    """Điều kiện khớp ĐÚNG MỘT principal trong một scope.

    Không dùng `principal_matches` của `permissions.py`: hàm đó trả lời "hàng
    này có áp cho principal đang đăng nhập không" và cố ý gộp cả nhóm của người
    đó. Ở đây câu hỏi là "hàng này có phải hàng đang bị thu không", và gộp nhóm
    vào sẽ biến một lệnh thu một người thành lệnh thu cả nhóm.
    """
    if user_id is not None:
        return RoleAssignment.principal_user_id == user_id
    return RoleAssignment.principal_group == group


class RoleStore:
    def __init__(self, session: AsyncSession, principal: Principal) -> None:
        self._session = session
        self._principal = principal
        self._perms = PermissionService(session, principal)

    @property
    def perms(self) -> PermissionService:
        """Cùng một `PermissionService` mà các phép kiểm ở đây đã dùng.

        Router cần vai trò hiệu lực của người gọi để tính `grantable_roles`. Dựng
        một service thứ hai cho ra cùng câu trả lời nhưng mất cache trong phạm vi
        request, tức là một round trip nữa cho một câu hỏi vừa hỏi xong.
        """
        return self._perms

    async def _require_role_grant(self, scope: Scope) -> Role:
        """Cửa quyền dùng CHUNG cho grant và revoke.

        Viết hai lần là mời gọi drift, và drift ở đây nghĩa là một trong hai
        đường quên mất `role.grant`.

        Trả về vai trò HIỆU LỰC (đã tính cả tổ tiên và cả nhóm), không phải một
        hàng cấp quyền trực tiếp: quy tắc 1 phải áp cho một member thừa hưởng từ
        domain y như cho một member được gán thẳng ở workspace.
        """
        scope_type, scope_id = scope
        if scope_type == "workspace":
            return await self._perms.require_workspace(scope_id, Action.role_grant)
        if scope_type == "item":
            return await self._perms.require_item(scope_id, Action.role_grant)
        # domain/tenant: chuỗi tổ tiên chỉ chạy TỪ tài nguyên LÊN, nên không có
        # câu hỏi "vai trò của tôi trên domain này" nào để hỏi bằng hai hàm ở
        # trên. Từ chối thẳng thay vì bỏ qua phép kiểm.
        raise Forbidden("only a tenant admin can change roles at this scope")

    async def grant(
        self,
        scope: Scope,
        role: Role,
        user_id: uuid.UUID | None = None,
        group: str | None = None,
    ) -> None:
        if (user_id is None) == (group is None):
            raise HTTPException(422, "give exactly one of user_id or group")
        actor_role = await self._require_role_grant(scope)

        # QUY TẮC 1. Không có dòng này thì `role.grant` một mình đủ để member tự
        # nâng thành admin — một bước, không cần ai duyệt.
        if role not in GRANTABLE_BY[actor_role]:
            raise Forbidden(f"a {actor_role} cannot grant the {role} role")

        scope_type, scope_id = scope
        # ON CONFLICT: gán lại cùng principal + scope là ĐỔI vai trò, không phải
        # lỗi. Bộ cột suy luận phải khớp index `uq_role_assignment_principal_scope`
        # — index đó là NULLS NOT DISTINCT, nên hàng nhóm (principal_user_id
        # NULL) cũng được coi là trùng nhau.
        stmt = (
            pg_insert(RoleAssignment)
            .values(
                id=uuid.uuid4(),
                tenant_id=DEFAULT_TENANT_ID,
                principal_type="user" if user_id else "group",
                principal_user_id=user_id,
                principal_group=group,
                scope_type=scope_type,
                scope_id=scope_id,
                role=str(role),
                created_by=self._principal.user_id,
            )
            .on_conflict_do_update(
                index_elements=[
                    "principal_user_id",
                    "principal_group",
                    "scope_type",
                    "scope_id",
                ],
                set_={"role": str(role), "created_by": self._principal.user_id},
            )
        )
        try:
            await self._session.execute(stmt)
            # flush ngay để lỗi khoá ngoại nổ Ở ĐÂY, nơi còn biết `user_id` nào
            # gây ra nó. Để tới lúc `commit()` ở router thì IntegrityError bay ra
            # từ một chỗ không có ngữ cảnh và client nhận 500.
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            if constraint_of(exc) != _PRINCIPAL_USER_FK or user_id is None:
                raise
            raise UnknownUser(user_id) from exc

    async def revoke(
        self,
        scope: Scope,
        user_id: uuid.UUID | None = None,
        group: str | None = None,
    ) -> None:
        if (user_id is None) == (group is None):
            raise HTTPException(422, "give exactly one of user_id or group")
        actor_role = await self._require_role_grant(scope)
        scope_type, scope_id = scope
        target = _one_principal(user_id, group)

        # QUY TẮC 3 — đối xứng với quy tắc 1: chỉ thu được vai trò mình gán được.
        #
        # Không có nó, `GRANTABLE_BY` chặn member GÁN lên `admin` nhưng không gì
        # chặn member THU của một admin đang có. Bất đối xứng đó nghĩa là thứ bạn
        # không được phép cho, bạn lại được phép lấy đi. Không phải leo thang —
        # member vẫn không thành admin được, và quy tắc 2 giữ sàn ở một admin —
        # nhưng nó cho một member đơn phương gỡ admin của workspace xuống còn một.
        #
        # Dùng lại đúng `GRANTABLE_BY` của quy tắc 1, không dựng bảng thứ hai:
        # hai bảng nói cùng một điều là hai bảng sẽ trôi khỏi nhau.
        existing = (
            (
                await self._session.execute(
                    select(RoleAssignment.role).where(
                        RoleAssignment.scope_type == scope_type,
                        RoleAssignment.scope_id == scope_id,
                        target,
                    )
                )
            )
            .scalars()
            .all()
        )
        for role_name in existing:
            role = Role[role_name]
            if role not in GRANTABLE_BY[actor_role]:
                raise Forbidden(f"a {actor_role} cannot remove the {role} role")

        # QUY TẮC 2, và nó phải chạy TRONG CÙNG transaction với DELETE.
        #
        # `FOR UPDATE` khoá các hàng admin lại. Không có nó, hai lệnh thu đồng
        # thời đều đọc "còn hai admin" trước khi lệnh nào commit, cả hai đều
        # xoá, và phạm vi mất sạch admin. Có nó, lệnh thứ hai phải đợi lệnh đầu
        # commit rồi đọc lại — ở READ COMMITTED, hàng đã bị xoá không còn khớp
        # nữa, nên nó đếm ra một admin và bị chặn. Đã chứng minh bằng cách gỡ
        # `FOR UPDATE` và xem test đồng thời đỏ.
        #
        # `ORDER BY id` KHÔNG phải để cho đẹp: hai transaction khoá cùng một tập
        # hàng theo hai thứ tự khác nhau là deadlock. Cùng một thứ tự thì lệnh
        # thứ hai chỉ đợi.
        #
        # MỘT truy vấn cho cả hai câu hỏi ("còn mấy admin" và "hàng bị thu có
        # phải admin không"): hỏi hai lần là hai ảnh chụp có thể lệch nhau, và
        # lệch ở đây nghĩa là quy tắc đọc một trạng thái không bao giờ tồn tại.
        rows = (
            await self._session.execute(
                select(
                    RoleAssignment.id,
                    RoleAssignment.principal_user_id,
                    RoleAssignment.principal_group,
                )
                .where(
                    # scope_type + scope_id: admin của một phạm vi KHÁC không
                    # cứu được phạm vi này.
                    RoleAssignment.scope_type == scope_type,
                    RoleAssignment.scope_id == scope_id,
                    # Chỉ đếm admin CỦA CHÍNH phạm vi này, không tính tổ tiên.
                    # Tính cả admin cấp tenant thì quy tắc không bao giờ kích
                    # hoạt nữa — hầu như hệ thống nào cũng có một tài khoản như
                    # thế, và nó sẽ làm quy tắc xanh vĩnh viễn mà không bảo vệ gì.
                    RoleAssignment.role == str(Role.admin),
                )
                .order_by(RoleAssignment.id)
                .with_for_update()
            )
        ).all()

        if user_id is not None:
            being_removed = [r for r in rows if r.principal_user_id == user_id]
        else:
            being_removed = [r for r in rows if r.principal_group == group]
        # Cần CẢ HAI vế. Chỉ `len(rows) <= 1` thì thu một viewer bất kỳ cũng bị
        # chặn oan khi phạm vi còn đúng một admin.
        if being_removed and len(rows) <= 1:
            raise LastAdminError

        await self._session.execute(
            delete(RoleAssignment).where(
                RoleAssignment.scope_type == scope_type,
                RoleAssignment.scope_id == scope_id,
                target,
            )
        )

    async def list_roles(self, scope: Scope) -> list[RoleAssignment]:
        scope_type, scope_id = scope
        if scope_type == "workspace":
            await self._perms.require_workspace(scope_id, Action.role_read)
        elif scope_type == "item":
            await self._perms.require_item(scope_id, Action.role_read)
        else:
            raise Forbidden("only a tenant admin can read roles at this scope")
        return list(
            (
                await self._session.execute(
                    select(RoleAssignment)
                    .where(
                        RoleAssignment.scope_type == scope_type,
                        RoleAssignment.scope_id == scope_id,
                    )
                    # Thứ tự XÁC ĐỊNH, theo lúc gán. Không có nó Postgres tự do trả
                    # theo thứ tự nào cũng được, và bảng quyền trên giao diện đổi chỗ
                    # giữa hai lần tải — người dùng đọc đó là "có ai vừa sửa gì".
                    # `id` phá thế hoà: nhiều grant trong cùng một transaction chia
                    # nhau một `created_at`.
                    .order_by(RoleAssignment.created_at, RoleAssignment.id)
                )
            )
            .scalars()
            .all()
        )
