"""Run real SQL/RPC contracts in disposable official Supabase containers."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from threading import Event
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def docker(*args, env=None):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, env=env, timeout=60)
    if result.returncode:
        # Docker may print environment/connection strings on failure.
        raise RuntimeError("Isolated Docker operation failed: " + args[0])
    return result.stdout.strip()


def port(container, inner):
    return int(docker("port", container, str(inner)).rsplit(":", 1)[1])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", default="docs/tdd08-evidence")
    parser.add_argument("--pytest-args", nargs="*", default=[])
    args = parser.parse_args()
    import psycopg
    from psycopg import sql
    from src.memory.deployment import SupabaseSQL
    evidence = Path(args.evidence_dir).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    name = "funagent-tdd08-" + uuid4().hex[:12]
    containers = []
    network = None
    password, jwt_secret = secrets.token_hex(24), secrets.token_hex(32)
    env = os.environ | {"POSTGRES_PASSWORD": password, "JWT_SECRET": jwt_secret}
    try:
        network = docker("network", "create", "--label", "funagent.tdd08=" + name, name)
        db = docker("run", "-d", "--name", name + "-db", "--label", "funagent.tdd08=" + name,
            "--network", name, "--network-alias", "db", "-p", "127.0.0.1::5432", "-e", "POSTGRES_PASSWORD",
            "-e", "JWT_SECRET", "-e", "POSTGRES_USER=supabase_admin", "supabase/postgres:17.11.0.004", "postgres", "-c", "config_file=/etc/postgresql/postgresql.conf", env=env)
        containers.append(db)
        dsn = f"postgresql://supabase_admin:{password}@127.0.0.1:{port(db, 5432)}/postgres"
        end = time.monotonic() + 60
        while True:
            try:
                connection = psycopg.connect(dsn, connect_timeout=2, autocommit=True)
                break
            except psycopg.OperationalError:
                if time.monotonic() >= end:
                    raise RuntimeError("Isolated database did not become ready") from None
                Event().wait(0.25)
        with connection as admin:
            # Official DB images can run without the complete Auth/Storage stack.
            for role in ("anon", "authenticated", "service_role", "authenticator"):
                if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                    admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
            admin.execute(sql.SQL("CREATE ROLE tdd08_authenticator LOGIN NOINHERIT PASSWORD {}").format(sql.Literal(password)))
            admin.execute(SupabaseSQL())
            admin.execute("GRANT funagent_memory, service_role, anon, authenticated TO tdd08_authenticator")
            versions = {"postgres": admin.execute("SHOW server_version").fetchone()[0],
                        "vector": admin.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0]}
        env |= {"PGRST_DB_URI": f"postgresql://tdd08_authenticator:{password}@db:5432/postgres", "PGRST_JWT_SECRET": jwt_secret}
        rpc = docker("run", "-d", "--name", name + "-rpc", "--label", "funagent.tdd08=" + name,
            "--network", name, "-p", "127.0.0.1::3000", "-e", "PGRST_DB_URI", "-e", "PGRST_JWT_SECRET",
            "-e", "PGRST_DB_SCHEMAS=public", "-e", "PGRST_DB_ANON_ROLE=anon", "postgrest/postgrest:v16.4", env=env)
        containers.append(rpc)
        rpc_url = f"http://127.0.0.1:{port(rpc, 3000)}"
        import httpx
        end = time.monotonic() + 30
        while True:
            try:
                with httpx.Client(trust_env=False) as client:
                    response = client.get(rpc_url + "/", timeout=2)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() >= end:
                raise RuntimeError("Isolated RPC did not become ready")
            Event().wait(0.25)
        versions |= {"postgrest": response.headers.get("server", "postgrest:v16.4"),
                     "images": {image: json.loads(docker("image", "inspect", image))[0]["RepoDigests"] for image in
                                ("supabase/postgres:17.11.0.004", "postgrest/postgrest:v16.4")}}
        (evidence / "backend-versions.json").write_text(json.dumps(versions, indent=2), encoding="utf8")
        print("Clean official Supabase SQL deployment and PostgREST ready", flush=True)
        test_env = os.environ | {"TDD08_SUPABASE_ISOLATED": "1", "TDD08_SUPABASE_DB_DSN": dsn,
            "TDD08_SUPABASE_RPC_URL": rpc_url, "TDD08_SUPABASE_JWT_SECRET": jwt_secret}
        result = subprocess.run([sys.executable, "-m", "pytest", "tests_refactor", "-o", "addopts=", "-m", "supabase_integration",
            "-q", "-ra", "--junitxml=" + str(evidence / "supabase-suite.xml"), *args.pytest_args], cwd=ROOT, env=test_env)
        return result.returncode
    finally:
        for container in reversed(containers):
            docker("rm", "-fv", container)
        if network:
            docker("network", "rm", network)
        print("Isolated test containers, volumes and network removed", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
