"""Read configured Supabase PDFs and queue one durable indexing job."""

import argparse
import asyncio
import json


async def run(args):
    from repositories.institution.base import Repository
    from services.advisory.managed_sources import prepare
    from tasks.institution_tasks import engine

    db = engine()
    try:
        async with db.begin() as conn:
            repo = Repository(conn)
            actor = await repo.one(
                "SELECT * FROM users WHERE firebase_uid=:uid AND phone_number IS NOT NULL",
                {"uid": args.firebase_uid},
            )
            print(json.dumps(await prepare(repo, actor)))
    finally:
        await db.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--firebase-uid",
        required=True,
        help="Existing user with an active KNOWLEDGE_REVIEW grant.",
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
