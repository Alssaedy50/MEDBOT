import asyncio
import os
import shutil
import tempfile
import sqlite3
import database

async def main():
    source_db = "medbot_v2.sqlite3"

    temp_dir = tempfile.mkdtemp(
        prefix="medbot-db-test-"
    )

    test_db = os.path.join(
        temp_dir,
        "test.sqlite3"
    )

    shutil.copy2(source_db, test_db)

    old_path = database.DB_PATH
    database.DB_PATH = test_db

    try:
        await database.init_db()

        con = sqlite3.connect(test_db)

        users = con.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        folders = con.execute(
            "SELECT COUNT(*) FROM folders"
        ).fetchone()[0]

        content = con.execute(
            "SELECT COUNT(*) FROM content"
        ).fetchone()[0]

        contributions = con.execute(
            "SELECT COUNT(*) FROM contributions"
        ).fetchone()[0]

        con.close()

        print("USERS_BEFORE =", users)
        print("FOLDERS_BEFORE =", folders)
        print("CONTENT_BEFORE =", content)
        print("CONTRIBUTIONS_BEFORE =", contributions)

        assert users == 2
        assert folders == 46
        assert content == 4
        assert contributions == 5

        print("EXISTING_DATA=PASS")

    finally:
        database.DB_PATH = old_path
        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

asyncio.run(main())

async def quota_test():
    temp_dir = tempfile.mkdtemp(
        prefix="medbot-quota-test-"
    )

    test_db = os.path.join(
        temp_dir,
        "quota.sqlite3"
    )

    shutil.copy2(
        "medbot_v2.sqlite3",
        test_db
    )

    old_path = database.DB_PATH
    database.DB_PATH = test_db

    try:
        await database.init_db()

        user_id = 987654321

        print("QUOTA_TEST_START")

        for i in range(20):
            allowed, remaining = (
                await database.check_and_increment_quota(
                    user_id,
                    20
                )
            )

            print(
                "REQUEST",
                i + 1,
                "ALLOWED=",
                allowed,
                "REMAINING=",
                remaining
            )

            assert allowed is True
            assert remaining == 19 - i

        allowed, remaining = (
            await database.check_and_increment_quota(
                user_id,
                20
            )
        )

        print(
            "REQUEST_21",
            "ALLOWED=",
            allowed,
            "REMAINING=",
            remaining
        )

        assert allowed is False
        assert remaining == 0

        remaining = (
            await database.get_remaining_quota(
                user_id,
                20
            )
        )

        print(
            "FINAL_REMAINING=",
            remaining
        )

        assert remaining == 0

        print("QUOTA_TEST=PASS")

    finally:
        database.DB_PATH = old_path
        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

asyncio.run(quota_test())

async def concurrency_test():
    temp_dir = tempfile.mkdtemp(
        prefix="medbot-concurrency-test-"
    )

    test_db = os.path.join(
        temp_dir,
        "concurrency.sqlite3"
    )

    shutil.copy2(
        "medbot_v2.sqlite3",
        test_db
    )

    old_path = database.DB_PATH
    database.DB_PATH = test_db

    try:
        await database.init_db()

        user_id = 777777777

        async def one_request():
            return await database.check_and_increment_quota(
                user_id,
                20
            )

        results = await asyncio.gather(
            *[one_request() for _ in range(40)]
        )

        allowed = sum(
            1 for ok, _ in results if ok
        )

        denied = sum(
            1 for ok, _ in results if not ok
        )

        remaining_values = [
            remaining
            for _, remaining in results
        ]

        con = sqlite3.connect(test_db)

        row = con.execute(
            """
            SELECT request_count
            FROM daily_ai_usage
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchone()

        con.close()

        final_count = row[0] if row else 0

        print("CONCURRENT_REQUESTS=40")
        print("ALLOWED=", allowed)
        print("DENIED=", denied)
        print("FINAL_COUNT=", final_count)
        print(
            "MIN_REMAINING=",
            min(remaining_values)
        )

        assert allowed == 20
        assert denied == 20
        assert final_count == 20
        assert min(remaining_values) == 0

        print("CONCURRENCY_TEST=PASS")

    finally:
        database.DB_PATH = old_path
        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

asyncio.run(concurrency_test())
