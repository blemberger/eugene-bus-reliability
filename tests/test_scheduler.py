"""The scheduler's choice between an incremental build and a full refresh."""

import json

from eugene_bus_reliability import scheduler


def make_project(root):
    (root / "models").mkdir()
    (root / "macros").mkdir()
    (root / "tests").mkdir()
    (root / "dbt_project.yml").write_text("name: x\n")
    (root / "models" / "a.sql").write_text("select 1")


def test_code_version_changes_only_with_the_analysis_code(tmp_path, monkeypatch):
    make_project(tmp_path)
    monkeypatch.setattr(scheduler, "DBT_DIR", tmp_path)
    v1 = scheduler.analysis_code_version()
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "run_results.json").write_text("{}")  # build output: not code
    assert scheduler.analysis_code_version() == v1
    (tmp_path / "models" / "a.sql").write_text("select 2")
    assert scheduler.analysis_code_version() != v1


def test_models_built_ok_ignores_failing_tests(tmp_path, monkeypatch):
    make_project(tmp_path)
    monkeypatch.setattr(scheduler, "DBT_DIR", tmp_path)
    assert not scheduler.models_built_ok()  # no results yet
    (tmp_path / "target").mkdir()
    results = tmp_path / "target" / "run_results.json"
    results.write_text(
        json.dumps(
            {
                "results": [
                    {"unique_id": "model.p.a", "status": "success"},
                    {"unique_id": "test.p.t", "status": "fail"},
                ]
            }
        )
    )
    assert scheduler.models_built_ok()
    results.write_text(json.dumps({"results": [{"unique_id": "model.p.a", "status": "error"}]}))
    assert not scheduler.models_built_ok()
