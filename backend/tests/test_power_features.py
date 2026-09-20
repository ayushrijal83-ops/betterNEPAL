"""Announcements, travel planner and the RAG chatbot.

Runs on SQLite, so the travel planner exercises its documented non-PostGIS
path (bounding box + point-to-segment distance) and reports
``method: "approximate"``.

All districts, authorities and coordinates are SYNTHETIC test data, not Nepal.
"""
import uuid

import pytest
import sqlalchemy as sa

from app.models import Announcement, District, Incident, IncidentStatus, Municipality
from app.services import chat_service, travel_service
from app.services.ai_service import AIUnavailable, BaseAIService, set_ai_service

PASSWORD = "correct-horse-battery"

VALID_ANNOUNCEMENT = {
    "title": "Flood warning for the river basin",
    "body": "Water levels are rising after sustained rainfall. Avoid low-lying roads.",
}

REPORT = {
    "title": "Landslide blocking the highway",
    "description": "A large landslide has come down across both lanes of the road.",
    "category": "natural_disaster",
    "lat": 0.5,
    "lng": 0.5,
}


class FakeAI(BaseAIService):
    """In-process provider. Records prompts; never touches a network."""

    name = "fake"

    def __init__(self, answer="The highway is blocked by a landslide.", fail=False):
        self._answer = answer
        self._fail = fail
        self.prompts = []

    @property
    def available(self):
        return not self._fail

    def analyze_report_text(self, title, description):
        raise NotImplementedError

    def compare_reports(self, a, b):
        raise NotImplementedError

    def chat(self, prompt):
        self.prompts.append(prompt)
        if self._fail:
            raise AIUnavailable("The AI provider could not be reached.")
        return self._answer


@pytest.fixture
def areas(db):
    first = District(name="Testland", province="TestProvince", code="D-01")
    second = District(name="Otherland", province="TestProvince", code="D-02")
    db.session.add_all([first, second])
    db.session.commit()
    municipality = Municipality(district_id=first.id, name="Testville", code="M-01")
    db.session.add(municipality)
    db.session.commit()
    return {"district": first, "other_district": second, "municipality": municipality}


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
def citizen_headers(make_user, auth_headers):
    make_user(email="citizen@betternepal.np", role_names=("citizen",))
    return auth_headers("citizen@betternepal.np", PASSWORD)


@pytest.fixture
def authority(make_user):
    return make_user(email="officer@betternepal.np", role_names=("authority",))


@pytest.fixture
def authority_headers(authority, auth_headers):
    return auth_headers("officer@betternepal.np", PASSWORD)


@pytest.fixture
def admin_headers(make_user, auth_headers):
    make_user(email="admin@betternepal.np", role_names=("admin",))
    return auth_headers("admin@betternepal.np", PASSWORD)


@pytest.fixture
def ai(app):
    fake = FakeAI()
    with app.app_context():
        set_ai_service(fake)
    return fake


def _post_announcement(client, headers, **overrides):
    return client.post(
        "/api/v1/announcements", json={**VALID_ANNOUNCEMENT, **overrides}, headers=headers
    )


def _incident(client, citizen_headers, authority_headers, severity="high", **report):
    response = client.post(
        "/api/v1/reports", json={**REPORT, **report}, headers=citizen_headers
    )
    assert response.status_code == 201, response.get_json()
    promoted = client.post(
        "/api/v1/incidents/from-report",
        json={"report_id": response.get_json()["data"]["report"]["id"], "severity": severity},
        headers=authority_headers,
    )
    assert promoted.status_code == 201, promoted.get_json()
    return promoted.get_json()["data"]["incident"]


# ===========================================================================
# Announcements
# ===========================================================================


def test_authority_can_publish(client, authority_headers, db):
    response = _post_announcement(client, authority_headers)
    assert response.status_code == 201

    payload = response.get_json()["data"]["announcement"]
    assert payload["title"] == VALID_ANNOUNCEMENT["title"]
    assert payload["is_draft"] is False
    assert payload["scope"] == "national"


def test_admin_can_publish(client, admin_headers, db):
    assert _post_announcement(client, admin_headers).status_code == 201


def test_uses_the_locked_success_envelope(client, authority_headers, db):
    body = _post_announcement(client, authority_headers).get_json()
    assert set(body) == {"status", "data"}


def test_citizen_cannot_publish(client, citizen_headers, db):
    response = _post_announcement(client, citizen_headers)
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "permission_denied"


def test_unauthenticated_publish_is_401(client, db):
    assert client.post("/api/v1/announcements", json=VALID_ANNOUNCEMENT).status_code == 401


def test_author_comes_from_the_token(client, authority, authority_headers, db):
    """An alert must carry the name of someone accountable for it."""
    _post_announcement(client, authority_headers, author_id=str(uuid.uuid4()))
    assert db.session.scalar(sa.select(Announcement)).author_id == authority.id


def test_district_scoped_announcement(client, authority_headers, areas):
    response = _post_announcement(
        client, authority_headers, district_id=str(areas["district"].id)
    )
    payload = response.get_json()["data"]["announcement"]
    assert payload["scope"] == "district"
    assert payload["district"] == "Testland"


def test_unknown_district_is_rejected(client, authority_headers, db):
    response = _post_announcement(client, authority_headers, district_id=str(uuid.uuid4()))
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "district_not_found"


@pytest.mark.parametrize("title", ["", "abc", "x" * 201])
def test_invalid_title_is_rejected(client, authority_headers, db, title):
    response = _post_announcement(client, authority_headers, title=title)
    assert response.status_code == 400
    assert "title" in response.get_json()["error"]["details"]


@pytest.mark.parametrize("body", ["", "short"])
def test_invalid_body_is_rejected(client, authority_headers, db, body):
    response = _post_announcement(client, authority_headers, body=body)
    assert response.status_code == 400
    assert "body" in response.get_json()["error"]["details"]


# --- the feed --------------------------------------------------------------


def test_feed_is_public(client, authority_headers, db):
    _post_announcement(client, authority_headers)
    response = client.get("/api/v1/announcements")
    assert response.status_code == 200
    assert len(response.get_json()["data"]["announcements"]) == 1


def test_empty_feed_is_not_an_error(client, db):
    data = client.get("/api/v1/announcements").get_json()["data"]
    assert data["announcements"] == []
    assert data["pagination"]["total"] == 0


def test_local_feed_includes_national_items(client, authority_headers, areas):
    """A reader in one district still needs the nationwide warning."""
    _post_announcement(client, authority_headers, title="Nationwide flood warning")
    _post_announcement(
        client,
        authority_headers,
        title="Local road closure notice",
        district_id=str(areas["district"].id),
    )
    _post_announcement(
        client,
        authority_headers,
        title="Elsewhere entirely notice",
        district_id=str(areas["other_district"].id),
    )

    data = client.get(
        f"/api/v1/announcements?district_id={areas['district'].id}"
    ).get_json()["data"]
    titles = {a["title"] for a in data["announcements"]}

    assert "Nationwide flood warning" in titles
    assert "Local road closure notice" in titles
    assert "Elsewhere entirely notice" not in titles


def test_global_feed_excludes_district_items(client, authority_headers, areas):
    _post_announcement(client, authority_headers, title="Nationwide flood warning")
    _post_announcement(
        client, authority_headers, title="Local notice here", district_id=str(areas["district"].id)
    )

    data = client.get("/api/v1/announcements?scope=national").get_json()["data"]
    assert [a["title"] for a in data["announcements"]] == ["Nationwide flood warning"]


def test_feed_is_newest_first(client, authority_headers, db):
    import time

    _post_announcement(client, authority_headers, title="The earlier notice")
    time.sleep(0.01)  # clock granularity, so ordering is not a coin flip
    _post_announcement(client, authority_headers, title="The later notice")

    data = client.get("/api/v1/announcements").get_json()["data"]
    assert data["announcements"][0]["title"] == "The later notice"


def test_feed_does_not_leak_the_author_email(client, authority_headers, db):
    _post_announcement(client, authority_headers)
    body = client.get("/api/v1/announcements").get_data(as_text=True)
    assert "officer@betternepal.np" not in body
    assert "password_hash" not in body


def test_feed_pagination(client, authority_headers, db):
    for index in range(3):
        _post_announcement(client, authority_headers, title=f"Notice number {index} here")
    data = client.get("/api/v1/announcements?page=1&per_page=2").get_json()["data"]
    assert len(data["announcements"]) == 2
    assert data["pagination"] == {"page": 1, "per_page": 2, "total": 3, "pages": 2}


# --- drafts: the human gate on AI-written alerts ----------------------------


def test_a_draft_never_appears_in_the_public_feed(client, authority_headers, db):
    """The whole point of the draft flag."""
    _post_announcement(client, authority_headers, is_draft=True)
    assert client.get("/api/v1/announcements").get_json()["data"]["announcements"] == []


def test_a_draft_is_stored(client, authority_headers, db):
    response = _post_announcement(client, authority_headers, is_draft=True)
    assert response.status_code == 201
    assert response.get_json()["data"]["announcement"]["is_draft"] is True
    assert db.session.scalar(sa.select(Announcement)).is_draft is True


def test_authority_can_list_drafts(client, authority_headers, db):
    _post_announcement(client, authority_headers, is_draft=True, title="AI drafted alert")
    _post_announcement(client, authority_headers, title="A published notice")

    data = client.get("/api/v1/announcements/drafts", headers=authority_headers).get_json()["data"]
    assert [a["title"] for a in data["announcements"]] == ["AI drafted alert"]


def test_citizen_cannot_list_drafts(client, authority_headers, citizen_headers, db):
    _post_announcement(client, authority_headers, is_draft=True)
    assert client.get("/api/v1/announcements/drafts", headers=citizen_headers).status_code == 403


def test_a_draft_404s_for_the_public(client, authority_headers, db):
    """404 rather than 403: a 403 would confirm something exists at that id."""
    draft_id = _post_announcement(client, authority_headers, is_draft=True).get_json()["data"][
        "announcement"
    ]["id"]
    assert client.get(f"/api/v1/announcements/{draft_id}").status_code == 404


def test_an_authority_can_read_a_draft(client, authority_headers, db):
    draft_id = _post_announcement(client, authority_headers, is_draft=True).get_json()["data"][
        "announcement"
    ]["id"]
    response = client.get(f"/api/v1/announcements/{draft_id}", headers=authority_headers)
    assert response.status_code == 200
    assert response.get_json()["data"]["announcement"]["is_draft"] is True


def test_publishing_a_draft_makes_it_public(client, authority_headers, db):
    draft_id = _post_announcement(client, authority_headers, is_draft=True).get_json()["data"][
        "announcement"
    ]["id"]

    response = client.post(
        f"/api/v1/announcements/{draft_id}/publish", headers=authority_headers
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["announcement"]["is_draft"] is False
    assert len(client.get("/api/v1/announcements").get_json()["data"]["announcements"]) == 1


def test_publishing_records_the_publisher_as_author(
    client, make_user, authority_headers, auth_headers, db
):
    """An alert goes out under the name of whoever cleared it."""
    draft_id = _post_announcement(client, authority_headers, is_draft=True).get_json()["data"][
        "announcement"
    ]["id"]

    reviewer = make_user(email="reviewer@betternepal.np", role_names=("admin",))
    client.post(
        f"/api/v1/announcements/{draft_id}/publish",
        headers=auth_headers("reviewer@betternepal.np", PASSWORD),
    )
    assert db.session.scalar(sa.select(Announcement)).author_id == reviewer.id


def test_publishing_twice_is_refused(client, authority_headers, db):
    draft_id = _post_announcement(client, authority_headers, is_draft=True).get_json()["data"][
        "announcement"
    ]["id"]
    client.post(f"/api/v1/announcements/{draft_id}/publish", headers=authority_headers)

    response = client.post(
        f"/api/v1/announcements/{draft_id}/publish", headers=authority_headers
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "already_published"


def test_citizen_cannot_publish_a_draft(client, authority_headers, citizen_headers, db):
    draft_id = _post_announcement(client, authority_headers, is_draft=True).get_json()["data"][
        "announcement"
    ]["id"]
    assert client.post(
        f"/api/v1/announcements/{draft_id}/publish", headers=citizen_headers
    ).status_code == 403


def test_only_admin_can_delete(client, authority_headers, admin_headers, db):
    published = _post_announcement(client, authority_headers).get_json()["data"][
        "announcement"
    ]["id"]
    assert client.delete(
        f"/api/v1/announcements/{published}", headers=authority_headers
    ).status_code == 403
    assert client.delete(
        f"/api/v1/announcements/{published}", headers=admin_headers
    ).status_code == 200


def test_unknown_announcement_is_404(client, db):
    assert client.get(f"/api/v1/announcements/{uuid.uuid4()}").status_code == 404


def test_invalid_id_is_400(client, db):
    response = client.get("/api/v1/announcements/not-a-uuid")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_identifier"


def test_deleting_an_author_with_announcements_is_refused(
    client, authority, authority_headers, db
):
    _post_announcement(client, authority_headers)
    db.session.delete(authority)
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


# ===========================================================================
# Travel planner
# ===========================================================================


def test_route_with_no_hazards(client, db):
    data = client.get(
        "/api/v1/travel/route?start_lat=0.5&start_lng=0.5&end_lat=0.6&end_lng=0.6"
    ).get_json()["data"]

    assert data["danger_level"] == "LOW"
    assert data["hazard_count"] == 0
    assert data["hazards"] == []


def test_route_is_public(client, db):
    assert client.get(
        "/api/v1/travel/route?start_lat=0.5&start_lng=0.5&end_lat=0.6&end_lng=0.6"
    ).status_code == 200


def test_route_reports_its_method_and_limitation(client, db):
    """The response must say it is a straight-line corridor, not road routing."""
    data = client.get(
        "/api/v1/travel/route?start_lat=0.5&start_lng=0.5&end_lat=0.6&end_lng=0.6"
    ).get_json()["data"]

    assert data["method"] == "approximate"
    assert data["route"]["is_straight_line_corridor"] is True
    assert data["route"]["length_km"] > 0


def test_an_incident_on_the_route_is_found(client, citizen_headers, authority_headers, located):
    """Midway along the line, well inside the 5km corridor."""
    _incident(client, citizen_headers, authority_headers, lat=0.55, lng=0.55)

    data = client.get(
        "/api/v1/travel/route?start_lat=0.5&start_lng=0.5&end_lat=0.6&end_lng=0.6"
    ).get_json()["data"]

    assert data["hazard_count"] == 1
    assert data["hazards"][0]["distance_from_route_metres"] < 5000


def test_an_incident_far_from_the_route_is_ignored(
    client, citizen_headers, authority_headers, located
):
    """~33km off the line: outside even the widened 20km corridor.

    Was 11km when the default was 5km. The default is now 20km (Nepal's
    highways follow valleys, not straight lines - see travel_service), so the
    "clearly off-route" case has to move out with it.
    """
    _incident(client, citizen_headers, authority_headers, lat=0.8, lng=0.5)

    data = client.get(
        "/api/v1/travel/route?start_lat=0.5&start_lng=0.5&end_lat=0.6&end_lng=0.6"
    ).get_json()["data"]
    assert data["hazard_count"] == 0


def test_an_incident_beyond_the_endpoint_is_ignored(
    client, citizen_headers, authority_headers, located
):
    """Past the destination is not "on the route" - the segment is clamped."""
    _incident(client, citizen_headers, authority_headers, lat=1.3, lng=1.3)

    data = client.get(
        "/api/v1/travel/route?start_lat=0.5&start_lng=0.5&end_lat=0.6&end_lng=0.6"
    ).get_json()["data"]
    assert data["hazard_count"] == 0


def test_a_wider_corridor_picks_up_more(client, citizen_headers, authority_headers, located):
    """~33km off the line: missed at the 20km default, found at 50km."""
    _incident(client, citizen_headers, authority_headers, lat=0.8, lng=0.5)

    narrow = client.get(
        "/api/v1/travel/route?start_lat=0.5&start_lng=0.5&end_lat=0.6&end_lng=0.6"
    ).get_json()["data"]
    wide = client.get(
        "/api/v1/travel/route?start_lat=0.5&start_lng=0.5&end_lat=0.6&end_lng=0.6&corridor=50000"
    ).get_json()["data"]

    assert narrow["hazard_count"] == 0
    assert wide["hazard_count"] == 1


def test_resolved_incidents_are_not_hazards(
    client, citizen_headers, authority_headers, located
):
    """A resolved incident is a repaired road."""
    incident = _incident(client, citizen_headers, authority_headers, lat=0.55, lng=0.55)
    client.patch(
        f"/api/v1/incidents/{incident['id']}",
        json={"status": "resolved"},
        headers=authority_headers,
    )

    data = client.get(
        "/api/v1/travel/route?start_lat=0.5&start_lng=0.5&end_lat=0.6&end_lng=0.6"
    ).get_json()["data"]
    assert data["hazard_count"] == 0


@pytest.mark.parametrize(
    "severity,expected",
    [("low", "LOW"), ("medium", "MODERATE"), ("high", "MODERATE"), ("critical", "HIGH")],
)
def test_danger_level_scales_with_severity(
    client, citizen_headers, authority_headers, located, severity, expected
):
    """One critical incident alone reaches HIGH: a closed road warrants it."""
    _incident(client, citizen_headers, authority_headers, severity=severity, lat=0.55, lng=0.55)

    data = client.get(
        "/api/v1/travel/route?start_lat=0.5&start_lng=0.5&end_lat=0.6&end_lng=0.6"
    ).get_json()["data"]
    assert data["danger_level"] == expected


def test_hazards_are_sorted_by_distance(client, citizen_headers, authority_headers, located):
    _incident(client, citizen_headers, authority_headers, lat=0.5, lng=0.53)
    _incident(client, citizen_headers, authority_headers, lat=0.55, lng=0.55)

    data = client.get(
        "/api/v1/travel/route?start_lat=0.5&start_lng=0.5&end_lat=0.6&end_lng=0.6"
    ).get_json()["data"]
    distances = [h["distance_from_route_metres"] for h in data["hazards"]]
    assert distances == sorted(distances)


def test_severity_breakdown_is_complete(client, db):
    from app.models import IncidentSeverity

    data = client.get(
        "/api/v1/travel/route?start_lat=0.5&start_lng=0.5&end_lat=0.6&end_lng=0.6"
    ).get_json()["data"]
    assert set(data["by_severity"]) == {m.value for m in IncidentSeverity}


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?start_lat=0.5",
        "?start_lat=0.5&start_lng=0.5&end_lat=0.6",
        "?start_lat=abc&start_lng=0.5&end_lat=0.6&end_lng=0.6",
    ],
)
def test_missing_or_invalid_coordinates_are_rejected(client, db, query):
    response = client.get(f"/api/v1/travel/route{query}")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
    "query",
    [
        "?start_lat=95&start_lng=0.5&end_lat=0.6&end_lng=0.6",
        "?start_lat=0.5&start_lng=200&end_lat=0.6&end_lng=0.6",
    ],
)
def test_out_of_range_coordinates_are_rejected(client, db, query):
    response = client.get(f"/api/v1/travel/route{query}")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_coordinates"


def test_an_absurdly_long_route_is_refused(client, db):
    """Scanning the planet for a 20,000km journey helps nobody."""
    response = client.get(
        "/api/v1/travel/route?start_lat=-89&start_lng=-179&end_lat=89&end_lng=179"
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "route_too_long"


def test_a_zero_length_route_does_not_divide_by_zero(client, db):
    """Start == end degenerates the segment; it must not crash."""
    response = client.get(
        "/api/v1/travel/route?start_lat=0.5&start_lng=0.5&end_lat=0.5&end_lng=0.5"
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["route"]["length_km"] == 0


def test_point_to_segment_distance_maths():
    """A point one degree off a west-east line is ~111km from it."""
    distance = travel_service._distance_to_segment_metres((1.0, 0.5), (0.0, 0.0), (0.0, 1.0))
    assert 110_000 < distance < 112_000


def test_point_beyond_the_segment_measures_to_the_endpoint():
    """Clamped, not projected onto the infinite line."""
    beyond = travel_service._distance_to_segment_metres((0.0, 2.0), (0.0, 0.0), (0.0, 1.0))
    assert 110_000 < beyond < 112_000


# ===========================================================================
# Chatbot
# ===========================================================================


def test_chat_requires_authentication(client, db, ai):
    assert client.post("/api/v1/chat", json={"message": "Is the road open?"}).status_code == 401


def test_chat_answers_from_records(client, citizen_headers, authority_headers, located, ai):
    _post_announcement(client, authority_headers, title="Highway closure notice")
    _incident(client, citizen_headers, authority_headers)

    response = client.post(
        "/api/v1/chat", json={"message": "Is the highway open?"}, headers=citizen_headers
    )
    assert response.status_code == 200

    data = response.get_json()["data"]
    assert data["answer"] == "The highway is blocked by a landslide."
    assert data["grounded"] is True
    assert data["provider"] == "fake"
    assert len(data["sources"]["announcements"]) == 1
    assert len(data["sources"]["incidents"]) == 1


def test_the_prompt_actually_contains_the_records(
    client, citizen_headers, authority_headers, located, ai
):
    """RAG that does not put the records in the prompt is not RAG."""
    _post_announcement(client, authority_headers, title="Highway closure notice")
    _incident(client, citizen_headers, authority_headers)

    client.post("/api/v1/chat", json={"message": "What is happening?"}, headers=citizen_headers)

    prompt = ai.prompts[0]
    assert "Highway closure notice" in prompt
    assert "Landslide blocking the highway" in prompt
    assert "What is happening?" in prompt


def test_the_prompt_fences_records_and_question_separately(
    client, citizen_headers, authority_headers, located, ai
):
    _post_announcement(client, authority_headers)
    client.post("/api/v1/chat", json={"message": "Anything nearby?"}, headers=citizen_headers)

    prompt = ai.prompts[0]
    assert "-----BEGIN RECORDS-----" in prompt
    assert "-----BEGIN QUESTION-----" in prompt
    assert "DATA" in prompt


def test_with_no_records_the_model_is_told_to_say_so(client, citizen_headers, db, ai):
    """Better a blank answer than an invented road closure."""
    response = client.post(
        "/api/v1/chat", json={"message": "Is the road open?"}, headers=citizen_headers
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["grounded"] is False

    prompt = ai.prompts[0]
    assert "NO records" in prompt
    assert "Do NOT answer from general knowledge" in prompt


def test_drafts_never_reach_the_model(client, citizen_headers, authority_headers, db, ai):
    """An unreviewed AI draft must not be fed back and repeated as official."""
    _post_announcement(
        client, authority_headers, is_draft=True, title="Unreviewed draft warning"
    )
    client.post("/api/v1/chat", json={"message": "Any warnings?"}, headers=citizen_headers)

    assert "Unreviewed draft warning" not in ai.prompts[0]


def test_district_scopes_the_context(client, citizen_headers, authority_headers, areas, ai):
    _post_announcement(
        client, authority_headers, title="Local notice here",
        district_id=str(areas["district"].id),
    )
    _post_announcement(
        client, authority_headers, title="Elsewhere notice here",
        district_id=str(areas["other_district"].id),
    )

    client.post(
        "/api/v1/chat",
        json={"message": "What is happening?", "district_id": str(areas["district"].id)},
        headers=citizen_headers,
    )

    prompt = ai.prompts[0]
    assert "Local notice here" in prompt
    assert "Elsewhere notice here" not in prompt


def test_resolved_incidents_are_not_in_the_context(
    client, citizen_headers, authority_headers, located, ai
):
    incident = _incident(client, citizen_headers, authority_headers)
    client.patch(
        f"/api/v1/incidents/{incident['id']}",
        json={"status": "resolved"},
        headers=authority_headers,
    )

    client.post("/api/v1/chat", json={"message": "Any problems?"}, headers=citizen_headers)
    assert "Landslide blocking the highway" not in ai.prompts[0]


def test_chat_without_a_provider_is_503(client, citizen_headers, db):
    """No provider configured is a supported state, not a crash."""
    response = client.post(
        "/api/v1/chat", json={"message": "Is the road open?"}, headers=citizen_headers
    )
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "ai_unavailable"


def test_a_provider_failure_is_503_not_500(client, citizen_headers, app, db):
    with app.app_context():
        set_ai_service(FakeAI(fail=True))
    response = client.post(
        "/api/v1/chat", json={"message": "Is the road open?"}, headers=citizen_headers
    )
    assert response.status_code == 503


@pytest.mark.parametrize("message", ["", "   ", None, 42, []])
def test_an_empty_message_is_rejected(client, citizen_headers, db, ai, message):
    response = client.post("/api/v1/chat", json={"message": message}, headers=citizen_headers)
    assert response.status_code == 400
    assert "message" in response.get_json()["error"]["details"]


def test_an_overlong_message_is_rejected(client, citizen_headers, db, ai):
    response = client.post(
        "/api/v1/chat", json={"message": "x" * 1001}, headers=citizen_headers
    )
    assert response.status_code == 400


def test_the_answer_is_length_capped(client, citizen_headers, app, db):
    with app.app_context():
        set_ai_service(FakeAI(answer="y" * 10000))
    response = client.post(
        "/api/v1/chat", json={"message": "Tell me everything"}, headers=citizen_headers
    )
    assert len(response.get_json()["data"]["answer"]) == chat_service.MAX_ANSWER_LENGTH


def test_context_preview_spends_no_model_call(
    client, citizen_headers, authority_headers, located, ai
):
    """The retrieval half, inspectable without a generation."""
    _post_announcement(client, authority_headers)
    _incident(client, citizen_headers, authority_headers)

    response = client.get("/api/v1/chat/context", headers=citizen_headers)
    assert response.status_code == 200

    data = response.get_json()["data"]
    assert data["announcement_count"] == 1
    assert data["incident_count"] == 1
    assert data["grounded"] is True
    assert ai.prompts == []


def test_context_preview_requires_authentication(client, db):
    assert client.get("/api/v1/chat/context").status_code == 401


# ===========================================================================
# Earlier phases still work
# ===========================================================================


def test_previous_endpoints_are_unaffected(client, db):
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/reports").status_code == 200
    assert client.get("/api/v1/incidents").status_code == 200
    assert client.get("/api/v1/projects").status_code == 200
    assert client.get("/api/v1/analytics/overview").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401
