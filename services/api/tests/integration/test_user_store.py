"""Integration test cho PostgresUserStore — cần Postgres thật (testcontainers).

Tách khỏi test_sessions.py cùng lý do Task 7 tách oidc.py: nửa thuần (PKCE,
cookie ký) không chạm I/O nên test unit là đủ; PostgresUserStore là lớp duy
nhất trong chồng auth chạm DB thật, và trước bản vá này không hề có test nào
cho nó — mọi câu hỏi review đặt ra đều phải viết script dùng một lần.
"""

import asyncio
import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from loom_api.db import Database, build_sqlalchemy_url
from loom_api.models import DEFAULT_TENANT_ID, AppUser, UserSession
from loom_api.oidc_verifier import IdTokenClaims, InvalidIdToken
from loom_api.user_store import PostgresUserStore
from loom_core.config import Settings
from loom_core.schemas import Principal

API_DIR = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def pg_settings() -> Iterator[Settings]:
    # Container + migration một lần cho cả module — dựng container mỗi test
    # (như test_migrations.py làm, vì đó chỉ có một test) sẽ chậm không cần
    # thiết cho một file nhiều test như thế này.
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:17-alpine") as pg:
        env = {
            "LOOM_DB_HOST": pg.get_container_host_ip(),
            "LOOM_DB_PORT": str(pg.get_exposed_port(5432)),
            "LOOM_DB_NAME": pg.dbname,
            "LOOM_DB_USER": pg.username,
            "LOOM_DB_PASSWORD": pg.password,
            # Container testcontainers không cấu hình TLS; xem ghi chú ở
            # Settings(...) bên dưới.
            "LOOM_DB_SSLMODE": "disable",
        }
        result = subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],  # noqa: S607 — uv chạy qua PATH có chủ đích
            cwd=API_DIR,
            env={**os.environ, **env},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

        yield Settings(
            db_host=pg.get_container_host_ip(),
            db_port=int(pg.get_exposed_port(5432)),
            db_name=pg.dbname,
            db_user=pg.username,
            db_password=pg.password,
            # Container testcontainers không cấu hình TLS. Mặc định
            # `db_sslmode` là "verify-full" (đúng cho Aiven qua Internet công
            # khai — xem Task 14), nhưng ở đây sẽ nổ bằng
            # `root certificate file "~/.postgresql/root.crt" does not exist`.
            # Khai rõ "disable" cho container Postgres dùng một lần, dùng
            # xong bỏ, thay vì lặng lẽ kế thừa một giá trị nhắm cho DB thật.
            db_sslmode="disable",
        )


@pytest.fixture
async def db(pg_settings: Settings) -> AsyncIterator[Database]:
    database = Database(build_sqlalchemy_url(pg_settings))
    yield database
    # Dọn bảng sau mỗi test để test sau bắt đầu từ trạng thái sạch — container
    # và schema (migration) được tái sử dụng cho cả module, chỉ dữ liệu bị xoá.
    async with database.session() as session:
        await session.execute(sa.delete(UserSession))
        await session.execute(sa.delete(AppUser))
        await session.commit()
    await database.dispose()


@pytest.fixture
def store(db: Database) -> PostgresUserStore:
    return PostgresUserStore(db, session_ttl_hours=12)


async def test_upsert_creates_user_and_session(store: PostgresUserStore, db: Database) -> None:
    claims = IdTokenClaims(subject="subject-fresh", email="a@loom.local", display_name="A")

    session_id = await store.upsert_user_and_create_session(claims, refresh_token="rt-fresh")

    async with db.session() as session:
        user = (await session.execute(select_user_by_subject("subject-fresh"))).scalar_one()
        assert user.email == "a@loom.local"
        assert user.display_name == "A"

        sess_row = (
            await session.execute(
                sa.select(UserSession).where(UserSession.id == uuid.UUID(session_id))
            )
        ).scalar_one()
        assert sess_row.user_id == user.id
        assert sess_row.refresh_token == "rt-fresh"
        # Token không có claim `groups` → cột phải là mảng RỖNG, không NULL. Cột
        # NOT NULL, nên một NULL ở đây sẽ là lỗi ghi, không phải "không có nhóm".
        assert sess_row.groups == []


async def test_second_login_reuses_user_and_updates_last_login(
    store: PostgresUserStore, db: Database
) -> None:
    first_claims = IdTokenClaims(subject="subject-2", email="old@loom.local", display_name="Old")
    await store.upsert_user_and_create_session(first_claims, refresh_token="rt1")

    async with db.session() as session:
        first_user = (await session.execute(select_user_by_subject("subject-2"))).scalar_one()
        first_login_at = first_user.last_login_at
        first_user_id = first_user.id

    await asyncio.sleep(0.01)  # mốc thời gian khác đi để so sánh > có ý nghĩa
    second_claims = IdTokenClaims(subject="subject-2", email="new@loom.local", display_name="New")
    await store.upsert_user_and_create_session(second_claims, refresh_token="rt2")

    async with db.session() as session:
        users = (
            (await session.execute(sa.select(AppUser).where(AppUser.subject == "subject-2")))
            .scalars()
            .all()
        )
        assert len(users) == 1, "đăng nhập lần hai phải TÁI SỬ DỤNG hàng user, không tạo hàng mới"
        user = users[0]
        assert user.id == first_user_id
        assert user.email == "new@loom.local"
        assert user.display_name == "New"
        assert user.last_login_at is not None
        assert first_login_at is not None
        assert user.last_login_at > first_login_at

        sessions = (
            (await session.execute(sa.select(UserSession).where(UserSession.user_id == user.id)))
            .scalars()
            .all()
        )
        assert len(sessions) == 2, "mỗi lần đăng nhập phải tạo MỘT session mới"


async def test_concurrent_first_logins_produce_one_user_and_n_sessions(
    store: PostgresUserStore, db: Database
) -> None:
    """Bảo vệ cho race SELECT-rồi-INSERT: hai (hoặc N) lần đăng nhập ĐẦU TIÊN
    đồng thời của cùng một subject mới đều phải thấy chưa có hàng, và trước
    bản vá ON CONFLICT, tất cả-trừ-một sẽ vi phạm uq_app_user_subject và ném
    IntegrityError — một double-click vào nút đăng nhập là đủ để bật ra 500
    trên một cổng uvicorn thật."""
    claims = IdTokenClaims(subject="subject-concurrent", email="c@loom.local", display_name="C")
    n = 10

    session_ids = await asyncio.gather(
        *(store.upsert_user_and_create_session(claims, refresh_token=f"rt-{i}") for i in range(n))
    )

    assert len(session_ids) == n
    assert len(set(session_ids)) == n  # N session phân biệt, không lỗi nào rơi ra

    async with db.session() as session:
        users = (
            (await session.execute(select_user_by_subject("subject-concurrent"))).scalars().all()
        )
        assert len(users) == 1, f"kỳ vọng đúng 1 app_user, thấy {len(users)}"

        sessions = (
            (
                await session.execute(
                    sa.select(UserSession).where(UserSession.user_id == users[0].id)
                )
            )
            .scalars()
            .all()
        )
        assert len(sessions) == n, f"kỳ vọng đúng {n} user_session, thấy {len(sessions)}"


async def test_load_session_returns_principal_for_live_session(
    store: PostgresUserStore, db: Database
) -> None:
    claims = IdTokenClaims(subject="subject-live", email="live@loom.local", display_name="Live")
    session_id = await store.upsert_user_and_create_session(claims, refresh_token=None)

    loaded = await store.load_session(session_id)

    async with db.session() as session:
        user = (await session.execute(select_user_by_subject("subject-live"))).scalar_one()

    # So sánh CẢ đối tượng với UUID ĐỌC TỪ DB. `assert user_id is not None` sẽ
    # xanh kể cả khi load_session() trả uuid4() mới mỗi lần — và user_id là thứ
    # khớp với role_assignment.principal_user_id, nên sai nó là RBAC nhìn vào
    # một người khác. Đây là mấu chốt của cả Task 3.
    assert loaded == Principal(
        user_id=user.id,
        subject="subject-live",
        email="live@loom.local",
        display_name="Live",
        groups=(),
    )


async def test_load_session_user_id_survives_a_second_login(
    store: PostgresUserStore, db: Database
) -> None:
    """Hai phiên của cùng một người phải mang CÙNG user_id — nếu load_session()
    lấy id từ hàng session (hoặc dựng mới) thay vì từ app_user thì hai lần đăng
    nhập cho ra hai principal khác nhau và mọi role_assignment gắn với người này
    biến mất sau khi đăng nhập lại."""
    claims = IdTokenClaims(subject="subject-stable", email="s@loom.local", display_name="S")
    first = await store.upsert_user_and_create_session(claims, refresh_token=None)
    second = await store.upsert_user_and_create_session(claims, refresh_token=None)
    assert first != second

    p1 = await store.load_session(first)
    p2 = await store.load_session(second)
    assert p1 is not None
    assert p2 is not None

    async with db.session() as session:
        user = (await session.execute(select_user_by_subject("subject-stable"))).scalar_one()
    assert p1.user_id == p2.user_id == user.id


async def test_two_users_get_distinct_user_ids(store: PostgresUserStore) -> None:
    """Bảo vệ ngược lại: một cài đặt trả về hằng số (hay tenant id) cũng làm
    test trên xanh nếu nó chỉ so hai phiên của MỘT người."""
    a = await store.upsert_user_and_create_session(
        IdTokenClaims(subject="subject-a", email="a@loom.local", display_name="A"), None
    )
    b = await store.upsert_user_and_create_session(
        IdTokenClaims(subject="subject-b", email="b@loom.local", display_name="B"), None
    )
    pa = await store.load_session(a)
    pb = await store.load_session(b)
    assert pa is not None
    assert pb is not None
    assert pa.user_id != pb.user_id


async def test_groups_survive_login_and_reload(store: PostgresUserStore, db: Database) -> None:
    """Nhóm đi trọn vòng: claims → cột user_session.groups → Principal."""
    claims = IdTokenClaims(
        subject="CgVsb25n",
        email="long@loom.local",
        display_name="long",
        groups=("data-eng", "admins"),
    )
    session_id = await store.upsert_user_and_create_session(claims, None)

    async with db.session() as session:
        sess_row = (
            await session.execute(
                sa.select(UserSession).where(UserSession.id == uuid.UUID(session_id))
            )
        ).scalar_one()
        # Cột thật sự mang nhóm — không phải mảng rỗng mà Principal rồi lại
        # điền vào từ đâu khác.
        assert sorted(sess_row.groups) == ["admins", "data-eng"]

    principal = await store.load_session(session_id)
    assert principal is not None
    # Đã sắp xếp: chứng minh chuẩn hoá xảy ra chứ không phải trùng hợp thứ tự —
    # claims đưa vào là ("data-eng", "admins").
    assert principal.groups == ("admins", "data-eng")
    assert claims.groups != principal.groups, "input phải KHÁC output đã sắp xếp"


async def test_load_session_deduplicates_groups_from_the_row(
    store: PostgresUserStore, db: Database
) -> None:
    """Cột là text[] — không có gì trong Postgres chặn phần tử trùng, và một
    migration hay một bản vá tay hoàn toàn có thể để lại chúng. Chuẩn hoá phải
    đứng ở đường ĐỌC, không chỉ ở đường ghi."""
    claims = IdTokenClaims(subject="subject-dupes", email="d@loom.local", display_name="D")
    session_id = await store.upsert_user_and_create_session(claims, None)

    async with db.session() as session:
        await session.execute(
            sa.update(UserSession)
            .where(UserSession.id == uuid.UUID(session_id))
            .values(groups=["zulu", "admins", "zulu"])
        )
        await session.commit()

    principal = await store.load_session(session_id)
    assert principal is not None
    assert principal.groups == ("admins", "zulu")


async def test_load_session_rejects_a_row_with_an_empty_group_name(
    store: PostgresUserStore, db: Database
) -> None:
    """`principal_group = ''` trong role_assignment khớp với một nhóm tên rỗng.
    Cột text[] cho phép '' hoàn toàn tự nhiên, nên validator của Principal là
    thứ DUY NHẤT chặn một hàng như thế biến thành quyền."""
    claims = IdTokenClaims(subject="subject-blank-group", email="bg@loom.local", display_name="BG")
    session_id = await store.upsert_user_and_create_session(claims, None)

    async with db.session() as session:
        await session.execute(
            sa.update(UserSession)
            .where(UserSession.id == uuid.UUID(session_id))
            .values(groups=["admins", ""])
        )
        await session.commit()

    with pytest.raises(InvalidIdToken) as caught:
        await store.load_session(session_id)
    assert caught.value.reason == "unusable_session_row"


async def test_load_session_returns_none_for_expired_session(
    store: PostgresUserStore, db: Database
) -> None:
    claims = IdTokenClaims(subject="subject-expired", email="e@loom.local", display_name="E")
    session_id = await store.upsert_user_and_create_session(claims, refresh_token=None)

    async with db.session() as session:
        await session.execute(
            sa.update(UserSession)
            .where(UserSession.id == uuid.UUID(session_id))
            .values(expires_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await session.commit()

    assert await store.load_session(session_id) is None


async def test_load_session_returns_none_for_malformed_id(store: PostgresUserStore) -> None:
    assert await store.load_session("not-a-uuid-at-all") is None


async def test_load_session_returns_none_for_unknown_id(store: PostgresUserStore) -> None:
    assert await store.load_session(str(uuid.uuid4())) is None


async def test_load_session_raises_when_row_has_empty_subject(
    store: PostgresUserStore, db: Database
) -> None:
    """load_session() dựng danh tính trực tiếp từ hàng DB, đi vòng qua verify().
    Bất biến "subject không rỗng" phải vẫn chặn trên đường này (dữ liệu hỏng),
    không được âm thầm trả về danh tính rác.

    Trước Task 3 thì __post_init__ của IdTokenClaims giữ bất biến ấy. Giờ
    load_session() trả Principal, nên nó nằm ở validator của Principal và
    load_session() dịch ValidationError sang InvalidIdToken — nếu không, nhánh
    `except InvalidIdToken` ở /me (và deps.py của Task 4) thành mã chết và một
    hàng hỏng đi ra thành 500."""
    async with db.session() as session:
        bad_user = AppUser(
            id=uuid.uuid4(),
            tenant_id=DEFAULT_TENANT_ID,
            subject="",
            email="empty-subject@loom.local",
            display_name="Empty Subject",
        )
        session.add(bad_user)
        await session.flush()
        bad_session = UserSession(
            id=uuid.uuid4(),
            user_id=bad_user.id,
            refresh_token=None,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.add(bad_session)
        await session.commit()
        bad_session_id = str(bad_session.id)

    with pytest.raises(InvalidIdToken) as caught:
        await store.load_session(bad_session_id)
    assert caught.value.reason == "unusable_session_row"


async def test_delete_session_removes_row(store: PostgresUserStore, db: Database) -> None:
    claims = IdTokenClaims(subject="subject-delete", email="d@loom.local", display_name="D")
    session_id = await store.upsert_user_and_create_session(claims, refresh_token=None)

    await store.delete_session(session_id)

    async with db.session() as session:
        row = (
            await session.execute(
                sa.select(UserSession).where(UserSession.id == uuid.UUID(session_id))
            )
        ).scalar_one_or_none()
        assert row is None


async def test_delete_session_is_noop_for_unknown_id(store: PostgresUserStore) -> None:
    await store.delete_session(str(uuid.uuid4()))  # không raise


async def test_delete_session_is_noop_for_malformed_id(store: PostgresUserStore) -> None:
    await store.delete_session("not-a-uuid-at-all")  # không raise


def select_user_by_subject(subject: str) -> sa.Select[tuple[AppUser]]:
    return sa.select(AppUser).where(AppUser.subject == subject)
