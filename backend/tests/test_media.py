"""Phase 9: media attachments, storage and file validation.

Runs on SQLite. Uploads are built in memory with ``io.BytesIO`` and written to
a per-test temporary directory, so the suite never touches the real
``uploads/`` folder and leaves nothing behind.

Every byte string below is a minimal but *genuine* file header - the point of
the signature check is that contents must match the declared type, so tests
that fed it arbitrary bytes would prove nothing.
"""
import io
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.models import (
    Authority,
    AuthorityType,
    District,
    EntityType,
    GovernmentLevel,
    MediaAttachment,
    MediaType,
    Municipality,
    ProgressUpdate,
    Project,
)
from app.services import media_service

PASSWORD = "correct-horse-battery"

# --- real file signatures --------------------------------------------------
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 64
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00" + b"\x00" * 64
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"\x00" * 64

# Not on any allowlist, and deliberately recognisable.
EXECUTABLE = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 64
HTML = b"<!DOCTYPE html><script>alert(1)</script>"
SHELL = b"#!/bin/sh\nrm -rf /\n"


@pytest.fixture
def app(tmp_path):
    """Override the app fixture so uploads land in a temp directory."""
    from app import create_app

    application = create_app("testing")
    application.config.update(
        TESTING=True, UPLOAD_FOLDER=str(tmp_path / "uploads")
    )
    return application


@pytest.fixture
def upload_root(app):
    return Path(app.config["UPLOAD_FOLDER"])


@pytest.fixture
def areas(db):
    district = District(name="Testland", province="TestProvince", code="D-01")
    db.session.add(district)
    db.session.commit()
    municipality = Municipality(district_id=district.id, name="Testville", code="M-01")
    db.session.add(municipality)
    db.session.commit()
    return {"district": district, "municipality": municipality}


@pytest.fixture
def located(monkeypatch, areas):
    from app.services import geolocation_service

    def _fake_reverse_geocode(coordinates):
        return {
            "point": coordinates.to_geojson(),
            "coordinates": coordinates.as_dict(),
            "resolved": True,
            "reason": None,
            "province": areas["district"].province,
            "district": areas["district"].to_dict(),
            "municipality": areas["municipality"].to_dict(),
        }

    monkeypatch.setattr(geolocation_service, "reverse_geocode", _fake_reverse_geocode)
    return areas


@pytest.fixture
def citizen(make_user):
    return make_user(email="citizen@betternepal.np", role_names=("citizen",))


@pytest.fixture
def citizen_headers(citizen, auth_headers):
    return auth_headers("citizen@betternepal.np", PASSWORD)


@pytest.fixture
def other_citizen_headers(make_user, auth_headers):
    make_user(email="stranger@betternepal.np", role_names=("citizen",))
    return auth_headers("stranger@betternepal.np", PASSWORD)


@pytest.fixture
def authority_headers(make_user, auth_headers):
    make_user(email="officer@betternepal.np", role_names=("authority",))
    return auth_headers("officer@betternepal.np", PASSWORD)


@pytest.fixture
def admin_headers(make_user, auth_headers):
    make_user(email="admin@betternepal.np", role_names=("admin",))
    return auth_headers("admin@betternepal.np", PASSWORD)


@pytest.fixture
def report_id(client, citizen_headers, located):
    response = client.post(
        "/api/v1/reports",
        json={
            "title": "Collapsed culvert on the link road",
            "description": "The culvert has given way and the road is impassable.",
            "category": "road_damage",
            "lat": 0.5,
            "lng": 0.5,
        },
        headers=citizen_headers,
    )
    assert response.status_code == 201
    return response.get_json()["data"]["report"]["id"]


def _upload(client, headers, entity_type="report", entity_id=None, content=JPEG,
            filename="evidence.jpg", content_type="image/jpeg"):
    return client.post(
        "/api/v1/media/upload",
        data={
            "file": (io.BytesIO(content), filename, content_type),
            "entity_type": entity_type,
            "entity_id": entity_id or str(uuid.uuid4()),
        },
        headers=headers,
        content_type="multipart/form-data",
    )


# --- successful uploads ----------------------------------------------------


@pytest.mark.parametrize(
    "content,filename,content_type,expected_media_type",
    [
        (JPEG, "photo.jpg", "image/jpeg", "image"),
        (JPEG, "photo.jpeg", "image/jpeg", "image"),
        (PNG, "diagram.png", "image/png", "image"),
        (WEBP, "shot.webp", "image/webp", "image"),
        (HEIC, "iphone.heic", "image/heic", "image"),
        (PDF, "survey.pdf", "application/pdf", "document"),
    ],
)
def test_allowed_file_types_upload(
    client, citizen_headers, report_id, content, filename, content_type, expected_media_type
):
    response = _upload(
        client, citizen_headers, entity_id=report_id, content=content,
        filename=filename, content_type=content_type,
    )
    assert response.status_code == 201, response.get_json()

    media = response.get_json()["data"]["media"]
    assert media["original_filename"] == filename
    assert media["mime_type"] == content_type
    assert media["media_type"] == expected_media_type
    assert media["file_size_bytes"] == len(content)


def test_upload_uses_the_locked_success_envelope(client, citizen_headers, report_id):
    body = _upload(client, citizen_headers, entity_id=report_id).get_json()
    assert set(body) == {"status", "data"}
    assert body["status"] == "success"


def test_uploaded_file_lands_on_disk(client, citizen_headers, report_id, upload_root, db):
    _upload(client, citizen_headers, entity_id=report_id)
    attachment = db.session.scalar(sa.select(MediaAttachment))
    assert (upload_root / attachment.file_path).is_file()
    assert (upload_root / attachment.file_path).read_bytes() == JPEG


def test_uploader_comes_from_the_token(client, citizen, citizen_headers, report_id, db):
    """A body field must not be able to attribute evidence to someone else."""
    client.post(
        "/api/v1/media/upload",
        data={
            "file": (io.BytesIO(JPEG), "photo.jpg", "image/jpeg"),
            "entity_type": "report",
            "entity_id": report_id,
            "uploader_id": str(uuid.uuid4()),
        },
        headers=citizen_headers,
        content_type="multipart/form-data",
    )
    assert db.session.scalar(sa.select(MediaAttachment)).uploader_id == citizen.id


# --- stored filenames ------------------------------------------------------


def test_stored_filename_is_a_uuid_not_the_client_name(
    client, citizen_headers, report_id, db
):
    _upload(client, citizen_headers, entity_id=report_id, filename="my holiday photo.jpg")
    attachment = db.session.scalar(sa.select(MediaAttachment))

    stem = Path(attachment.file_path).stem
    assert len(stem) == 32
    uuid.UUID(stem)  # raises if it is not a uuid4 hex
    assert "holiday" not in attachment.file_path
    # secure_filename normalises the display name too; the stored path is
    # unrelated to it either way.
    assert attachment.original_filename == "my_holiday_photo.jpg"


def test_path_traversal_in_the_filename_is_neutralised(
    client, citizen_headers, report_id, upload_root, db
):
    """`../../` in a name must not escape the upload root."""
    response = _upload(
        client, citizen_headers, entity_id=report_id,
        filename="../../../../etc/passwd.jpg",
    )
    assert response.status_code == 201

    attachment = db.session.scalar(sa.select(MediaAttachment))
    stored = (upload_root / attachment.file_path).resolve()
    assert stored.is_relative_to(upload_root.resolve())
    assert ".." not in attachment.file_path


def test_two_uploads_of_the_same_name_do_not_collide(
    client, citizen_headers, report_id, db
):
    _upload(client, citizen_headers, entity_id=report_id, filename="photo.jpg")
    _upload(client, citizen_headers, entity_id=report_id, filename="photo.jpg")

    paths = db.session.scalars(sa.select(MediaAttachment.file_path)).all()
    assert len(paths) == 2
    assert len(set(paths)) == 2


def test_file_path_is_never_exposed_in_the_api(client, citizen_headers, report_id):
    body = _upload(client, citizen_headers, entity_id=report_id).get_data(as_text=True)
    assert "file_path" not in body


def test_double_extension_keeps_only_the_declared_one(
    client, citizen_headers, report_id, db
):
    """`payload.php.jpg` is a jpeg upload; nothing may end in .php."""
    response = _upload(
        client, citizen_headers, entity_id=report_id, filename="payload.php.jpg"
    )
    assert response.status_code == 201
    assert db.session.scalar(sa.select(MediaAttachment)).file_path.endswith(".jpg")


# --- rejected file types ---------------------------------------------------


@pytest.mark.parametrize(
    "filename,content_type",
    [
        ("malware.exe", "application/x-msdownload"),
        ("script.sh", "application/x-sh"),
        ("page.html", "text/html"),
        ("sheet.xlsx", "application/vnd.ms-excel"),
        ("archive.zip", "application/zip"),
        ("vector.svg", "image/svg+xml"),
        ("notes.txt", "text/plain"),
    ],
)
def test_disallowed_content_types_are_rejected(
    client, citizen_headers, report_id, filename, content_type, db
):
    response = _upload(
        client, citizen_headers, entity_id=report_id, content=EXECUTABLE,
        filename=filename, content_type=content_type,
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "unsupported_media_type"
    assert db.session.scalar(sa.select(sa.func.count()).select_from(MediaAttachment)) == 0


def test_rejection_lists_the_accepted_types(client, citizen_headers, report_id):
    response = _upload(
        client, citizen_headers, entity_id=report_id, filename="x.exe",
        content_type="application/x-msdownload",
    )
    allowed = response.get_json()["error"]["details"]["allowed_types"]
    assert "image/jpeg" in allowed
    assert "application/pdf" in allowed


@pytest.mark.parametrize(
    "content,label",
    [(EXECUTABLE, "an executable"), (HTML, "an html page"), (SHELL, "a shell script")],
)
def test_a_renamed_file_is_caught_by_its_signature(
    client, citizen_headers, report_id, content, label, db
):
    """The decisive check: declaring image/jpeg does not make it a jpeg."""
    response = _upload(
        client, citizen_headers, entity_id=report_id, content=content,
        filename="innocent.jpg", content_type="image/jpeg",
    )
    assert response.status_code == 400, label
    assert response.get_json()["error"]["code"] == "content_type_mismatch"
    assert db.session.scalar(sa.select(sa.func.count()).select_from(MediaAttachment)) == 0


def test_a_rejected_upload_writes_nothing_to_disk(
    client, citizen_headers, report_id, upload_root
):
    _upload(
        client, citizen_headers, entity_id=report_id, content=EXECUTABLE,
        filename="innocent.jpg", content_type="image/jpeg",
    )
    written = [path for path in upload_root.rglob("*") if path.is_file()] if upload_root.exists() else []
    assert written == []


def test_extension_must_match_the_declared_type(client, citizen_headers, report_id):
    """A PNG declared as image/jpeg is a confused client, or a probe."""
    response = _upload(
        client, citizen_headers, entity_id=report_id, content=PNG,
        filename="image.png", content_type="image/jpeg",
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "extension_mismatch"


def test_pdf_content_declared_as_an_image_is_rejected(client, citizen_headers, report_id):
    response = _upload(
        client, citizen_headers, entity_id=report_id, content=PDF,
        filename="doc.jpg", content_type="image/jpeg",
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "content_type_mismatch"


def test_an_empty_file_is_rejected(client, citizen_headers, report_id):
    response = _upload(client, citizen_headers, entity_id=report_id, content=b"")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] in {"empty_file", "file_required"}


def test_an_oversized_file_is_rejected(client, citizen_headers, report_id, app):
    """Checked against MAX_UPLOAD_BYTES, which sits below the body limit so the
    validator gets a chance to explain rather than Werkzeug returning 413."""
    app.config["MAX_UPLOAD_BYTES"] = 1024
    response = _upload(
        client, citizen_headers, entity_id=report_id, content=JPEG + b"\x00" * 4096
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "file_too_large"


def test_the_body_limit_sits_above_the_file_limit(app):
    """Otherwise an exactly-at-limit upload dies as an opaque 413."""
    assert app.config["MAX_CONTENT_LENGTH"] > app.config["MAX_UPLOAD_BYTES"]


def test_missing_file_field_is_rejected(client, citizen_headers, report_id):
    response = client.post(
        "/api/v1/media/upload",
        data={"entity_type": "report", "entity_id": report_id},
        headers=citizen_headers,
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "file_required"


# --- entity validation -----------------------------------------------------


def test_upload_to_a_nonexistent_report_is_rejected(client, citizen_headers, db):
    """The service check that stands in for the missing foreign key."""
    response = _upload(client, citizen_headers, entity_id=str(uuid.uuid4()))
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "report_not_found"
    assert db.session.scalar(sa.select(sa.func.count()).select_from(MediaAttachment)) == 0


def test_upload_with_an_unknown_entity_type_is_rejected(client, citizen_headers, report_id):
    response = _upload(client, citizen_headers, entity_type="banana", entity_id=report_id)
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_entity_type"


def test_upload_with_an_invalid_entity_id_is_rejected(client, citizen_headers):
    response = _upload(client, citizen_headers, entity_id="not-a-uuid")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_identifier"


def test_missing_entity_fields_are_rejected(client, citizen_headers):
    response = client.post(
        "/api/v1/media/upload",
        data={"file": (io.BytesIO(JPEG), "photo.jpg", "image/jpeg")},
        headers=citizen_headers,
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "validation_error"


@pytest.fixture
def all_entities(client, citizen_headers, authority_headers, located, report_id, db, areas):
    """One of each attachable entity type."""
    incident = client.post(
        "/api/v1/incidents/from-report",
        json={"report_id": report_id},
        headers=authority_headers,
    )
    assert incident.status_code == 201
    incident_id = incident.get_json()["data"]["incident"]["id"]

    authority = Authority(
        name="Test Roads Office",
        level=GovernmentLevel.PROVINCIAL,
        type=AuthorityType.DEPARTMENT_OF_ROADS,
        district_id=areas["district"].id,
    )
    db.session.add(authority)
    db.session.commit()

    project = Project(
        title="Culvert reconstruction",
        description="Rebuild the failed culvert.",
        authority_id=authority.id,
    )
    db.session.add(project)
    db.session.commit()

    update = ProgressUpdate(
        project_id=project.id,
        author_id=db.session.scalar(sa.select(MediaAttachment.uploader_id))
        or project.authority_id,
        notes="Initial site visit completed.",
    )
    return {
        "report": report_id,
        "incident": incident_id,
        "project": str(project.id),
        "_project": project,
        "_update": update,
    }


@pytest.mark.parametrize("entity_type", ["report", "incident", "project"])
def test_media_attaches_to_every_entity_type(
    client, citizen_headers, all_entities, entity_type
):
    response = _upload(
        client, citizen_headers, entity_type=entity_type,
        entity_id=all_entities[entity_type],
    )
    assert response.status_code == 201
    assert response.get_json()["data"]["media"]["entity_type"] == entity_type


def test_media_attaches_to_a_progress_update(
    client, citizen, citizen_headers, all_entities, db
):
    update = ProgressUpdate(
        project_id=all_entities["_project"].id,
        author_id=citizen.id,
        notes="Site photographs attached.",
    )
    db.session.add(update)
    db.session.commit()

    response = _upload(
        client, citizen_headers, entity_type="progress_update", entity_id=str(update.id)
    )
    assert response.status_code == 201


# --- retrieval -------------------------------------------------------------


def test_listing_media_for_an_entity_is_public(client, citizen_headers, report_id):
    _upload(client, citizen_headers, entity_id=report_id, filename="one.jpg")
    _upload(client, citizen_headers, entity_id=report_id, filename="two.png",
            content=PNG, content_type="image/png")

    response = client.get(f"/api/v1/media/entity/report/{report_id}")
    assert response.status_code == 200
    assert response.get_json()["data"]["count"] == 2


def test_listing_returns_newest_first(client, citizen_headers, report_id):
    import time

    _upload(client, citizen_headers, entity_id=report_id, filename="first.jpg")
    time.sleep(0.01)  # clock granularity, so the ordering is not a coin flip
    _upload(client, citizen_headers, entity_id=report_id, filename="second.jpg")

    media = client.get(f"/api/v1/media/entity/report/{report_id}").get_json()["data"]["media"]
    assert media[0]["original_filename"] == "second.jpg"


def test_listing_for_an_entity_with_no_media_is_empty_not_an_error(client, report_id):
    response = client.get(f"/api/v1/media/entity/report/{report_id}")
    assert response.status_code == 200
    assert response.get_json()["data"]["media"] == []


def test_listing_for_an_unknown_entity_is_404(client, db):
    response = client.get(f"/api/v1/media/entity/report/{uuid.uuid4()}")
    assert response.status_code == 404


def test_media_of_one_entity_does_not_leak_into_another(
    client, citizen_headers, all_entities
):
    _upload(client, citizen_headers, entity_type="report", entity_id=all_entities["report"])
    _upload(client, citizen_headers, entity_type="project", entity_id=all_entities["project"])

    report_media = client.get(
        f"/api/v1/media/entity/report/{all_entities['report']}"
    ).get_json()["data"]
    assert report_media["count"] == 1
    assert report_media["media"][0]["entity_type"] == "report"


def test_getting_media_metadata_is_public(client, citizen_headers, report_id):
    media_id = _upload(client, citizen_headers, entity_id=report_id).get_json()["data"][
        "media"
    ]["id"]

    response = client.get(f"/api/v1/media/{media_id}")
    assert response.status_code == 200
    assert response.get_json()["data"]["media"]["id"] == media_id


def test_media_response_does_not_leak_the_uploader_email(client, citizen_headers, report_id):
    _upload(client, citizen_headers, entity_id=report_id)
    body = client.get(f"/api/v1/media/entity/report/{report_id}").get_data(as_text=True)
    assert "citizen@betternepal.np" not in body
    assert "password_hash" not in body


def test_unknown_media_returns_404(client, db):
    response = client.get(f"/api/v1/media/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "media_not_found"


def test_invalid_media_id_returns_400(client, db):
    response = client.get("/api/v1/media/not-a-uuid")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_identifier"


# --- download --------------------------------------------------------------


def test_download_returns_the_original_bytes(client, citizen_headers, report_id):
    media_id = _upload(client, citizen_headers, entity_id=report_id).get_json()["data"][
        "media"
    ]["id"]

    response = client.get(f"/api/v1/media/{media_id}/download")
    assert response.status_code == 200
    assert response.data == JPEG


def test_download_is_served_as_an_attachment(client, citizen_headers, report_id):
    """Inline rendering of user-supplied files is how stored XSS happens."""
    media_id = _upload(client, citizen_headers, entity_id=report_id).get_json()["data"][
        "media"
    ]["id"]

    response = client.get(f"/api/v1/media/{media_id}/download")
    assert "attachment" in response.headers["Content-Disposition"]
    assert response.headers["Content-Type"].startswith("image/jpeg")


def test_download_of_a_pdf_keeps_its_content_type(client, citizen_headers, report_id):
    media_id = _upload(
        client, citizen_headers, entity_id=report_id, content=PDF,
        filename="survey.pdf", content_type="application/pdf",
    ).get_json()["data"]["media"]["id"]

    response = client.get(f"/api/v1/media/{media_id}/download")
    assert response.headers["Content-Type"].startswith("application/pdf")


def test_download_url_is_built_from_the_id(client, citizen_headers, report_id):
    media = _upload(client, citizen_headers, entity_id=report_id).get_json()["data"]["media"]
    assert media["url"] == f"/api/v1/media/{media['id']}/download"


def test_download_when_the_file_is_missing_is_a_clean_404(
    client, citizen_headers, report_id, upload_root, db
):
    """A row that outlives its bytes must not produce a 500."""
    media_id = _upload(client, citizen_headers, entity_id=report_id).get_json()["data"][
        "media"
    ]["id"]
    attachment = db.session.scalar(sa.select(MediaAttachment))
    (upload_root / attachment.file_path).unlink()

    response = client.get(f"/api/v1/media/{media_id}/download")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "file_missing"


# --- authorization ---------------------------------------------------------


def test_unauthenticated_upload_is_rejected(client, report_id, db):
    response = client.post(
        "/api/v1/media/upload",
        data={
            "file": (io.BytesIO(JPEG), "photo.jpg", "image/jpeg"),
            "entity_type": "report",
            "entity_id": report_id,
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 401
    assert db.session.scalar(sa.select(sa.func.count()).select_from(MediaAttachment)) == 0


def test_the_uploader_can_delete_their_own_media(
    client, citizen_headers, report_id, upload_root, db
):
    media_id = _upload(client, citizen_headers, entity_id=report_id).get_json()["data"][
        "media"
    ]["id"]
    stored = upload_root / db.session.scalar(sa.select(MediaAttachment)).file_path

    response = client.delete(f"/api/v1/media/{media_id}", headers=citizen_headers)
    assert response.status_code == 200
    assert response.get_json()["data"]["deleted"] is True
    assert db.session.scalar(sa.select(sa.func.count()).select_from(MediaAttachment)) == 0
    assert not stored.exists()


def test_deleting_media_removes_the_file_from_disk(
    client, citizen_headers, report_id, upload_root, db
):
    media_id = _upload(client, citizen_headers, entity_id=report_id).get_json()["data"][
        "media"
    ]["id"]
    stored = upload_root / db.session.scalar(sa.select(MediaAttachment)).file_path
    assert stored.is_file()

    response = client.delete(f"/api/v1/media/{media_id}", headers=citizen_headers)
    assert response.get_json()["data"]["file_removed"] is True
    assert not stored.exists()


def test_another_citizen_cannot_delete_someone_elses_media(
    client, citizen_headers, other_citizen_headers, report_id, upload_root, db
):
    media_id = _upload(client, citizen_headers, entity_id=report_id).get_json()["data"][
        "media"
    ]["id"]
    stored = upload_root / db.session.scalar(sa.select(MediaAttachment)).file_path

    response = client.delete(f"/api/v1/media/{media_id}", headers=other_citizen_headers)
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "permission_denied"
    assert db.session.scalar(sa.select(sa.func.count()).select_from(MediaAttachment)) == 1
    assert stored.is_file()  # the bytes survive a refused delete


def test_an_authority_can_delete_any_media(client, citizen_headers, authority_headers, report_id):
    media_id = _upload(client, citizen_headers, entity_id=report_id).get_json()["data"][
        "media"
    ]["id"]
    response = client.delete(f"/api/v1/media/{media_id}", headers=authority_headers)
    assert response.status_code == 200


def test_an_admin_can_delete_any_media(client, citizen_headers, admin_headers, report_id):
    media_id = _upload(client, citizen_headers, entity_id=report_id).get_json()["data"][
        "media"
    ]["id"]
    response = client.delete(f"/api/v1/media/{media_id}", headers=admin_headers)
    assert response.status_code == 200


def test_unauthenticated_delete_is_401(client, citizen_headers, report_id, db):
    media_id = _upload(client, citizen_headers, entity_id=report_id).get_json()["data"][
        "media"
    ]["id"]
    response = client.delete(f"/api/v1/media/{media_id}")
    assert response.status_code == 401
    assert db.session.scalar(sa.select(sa.func.count()).select_from(MediaAttachment)) == 1


def test_deleting_unknown_media_returns_404(client, citizen_headers, db):
    response = client.delete(f"/api/v1/media/{uuid.uuid4()}", headers=citizen_headers)
    assert response.status_code == 404


# --- storage service -------------------------------------------------------


def test_storage_key_is_date_partitioned(app):
    from app.services.storage_service import build_storage_key

    with app.app_context():
        key = build_storage_key("photo.jpg", {".jpg"})
    parts = key.split("/")
    assert len(parts) == 3
    assert len(parts[0]) == 4 and parts[0].isdigit()  # year
    assert len(parts[1]) == 2 and parts[1].isdigit()  # month


def test_storage_key_drops_a_disallowed_extension(app):
    """Only an allowlisted extension is ever taken from the client's name."""
    from app.services.storage_service import build_storage_key

    with app.app_context():
        rejected = build_storage_key("payload.exe", {".jpg"})
        accepted = build_storage_key("photo.jpg", {".jpg"})

    assert not rejected.endswith(".exe")
    assert Path(rejected).suffix == ""
    assert accepted.endswith(".jpg")


def test_storage_refuses_to_resolve_outside_its_root(app, upload_root):
    """Last line before the filesystem, for a key that should never exist."""
    from app.services.storage_service import LocalStorageService, StorageError

    with app.app_context():
        storage = LocalStorageService()
        with pytest.raises(StorageError):
            storage.get_file_path("../../../../etc/passwd")


def test_storage_delete_of_a_missing_file_returns_false(app):
    from app.services.storage_service import LocalStorageService

    with app.app_context():
        assert LocalStorageService().delete_file("2026/01/doesnotexist.jpg") is False


def test_storage_exists_is_false_for_an_escaping_path(app):
    from app.services.storage_service import LocalStorageService

    with app.app_context():
        assert LocalStorageService().exists("../../../../etc/passwd") is False


def test_upload_directory_is_created_on_demand(app, tmp_path):
    from app.services.storage_service import LocalStorageService

    target = tmp_path / "brand" / "new" / "dir"
    app.config["UPLOAD_FOLDER"] = str(target)
    with app.app_context():
        assert LocalStorageService().root.is_dir()


def test_local_storage_round_trip(app, upload_root):
    from app.services.storage_service import LocalStorageService

    with app.app_context():
        storage = LocalStorageService()
        key = storage.save_file(io.BytesIO(PNG), "diagram.png")

        assert storage.exists(key) is True
        assert Path(storage.get_file_path(key)).read_bytes() == PNG
        assert storage.delete_file(key) is True
        assert storage.exists(key) is False


# --- service helpers -------------------------------------------------------


def test_delete_media_for_entity_clears_rows_and_files(
    client, citizen_headers, report_id, upload_root, app, db
):
    """The counterpart to the missing foreign key."""
    _upload(client, citizen_headers, entity_id=report_id, filename="one.jpg")
    _upload(client, citizen_headers, entity_id=report_id, filename="two.jpg")
    paths = [
        upload_root / path
        for path in db.session.scalars(sa.select(MediaAttachment.file_path)).all()
    ]
    assert all(path.is_file() for path in paths)

    removed = media_service.delete_media_for_entity(EntityType.REPORT, report_id)
    assert removed == 2
    assert db.session.scalar(sa.select(sa.func.count()).select_from(MediaAttachment)) == 0
    assert not any(path.exists() for path in paths)


def test_can_delete_media_helper(db, citizen, make_user, report_id, client, citizen_headers):
    _upload(client, citizen_headers, entity_id=report_id)
    attachment = db.session.scalar(sa.select(MediaAttachment))

    stranger = make_user(email="nobody@betternepal.np", role_names=("citizen",))
    officer = make_user(email="boss@betternepal.np", role_names=("authority",))

    assert media_service.can_delete_media(attachment, citizen) is True
    assert media_service.can_delete_media(attachment, stranger) is False
    assert media_service.can_delete_media(attachment, officer) is True


# --- model and database ----------------------------------------------------


def test_attachment_requires_an_uploader(db, report_id):
    db.session.add(
        MediaAttachment(
            entity_type=EntityType.REPORT,
            entity_id=uuid.UUID(report_id),
            file_path="2026/01/x.jpg",
            original_filename="x.jpg",
            mime_type="image/jpeg",
            file_size_bytes=10,
            media_type=MediaType.IMAGE,
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_database_rejects_a_zero_byte_attachment(db, citizen, report_id):
    db.session.add(
        MediaAttachment(
            uploader_id=citizen.id,
            entity_type=EntityType.REPORT,
            entity_id=uuid.UUID(report_id),
            file_path="2026/01/x.jpg",
            original_filename="x.jpg",
            mime_type="image/jpeg",
            file_size_bytes=0,
            media_type=MediaType.IMAGE,
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_deleting_an_uploader_with_media_is_refused(client, citizen, citizen_headers, report_id, db):
    """RESTRICT: who supplied evidence is part of what makes it evidence."""
    _upload(client, citizen_headers, entity_id=report_id)
    db.session.delete(citizen)
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_attachment_has_no_updated_at_column():
    """Append-only: the bytes never change, so neither does the row."""
    assert "updated_at" not in MediaAttachment.__table__.columns
    assert "created_at" in MediaAttachment.__table__.columns


def test_relationship_is_navigable(client, citizen, citizen_headers, report_id, db):
    _upload(client, citizen_headers, entity_id=report_id)
    attachment = db.session.scalar(sa.select(MediaAttachment))
    assert attachment.uploader is citizen
    assert attachment in citizen.media_attachments


def test_orphaned_attachments_survive_their_entity(
    client, citizen_headers, report_id, db
):
    """Documented consequence of the polymorphic reference: no FK cascade.

    Deleting the report leaves the attachment row, which is exactly why
    delete_media_for_entity exists.
    """
    from app.models import Report

    _upload(client, citizen_headers, entity_id=report_id)
    db.session.delete(db.session.get(Report, uuid.UUID(report_id)))
    db.session.commit()

    assert db.session.scalar(sa.select(sa.func.count()).select_from(MediaAttachment)) == 1


# --- statistics ------------------------------------------------------------


def test_statistics_on_an_empty_database(client, db):
    data = client.get("/api/v1/media/statistics").get_json()["data"]
    assert data["total"] == 0
    assert data["total_bytes"] == 0
    assert set(data["by_media_type"]) == {member.value for member in MediaType}
    assert set(data["by_entity_type"]) == {member.value for member in EntityType}


def test_statistics_count_files_and_bytes(client, citizen_headers, report_id):
    _upload(client, citizen_headers, entity_id=report_id, filename="a.jpg")
    _upload(client, citizen_headers, entity_id=report_id, content=PDF,
            filename="b.pdf", content_type="application/pdf")

    data = client.get("/api/v1/media/statistics").get_json()["data"]
    assert data["total"] == 2
    assert data["total_bytes"] == len(JPEG) + len(PDF)
    assert data["by_media_type"]["image"] == 1
    assert data["by_media_type"]["document"] == 1
    assert data["by_entity_type"]["report"] == 2


def test_statistics_path_is_not_shadowed_by_the_id_route(client, db):
    assert client.get("/api/v1/media/statistics").status_code == 200


# --- earlier phases still work ---------------------------------------------


def test_previous_endpoints_are_unaffected(client, db):
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/map/districts").status_code == 200
    assert client.get("/api/v1/reports").status_code == 200
    assert client.get("/api/v1/incidents").status_code == 200
    assert client.get("/api/v1/authorities").status_code == 200
    assert client.get("/api/v1/projects").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401
