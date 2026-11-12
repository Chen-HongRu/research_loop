import os

import pytest

from research_loop.state import PublicationStore, writer


def test_atomic_snapshot_does_not_share_draft_inode(tmp_path):
    knowledge = PublicationStore(tmp_path)
    baseline = knowledge.prepare()
    path = knowledge.draft / "finding.md"
    path.write_text("negative result")
    with path.open("a") as old_fd:
        version = knowledge.publish(baseline)
        old_fd.write(" unfinished later edit")
        old_fd.flush()
    published = knowledge.current / "finding.md"
    assert published.read_text() == "negative result"
    assert os.stat(path).st_ino != os.stat(published).st_ino
    assert knowledge.current.resolve().name == version
    with pytest.raises(RuntimeError, match="changed"):
        knowledge.publish(baseline)
    assert published.read_text() == "negative result"


def test_publication_rejects_symlink_without_reading_its_target(tmp_path):
    knowledge = PublicationStore(tmp_path)
    baseline = knowledge.prepare()
    (knowledge.draft / "external").symlink_to(tmp_path / "not-a-knowledge-file")
    with pytest.raises(ValueError, match="regular"):
        knowledge.publish(baseline)
    assert str(knowledge.current.resolve()) == baseline


def test_one_controller_writer(tmp_path):
    with writer(tmp_path):
        with pytest.raises(RuntimeError, match="already"):
            with writer(tmp_path):
                pass
    with writer(tmp_path):
        pass


def test_publication_metadata_is_written_before_pointer_activation(
    tmp_path, monkeypatch
):
    from pathlib import Path
    from research_loop.state import write_json

    knowledge = PublicationStore(tmp_path)
    baseline = knowledge.prepare()
    seen = []

    def check_metadata(path, value):
        assert str(knowledge.current.resolve()) == baseline
        assert path.parent == knowledge.root / "publications"
        seen.append(value)
        write_json(path, value)

    monkeypatch.setattr("research_loop.state.write_json", check_metadata)
    version = knowledge.publish(
        baseline, publication={"iteration": 1, "result": {"action": "continue"}}
    )
    assert seen == [
        {
            "iteration": 1,
            "result": {"action": "continue"},
            "publication_version": version,
        }
    ]
    assert knowledge.published_state() == seen[0]
    assert Path(knowledge.current.resolve()).name == version


def test_stop_during_copy_discards_staged_version_and_metadata(tmp_path, monkeypatch):
    import shutil
    from research_loop.state import PublicationStopped

    knowledge = PublicationStore(tmp_path)
    baseline = knowledge.prepare()
    (knowledge.draft / "index.md").write_text("retained unfinished draft")
    copytree = shutil.copytree

    def stop_after_copy(*args, **kwargs):
        result = copytree(*args, **kwargs)
        (tmp_path / "STOP").write_text("operator stop\n")
        return result

    monkeypatch.setattr("research_loop.state.shutil.copytree", stop_after_copy)
    with pytest.raises(PublicationStopped, match="before publication activation"):
        knowledge.publish(baseline, publication={"iteration": 1})
    assert str(knowledge.current.resolve()) == baseline
    assert (knowledge.draft / "index.md").read_text() == "retained unfinished draft"
    assert list((knowledge.root / "versions").iterdir()) == [
        knowledge.root / "versions/initial"
    ]
    assert not list((knowledge.root / "publications").glob("*.json"))
    assert not list(knowledge.root.glob(".current-*"))


def test_interruption_immediately_after_pointer_rename_keeps_active_tree(
    tmp_path, monkeypatch
):
    from pathlib import Path

    store = PublicationStore(tmp_path)
    baseline = store.prepare()
    (store.draft / "answer.txt").write_text("42")
    replace = Path.replace

    def interrupt_after_rename(source, target):
        result = replace(source, target)
        if source.name.startswith(".current-"):
            raise RuntimeError("interrupted after atomic rename")
        return result

    monkeypatch.setattr(Path, "replace", interrupt_after_rename)
    with pytest.raises(RuntimeError, match="after atomic rename"):
        store.publish(
            baseline,
            publication={
                "iteration": 1,
                "result": {"answer": 42},
                "action": "stop",
                "status": "completed",
            },
        )
    assert str(store.current.resolve()) != baseline
    assert (store.current / "answer.txt").read_text() == "42"
    assert store.published_state()["result"] == {"answer": 42}
