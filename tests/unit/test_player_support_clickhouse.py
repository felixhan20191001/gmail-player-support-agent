import pytest

from player_support_agent.tools.clickhouse_tools import ClickHouseTools, _clean_value
from player_support_agent.tools.config import (
    ClickHouseConfig,
    ClickHouseEvidenceRecipeConfig,
    ClickHouseTableConfig,
)


def _tools() -> ClickHouseTools:
    return ClickHouseTools(
        ClickHouseConfig(
            allowed_schema={
                "numbercrush": ClickHouseTableConfig(
                    columns=["event_time", "user_id", "event_name", "amount"],
                    player_id_columns=["user_id"],
                    time_column="event_time",
                ),
                "blackhole_payments": ClickHouseTableConfig(
                    columns=["event_time", "user_id", "event_name", "amount"],
                    player_id_columns=["user_id"],
                    time_column="event_time",
                ),
            },
            case_type_tables={"payment": ["numbercrush"]},
            project_case_type_tables={
                "BlackHole": {"payment": ["blackhole_payments"]},
            },
            max_rows=20,
            max_time_window_hours=72,
        )
    )


def _valid_sql() -> str:
    return (
        "SELECT event_time, user_id, event_name FROM numbercrush "
        "WHERE user_id = 'u1' "
        "AND event_time >= '2026-05-01T00:00:00+00:00' "
        "AND event_time < '2026-05-02T00:00:00+00:00' "
        "LIMIT 20"
    )


def test_clean_value_strips_clickhouse_fixed_string_null_padding():
    raw = {
        "user_id": "abc\x00\x00",
        "events": [{"event_name": "Purchase\x00"}],
    }

    assert _clean_value(raw) == {
        "user_id": "abc",
        "events": [{"event_name": "Purchase"}],
    }


@pytest.mark.parametrize(
    "keyword",
    ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE"],
)
def test_validator_rejects_write_or_admin_sql(keyword):
    result = _tools().validate_clickhouse_sql(
        sql=f"{keyword} TABLE numbercrush",
        player_id="u1",
        time_window_start="2026-05-01T00:00:00+00:00",
        time_window_end="2026-05-02T00:00:00+00:00",
        case_type="payment",
    )

    assert result["ok"] is False


def test_validator_requires_limit():
    sql = _valid_sql().replace(" LIMIT 20", "")

    result = _tools().validate_clickhouse_sql(
        sql=sql,
        player_id="u1",
        time_window_start="2026-05-01T00:00:00+00:00",
        time_window_end="2026-05-02T00:00:00+00:00",
        case_type="payment",
    )

    assert result["ok"] is False
    assert "LIMIT" in result["reason"]


def test_validator_requires_player_filter():
    sql = _valid_sql().replace("user_id = 'u1' AND ", "")

    result = _tools().validate_clickhouse_sql(
        sql=sql,
        player_id="u1",
        time_window_start="2026-05-01T00:00:00+00:00",
        time_window_end="2026-05-02T00:00:00+00:00",
        case_type="payment",
    )

    assert result["ok"] is False
    assert "player" in result["reason"]


def test_validator_requires_requested_time_window_dates():
    sql = _valid_sql().replace("2026-05-02", "2026-05-05")

    result = _tools().validate_clickhouse_sql(
        sql=sql,
        player_id="u1",
        time_window_start="2026-05-01T00:00:00+00:00",
        time_window_end="2026-05-02T00:00:00+00:00",
        case_type="payment",
    )

    assert result["ok"] is False
    assert "time window" in result["reason"]


def test_validator_rejects_unknown_table():
    sql = _valid_sql().replace("numbercrush", "other_table")

    result = _tools().validate_clickhouse_sql(
        sql=sql,
        player_id="u1",
        time_window_start="2026-05-01T00:00:00+00:00",
        time_window_end="2026-05-02T00:00:00+00:00",
        case_type="payment",
    )

    assert result["ok"] is False
    assert "non-whitelisted" in result["reason"]


def test_get_schema_uses_project_specific_tables():
    result = _tools().get_clickhouse_schema(case_type="payment", project="BlackHole")

    assert result["project"] == "BlackHole"
    assert result["allowed_tables"] == ["blackhole_payments"]


def test_validator_uses_project_specific_table_allowlist():
    result = _tools().validate_clickhouse_sql(
        sql=_valid_sql(),
        player_id="u1",
        time_window_start="2026-05-01T00:00:00+00:00",
        time_window_end="2026-05-02T00:00:00+00:00",
        case_type="payment",
        project="BlackHole",
    )

    assert result["ok"] is False
    assert "non-whitelisted" in result["reason"]


def test_project_without_table_mapping_does_not_fallback_to_global_table():
    schema = _tools().get_clickhouse_schema(case_type="payment", project="Water Sort")

    assert schema["allowed_tables"] == []

    result = _tools().validate_clickhouse_sql(
        sql=_valid_sql(),
        player_id="u1",
        time_window_start="2026-05-01T00:00:00+00:00",
        time_window_end="2026-05-02T00:00:00+00:00",
        case_type="payment",
        project="Water Sort",
    )

    assert result["ok"] is False
    assert "No allowed ClickHouse tables" in result["reason"]


def test_require_project_for_queries_blocks_projectless_global_fallback():
    tools = ClickHouseTools(
        ClickHouseConfig(
            allowed_schema={
                "numbercrush": ClickHouseTableConfig(
                    columns=["event_time", "user_id", "event_name"],
                    player_id_columns=["user_id"],
                    time_column="event_time",
                ),
            },
            case_type_tables={"payment": ["numbercrush"]},
            require_project_for_queries=True,
        )
    )

    result = tools.validate_clickhouse_sql(
        sql=_valid_sql(),
        player_id="u1",
        time_window_start="2026-05-01T00:00:00+00:00",
        time_window_end="2026-05-02T00:00:00+00:00",
        case_type="payment",
    )

    assert result["ok"] is False
    assert "No allowed ClickHouse tables" in result["reason"]


def test_evidence_catalog_returns_empty_when_no_recipe():
    result = _tools().get_support_evidence_catalog(
        project="BlackHole",
        case_type="payment",
    )

    assert result["available"] is False
    assert result["evidence_kinds"] == []
    assert "No configured evidence recipes" in result["reason"]
    assert result["skip_clickhouse_fallback"] is True
    assert "decide_support_action" in " ".join(result["next_steps"])
    assert "get_clickhouse_schema" in " ".join(result["next_steps"])


def test_ads_after_purchase_allows_clickhouse_schema():
    tools = ClickHouseTools(
        ClickHouseConfig(
            allowed_schema={
                "numbersum": ClickHouseTableConfig(
                    columns=["log_time", "user_id", "event_name", "product_id", "price", "pos"],
                    player_id_columns=["user_id"],
                    time_column="log_time",
                ),
            },
            project_case_type_tables={"Number Sum": {"*": ["numbersum"]}},
            require_project_for_queries=True,
        )
    )

    schema = tools.get_clickhouse_schema(
        case_type="ads_after_purchase",
        project="Number Sum",
    )

    assert schema.get("log_query_skipped") is not True
    assert schema["allowed_tables"] == ["numbersum"]


def test_ads_after_purchase_evidence_catalog_points_to_playbook():
    tools = ClickHouseTools(
        ClickHouseConfig(
            allowed_schema={
                "numbersum": ClickHouseTableConfig(
                    columns=["log_time", "user_id", "event_name", "product_id", "price", "pos"],
                    player_id_columns=["user_id"],
                    time_column="log_time",
                ),
            },
            project_case_type_tables={"Number Sum": {"*": ["numbersum"]}},
        )
    )

    result = tools.get_support_evidence_catalog(
        project="Number Sum",
        case_type="ads_after_purchase",
    )

    assert result["available"] is False
    assert result["skip_clickhouse_fallback"] is False
    assert "get_remove_ads_investigation_playbook" in " ".join(result["next_steps"])


def test_busfever_schema_includes_platform_table_routing():
    tools = ClickHouseTools(
        ClickHouseConfig(
            allowed_schema={
                "busfever": ClickHouseTableConfig(
                    columns=["log_time", "user_id", "event_name", "product_id", "pos"],
                    player_id_columns=["user_id"],
                    time_column="log_time",
                ),
                "carmania": ClickHouseTableConfig(
                    columns=["log_time", "user_id", "event_name", "product_id", "pos"],
                    player_id_columns=["user_id"],
                    time_column="log_time",
                ),
            },
            project_case_type_tables={"BusFever": {"*": ["busfever", "carmania"]}},
            project_platform_tables={
                "BusFever": {"ios": ["carmania"], "android": ["busfever"]},
            },
        )
    )

    schema = tools.get_clickhouse_schema(case_type="payment", project="BusFever")
    playbook = tools.get_remove_ads_investigation_playbook(
        project="BusFever",
        case_type="ads_after_purchase",
    )

    assert set(schema["allowed_tables"]) == {"busfever", "carmania"}
    assert schema["platform_table_routing"]["ios"]["tables"] == ["carmania"]
    assert schema["platform_table_routing"]["android"]["tables"] == ["busfever"]
    assert playbook["platform_table_routing"]["ios"]["tables"] == ["carmania"]
    assert "platform:iOS" in " ".join(playbook["next_steps"])


def test_coin_frenzy_playbook_resolves_blackhole_table():
    tools = ClickHouseTools(
        ClickHouseConfig(
            allowed_schema={
                "blackhole": ClickHouseTableConfig(
                    columns=["log_time", "user_id", "event_name", "product_id", "price", "pos"],
                    player_id_columns=["user_id"],
                    time_column="log_time",
                ),
            },
            project_case_type_tables={"BlackHole": {"*": ["blackhole"]}},
        )
    )

    result = tools.get_coin_frenzy_investigation_playbook(
        project="BlackHole",
        case_type="pass_purchase_misunderstanding",
    )

    assert result["available"] is True
    assert result["clickhouse_table"] == "blackhole"
    assert "coin.frenzy" in str(result["purchase_query_hints"])


def test_remove_ads_playbook_resolves_number_sum_table():
    tools = ClickHouseTools(
        ClickHouseConfig(
            allowed_schema={
                "numbersum": ClickHouseTableConfig(
                    columns=["log_time", "user_id", "event_name", "product_id", "price", "pos"],
                    player_id_columns=["user_id"],
                    time_column="log_time",
                ),
            },
            project_case_type_tables={"Number Sum": {"*": ["numbersum"]}},
        )
    )

    result = tools.get_remove_ads_investigation_playbook(
        project="Number Sum",
        case_type="ads_after_purchase",
    )

    assert result["available"] is True
    assert result["clickhouse_table"] == "numbersum"
    assert result["investigation_steps"]
    assert result["outcome_branches"]


def test_gameplay_misunderstanding_catalog_does_not_point_to_coin_frenzy():
    result = _tools().get_support_evidence_catalog(
        project="BlackHole",
        case_type="gameplay_misunderstanding",
    )

    assert result["available"] is False
    assert result["skip_clickhouse_fallback"] is True
    next_steps = " ".join(result["next_steps"])
    assert "get_coin_frenzy_investigation_playbook" not in next_steps
    assert "requires_logs=true" in next_steps


def test_feature_request_skips_clickhouse_catalog():
    result = _tools().get_support_evidence_catalog(
        project="BlackHole",
        case_type="feature_request",
    )

    assert result["available"] is False
    assert result["log_query_skipped"] is True
    assert result["skip_clickhouse_fallback"] is True
    next_steps = " ".join(result["next_steps"])
    assert "get_coin_frenzy_investigation_playbook" not in next_steps


def test_coin_frenzy_playbook_rejects_gameplay_misunderstanding():
    tools = ClickHouseTools(
        ClickHouseConfig(
            allowed_schema={
                "blackhole": ClickHouseTableConfig(
                    columns=["log_time", "user_id", "event_name", "product_id", "price"],
                    player_id_columns=["user_id"],
                    time_column="log_time",
                ),
            },
            project_case_type_tables={"BlackHole": {"*": ["blackhole"]}},
        )
    )

    result = tools.get_coin_frenzy_investigation_playbook(
        project="BlackHole",
        case_type="gameplay_misunderstanding",
    )

    assert result["available"] is False
    assert "pass_purchase_misunderstanding" in result["reason"]


def test_ad_issue_skips_clickhouse_schema_and_sql():
    tools = _tools()

    schema = tools.get_clickhouse_schema(case_type="ad_issue", project="BlackHole")
    validation = tools.validate_clickhouse_sql(
        sql=_valid_sql(),
        player_id="u1",
        time_window_start="2026-05-01T00:00:00+00:00",
        time_window_end="2026-05-02T00:00:00+00:00",
        case_type="ad_issue",
        project="BlackHole",
    )
    catalog = tools.get_support_evidence_catalog(
        project="BlackHole",
        case_type="ad_issue",
    )

    assert schema["log_query_skipped"] is True
    assert schema["allowed_tables"] == []
    assert "ad_issue" in schema["reason"]
    assert validation["ok"] is False
    assert validation["log_query_skipped"] is True
    assert catalog["available"] is False
    assert catalog["log_query_skipped"] is True
    next_steps = " ".join(catalog["next_steps"])
    assert "get_relevant_support_rules" in next_steps
    assert "rule_action" in next_steps


@pytest.mark.asyncio
async def test_ad_issue_query_support_evidence_is_skipped():
    result = await _tools().query_support_evidence(
        project="BlackHole",
        case_type="ad_issue",
        player_id="u1",
        time_window_start="2026-05-01T00:00:00+00:00",
        time_window_end="2026-05-02T00:00:00+00:00",
        evidence_kind="any",
    )

    assert result["status"] == "skipped"
    assert result["log_query_skipped"] is True


@pytest.mark.asyncio
async def test_query_support_evidence_fails_closed_without_project_mapping():
    tools = ClickHouseTools(
        ClickHouseConfig(
            allowed_schema={
                "numbercrush": ClickHouseTableConfig(
                    columns=["event_time", "user_id", "event_name"],
                    player_id_columns=["user_id"],
                    time_column="event_time",
                ),
            },
            require_project_for_queries=True,
            evidence_recipes=[
                ClickHouseEvidenceRecipeConfig(
                    id="purchase_success",
                    projects=["Water Sort"],
                    case_types=["payment"],
                    table="numbercrush",
                    select_columns=["event_time", "user_id", "event_name"],
                    event_names=["PaySuccess"],
                )
            ],
        )
    )

    result = await tools.query_support_evidence(
        project="Water Sort",
        case_type="payment",
        player_id="u1",
        time_window_start="2026-05-01T00:00:00+00:00",
        time_window_end="2026-05-02T00:00:00+00:00",
        evidence_kind="purchase_success",
    )

    assert result["available"] is False
    assert result["status"] == "unavailable"
    assert "No allowed ClickHouse tables" in result["reason"]


@pytest.mark.asyncio
async def test_query_support_evidence_generates_validated_sql(monkeypatch):
    tools = ClickHouseTools(
        ClickHouseConfig(
            allowed_schema={
                "numbercrush": ClickHouseTableConfig(
                    columns=["event_time", "user_id", "event_name", "product_id"],
                    player_id_columns=["user_id"],
                    time_column="event_time",
                ),
            },
            project_case_type_tables={"NumberCrush": {"payment": ["numbercrush"]}},
            evidence_recipes=[
                ClickHouseEvidenceRecipeConfig(
                    id="purchase_success",
                    projects=["NumberCrush"],
                    case_types=["payment"],
                    table="numbercrush",
                    select_columns=["event_time", "user_id", "event_name", "product_id"],
                    event_names=["PaySuccess"],
                    product_id_contains=["pass"],
                    limit=20,
                )
            ],
        )
    )
    captured: dict[str, str] = {}

    async def fake_query_clickhouse(**kwargs):
        captured.update(kwargs)
        return {
            "sql": kwargs["sql"],
            "row_count": 1,
            "summary": {"event_counts": {"PaySuccess": 1}},
        }

    monkeypatch.setattr(tools, "query_clickhouse", fake_query_clickhouse)

    result = await tools.query_support_evidence(
        project="NumberCrush",
        case_type="payment",
        player_id="u1",
        time_window_start="2026-05-01T00:00:00+00:00",
        time_window_end="2026-05-02T00:00:00+00:00",
        evidence_kind="purchase_success",
    )

    assert result["available"] is True
    assert result["status"] == "supported"
    assert "PaySuccess" in captured["sql"]
    assert "product_id LIKE '%pass%'" in captured["sql"]
    assert "LIMIT 20" in captured["sql"]


@pytest.mark.asyncio
async def test_query_clickhouse_returns_summary_not_raw_rows(monkeypatch):
    class FakeResponse:
        text = (
            '{"event_time":"2026-05-01T01:00:00Z","user_id":"u1","event_name":"Purchase"}\n'
            '{"event_time":"2026-05-01T02:00:00Z","user_id":"u1","event_name":"Purchase"}\n'
        )

        def raise_for_status(self):
            return None

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(
        "player_support_agent.tools.clickhouse_tools.httpx.AsyncClient",
        FakeAsyncClient,
    )

    result = await _tools().query_clickhouse(
        sql=_valid_sql(),
        player_id="u1",
        time_window_start="2026-05-01T00:00:00+00:00",
        time_window_end="2026-05-02T00:00:00+00:00",
        case_type="payment",
    )

    assert "rows" not in result
    assert result["row_count"] == 2
    assert result["summary"]["raw_rows_omitted"] is True
    assert result["summary"]["event_counts"] == {"Purchase": 2}


@pytest.mark.asyncio
async def test_compact_query_clickhouse_limits_sample_rows(monkeypatch):
    class FakeResponse:
        text = "\n".join(
            [
                (
                    '{"event_time":"2026-05-01T0%s:00:00Z",'
                    '"user_id":"u1","event_name":"Purchase","amount":%s}'
                )
                % (index, index)
                for index in range(1, 5)
            ]
        )

        def raise_for_status(self):
            return None

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(
        "player_support_agent.tools.clickhouse_tools.httpx.AsyncClient",
        FakeAsyncClient,
    )
    tools = ClickHouseTools(
        ClickHouseConfig(
            allowed_schema={
                "numbercrush": ClickHouseTableConfig(
                    columns=["event_time", "user_id", "event_name", "amount"],
                    player_id_columns=["user_id"],
                    time_column="event_time",
                ),
            },
            case_type_tables={"payment": ["numbercrush"]},
        ),
        compact_results=True,
    )

    result = await tools.query_clickhouse(
        sql=_valid_sql(),
        player_id="u1",
        time_window_start="2026-05-01T00:00:00+00:00",
        time_window_end="2026-05-02T00:00:00+00:00",
        case_type="payment",
    )

    assert result["row_count"] == 4
    assert result["summary"]["raw_rows_omitted"] is True
    assert len(result["summary"]["sample_rows"]) == 2
