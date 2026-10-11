"""Installed-package access to the standalone, transactional deployment asset."""
from importlib.resources import files


def SupabaseSQL() -> str:
    migrations = files("src.memory").joinpath("assets/supabase/migrations")
    return "\n".join(file.read_text(encoding="utf8") for file in sorted(migrations.iterdir(), key=lambda item: item.name)
                     if file.name.endswith(".sql"))
