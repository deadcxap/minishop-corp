"""Build a local fixture; never trust this disposable publisher in production."""

from package_support import ROOT, archive, create_key, load_key, manifest, payload, public_key

OUTPUT = ROOT / ".local/package"


def main() -> None:
    key_path = OUTPUT / "development.key"
    if not key_path.exists():
        create_key(key_path)
    key = load_key(key_path)
    encoded, _ = public_key(key)
    (OUTPUT / "development.pub").write_text(encoded, encoding="ascii")
    body = archive(key, manifest("minishop-corp-local-dev"), payload())
    from bot.plugins.packages import inspect_archive

    candidate = inspect_archive(ROOT / ".local/package-validation", body)
    assert candidate.manifest["id"] == "minishop-corp"
    archive_path = OUTPUT / "minishop-corp-dev.zip"
    archive_path.write_bytes(body)
    print(f"Validated development package: {archive_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
