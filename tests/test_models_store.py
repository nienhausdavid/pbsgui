from pbsgui.core.models import Profile, Snapshot, archive_name
from pbsgui.core.store import ProfileStore


def test_repository_and_archives(profile):
    assert profile.repository == "pbsgui-laptop@pbs!backup@pbs.example:8007:store1"
    assert profile.archive_specs() == ["home-david.pxar:/home/david", "etc.pxar:/etc"]


def test_archive_names_unique():
    p = Profile(name="x", server="s", datastore="d", auth_id="a", sources=["/a/b", "/a-b", "/"])
    names = [s.split(":")[0] for s in p.archive_specs()]
    assert names == ["a-b.pxar", "a-b-1.pxar", "root.pxar"]
    assert archive_name("/") == "root"


def test_store_roundtrip(tmp_path, profile):
    s = ProfileStore(tmp_path / "p")
    s.save(profile)
    assert s.load("Laptop") == profile
    assert [p.name for p in s.list()] == ["Laptop"]
    s.delete("Laptop")
    assert s.list() == []


def test_store_skips_broken(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "kaputt.json").write_text("{nicht json")
    assert ProfileStore(d).list() == []


def test_snapshot_path():
    s = Snapshot.from_json({"backup-type": "host", "backup-id": "l", "backup-time": 0, "files": [{"filename": "a.pxar"}]})
    assert s.path == "host/l/1970-01-01T00:00:00Z" and s.files == ["a.pxar"]
