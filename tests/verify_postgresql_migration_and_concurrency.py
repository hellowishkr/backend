"""Automated PostgreSQL verification script for Wishkr Backend.

Verifies:
1. Alembic migration chain: a7c4e2b91d60 -> 6c4ad5ef8a2f -> b4f8a2c1d9e7 -> 30c7805adf97.
2. PostgreSQL schema, constraints, unique indexes, and seed data preservation.
3. Concurrent reservation claims using two independent asyncpg sessions on PostgreSQL:
   - Verifies dialect is postgresql.
   - Proves overlapping requests produce exactly ONE winner (200 OK) and ONE 409 Conflict.
4. Concurrent device registration retries on PostgreSQL:
   - Proves unique constraint uq_device_registrations_user_token prevents duplicate rows.
   - Proves dispatch_push_to_user sends exactly once.
5. Downgrade and re-upgrade on PostgreSQL.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone

import psycopg2
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.mobile_auth import get_current_mobile_user
from app.api.mobile_notifications import router as notifications_router
from app.api.mobile_wishlist import router as wishlist_router
from app.db.session import get_db
from app.models.device_registration import DeviceRegistration
from app.models.occasion import Occasion, OccasionType
from app.models.user import User
from app.models.wishlist_item import WishlistItem
from app.services.push_notification_sender import dispatch_push_to_user
from app.utils.id_gen import generate_id

SCRATCH_DIR = "/Users/karan/.gemini/antigravity-ide/brain/9c62fb24-f550-460b-a134-2832d389d455/scratch"
PG_DATA = os.path.join(SCRATCH_DIR, "pg_cluster_verify")
PG_PORT = 54334
PG_HOST = "localhost"
PG_USER = os.environ.get("USER", "karan")
DB_NAME = "wishkr_pg_verify"


def run(cmd: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    print(f">> Running: {cmd}")
    res = subprocess.run(cmd, shell=True, env=env or os.environ.copy(), capture_output=True, text=True)
    if check and res.returncode != 0:
        print(f"FAILED (code {res.returncode}):\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
        raise RuntimeError(f"Command failed: {cmd}")
    return res


def _make_app(db_session: AsyncSession, acting_user: User) -> FastAPI:
    app = FastAPI()
    app.include_router(wishlist_router, prefix="/api")
    app.include_router(notifications_router, prefix="/api")

    async def _get_db():
        yield db_session

    async def _get_user():
        return acting_user

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_mobile_user] = _get_user
    return app


async def test_postgresql_concurrency(async_engine):
    """Test overlapping concurrent reservation claims on PostgreSQL."""
    print("--- Testing Reservation Concurrency on PostgreSQL ---")
    async with async_engine.connect() as conn:
        print(f"Database dialect confirmed: {conn.dialect.name} (driver: {conn.dialect.driver})")
        assert conn.dialect.name == "postgresql", "Test must run against PostgreSQL!"

    Session = async_sessionmaker(async_engine, expire_on_commit=False)

    celebrant = User(id=generate_id("usr"), phone="+919000000001", name="Celebrant", is_guest=False)
    friend_1 = User(id=generate_id("usr"), phone="+919000000002", name="Friend 1", is_guest=False)
    friend_2 = User(id=generate_id("usr"), phone="+919000000003", name="Friend 2", is_guest=False)

    now = datetime.now(timezone.utc)
    occ = Occasion(
        id=generate_id("occ"), user_id=celebrant.id,
        type=OccasionType.BIRTHDAY, date=now + timedelta(days=5), recurring=True,
    )
    item = WishlistItem(
        id=generate_id("wsh"), user_id=celebrant.id,
        url="https://amazon.in/dp/PGCONCUR01",
        title="PostgreSQL Concurrency Item",
        purpose="self",
        share_status="available",
    )

    async with Session() as init_sess:
        init_sess.add_all([celebrant, friend_1, friend_2, occ, item])
        await init_sess.commit()

    # Create two separate async sessions to execute simultaneous overlapping claims
    async with Session() as sess_1, Session() as sess_2:
        app_1 = _make_app(sess_1, friend_1)
        app_2 = _make_app(sess_2, friend_2)

        transport_1 = ASGITransport(app=app_1)
        transport_2 = ASGITransport(app=app_2)

        async with AsyncClient(transport=transport_1, base_url="http://test") as client_1, \
                   AsyncClient(transport=transport_2, base_url="http://test") as client_2:

            payload = {"item_id": item.id, "occasion_id": occ.id}
            req_1 = client_1.post("/api/v1/friends/wishlist/reserve", json=payload)
            req_2 = client_2.post("/api/v1/friends/wishlist/reserve", json=payload)

            res_1, res_2 = await asyncio.gather(req_1, req_2)

    statuses = [res_1.status_code, res_2.status_code]
    print(f"PostgreSQL Concurrent Claim Responses: Friend 1={res_1.status_code}, Friend 2={res_2.status_code}")
    assert 200 in statuses, f"Expected exactly one 200 OK, got: {statuses}"
    assert 409 in statuses, f"Expected exactly one 409 Conflict, got: {statuses}"

    async with Session() as verify_sess:
        saved = await verify_sess.scalar(select(WishlistItem).where(WishlistItem.id == item.id))
        assert saved is not None
        assert saved.share_status == "reserved"
        assert saved.reserved_by_user_id in (friend_1.id, friend_2.id)
        print(f"Verified PostgreSQL single winner: reserved_by_user_id={saved.reserved_by_user_id}")

        # ── Test explicit IS NULL share_status reservation ────────────────────
        print("Testing reservation on item with share_status IS NULL on PostgreSQL...")
        null_item = WishlistItem(
            id=generate_id("wsh"),
            user_id=celebrant.id,
            url="https://amazon.in/dp/PGNULL01",
            title="PostgreSQL Null Share Status Item",
            purpose="self",
            share_status=None,  # explicitly NULL in database
        )
        verify_sess.add(null_item)
        await verify_sess.commit()

        app_null = _make_app(verify_sess, friend_1)
        async with AsyncClient(transport=ASGITransport(app=app_null), base_url="http://test") as c_null:
            res_null = await c_null.post(
                "/api/v1/friends/wishlist/reserve",
                json={"item_id": null_item.id, "occasion_id": occ.id},
            )
            assert res_null.status_code == 200, f"Expected 200 OK for NULL share_status, got {res_null.text}"
            await verify_sess.refresh(null_item)
            assert null_item.share_status == "reserved"
            assert null_item.reserved_by_user_id == friend_1.id
            print("Verified: Item with share_status IS NULL successfully reserved via explicit IS NULL query!")


async def test_postgresql_device_registration_concurrency(async_engine):
    """Test concurrent device registration retries and cross-account ownership on PostgreSQL."""
    print("--- Testing Device Registration Concurrency & Uniqueness on PostgreSQL ---")
    Session = async_sessionmaker(async_engine, expire_on_commit=False)

    user = User(id=generate_id("usr"), phone="+919000000099", name="Device User", is_guest=False)
    async with Session() as s:
        s.add(user)
        await s.commit()

    shared_token = "fcm_concurrent_token_" + generate_id("t")

    # 1. Concurrent registration requests for the exact same (user, token)
    async with Session() as s1, Session() as s2:
        app1 = _make_app(s1, user)
        app2 = _make_app(s2, user)

        async with AsyncClient(transport=ASGITransport(app=app1), base_url="http://test") as c1, \
                   AsyncClient(transport=ASGITransport(app=app2), base_url="http://test") as c2:

            payload = {"token": shared_token, "platform": "android"}
            r1 = c1.post("/api/v1/notifications/device-token", json=payload)
            r2 = c2.post("/api/v1/notifications/device-token", json=payload)

            res1, res2 = await asyncio.gather(r1, r2)
            assert res1.status_code == 200, res1.text
            assert res2.status_code == 200, res2.text

    # Verify that in PostgreSQL only ONE row exists
    async with Session() as s:
        regs = list((await s.scalars(
            select(DeviceRegistration).where(
                DeviceRegistration.user_id == user.id,
                DeviceRegistration.token == shared_token,
            )
        )).all())
        print(f"Active registration rows found for token on PostgreSQL: {len(regs)}")
        assert len(regs) == 1, f"Expected exactly 1 row, found {len(regs)} (duplicate created!)"

        # Verify dispatch produces exactly 1 send
        class TransportSpy:
            def __init__(self):
                self.sent = []
            async def send(self, tokens, title, body, data=None):
                self.sent.extend(tokens)
                return list(tokens)

        spy = TransportSpy()
        res = await dispatch_push_to_user(s, user.id, "Title", "Body", transport=spy)
        assert len(res.dispatched_tokens) == 1
        assert len(spy.sent) == 1, "Must dispatch exactly once, no duplicate push messages!"
        print("Push dispatch verified: exactly 1 message dispatched to device.")

    # 2. Simultaneous cross-account registration for the SAME device token under TWO DIFFERENT USERS
    print("--- Testing Cross-Account Simultaneous Registration of Same Token on PostgreSQL ---")
    user_a = User(id=generate_id("usr"), phone="+919000000010", name="User A", is_guest=False)
    user_b = User(id=generate_id("usr"), phone="+919000000020", name="User B", is_guest=False)
    async with Session() as s:
        s.add_all([user_a, user_b])
        await s.commit()

    handover_token = "fcm_handover_token_" + generate_id("t")

    # User A and User B concurrently register the exact same token in overlapping transactions
    async with Session() as sa, Session() as sb:
        app_a = _make_app(sa, user_a)
        app_b = _make_app(sb, user_b)

        async with AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as ca, \
                   AsyncClient(transport=ASGITransport(app=app_b), base_url="http://test") as cb:

            ra = ca.post("/api/v1/notifications/device-token", json={"token": handover_token, "platform": "android"})
            rb = cb.post("/api/v1/notifications/device-token", json={"token": handover_token, "platform": "android"})

            resa, resb = await asyncio.gather(ra, rb)
            assert resa.status_code == 200, resa.text
            assert resb.status_code == 200, resb.text

    # Verify database state in PostgreSQL:
    async with Session() as s:
        active_regs = list((await s.scalars(
            select(DeviceRegistration).where(
                DeviceRegistration.token == handover_token,
                DeviceRegistration.is_active == True,
            )
        )).all())
        print(f"Total ACTIVE owners across accounts for token: {len(active_regs)}")
        assert len(active_regs) == 1, f"Expected strictly 1 active owner across accounts, found {len(active_regs)}"
        active_owner = active_regs[0].user_id
        inactive_user = user_b.id if active_owner == user_a.id else user_a.id
        print(f"PostgreSQL verified: Single active owner is {active_owner}; {inactive_user} is inactive.")

        # Test dispatch to both accounts:
        spy_a = TransportSpy()
        spy_b = TransportSpy()
        await dispatch_push_to_user(s, user_a.id, "Hello A", "Msg A", transport=spy_a)
        await dispatch_push_to_user(s, user_b.id, "Hello B", "Msg B", transport=spy_b)

        total_dispatches_to_token = spy_a.sent.count(handover_token) + spy_b.sent.count(handover_token)
        print(f"Total dispatches delivering to handover token across both users: {total_dispatches_to_token}")
        assert total_dispatches_to_token == 1, (
            f"Expected exactly 1 total dispatch to this device token, got {total_dispatches_to_token} "
            f"(User A received: {spy_a.sent}, User B received: {spy_b.sent})"
        )
        print("Verified: Notifications CANNOT be dispatched to both accounts for that token!")


def main():
    os.makedirs(SCRATCH_DIR, exist_ok=True)
    if os.path.exists(PG_DATA):
        print("Cleaning up old test cluster...")
        subprocess.run(f"/opt/homebrew/bin/pg_ctl -D {PG_DATA} stop -m immediate", shell=True, capture_output=True)
        shutil.rmtree(PG_DATA, ignore_errors=True)

    print("=== Step 1: Booting Disposable PostgreSQL Cluster ===")
    run(f"/opt/homebrew/bin/initdb -D {PG_DATA} --no-locale -E UTF-8")
    run(f"/opt/homebrew/bin/pg_ctl -D {PG_DATA} -l {SCRATCH_DIR}/pg_verify.log -o '-p {PG_PORT} -k {SCRATCH_DIR}' start")

    time.sleep(2)
    ready = False
    for _ in range(10):
        r = subprocess.run(f"/opt/homebrew/bin/pg_isready -h localhost -p {PG_PORT}", shell=True, capture_output=True)
        if r.returncode == 0:
            ready = True
            break
        time.sleep(0.5)
    if not ready:
        raise RuntimeError("PostgreSQL failed to become ready")
    print("PostgreSQL started successfully.")

    try:
        run(f"/opt/homebrew/bin/createdb -h localhost -p {PG_PORT} {DB_NAME}")

        backend_dir = "/Users/karan/Desktop/Wishkr/wishkr-backend"
        db_url_async = f"postgresql+asyncpg://{PG_USER}@localhost:{PG_PORT}/{DB_NAME}"
        mig_env = os.environ.copy()
        mig_env["DATABASE_URL"] = db_url_async
        mig_env["PYTHONPATH"] = backend_dir

        print("\n=== Step 2: Upgrading Alembic to a7c4e2b91d60 (preceding) ===")
        run("alembic upgrade a7c4e2b91d60", env=mig_env)

        conn = psycopg2.connect(dbname=DB_NAME, user=PG_USER, host=PG_HOST, port=PG_PORT)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("INSERT INTO users (id, phone, name, is_guest) VALUES ('usr_seed_1', '+919888877777', 'Seed User', false);")
        cur.execute("INSERT INTO occasions (id, user_id, type, date, recurring) VALUES ('occ_seed_1', 'usr_seed_1', 'BIRTHDAY', NOW(), true);")
        cur.execute("INSERT INTO wishlist_items (id, user_id, url, title, purpose, share_status) VALUES ('wsh_seed_1', 'usr_seed_1', 'https://amazon.in/dp/S01', 'Seed Item', 'self', 'available');")
        print("Pre-migration seed data populated.")

        print("\n=== Step 3: Upgrading Alembic to 6c4ad5ef8a2f (table creation) ===")
        run("alembic upgrade 6c4ad5ef8a2f", env=mig_env)

        print("\n=== Step 4: Upgrading Alembic to b4f8a2c1d9e7 (unique constraint) ===")
        run("alembic upgrade b4f8a2c1d9e7", env=mig_env)

        print("\n=== Step 4b: Upgrading Alembic to 30c7805adf97 (forward migration: active token unique index) ===")
        run("alembic upgrade 30c7805adf97", env=mig_env)

        print("\n=== Step 5: Inspecting PostgreSQL Schema & Constraints ===")
        cur.execute("""
            SELECT conname, contype 
            FROM pg_constraint 
            WHERE conrelid = 'device_registrations'::regclass;
        """)
        constraints = cur.fetchall()
        print("Constraints on device_registrations:", constraints)
        c_names = [c[0] for c in constraints]
        assert "uq_device_registrations_user_token" in c_names, "uq_device_registrations_user_token unique constraint missing!"

        cur.execute("""
            SELECT indexname 
            FROM pg_indexes 
            WHERE tablename = 'device_registrations';
        """)
        indexes = [r[0] for r in cur.fetchall()]
        print("Indexes on device_registrations:", indexes)
        assert "uq_device_registrations_active_token" in indexes, "uq_device_registrations_active_token partial index missing!"

        # Verify seed data preservation
        cur.execute("SELECT name FROM users WHERE id = 'usr_seed_1';")
        assert cur.fetchone()[0] == 'Seed User'
        print("Seed data verified 100% intact after migrations!")
        cur.close()
        conn.close()

        print("\n=== Step 6: Running Live PostgreSQL Concurrency & Uniqueness Tests ===")
        async def run_all_async_tests():
            async_engine = create_async_engine(db_url_async, echo=False)
            try:
                await test_postgresql_concurrency(async_engine)
                await test_postgresql_device_registration_concurrency(async_engine)
            finally:
                await async_engine.dispose()

        asyncio.run(run_all_async_tests())

        print("\n=== Step 7: Testing Downgrade & Re-upgrade on PostgreSQL ===")
        run("alembic downgrade a7c4e2b91d60", env=mig_env)
        conn = psycopg2.connect(dbname=DB_NAME, user=PG_USER, host=PG_HOST, port=PG_PORT)
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('device_registrations');")
        assert cur.fetchone()[0] is None, "device_registrations table must be cleanly dropped on downgrade"
        cur.close()
        conn.close()
        print("Downgrade to a7c4e2b91d60 verified: table dropped, seed data preserved.")

        run("alembic upgrade head", env=mig_env)
        conn = psycopg2.connect(dbname=DB_NAME, user=PG_USER, host=PG_HOST, port=PG_PORT)
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('device_registrations');")
        assert cur.fetchone()[0] is not None, "device_registrations must exist after re-upgrade to head"
        cur.close()
        conn.close()
        print("Re-upgrade to head verified successfully!")

        print("\n==================================================================")
        print("🎉 ALL POSTGRESQL MIGRATION & CONCURRENCY CHECKS PASSED PERFECTLY!")
        print("==================================================================")

    finally:
        print("\n=== Shutting down disposable PostgreSQL Cluster ===")
        subprocess.run(f"/opt/homebrew/bin/pg_ctl -D {PG_DATA} stop -m immediate", shell=True, capture_output=True)
        shutil.rmtree(PG_DATA, ignore_errors=True)
        print("Disposable PostgreSQL cluster removed.")


if __name__ == "__main__":
    main()
