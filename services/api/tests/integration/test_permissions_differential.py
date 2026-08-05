"""Test đối chiếu: hai đường đánh giá phân quyền phải đồng ý trên MỌI item.

Lệch một phần tử là một lỗi bảo mật, không phải một lỗi làm tròn. Đây là thứ duy
nhất biến "một nguồn quy tắc" từ lời hứa thành ràng buộc kiểm được.

Ba điều test này phải nói rõ, vì thiếu bất kỳ điều nào là nó xanh mà không canh
được gì:

1. **Item so bằng ĐẲNG THỨC, workspace so bằng BAO HÀM.**
   `visible_workspaces_select` CỐ Ý hiện một workspace khi principal có grant lên
   một item bên trong, còn `effective_role_for_workspace` trả `None` đúng trong
   trường hợp đó. Hai đường bất đồng về workspace theo THIẾT KẾ; đẳng thức ở đó
   sẽ là một test sai. Quan hệ đúng nằm ở `_expected_visible_workspaces`.

2. **Đường SQL lọc `state`, đường một-tài-nguyên KHÔNG.** Một item đã xoá mềm vẫn
   phải đọc được theo id (để khôi phục được) nhưng không được nằm trong danh sách.
   Thế giới ngẫu nhiên ở đây LUÔN chứa cả ba dạng hàng đã xoá mềm, và các câu
   khẳng định `# phủ:` bên dưới là thứ chứng minh chúng có mặt — không có chúng
   thì bộ lọc `state` không bao giờ được chạy qua và phép trừ `& listable` chỉ là
   trang trí.

3. **Hai đường TÍNH HAI THỨ KHÁC NHAU.** Một bên `max(roles)` rồi `allows(...)`,
   bên kia `EXISTS(role IN roles_allowing(action))`. Chúng chỉ trùng nhau khi
   `ACTION_MATRIX` là chuỗi lồng nghiêm ngặt. Tiền đề đó được khẳng định ngay
   trong file này, tại chỗ dùng nó.
"""

import itertools
import random
import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from loom_api.models import (
    ACTIVE,
    DEFAULT_TENANT_ID,
    DELETED,
    AppUser,
    Domain,
    Item,
    RoleAssignment,
    Workspace,
)
from loom_api.permissions import (
    Forbidden,
    NotVisible,
    PermissionService,
    _roles_allowing,
    visible_items_select,
    visible_workspaces_select,
)
from loom_api.routers.search import search_items_select
from loom_core.roles import ACTION_MATRIX, Action, Role
from loom_core.schemas import Principal

pytestmark = pytest.mark.integration

GROUPS = ("data-eng", "analysts", "ops")
SEEDS = list(range(25))


def _rng(seed: int) -> random.Random:
    """Nguồn ngẫu nhiên TÁI LẬP ĐƯỢC theo seed.

    `random` chứ không `secrets`: một seed đỏ phải đỏ lại y hệt trên máy khác và
    ở lần chạy sau, nếu không thì báo cáo lỗi không đi kèm cách dựng lại hiện
    trường. Một nguồn ngẫu nhiên mật mã học không cho điều đó, và ở đây không có
    gì cần giữ bí mật.
    """
    return random.Random(seed)  # noqa: S311 — không dùng cho mục đích mật mã


@dataclass(frozen=True)
class World:
    principal: Principal
    items: tuple[uuid.UUID, ...]
    workspaces: tuple[uuid.UUID, ...]
    workspace_of: dict[uuid.UUID, uuid.UUID]
    active_items: frozenset[uuid.UUID]
    active_workspaces: frozenset[uuid.UUID]
    # Ba hàng CỐ ĐỊNH trong mọi thế giới, mỗi hàng phủ một bộ lọc `state` khác
    # nhau. Xem các câu khẳng định `# phủ:` trong test.
    shared_deleted_item: uuid.UUID
    dead_workspace: uuid.UUID
    item_in_dead_workspace: uuid.UUID

    def listable(self) -> set[uuid.UUID]:
        """Những item mà đường SQL còn được phép trả về, BỎ QUA quyền: còn sống
        và nằm trong một workspace còn sống."""
        return {
            i
            for i in self.items
            if i in self.active_items and self.workspace_of[i] in self.active_workspaces
        }


async def _build_world(session: AsyncSession, rng: random.Random) -> World:
    """Hai domain, bốn workspace sống + một đã xoá mềm, mười ba item, và một nắm
    assignment rải ở cả bốn cấp scope cho cả user lẫn group."""
    actor = uuid.uuid4()
    subject_user = uuid.uuid4()
    for uid, tag in ((actor, "a"), (subject_user, "s")):
        session.add(
            AppUser(
                id=uid,
                tenant_id=DEFAULT_TENANT_ID,
                subject=f"{tag}{uid.hex}",
                email=f"{tag}@loom.local",
                display_name=tag,
            )
        )
    await session.flush()

    domains = [uuid.uuid4() for _ in range(2)]
    for i, d in enumerate(domains):
        session.add(
            Domain(
                id=d,
                tenant_id=DEFAULT_TENANT_ID,
                name=f"d{i}-{d.hex[:12]}",
                display_name=f"D{i}",
                created_by=actor,
                updated_by=actor,
            )
        )

    def new_workspace(index: int, domain_id: uuid.UUID | None, state: str) -> uuid.UUID:
        ws = uuid.uuid4()
        session.add(
            Workspace(
                id=ws,
                tenant_id=DEFAULT_TENANT_ID,
                domain_id=domain_id,
                name=f"w{index}-{ws.hex[:12]}",
                display_name=f"W{index}",
                storage_prefix=f"w{index}",
                state=state,
                created_by=actor,
                updated_by=actor,
            )
        )
        return ws

    # Bốn workspace còn sống, mỗi cái mang MỘT hình dạng quyền khác nhau (xem
    # phần phát assignment ở dưới). Cái thứ nhất BUỘC thuộc một domain vì nó là
    # workspace "chỉ với tới được qua domain"; ba cái còn lại bốc ngẫu nhiên, kể
    # cả `None` — một workspace không thuộc domain nào phải không bị nhánh domain
    # với tới, và `scope_id = NULL` là NULL chứ không phải TRUE.
    live_workspaces = [new_workspace(0, domains[0], ACTIVE)]
    live_workspaces += [new_workspace(i, rng.choice([*domains, None]), ACTIVE) for i in range(1, 4)]
    dead_workspace = new_workspace(4, rng.choice([*domains, None]), DELETED)
    await session.flush()

    def new_item(index: int, workspace_id: uuid.UUID, state: str) -> uuid.UUID:
        it = uuid.uuid4()
        session.add(
            Item(
                id=it,
                tenant_id=DEFAULT_TENANT_ID,
                workspace_id=workspace_id,
                type="sql_script",
                name=f"i{index}-{it.hex[:12]}",
                display_name=f"I{index}",
                definition={"schema_version": 1, "sql": ""},
                definition_hash="0" * 64,
                created_by=actor,
                updated_by=actor,
                state=state,
            )
        )
        return it

    # Item 0 luôn bị xoá mềm và luôn được chia sẻ lẻ cho principal ở dưới: đó là
    # hàng chứng minh đường SQL lọc `Item.state` còn `require_item` thì không.
    # Phần còn lại xoá mềm ngẫu nhiên khoảng một phần tư.
    deleted_indexes = {0} | {i for i in range(1, 12) if rng.random() < 0.25}
    live_items = [
        new_item(
            i,
            live_workspaces[i % len(live_workspaces)],
            DELETED if i in deleted_indexes else ACTIVE,
        )
        for i in range(12)
    ]
    # Item CÒN SỐNG trong một workspace ĐÃ XOÁ: phủ bộ lọc `Workspace.state` của
    # `visible_items_select`, thứ mà xoá mềm item một mình không chạm tới.
    item_in_dead_workspace = new_item(12, dead_workspace, ACTIVE)
    await session.flush()

    items = (*live_items, item_in_dead_workspace)
    workspaces = (*live_workspaces, dead_workspace)
    workspace_of = {i: live_workspaces[n % len(live_workspaces)] for n, i in enumerate(live_items)}
    workspace_of[item_in_dead_workspace] = dead_workspace

    my_groups = tuple(sorted(rng.sample(GROUPS, rng.randint(0, len(GROUPS)))))

    # `uq_role_assignment_principal_scope` là UNIQUE với NULLS NOT DISTINCT, nên
    # hai lần bốc trúng cùng (principal, scope) là IntegrityError chứ không phải
    # một thế giới thú vị hơn. Nhớ lại những gì đã phát.
    issued: set[tuple[uuid.UUID | None, str | None, str, uuid.UUID]] = set()

    def issue(
        scope_type: str,
        scope_id: uuid.UUID,
        role: Role,
        user_id: uuid.UUID | None = None,
        group: str | None = None,
    ) -> bool:
        key = (user_id, group, scope_type, scope_id)
        if key in issued:
            return False
        issued.add(key)
        session.add(
            RoleAssignment(
                id=uuid.uuid4(),
                tenant_id=DEFAULT_TENANT_ID,
                principal_type="user" if user_id else "group",
                principal_user_id=user_id,
                principal_group=group,
                scope_type=scope_type,
                scope_id=scope_id,
                role=str(role),
                created_by=actor,
            )
        )
        return True

    # Hai grant CỐ ĐỊNH, phát trước để chúng không bị một lần bốc ngẫu nhiên
    # trùng khoá chiếm mất. Chúng là thứ giữ cho ba hàng đã xoá mềm ở trên thật
    # sự NẰM TRONG câu trả lời của đường một-tài-nguyên — một hàng bị xoá mềm mà
    # principal cũng không có quyền đọc thì không phân biệt được hai đường.
    #
    # `admin` chứ không `viewer`: đây là mỏ neo của một phép đo về bộ lọc `state`,
    # nên nó không được đổ vì một lý do khác. `viewer` bị một grant ngẫu nhiên cao
    # hơn đè lên là câu trả lời của đường một-tài-nguyên đổi, và mỏ neo báo đỏ về
    # một chuyện nó không nói.
    issue("item", live_items[0], Role.admin, user_id=subject_user)
    issue("workspace", dead_workspace, Role.admin, user_id=subject_user)

    # Bốn hình dạng quyền, mỗi workspace còn sống một hình. Bốc đều tay KHÔNG
    # cho đủ chúng, và một hình vắng mặt là một nhánh của biểu thức lọc không
    # được đi qua ở seed đó. Đã đo trên bộ sinh chỉ-ngẫu-nhiên: 11/25 seed có nổi
    # một item mang hai vai trò khác nhau, và KHÔNG seed nào có nó trên một item
    # còn liệt kê được — nên một vai trò không đơn điệu đi qua trọn 25 seed mà
    # không làm đỏ câu khẳng định chính. Đó là một test xanh không canh được gì.
    ws_domain_only, ws_stacked, ws_group_only, _ws_untouched = live_workspaces

    def items_in(workspace_id: uuid.UUID) -> list[uuid.UUID]:
        return [i for i in live_items if workspace_of[i] == workspace_id]

    # (1) CHỈ với tới được qua nhánh `domain`. Nếu nhánh domain của đường danh
    # sách biến mất, những item này rơi khỏi danh sách trong khi `require_item`
    # vẫn cho qua.
    issue("domain", domains[0], rng.choice(list(Role)), user_id=subject_user)

    # (2) CHỒNG hai vai trò khác nhau ở hai tầng scope. Đây là hình dạng DUY NHẤT
    # phân biệt được `allows(max(roles), action)` với
    # `EXISTS(role ∈ roles_allowing(action))`: chuỗi chỉ mang một vai trò thì hai
    # công thức trùng nhau về mặt cú pháp và test không nói lên điều gì về chúng.
    stacked_items = items_in(ws_stacked)
    if stacked_items:
        low, high = rng.sample(list(Role), 2)
        # Một vế đi qua NHÓM khi principal có nhóm: "vai trò của người và vai trò
        # của nhóm gộp bằng max" cũng phải đúng ở đường danh sách.
        if my_groups and rng.random() < 0.5:
            issue("item", rng.choice(stacked_items), low, group=rng.choice(my_groups))
        else:
            issue("item", rng.choice(stacked_items), low, user_id=subject_user)
        issue("workspace", ws_stacked, high, user_id=subject_user)

    # (3) CHỈ với tới được qua một nhóm principal thuộc.
    if my_groups:
        issue("workspace", ws_group_only, rng.choice(list(Role)), group=my_groups[0])

    # (4) `_ws_untouched` không nhận gì có chủ đích: phải có item mà principal
    # KHÔNG thấy, nếu không "quá rộng" là trạng thái không quan sát được.

    # Rải phần còn lại. Cố ý gồm cả những assignment KHÔNG áp cho principal (user
    # khác, nhóm principal không thuộc) để test cũng đỏ khi biểu thức QUÁ RỘNG,
    # chứ không chỉ khi quá hẹp.
    #
    # Một lần bốc trúng scope của (1) hoặc (3) và trúng principal sẽ mở một đường
    # thứ hai tới đúng những item mà hình dạng đó tồn tại để cô lập, và hình dạng
    # đó im lặng biến mất khỏi seed. Đổi hướng sang `actor` giữ nguyên số hàng và
    # nguyên phép thử "quá rộng", chỉ bỏ đúng cái trùng đường.
    reserved: set[tuple[str, uuid.UUID]] = {("workspace", ws_domain_only)}
    reserved |= {("item", i) for i in items_in(ws_domain_only)}
    reserved |= {("workspace", ws_group_only)}
    reserved |= {("item", i) for i in items_in(ws_group_only)}

    candidates: list[tuple[str, uuid.UUID]] = [("tenant", DEFAULT_TENANT_ID)]
    candidates += [("domain", d) for d in domains]
    candidates += [("workspace", w) for w in workspaces]
    candidates += [("item", i) for i in items]

    for _ in range(rng.randint(0, 14)):
        scope_type, scope_id = rng.choice(candidates)
        role = rng.choice(list(Role))
        is_reserved = (scope_type, scope_id) in reserved
        if rng.random() < 0.5:
            pool = [g for g in GROUPS if g not in my_groups] if is_reserved else list(GROUPS)
            if pool:
                issue(scope_type, scope_id, role, group=rng.choice(pool))
            else:
                issue(scope_type, scope_id, role, user_id=actor)
        else:
            # 50% gán cho principal, 50% cho một user khác.
            to_principal = rng.random() < 0.5 and not is_reserved
            issue(scope_type, scope_id, role, user_id=subject_user if to_principal else actor)
    await session.flush()

    return World(
        principal=Principal(
            user_id=subject_user,
            subject="s",
            email="s@loom.local",
            display_name="s",
            groups=my_groups,
        ),
        items=items,
        workspaces=workspaces,
        workspace_of=workspace_of,
        active_items=frozenset(i for n, i in enumerate(live_items) if n not in deleted_indexes)
        | {item_in_dead_workspace},
        active_workspaces=frozenset(live_workspaces),
        shared_deleted_item=live_items[0],
        dead_workspace=dead_workspace,
        item_in_dead_workspace=item_in_dead_workspace,
    )


async def _readable_items(perms: PermissionService, world: World) -> set[uuid.UUID]:
    """Đường 1: hỏi từng item bằng `require_item`.

    Bắt CẢ `Forbidden`, không chỉ `NotVisible`. Câu hỏi ở đây là "đường một-tài-
    nguyên có cho đọc item này không", và nó từ chối bằng hai ngoại lệ khác nhau:
    404 khi không có vai trò nào, 403 khi có vai trò nhưng vai trò đó không cho
    `item_read`. Chỉ bắt `NotVisible` thì đúng trường hợp lệch thú vị nhất — vai
    trò cao hơn nhưng thiếu `item_read` — thoát ra thành exception chưa bắt thay
    vì một hiệu hai tập, tức là test đỏ ở một chỗ không nói lên điều gì.
    """
    readable: set[uuid.UUID] = set()
    for item_id in world.items:
        try:
            await perms.require_item(item_id, Action.item_read)
        except (NotVisible, Forbidden):
            continue
        readable.add(item_id)
    return readable


async def _readable_workspaces(perms: PermissionService, world: World) -> set[uuid.UUID]:
    readable: set[uuid.UUID] = set()
    for ws_id in world.workspaces:
        try:
            await perms.require_workspace(ws_id, Action.workspace_read)
        except (NotVisible, Forbidden):
            continue
        readable.add(ws_id)
    return readable


def _expected_visible_workspaces(
    world: World, readable_workspaces: set[uuid.UUID], readable_items: set[uuid.UUID]
) -> set[uuid.UUID]:
    """Quan hệ CHÍNH XÁC giữa hai đường ở tầng workspace.

    Một workspace hiện ra khi và chỉ khi nó còn sống VÀ (đọc được trực tiếp HOẶC
    chứa ít nhất một item mà danh sách được phép trả về). Vế thứ hai là spec mục
    4.3: chia sẻ lẻ một item phải kéo theo workspace chứa nó, nếu không item đó
    không có đường nào tới.

    Cả hai vế được dựng từ đường MỘT-TÀI-NGUYÊN, không từ `visible_items_select`.
    Dùng kết quả SQL để dựng kỳ vọng cho một câu khẳng định về SQL là để cho một
    biểu thức lọc quá rộng tự cấp cho mình một workspace rộng theo — vế phải phải
    độc lập với thứ đang bị kiểm.

    "Không lỏng hơn": bỏ `Workspace.state`, bỏ `Item.state` trong nhánh item, bỏ
    vế `Item.workspace_id == Workspace.id`, hay bỏ `scope_type = 'item'` đều làm
    vế trái to ra mà vế phải đứng yên.
    "Không chặt hơn": bỏ hẳn nhánh `by_item_inside` làm vế trái nhỏ đi.
    """
    listable = world.listable()
    reachable = {world.workspace_of[i] for i in readable_items & listable}
    return (readable_workspaces & world.active_workspaces) | reachable


@pytest.mark.parametrize("seed", SEEDS)
async def test_two_paths_agree_on_every_item(db_session: AsyncSession, seed: int) -> None:
    rng = _rng(seed)
    world = await _build_world(db_session, rng)
    perms = PermissionService(db_session, world.principal)

    via_check = await _readable_items(perms, world)
    via_sql = {
        row.id
        for row in (await db_session.execute(visible_items_select(world.principal))).scalars().all()
    }

    listable = world.listable()
    assert via_sql == via_check & listable, (
        f"seed={seed} hai đường KHÔNG đồng ý\n"
        f"  chỉ SQL thấy   : {sorted(via_sql - (via_check & listable))}\n"
        f"  chỉ check thấy : {sorted((via_check & listable) - via_sql)}\n"
        f"  nhóm principal : {world.principal.groups}"
    )

    # phủ: hai hàng dưới đây là chỗ DUY NHẤT `& listable` ở trên có tác dụng. Nếu
    # thế giới ngẫu nhiên thôi không sinh ra chúng thì bộ lọc `state` của đường
    # SQL không bao giờ được chạy qua, và câu khẳng định trên xanh mà không biết.
    assert world.shared_deleted_item in via_check, "item đã xoá mềm vẫn phải đọc được theo id"
    assert world.shared_deleted_item not in via_sql, "…nhưng không được nằm trong danh sách"
    assert world.item_in_dead_workspace in via_check
    assert world.item_in_dead_workspace not in via_sql, (
        "workspace đã xoá thì item bên trong phải ẩn theo"
    )


@pytest.mark.parametrize("seed", SEEDS)
async def test_the_two_paths_keep_their_intended_containment_on_workspaces(
    db_session: AsyncSession, seed: int
) -> None:
    """Ở tầng workspace hai đường CỐ Ý khác nhau — nên khẳng định là bao hàm, và
    quan hệ bao hàm đó phải đúng đến từng phần tử theo cả hai chiều."""
    rng = _rng(seed)
    world = await _build_world(db_session, rng)
    perms = PermissionService(db_session, world.principal)

    via_check_ws = await _readable_workspaces(perms, world)
    via_check_items = await _readable_items(perms, world)
    via_sql_ws = {
        row.id
        for row in (await db_session.execute(visible_workspaces_select(world.principal)))
        .scalars()
        .all()
    }

    expected = _expected_visible_workspaces(world, via_check_ws, via_check_items)
    assert via_sql_ws == expected, (
        f"seed={seed} quan hệ bao hàm giữa hai đường đã đổi\n"
        f"  chỉ SQL thấy  : {sorted(via_sql_ws - expected)}\n"
        f"  chỉ kỳ vọng   : {sorted(expected - via_sql_ws)}\n"
        f"  nhóm principal: {world.principal.groups}"
    )

    # phủ: workspace đã xoá mềm vẫn đọc được theo id nhưng không được liệt kê.
    assert world.dead_workspace in via_check_ws
    assert world.dead_workspace not in via_sql_ws

    # Và bất biến UX mà cả quan hệ trên tồn tại vì nó: không item nào lọt vào
    # danh sách mà workspace chứa nó lại không lọt.
    orphans = {
        i for i in via_check_items & world.listable() if world.workspace_of[i] not in via_sql_ws
    }
    assert orphans == set(), f"seed={seed}: item liệt kê được nhưng workspace của nó thì không"


@pytest.mark.parametrize("seed", SEEDS)
async def test_search_agrees_with_the_per_item_check(db_session: AsyncSession, seed: int) -> None:
    """`search` là endpoint DỄ RÒ RỈ NHẤT: không có `workspace_id` trong đường dẫn
    nên không có gì nhắc người viết phải lọc quyền. Spec mục 6 đòi nó có mặt ở đây.

    Gọi ĐÚNG `search_items_select` mà router dùng, không dựng lại biểu thức: một
    test tự ghép `visible_items_select` với một bộ lọc văn bản chỉ chứng minh rằng
    bản ghép trong test là đúng, và sẽ vẫn xanh sau khi ai đó sửa router thành
    `select(Item)`.

    Term `"i"` khớp MỌI item trong thế giới này — chúng được đặt tên `i{index}-…`
    (xem `_build_world`) — nên quan hệ đúng là ĐẲNG THỨC với đường SQL, không phải
    một quan hệ tập con. Tập con là câu mà một endpoint trả rỗng cũng thoả.
    """
    rng = _rng(seed)
    world = await _build_world(db_session, rng)
    perms = PermissionService(db_session, world.principal)

    via_check = await _readable_items(perms, world)
    listable = world.listable()
    via_search = {
        row.id
        for row in (await db_session.execute(search_items_select(world.principal, "i")))
        .scalars()
        .all()
    }

    assert via_search == via_check & listable, (
        f"seed={seed} search KHÔNG khớp phép kiểm từng item\n"
        f"  chỉ search thấy: {sorted(via_search - (via_check & listable))}\n"
        f"  search bỏ sót  : {sorted((via_check & listable) - via_search)}"
    )

    # Tiền đề của đẳng thức trên, đọc từ DỮ LIỆU THẬT: term `"i"` phải khớp mọi item
    # mà đường SQL trả về. Nếu quy ước đặt tên trong `_build_world` đổi, bộ lọc văn
    # bản sẽ bắt đầu loại bỏ hàng và câu khẳng định kia thoái hoá thành một quan hệ
    # tập con — thứ mà một endpoint trả rỗng cũng thoả. Dòng này đỏ TRƯỚC, kèm lý do
    # thật, thay vì để đẳng thức kia đỏ với một thông báo nói về phân quyền.
    via_sql = {
        row.id
        for row in (await db_session.execute(visible_items_select(world.principal))).scalars().all()
    }
    assert via_search == via_sql, "term 'i' không còn khớp mọi item — quy ước đặt tên đã đổi"

    # phủ: `search_items_select` bọc `visible_items_select`, nên nó cũng phải thừa
    # hưởng bộ lọc `state`. Không có hai dòng này thì đẳng thức trên xanh kể cả với
    # một bản bỏ bộ lọc đó, vì `& listable` đã trừ chúng khỏi vế phải.
    assert world.shared_deleted_item not in via_search
    assert world.item_in_dead_workspace not in via_search


def test_the_differential_test_rests_on_role_monotonicity() -> None:
    """Test đối chiếu chỉ có nghĩa NẾU `ACTION_MATRIX` là chuỗi lồng.

    Đường một-tài-nguyên hỏi `allows(max(roles), action)`; đường danh sách hỏi
    `∃ role ∈ roles: role ∈ roles_allowing(action)`. Hai câu đó tương đương khi
    và chỉ khi vai trò cao luôn bao quyền của vai trò thấp. Nếu ai đó thêm một
    vai trò cao hơn nhưng thiếu một quyền của vai trò thấp, hai đường bất đồng
    một cách HỢP LỆ và test đối chiếu sẽ đỏ ở một chỗ khó hiểu — người đọc sẽ
    tưởng biểu thức SQL sai. Khẳng định tiền đề ngay tại chỗ dùng nó.

    `test_roles.py` đã có một test cùng nội dung. Bản đó bảo vệ `max_role()`;
    bản này ghi rằng test đối chiếu SỤP nếu tiền đề mất, và một test đọc file
    này phải thấy được điều đó mà không phải đi tìm.
    """
    for lower, higher in itertools.pairwise(sorted(Role)):
        assert ACTION_MATRIX[lower] <= ACTION_MATRIX[higher], (
            f"{higher} thiếu quyền mà {lower} có — test đối chiếu không còn hợp lệ"
        )


def test_the_workspace_containment_rests_on_item_read_implying_workspace_read() -> None:
    """Tiền đề thứ hai, của riêng quan hệ bao hàm ở tầng workspace.

    `_expected_visible_workspaces` nói: liệt kê được một item thì workspace chứa
    nó cũng phải liệt kê được. Khi item đó đến từ một grant cấp workspace/domain/
    tenant, điều đó chỉ đúng nếu vai trò cho `item_read` cũng cho `workspace_read`
    — hai nhánh của `visible_workspaces_select` lọc theo hai hành động KHÁC nhau.
    Một vai trò đọc được item mà không đọc được workspace sẽ tạo ra item mồ côi.
    """
    assert set(_roles_allowing(Action.item_read)) <= set(_roles_allowing(Action.workspace_read))
