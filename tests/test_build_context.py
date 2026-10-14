from scripts.build_context import source_files


def test_custom_context_excludes_secrets_and_generated_files(tmp_path):
    backend = tmp_path / "backend"
    backend.mkdir()
    names = [
        "main.py",
        ".env",
        ".env.local",
        ".env.example",
        "client.key",
        "ca.pem",
        "credentials.json",
        "state.db",
    ]
    for name in names:
        (backend / name).write_text("source or private value", encoding="utf-8")
    for name in ["__pycache__", ".secrets", "node_modules", ".runtime"]:
        (backend / name).mkdir()
        (backend / name / "private.py").write_text("private", encoding="utf-8")
    assert [path.name for path in source_files(tmp_path, backend)] == ["main.py"]
