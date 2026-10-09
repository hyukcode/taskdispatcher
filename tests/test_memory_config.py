import json

import pytest

from tasker.config import (
    Config,
    load_config,
)


def test_memory_config_long_term_path():
    cfg = Config()

    path = (
        cfg.memory
        .long_term_path
    )

    assert path.is_absolute()


def test_memory_and_artifact_config_can_be_loaded(
    tmp_path,
):
    long_term_dir = (
        tmp_path
        / "memory"
    )

    config_file = (
        tmp_path
        / "config.json"
    )

    config_file.write_text(
        json.dumps(
            {
                "memory": {
                    "enabled": True,

                    "session_limit": 3,

                    "long_term_limit": 2,

                    "task_result_max_chars":
                        1200,

                    "context_max_chars":
                        3000,

                    "long_term_enabled":
                        True,

                    "long_term_dir":
                        str(
                            long_term_dir
                        ),

                    "max_task_results":
                        10,

                    "max_notes":
                        6,

                    "artifact_gc_grace_seconds":
                        100,
                },

                "artifacts": {
                    "enabled": True,

                    "threshold_chars":
                        2000,

                    "preview_chars":
                        300,
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config(
        config_file
    )

    assert (
        cfg.memory.session_limit
        == 3
    )

    assert (
        cfg.memory.long_term_limit
        == 2
    )

    assert (
        cfg.memory.task_result_max_chars
        == 1200
    )

    assert (
        cfg.memory.context_max_chars
        == 3000
    )

    assert (
        cfg.memory.long_term_enabled
        is True
    )

    assert (
        cfg.memory.long_term_path
        == long_term_dir
    )

    assert (
        cfg.memory.max_task_results
        == 10
    )

    assert (
        cfg.memory.max_notes
        == 6
    )

    assert (
        cfg.artifacts.threshold_chars
        == 2000
    )

    assert (
        cfg.artifacts.preview_chars
        == 300
    )


def test_artifact_preview_must_be_smaller_than_threshold():
    cfg = Config()

    cfg.artifacts.threshold_chars = 500

    cfg.artifacts.preview_chars = 500

    with pytest.raises(
        ValueError
    ):
        cfg.validate()


def test_memory_context_budget_has_minimum():
    cfg = Config()

    cfg.memory.context_max_chars = 100

    with pytest.raises(
        ValueError
    ):
        cfg.validate()


def test_negative_memory_limits_rejected():
    cfg = Config()

    cfg.memory.session_limit = -1

    with pytest.raises(
        ValueError
    ):
        cfg.validate()