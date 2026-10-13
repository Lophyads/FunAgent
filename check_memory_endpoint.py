"""Read-only Supabase REST/RPC check; Python 3.11+ and project dependencies.

Run: uv run --no-sync python -X utf8 check_memory_endpoint.py --conf-dir .
Exit codes: 0 = memory reads work, 1 = remote check failed, 2 = local setup error.
"""
import argparse
import math
import os
from pathlib import Path
import time
import tomllib
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv


class ConfigError(ValueError):
    """A diagnostic safe to display without quoting configuration contents."""


def redact(value, secrets):
    text = str(value)
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def check_rpc(client, name, payload, secrets):
    started = time.monotonic()
    try:
        response = client.post("rpc/funagent_memory_" + name, json=payload)
    except httpx.RequestError as error:
        print(f"[FAIL] {name}: {type(error).__name__}: {redact(error, secrets)}")
        return False
    print(f"[HTTP] {name}: {response.status_code}, {time.monotonic() - started:.2f}s")
    try:
        data = response.json()
    except ValueError:
        print(f"[FAIL] {name}: 响应不是 JSON，无法确认记忆接口可用")
        return False
    if response.status_code != 200:
        if isinstance(data, dict):
            for field in ("code", "message", "hint", "details"):
                if data.get(field):
                    print(f"  {field}: {redact(data[field], secrets)[:600]}")
            if data.get("code") == "PGRST202":
                print("  诊断：当前 RPC 函数签名不存在或 schema cache 未更新。")
        if response.status_code == 401:
            print("  诊断：检查配置指定的 API key / token 是否正确或已过期。")
        elif response.status_code == 403:
            print("  诊断：检查函数执行权限及 namespace / RLS 授权。")
        return False
    # An empty list is valid. Do not print memory contents or credentials.
    if not isinstance(data, list) or len(data) > payload["p_limit"]:
        print(f"[FAIL] {name}: 返回值不符合当前记忆 RPC 的列表协议")
        return False
    for row in data:
        if (not isinstance(row, dict)
                or row.get("namespace") != payload["p_namespace"]
                or row.get("source_id") != payload["p_source_id"]
                or row.get("kind") != "episodic"
                or type(row.get("seq")) is not int or row["seq"] <= 0
                or row.get("deleted") is not False
                or any(not isinstance(row.get(key), str) or not row[key].strip()
                       for key in ("record_id", "type", "content", "version"))
                or not isinstance(row.get("evidence"), list) or not row["evidence"]
                or any(not isinstance(item, str) or not item.strip() for item in row["evidence"])):
            print(f"[FAIL] {name}: 记录字段不符合当前记忆协议")
            return False
    print(f"[PASS] {name}: 数据库读取成功，返回 {len(data)} 条记录")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读验证 memory.conf 中的 Supabase endpoint 和记忆 RPC")
    parser.add_argument("--conf-dir", type=Path, default=Path(__file__).resolve().parent,
                        help="memory.conf / .env 所在目录，默认脚本目录")
    parser.add_argument("--source", default="facts", help="记忆来源名，默认 facts")
    parser.add_argument("--timeout", type=float, help="单次 HTTP 超时秒数，默认使用来源配置")
    args = parser.parse_args(argv)
    try:
        conf_dir = args.conf_dir.resolve()
        load_dotenv(conf_dir / ".env", override=False, encoding="utf-8")
        with (conf_dir / "memory.conf").open("rb") as file:
            source = tomllib.load(file)["sources"][args.source]
        if not isinstance(source, dict):
            raise ConfigError("所选来源必须是配置表")
        if source.get("store") != "supabase" or source.get("kind") != "episodic":
            raise ConfigError("所选来源必须是 supabase / episodic")
        if not isinstance(source["endpoint"], str):
            raise ConfigError("endpoint 必须是字符串")
        endpoint = source["endpoint"].rstrip("/")
        url = urlsplit(endpoint)
        if (url.scheme not in {"http", "https"} or not url.hostname
                or url.port == 0 or url.username or url.password or url.query or url.fragment):
            raise ConfigError("endpoint 必须是有效的 HTTP(S) REST 地址")
        namespace = source["namespace"]
        if not isinstance(namespace, str) or not namespace.strip():
            raise ConfigError("namespace 不能为空")
        timeout = args.timeout if args.timeout is not None else source.get("timeout", 20)
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ConfigError("timeout 必须为有限正数")
        token_env = source.get("credential_env", "SUPABASE_MEMORY_TOKEN")
        key_env = source.get("api_key_env", token_env)
        token, api_key = os.environ.get(token_env, ""), os.environ.get(key_env, "")
        if not token or not api_key:
            raise ConfigError(f"凭据缺失，请在进程环境或配置目录 .env 中设置 {token_env}、{key_env}")
        embedding = source["embedding"]
        profile = {"model": embedding["model"], "dimensions": embedding["dimensions"],
                   "query_prefix": embedding.get("query_prefix", ""),
                   "document_prefix": embedding.get("document_prefix", "")}
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"[ERROR] 本地配置错误：{type(error).__name__}；请检查配置文件、来源名、超时和凭据。")
        # Only our explicit validation messages are displayed, never TOML source text.
        if isinstance(error, ConfigError):
            print(f"  {error}")
        return 2

    secrets = (token, api_key)
    print(redact(f"endpoint: {endpoint}\nsource: {args.source}\nnamespace: {namespace}", secrets))
    headers = {"apikey": api_key, "Accept": "application/json"}
    if not token.startswith(("sb_secret_", "sb_publishable_")):
        headers["Authorization"] = "Bearer " + token
    filters = {"p_subject": None, "p_origin": None, "p_event_from": None, "p_event_to": None}
    base = {"p_namespace": namespace, "p_source_id": args.source, **filters}
    with httpx.Client(base_url=endpoint + "/", headers=headers, timeout=timeout,
                      trust_env=False, follow_redirects=False) as client:
        try:
            response = client.get("")
            print(f"[HTTP] REST 入口: {response.status_code}，已收到服务响应；最终可用性以 RPC 读取为准")
        except httpx.RequestError as error:
            print(f"[FAIL] REST 入口: {type(error).__name__}: {redact(error, secrets)}")
        listed = check_rpc(client, "list", {**base, "p_type": None, "p_limit": 1, "p_after": 0}, secrets)
        searched = check_rpc(client, "search", {**base, "p_types": ["note", "episode"], "p_limit": 1,
                             "p_include_deleted": False, "p_profile": profile, "p_vector": None,
                             "p_threshold": source.get("similarity_threshold", 0.0)}, secrets)
    if listed and searched:
        print("[PASS] 当前 endpoint 的记忆 RPC 可读取数据库；空列表也代表成功。")
        print("检查范围：list/search 读取协议；未验证 embedding、向量检索或写入能力。")
        return 0
    print("[FAIL] 当前 endpoint 未通过记忆 RPC 可用性检查，请查看上方具体错误。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
