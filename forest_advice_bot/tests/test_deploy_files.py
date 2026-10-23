"""Проверки файлов развёртывания. Docker здесь не запускается: проверяем структуру,
чтобы случайная правка не сломала главное требование («запуск одной командой»)."""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")  # PyYAML нужен только для этих тестов

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_compose_has_db_and_bot_services(compose):
    assert set(compose["services"]) == {"db", "bot"}


def test_bot_waits_for_healthy_database(compose):
    depends_on = compose["services"]["bot"]["depends_on"]
    assert depends_on["db"]["condition"] == "service_healthy"


def test_database_port_is_not_published(compose):
    assert "ports" not in compose["services"]["db"]


def test_database_data_survives_restarts(compose):
    assert "pgdata" in compose["volumes"]
    assert any("pgdata" in v for v in compose["services"]["db"]["volumes"])


def test_bot_talks_to_database_by_service_name(compose):
    assert compose["services"]["bot"]["environment"]["POSTGRES_HOST"] == "db"


def test_secrets_come_from_env_file_not_from_compose(compose):
    assert compose["services"]["bot"]["env_file"] == ".env"
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "sk-or-" not in text and "TELEGRAM_BOT_TOKEN=" not in text


def test_dockerfile_installs_requirements_before_copying_code():
    lines = (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
    install = next(i for i, line in enumerate(lines) if "pip install" in line)
    copy_code = next(i for i, line in enumerate(lines) if line.startswith("COPY --chown"))
    assert install < copy_code  # иначе правка кода каждый раз переустанавливала бы зависимости


def test_dockerfile_does_not_run_as_root():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "\nUSER app" in text


def test_env_files_are_excluded_from_image_and_git():
    for name in (".gitignore", ".dockerignore"):
        assert ".env" in (ROOT / name).read_text(encoding="utf-8").splitlines()


def test_archive_is_mounted_read_only(compose):
    mounts = compose["services"]["bot"]["volumes"]
    assert any(m.get("type") == "bind" and m.get("read_only") is True for m in mounts)


def test_dockerfile_bakes_model_and_runs_offline():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "HF_HUB_OFFLINE=1" in text
    assert "SentenceTransformer(" in text  # модель скачивается при сборке
    assert "whl/cpu" in text  # CPU-версия torch


def test_compose_passes_embedding_model_as_build_arg(compose):
    args = compose["services"]["bot"]["build"]["args"]

    assert "EMBEDDING_MODEL" in args


def test_gitignore_hides_only_the_root_archive_not_samples():
    """Запись «archive/» без слэша в начале скрыла бы и samples/archive/ с демо-документами."""
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "/archive/" in lines
    assert "archive/" not in lines


def test_git_really_keeps_samples_archive(tmp_path):
    import shutil
    import subprocess

    if shutil.which("git") is None:
        return
    shutil.copy(ROOT / ".gitignore", tmp_path / ".gitignore")
    (tmp_path / "samples" / "archive").mkdir(parents=True)
    (tmp_path / "samples" / "archive" / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "secret.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    def ignored(path: str) -> bool:
        result = subprocess.run(["git", "check-ignore", "-q", path], cwd=tmp_path, check=False)
        return result.returncode == 0

    assert ignored("archive/secret.txt") is True
    assert ignored("samples/archive/a.txt") is False
