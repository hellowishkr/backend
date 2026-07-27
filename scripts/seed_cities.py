"""Seed/upsert verified cities with aliases + pincode prefixes.

Worked example: Pune (covers Pune city + Pimpri-Chinchwad twin city, pincode
band 4110xx–4130xx). Add more cities to CITIES as Wishkr expands. Idempotent —
re-running updates the coverage data in place.
"""

import asyncio
import os
from pathlib import Path


def load_local_database_url() -> None:
    if os.environ.get("DATABASE_URL"):
        return
    env_dev = Path(__file__).resolve().parents[1] / ".env.dev"
    if not env_dev.exists():
        return
    for line in env_dev.read_text().splitlines():
        if line.startswith("DATABASE_URL="):
            os.environ["DATABASE_URL"] = line.split("=", 1)[1].strip().strip("\"'")
            return


load_local_database_url()

from app.db.session import async_session  # noqa: E402
from app.models.city import City  # noqa: E402

# id, name, state, aliases, pincode_prefixes
CITIES = [
    (
        "pune",
        "Pune",
        "Maharashtra",
        ["Pimpri", "Chinchwad", "Pimpri-Chinchwad", "Hinjewadi", "Hadapsar", "Wakad"],
        ["4110", "4111", "4112", "4113", "4114", "4130"],
    ),
]


async def main() -> None:
    async with async_session() as db:
        for city_id, name, state, aliases, prefixes in CITIES:
            city = await db.get(City, city_id)
            if city is None:
                city = City(id=city_id, name=name, auto_created=False)
                db.add(city)
            city.name = name
            city.state = state
            city.aliases = aliases
            city.pincode_prefixes = prefixes
            city.status = "verified"
            print(f"upserted city {city_id}: {len(prefixes)} prefixes, {len(aliases)} aliases")
        await db.commit()


if __name__ == "__main__":
    asyncio.run(main())
