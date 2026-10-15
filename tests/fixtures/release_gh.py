"""Stateful GitHub CLI double; Git publication still uses a real bare repository."""

import base64
import json
import os
import sys
from pathlib import Path


def main() -> None:
    path = Path(os.environ["CORP_TEST_GH_STATE"])
    state = json.loads(path.read_text())
    args = sys.argv[1:]
    state["calls"].append(args)
    path.write_text(json.dumps(state))

    def save() -> None:
        path.write_text(json.dumps(state))

    def fail(action: str) -> None:
        if os.environ.get("CORP_TEST_GH_FAIL") == action:
            sys.exit(f"Simulated interrupted {action}")

    if args[0] == "api":
        assert args[:4] == [
            "api",
            "--paginate",
            "repos/example/minishop-corp/releases?per_page=100",
            "--jq",
        ]
        fail("api")
        record = state["release"]
        if record is not None:
            record = {**record, "assets": [{"name": name} for name in state["files"]]}
        if record:
            assert args[4] == f'.[] | select(.tag_name == "{record["tag_name"]}")'
            print(json.dumps(record))
        return

    assert args[0] == "release"
    action, tag = args[1:3]
    options = args[3:]
    repo_index = options.index("--repo")
    assert options[repo_index + 1] == "example/minishop-corp"
    del options[repo_index : repo_index + 2]
    if action == "create":
        assert state["release"] is None
        assert "--draft" in options and "--verify-tag" in options
        notes = Path(options[options.index("--notes-file") + 1]).read_text()
        state["release"] = {"tag_name": tag, "draft": True, "prerelease": False, "body": notes}
        save()
        fail("create")
    elif action == "upload":
        assert state["release"]["draft"] is True
        for name in options:
            asset = Path(name)
            assert asset.name not in state["files"]
            state["files"][asset.name] = base64.b64encode(asset.read_bytes()).decode()
            save()
            fail("upload")
    elif action == "download":
        assert options[0] == "--pattern" and options[2:] == ["--output", "-"]
        sys.stdout.buffer.write(base64.b64decode(state["files"][options[1]]))
    elif action == "edit":
        assert options == ["--draft=false", "--latest"]
        state["release"]["draft"] = False
        save()
        fail("edit")
    else:
        raise AssertionError(args)


if __name__ == "__main__":
    main()
