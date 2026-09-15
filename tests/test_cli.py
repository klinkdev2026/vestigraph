import json
import subprocess
import sys
from pathlib import Path

from tests import gds_fixtures as fx
from vestigraph.cli import main
from vestigraph.storage.changes import read_jsonl
from vestigraph.storage.metadata import MetadataError

def output(capsys):
    return json.loads(capsys.readouterr().out)

def test_checkpoint_query_export_and_stats(tmp_path, capsys):
    repo = tmp_path / "data"; source = tmp_path / "layout.gds"
    source.write_bytes(b"simple CLI test bytes")
    assert main(["--repo", str(repo), "init"]) == 0
    assert output(capsys)["initialized"] is True
    assert main(["--repo", str(repo), "checkpoint", str(source), "--title", "初版"]) == 0
    saved = output(capsys); assert saved["title"] == "初版"
    assert main(["--repo", str(repo), "history", "--limit", "1"]) == 0
    assert output(capsys)[0]["id"] == saved["id"]
    assert main(["--repo", str(repo), "show", saved["id"]]) == 0
    assert output(capsys)["sha256"] == saved["sha256"]
    dest = tmp_path / "restored.gds"
    assert main(["--repo", str(repo), "export", saved["id"], str(dest)]) == 0
    assert output(capsys)["destination"] == str(dest)
    assert dest.read_bytes() == source.read_bytes()
    assert main(["--repo", str(repo), "stats"]) == 0
    assert output(capsys)["checkpoints"] == 1

def test_segments_and_events(tmp_path, capsys):
    repo = tmp_path / "data"
    assert main(["--repo", str(repo), "init"]) == 0; output(capsys)
    assert main(["--repo", str(repo), "segment-start", "布线调整"]) == 0
    segment = output(capsys)
    assert main(["--repo", str(repo), "segments", "--limit", "1"]) == 0
    assert output(capsys)[0]["id"] == segment["id"]
    assert main(["--repo", str(repo), "events", "--segment", segment["id"]]) == 0
    assert isinstance(output(capsys), list)
    assert main(["--repo", str(repo), "segment-end", segment["id"]]) == 0
    assert output(capsys)["status"] == "closed"

def test_uninitialized_repo_is_concise_failure(tmp_path, capsys):
    assert main(["--repo", str(tmp_path / "missing"), "stats"]) != 0
    captured = capsys.readouterr()
    assert not captured.out and "vestigraph:" in captured.err and "Traceback" not in captured.err

def test_changes_and_changes_export(tmp_path, capsys):
    repo = tmp_path / "data"; source = tmp_path / "layout.gds"
    n = 50
    assert main(["--repo", str(repo), "init"]) == 0; output(capsys)
    source.write_bytes(fx.sample_many_cells(n, stamp=fx.timestamps(second=1)))
    assert main(["--repo", str(repo), "checkpoint", str(source), "--title", "初版"]) == 0
    first = output(capsys)
    source.write_bytes(fx.sample_many_cells_edit(n, "modify", stamp=fx.timestamps(second=2)))
    assert main(["--repo", str(repo), "checkpoint", str(source), "--title", "改版"]) == 0
    second = output(capsys)

    # baseline: the first version has no recorded change entries yet.
    assert main(["--repo", str(repo), "changes", first["id"]]) == 0
    baseline = output(capsys)
    assert baseline["status"] == "baseline" and baseline["items"] == []

    # second version: paged entries (small --limit forces a next_cursor).
    assert main(["--repo", str(repo), "changes", second["id"], "--limit", "5"]) == 0
    page = output(capsys)
    assert page["status"] == "complete"
    assert 0 < len(page["items"]) <= 5
    assert page["entry_count"] == n + 1          # 1 changed cell + n timestamp-only cells (incl. TOP)
    assert page["next_cursor"]
    seen = list(page["items"])
    cursor = page["next_cursor"]
    while cursor:
        assert main(["--repo", str(repo), "changes", second["id"], "--limit", "5", "--cursor", cursor]) == 0
        more = output(capsys)
        seen.extend(more["items"])
        cursor = more["next_cursor"]
    assert len(seen) == n + 1
    assert any(e["kind"] == "cell.changed" for e in seen)

    # --kind filters server-side.
    assert main(["--repo", str(repo), "changes", second["id"], "--limit", "200", "--kind", "cell.changed"]) == 0
    only = output(capsys)
    assert [e["kind"] for e in only["items"]] == ["cell.changed"]

    # an invalid cursor is a clean CLI failure, not a traceback.
    assert main(["--repo", str(repo), "changes", second["id"], "--cursor", "bogus:0:0"]) != 0
    captured = capsys.readouterr()
    assert not captured.out and "vestigraph:" in captured.err and "Traceback" not in captured.err

    # changes-export: portable JSONL, independently readable with the stdlib reader.
    dest = tmp_path / "changes.jsonl"
    assert main(["--repo", str(repo), "changes-export", second["id"], str(dest)]) == 0
    exported = output(capsys)
    assert exported["destination"] == str(dest)
    assert exported["entries"] == n + 1
    with dest.open("rb") as stream:
        header, entries, footer = read_jsonl(stream)
    assert header["type"] == "header" and header["to_checkpoint_id"] == second["id"]
    assert footer["type"] == "footer" and footer["entry_count"] == n + 1
    assert len(entries) == n + 1 and all(e["type"] == "entry" for e in entries)

    # a tampered footer hash is rejected by the independent reader.
    lines = dest.read_bytes().split(b"\n")
    assert b'"seq":0' in lines[1]
    lines[1] = lines[1].replace(b'"seq":0', b'"seq":9')
    tampered = tmp_path / "tampered.jsonl"
    tampered.write_bytes(b"\n".join(lines))
    with tampered.open("rb") as stream:
        try:
            read_jsonl(stream)
            raised = False
        except MetadataError:
            raised = True
    assert raised

    # export refuses to overwrite an existing file.
    assert main(["--repo", str(repo), "changes-export", second["id"], str(dest)]) != 0
    captured = capsys.readouterr()
    assert not captured.out and "vestigraph:" in captured.err


def test_module_entrypoint_help():
    result = subprocess.run([sys.executable, "-m", "vestigraph", "--help"], text=True, encoding="utf-8",
                            capture_output=True, cwd=Path(__file__).parents[1], check=False)
    assert result.returncode == 0
    assert "checkpoint" in result.stdout
