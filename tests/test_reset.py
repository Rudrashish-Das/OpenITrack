import logging

from openitrack.reset import delete_user_data, saved_items


def make_data(d):
    (d / "config.toml").write_text("x")
    (d / "calibration.json").write_text("{}")
    (d / "paired_devices.json").write_text("{}")
    (d / "tls").mkdir()
    (d / "tls" / "key.pem").write_text("k")
    (d / "profiles").mkdir()
    (d / "profiles" / "dcs.toml").write_text("p")
    (d / "models").mkdir()
    (d / "models" / "face_landmarker.task").write_text("m")
    (d / "web-vendor").mkdir()


def test_reset_keeps_downloads_by_default(tmp_path):
    make_data(tmp_path)
    names = {p.name for p in saved_items(tmp_path)}
    assert names == {"config.toml", "calibration.json", "paired_devices.json", "tls", "profiles"}
    report = delete_user_data(tmp_path)
    assert not report.failed
    assert sorted(p.name for p in tmp_path.iterdir()) == ["models", "web-vendor"]


def test_reset_everything_removes_the_folder(tmp_path):
    data = tmp_path / "OpenITrack"
    data.mkdir()
    make_data(data)
    report = delete_user_data(data, include_downloads=True)
    assert not report.failed and not data.exists()


def test_open_log_file_is_closed_and_deleted(tmp_path):
    make_data(tmp_path)
    handler = logging.FileHandler(tmp_path / "openitrack.log")
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        logging.getLogger("t").warning("hello")
        report = delete_user_data(tmp_path)
        assert not report.failed
        assert not (tmp_path / "openitrack.log").exists()
    finally:
        root.removeHandler(handler)


def test_missing_folder_is_fine(tmp_path):
    assert saved_items(tmp_path / "nope") == []
    assert delete_user_data(tmp_path / "nope").removed == []
