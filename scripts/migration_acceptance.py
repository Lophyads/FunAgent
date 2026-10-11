"""Generate per-ID acceptance evidence from completed JUnit reports."""
import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET


# Select observable contracts, not a minimum suite size or a directory whitelist.
CONTRACTS = {
    "M01": ("独立 need_memory/need_knowledge", "test_memory_prepare::test_need_combinations"),
    "M02": ("真实问题/六组有界历史/授权描述", "test_memory_prepare::test_planning_receives_six_bounded_groups_question_and_authorized_descriptions"),
    "M03": ("事实类型/来源与画像独立", "test_memory_prepare::test_need_combinations", "test_memory_prepare::test_closed_and_unauthorized_branches_do_not_plan_or_query"),
    "M04": ("规划降级与知识去重限次", "test_memory_prepare::test_invalid_plan_falls_back_to_original_with_safe_warning", "test_memory_prepare::test_knowledge_query_variant_original_deduplicated_and_limited"),
    "M05": ("真实 SQLite OR/FTS/CJK/排序/CAS", "test_sqlite_memory_search"),
    "M06": ("截断保引用/三类独立预算", "test_memory_prepare::test_long_evidence_clipped_with_reference_and_independent_budgets", "test_memory_prepare::test_profile_fact_knowledge_budgets_are_independent"),
    "M07": ("状态和安全 warning 进入主请求", "test_memory_flow::test_formal_main_request_receives_source_state_even_without_records", "test_memory_prepare::test_source_child_timeout_keeps_other_source_results"),
    "M08": ("主模型五 action/批准/CAS/可信证据", "test_memory_actions"),
    "M09": ("元数据/过滤/分页/Patch 保留与清空", "test_sqlite_memory_search::test_metadata_roundtrip_filter_pages_and_patch_clear", "test_memory_actions::test_main_metadata_add_filter_and_read_roundtrip", "test_memory_backend_contract::test_backend_unicode_metadata_filters_and_stable_pagination", "test_supabase_integration::test_metadata_patch_preserves_omitted_fields_and_can_clear_explicit_null"),
    "M10": ("首次画像冻结/新会话最新/显式最新", "test_memory_prompt::test_latest_explicit_profile_and_new_session_do_not_rewrite_frozen_prompt", "test_memory_prompt::test_concurrent_profile_initialization_returns_same_sqlite_winner", "test_memory_prompt::test_failed_profile_initialization_freezes_base_once"),
    "M11": ("最后 user 副本注入/持久历史隔离", "test_memory_prompt::test_attach_recall_changes_only_last_real_user_copy", "test_memory_flow::test_one_prepare_per_turn_frozen_profile_and_recall_never_enter_raw_history", "test_tdd12_integration"),
    "S01": ("SQLite/Supabase Store 原子合同", "test_memory_backend_contract", "test_supabase_integration"),
    "S02": ("Embedding 前缀/批次/profile/控制", "test_embedding", "test_supabase_integration::test_real_vector_ranking_namespace_profile_and_dimension_filter"),
    "S03": ("打包 SQL/RPC/权限/干净部署", "test_release::test_supabase_positive_deployment_asset_and_optional_dependencies", "test_distribution", "test_supabase_integration::test_rpc_namespace_claim_and_least_privilege_are_real"),
    "S04": ("固定知识 MCP/引用/真实协议", "test_mcp_backend", "test_tdd12_integration"),
    "S05": ("正式 Start 可选主存储", "test_tdd02_startup::test_selected_persistence_factory_is_used"),
    "T01": ("真实 MCP/终端/网络/AX 后端生命周期", "test_mcp_backend", "test_process_backend", "test_web_backend", "test_computer_backend", "test_tool_backends"),
    "T02": ("工具选择交集/action 单次批准", "test_tool_policy"),
    "T03": ("自动发现/候选原子发布/快照", "test_tool_discovery"),
    "T04": ("13 原工具与动态 MCP 组合", "test_builtin_io", "test_tdd09_integration", "test_clarify", "test_memory_actions"),
    "R01": ("N/0 触发/持久去重/重启/pending", "test_review_trigger"),
    "R02": ("后台只准 list/search/add", "test_memory_actions::test_review_exposes_only_list_search_add", "test_memory_actions::test_review_forged_mutation_denied_even_without_schema", "test_review::test_review_five_actions_only_reads_and_adds"),
    "R03": ("无事实推进/失败不推进/写入幂等", "test_review::test_no_new_fact_and_successful_read_only_operations_advance", "test_review::test_plain_final_is_not_a_confirmed_memory_write", "test_review::test_unknown_write_effect_does_not_advance_even_with_ok_status", "test_review::test_partial_mutations_never_advance_but_preserve_success", "test_review::test_retry_changed_call_id_same_evidence_and_content_is_idempotent"),
    "R04": ("两条跨水位锚点/新证据隔离", "test_review::test_review_resolves_references_with_two_separate_anchors_and_only_new_evidence", "test_review::test_review_anchors_cannot_be_used_as_new_fact_evidence", "test_review::test_review_anchors_are_latest_two_nonempty_texts_inside_successful_watermark"),
    "A01": ("错误边界/降级重新 Encode", "test_provider_contract::test_real_sdk_failure_reencodes_fallback_profile_and_preserves_tool_pair", "test_provider_contract::test_only_model_or_endpoint_404_can_fallback"),
    "A02": ("有界退避/jitter/Retry-After/控制", "test_model_retry"),
    "A03": ("持久幂等/真实 HTTP/发布补偿", "test_submission"),
    "A04": ("实际日志输出/上下文/脱敏", "test_tdd02_startup::test_log_configuration_changes_actual_output", "test_tdd02_startup::test_task_logs_include_real_context_and_safe_failure"),
    "A05": ("Web 管理确认/授权/安全诊断", "test_memory_operations", "test_operations_cli::test_diagnostics_http_cli_whitelist_and_queries_do_not_run_models", "test_operations_cli::test_diagnostics_live_backend_availability_effective_config_and_secret_projection"),
    "A06": ("CLI logs/doctor/export", "test_operations_cli"),
    "A07": ("参数可纠错/身份损坏终止", "test_provider_contract::test_parameter_error_is_paired_persisted_and_corrected_without_replaying_raw", "test_provider_contract::test_real_sdk_damaged_batch_terminates_before_any_handler"),
    "D01": ("真实 SDK 流/SSE/reset/取消/drain", "test_model_streaming", "test_http_sse", "test_tdd12_integration"),
    "D02": ("同 Provider IO 并行/借用关闭", "test_provider_concurrency", "test_model_streaming::test_stream_borrows_overlap_review_and_close_waits_for_actual_io"),
    "D04": ("配置旁 .env/进程优先/生命周期", "test_tdd02_startup::test_dotenv_comes_from_config_dir_before_driver_construction", "test_tdd02_startup::test_explicit_environment_wins_and_missing_dotenv_is_allowed", "test_tdd12_integration"),
    "F01": ("接口与实现分离", "test_tdd01_contracts::test_public_import_does_not_load_optional_drivers", "test_tdd01_contracts::test_business_modules_do_not_import_driver_implementations"),
    "F02": ("窄接口与最小 fake 替换", "test_tdd01_contracts::test_runtime_can_run_with_minimal_tool_dispatch_and_journal", "test_tdd01_contracts::test_main_queue_uses_bound_executor"),
    "F03": ("域内构造/冻结配置/回滚", "test_tdd02_startup"),
    "F04": ("工具显式钩子自动注册", "test_tool_discovery"),
    "F05": ("唯一 Run/启动绑定 executor", "test_tdd01_contracts::test_main_recall_review_use_one_bound_runner", "test_tdd01_contracts::test_review_queue_uses_bound_executor"),
    "F06": ("可信 context 仅数据与控制", "test_tdd01_contracts::test_context_is_data_and_control_only"),
    "F07": ("启动失败回滚/实际 IO 归还后关闭", "test_release::test_failed_worker_drain_retains_resources_until_retry", "test_release::test_http_close_failure_retains_application_resources_for_retry", "test_tool_backends::test_stop_failure_retains_backend_until_actual_io_returns", "test_tdd12_integration"),
    "F08": ("真实装包/资源/锁文件/文档一致", "test_distribution", "test_release"),
}

LIMITATIONS = {
    "S01": "只部署并验收隔离官方容器；用户远程项目未部署。",
    "S02": "模型/维度/前缀升级需显式维护重建。",
    "S03": "只支持新 schema；部署资产要求干净目标库。",
    "S04": "示例知识 MCP 为受控协议服务；用户知识库内容另行接入。",
    "T01": "AX 操作需配置 HTTP 服务；未安装本机桌面驱动。",
    "T04": "不包含被排除的浏览器专用九工具；终端/可变桌面 effect 可为 unknown。",
    "D01": "事件仅当前进程有界展示；持久最终状态以数据库为准。",
    "D02": "主任务仍一个 worker，D03 未裁决。",
    "F08": "提供本地构建产物；未上传发行平台。",
}


def report(path):
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    if not cases or any(case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")):
        raise ValueError(f"Not a complete successful report: {path}")
    return {case.attrib["classname"].split(".")[-1] + "::" + case.attrib["name"] for case in cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, default=Path("docs/tdd12-evidence"))
    args = parser.parse_args()
    evidence = args.evidence_dir.resolve()
    root = Path(__file__).resolve().parents[1]
    reports = {name: report(evidence / file) for name, file in {
        "offline": "offline-suite-final.xml", "wheel": "wheel-tdd12-contracts.xml",
        "sdist": "sdist-tdd12-contracts.xml", "supabase": "supabase/supabase-suite.xml"}.items()}
    baseline = report(evidence / "baseline.xml")
    if baseline - reports["offline"]:
        raise ValueError("Previous valid nodes disappeared")
    ids = {}
    for identity, (behavior, *selectors) in CONTRACTS.items():
        results = {}
        for name, nodes in reports.items():
            selected = sorted(node for node in nodes if any(
                node.split("::")[0] == selector if "::" not in selector else
                node == selector or node.startswith(selector + "[") for selector in selectors))
            if selected:
                results[name] = {"passed": len(selected), "nodes": selected}
        for selector in selectors:
            if not any(node.split("::")[0] == selector if "::" not in selector else
                       node == selector or node.startswith(selector + "[") for nodes in reports.values() for node in nodes):
                raise ValueError(f"Missing actual evidence for {identity}: {selector}")
        ids[identity] = {"status": "验收通过", "behavior": behavior, "selectors": selectors, "results": results,
                         "limitation": LIMITATIONS.get(identity, "旧 API/config/schema 不兼容；本轮排除项不恢复。")}
    summary = {"phase": "TDD12", "date": "2026-10-11", "timezone": "Asia/Shanghai",
               "ids": ids, "migration_ids": 34, "architecture_ids": 8,
               "baseline": len(baseline), "node_comparison": {"retained": len(baseline),
               "added": sorted(reports["offline"] - baseline), "missing": []},
               "reports": {name: {"passed": len(nodes), "failed": 0, "errors": 0, "skipped": 0} for name, nodes in reports.items()},
               "pending": {"D03": "待裁决：一个 main worker。", "D05": "待裁决：重启只修复状态，不自动重放。"},
               "commands": {"offline": 'uv run --no-sync pytest tests_refactor -o addopts="-m \'not supabase_integration\'" -q --junitxml=docs/tdd12-evidence/offline-suite-final.xml',
                            "installed": "uv run --no-sync python scripts/release_smoke.py --wheel dist/funagent-0.2.0-py3-none-any.whl --sdist dist/funagent-0.2.0.tar.gz --offline --tdd12-contracts --contract-artifact-dir docs/tdd12-evidence",
                            "supabase": "uv run --no-sync python scripts/supabase_contract_smoke.py --evidence-dir docs/tdd12-evidence/supabase"}}
    (evidence / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    lines = ["# 迁移逐项实际验收", "", "日期：2026-10-11（Asia/Shanghai）。此表由 `scripts/migration_acceptance.py` 根据实际 JUnit 节点生成；缺报告、失败、skip 或指定行为缺节点都会阻止生成通过结果。完整节点、实际命令与剩余限制见 [机器可读证据](tdd12-evidence/summary.json)。原始证据保留在源码工作区，不随发行包提供。", "",
             "离线命令执行全部业务合同并明确排除 12 项外部集成；Supabase 单独运行隔离后端 job。wheel/sdist 直接安装到各自 venv，从无关 cwd 验证入口和资源，再执行完整离线行为合同；13 项构建/示例导出验收在源码 job 中执行。", "",
             "| ID | 状态与验收行为 | 实际测试入口 | 本次实际结果 | 剩余限制 |", "| --- | --- | --- | --- | --- |"]
    for identity, value in ids.items():
        selectors = "<br>".join("`" + selector + "`" for selector in value["selectors"])
        counts = "；".join(f"{name}: {result['passed']} passed" for name, result in value["results"].items())
        lines.append(f"| {identity} | 验收通过：{value['behavior']} | {selectors} | {counts} | {value['limitation']} |")
    lines += ["", "| 待裁决 ID | 当前边界 |", "| --- | --- |", "| D03 | 一个 main worker；没有跨会话多 worker 调度。 |", "| D05 | 启动修复 interrupted/unknown，不自动重放未知副作用。 |", "", "排除项仍为浏览器专用九工具、长期授权存储、Lease/多进程接管、渠道投递和旧 API/config/schema/legacy import。真实模型供应商与用户远程项目的线上可用性不是本次离线/隔离后端验收结果。"]
    (root / "docs/migration-acceptance.md").write_text("\n".join(lines) + "\n", encoding="utf8")
    print(f"34 migration IDs and 8 architecture IDs: actual evidence complete; {len(reports['offline'])} offline nodes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
