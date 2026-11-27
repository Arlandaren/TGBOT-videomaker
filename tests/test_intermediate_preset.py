"""Пресет x264 промежуточных файлов — настройка машины, а не константа в коде."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.core import proc
from src.core.config import Settings, settings

ROOT = Path(__file__).resolve().parents[1]


def cfg(**kw) -> Settings:
    return Settings(_env_file=None, bot_token="x", **kw)


def test_the_default_is_unchanged():
    assert cfg().intermediate_preset == "veryfast"


def test_the_stages_use_what_the_machine_is_set_to():
    from src.pipeline import crop, cut, fps, split

    assert proc.INTERMEDIATE_PRESET == settings.intermediate_preset
    for module in (crop, cut, fps, split):
        assert module.INTERMEDIATE_PRESET == proc.INTERMEDIATE_PRESET
    assert fps.X264[fps.X264.index("-preset") + 1] == proc.INTERMEDIATE_PRESET


def test_a_typo_is_refused_at_start_not_in_the_middle_of_a_job():
    # Опечатка в пресете иначе всплыла бы отказом ffmpeg на нарезке — после
    # распознавания, за которое человек уже подождал.
    with pytest.raises(ValidationError):
        cfg(intermediate_preset="ultrafats")


def test_the_environment_reaches_the_encoder_arguments(tmp_path):
    """Новый процесс с INTERMEDIATE_PRESET=ultrafast кодирует нарезку им."""
    code = (
        "import inspect\n"
        "from src.pipeline import cut, fps\n"
        "print(inspect.signature(cut.apply_cuts).parameters['preset_speed'].default,"
        " fps.X264[fps.X264.index('-preset') + 1])\n"
    )
    env = {**os.environ, "INTERMEDIATE_PRESET": "ultrafast", "BOT_TOKEN": "x", "PYTHONUTF8": "1"}
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert out == ["ultrafast", "ultrafast"]
