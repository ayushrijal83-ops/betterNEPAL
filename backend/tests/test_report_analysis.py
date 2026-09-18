"""Phase 10: applying AI suggestions to reports.

The provider is replaced with an in-process fake via ``set_ai_service`` - no
network call, no SDK. Spatial filtering runs for real through Phase 6's
approximate (non-PostGIS) path, so these tests exercise the actual intersection
of proximity and semantics rather than mocking both halves.

All coordinates and areas are SYNTHETIC test data, not Nepal.
"""
import uuid

import pytest
import sqlalchemy as sa

from app.models import District, Municipality, Report, ReportStatus
from app.services import report_analysis_service
from app.services.ai_service import (
    AIUnavailable,
    BaseAIService,
    DuplicateVerdict,
    ReportAnalysis,
    set_ai_service,
)

PASSWORD = "correct-horse-battery"

VALID_REPORT = {
    "title": "Deep pothole on the main road",
    "description": "A large pothole has opened and is dangerous for motorcycles.",
    "category": "road_damage",
    "lat": 0.5,
    "lng": 0.5,
}


class FakeAI(BaseAIService):
    """An in-process provider. Records calls; never touches a network."""

    name = "fake"

    def __init__(self, analysis=None, verdict=None, fail=False):
        self._analysis = analysis or ReportAnalysis(
            suggested_category="road_damage",
            confidence=0.92,
            severity="high",
            keywords=["pothole", "asphalt"],
            summary="A deep pothole in the carriageway.",
            model="fake-model",
        )
        self._verdict = verdict or DuplicateVerdict(
            is_duplicate=True, confidence=0.9, reasoning="Same pothole.", model="fake-model"
        )
        self._fail = fail
        self.analyze_calls = 0
        self.compare_calls = 0

    @property
    def available(self):
        return not self._fail

    def analyze_report_text(self, title, description):
        self.analyze_calls += 1
        if self._fail:
            raise AIUnavailable("The AI provider could not be reached.")
        return self._analysis

    def compare_reports(self, report_a, report_b):
        self.compare_calls += 1
        if self._fail:
            raise AIUnavailable("The AI provider could not be reached.")
        return self._verdict


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
    """Install the fake provider for the duration of a test."""
    fake = FakeAI()
    with app.app_context():
        set_ai_service(fake)
    return fake


def _make_report(client, headers, **overrides):
    response = client.post(
        "/api/v1/reports", json={**VALID_REPORT, **overrides}, headers=headers
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["report"]["id"]


# --- analysis --------------------------------------------------------------


def test_analysis_stores_the_suggestion(client, citizen_headers, authority_headers, located, ai, db):
    report_id = _make_report(client, citizen_headers)
    response = client.post(f"/api/v1/reports/{report_id}/analyze", headers=authority_headers)
    assert response.status_code == 200

    analysis = response.get_json()["data"]["analysis"]
    assert analysis["suggested_category"] == "road_damage"
    assert analysis["confidence"] == 0.92
    assert analysis["keywords"] == ["pothole", "asphalt"]
    assert analysis["provider"] == "fake"
    assert analysis["analyzed_at"]

    stored = db.session.get(Report, uuid.UUID(report_id)).ai_metadata
    assert stored["analysis"]["suggested_category"] == "road_damage"


def test_analysis_uses_the_locked_success_envelope(client, citizen_headers, authority_headers, located, ai):
    report_id = _make_report(client, citizen_headers)
    body = client.post(
        f"/api/v1/reports/{report_id}/analyze", headers=authority_headers
    ).get_json()
    assert set(body) == {"status", "data"}
    assert body["status"] == "success"


def test_analysis_does_not_change_the_reports_category(
    client, citizen_headers, authority_headers, located, app, db
):
    """AI understands; the database decides. A suggestion is not a verdict."""
    with app.app_context():
        set_ai_service(FakeAI(analysis=ReportAnalysis(suggested_category="water_leak")))

    report_id = _make_report(client, citizen_headers, category="road_damage")
    response = client.post(f"/api/v1/reports/{report_id}/analyze", headers=authority_headers)

    assert response.get_json()["data"]["applied_to_report"] is False
    assert response.get_json()["data"]["current_category"] == "road_damage"

    report = db.session.get(Report, uuid.UUID(report_id))
    assert report.category.value == "road_damage"
    assert report.ai_metadata["analysis"]["suggested_category"] == "water_leak"


def test_analysis_does_not_change_the_reports_status(
    client, citizen_headers, authority_headers, located, ai, db
):
    report_id = _make_report(client, citizen_headers)
    client.post(f"/api/v1/reports/{report_id}/analyze", headers=authority_headers)
    assert db.session.get(Report, uuid.UUID(report_id)).status == ReportStatus.SUBMITTED


def test_reanalysis_overwrites_the_previous_analysis(
    client, citizen_headers, authority_headers, located, app, db
):
    report_id = _make_report(client, citizen_headers)
    with app.app_context():
        set_ai_service(FakeAI(analysis=ReportAnalysis(suggested_category="road_damage")))
    client.post(f"/api/v1/reports/{report_id}/analyze", headers=authority_headers)

    with app.app_context():
        set_ai_service(FakeAI(analysis=ReportAnalysis(suggested_category="water_leak")))
    client.post(f"/api/v1/reports/{report_id}/analyze", headers=authority_headers)

    report = db.session.get(Report, uuid.UUID(report_id))
    assert report.ai_metadata["analysis"]["suggested_category"] == "water_leak"


def test_analysis_without_a_provider_is_a_503(
    client, citizen_headers, authority_headers, located, db
):
    """No key configured is a supported state, not a crash."""
    report_id = _make_report(client, citizen_headers)
    response = client.post(f"/api/v1/reports/{report_id}/analyze", headers=authority_headers)
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "ai_unavailable"
    assert db.session.get(Report, uuid.UUID(report_id)).ai_metadata is None


def test_analysis_of_an_unknown_report_is_404(client, authority_headers, ai, db):
    response = client.post(
        f"/api/v1/reports/{uuid.uuid4()}/analyze", headers=authority_headers
    )
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "report_not_found"


def test_analysis_of_an_invalid_id_is_400(client, authority_headers, ai, db):
    response = client.post("/api/v1/reports/not-a-uuid/analyze", headers=authority_headers)
    assert response.status_code == 400


# --- authorization ---------------------------------------------------------


def test_a_citizen_cannot_trigger_analysis(client, citizen_headers, located, ai, db):
    """Every call costs money to a third party; a public endpoint would be a
    denial-of-wallet."""
    report_id = _make_report(client, citizen_headers)
    response = client.post(f"/api/v1/reports/{report_id}/analyze", headers=citizen_headers)
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "permission_denied"


def test_analysis_refused_for_a_citizen_calls_no_model(client, citizen_headers, located, ai):
    report_id = _make_report(client, citizen_headers)
    client.post(f"/api/v1/reports/{report_id}/analyze", headers=citizen_headers)
    assert ai.analyze_calls == 0


def test_unauthenticated_analysis_is_401(client, citizen_headers, located, ai):
    report_id = _make_report(client, citizen_headers)
    assert client.post(f"/api/v1/reports/{report_id}/analyze").status_code == 401


def test_admin_can_trigger_analysis(client, citizen_headers, admin_headers, located, ai):
    report_id = _make_report(client, citizen_headers)
    assert client.post(
        f"/api/v1/reports/{report_id}/analyze", headers=admin_headers
    ).status_code == 200


def test_a_citizen_cannot_list_potential_duplicates(client, citizen_headers, located, ai):
    report_id = _make_report(client, citizen_headers)
    response = client.get(
        f"/api/v1/reports/{report_id}/potential-duplicates", headers=citizen_headers
    )
    assert response.status_code == 403


def test_a_citizen_cannot_mark_a_duplicate(client, citizen_headers, located, ai):
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers, lat=0.5001)
    response = client.post(
        f"/api/v1/reports/{second}/mark-duplicate",
        json={"duplicate_of_id": first},
        headers=citizen_headers,
    )
    assert response.status_code == 403


# --- duplicate detection ---------------------------------------------------


def test_spatial_and_semantic_filters_intersect(
    client, citizen_headers, authority_headers, located, ai
):
    """Near AND semantically matching. The AI only sees what GIS shortlisted."""
    anchor = _make_report(client, citizen_headers)
    _make_report(client, citizen_headers, lat=0.5001)  # ~11m: within 100m
    _make_report(client, citizen_headers, lat=0.6)     # ~11km: far outside

    response = client.get(
        f"/api/v1/reports/{anchor}/potential-duplicates", headers=authority_headers
    )
    assert response.status_code == 200

    data = response.get_json()["data"]
    assert data["nearby_count"] == 1       # GIS shortlisted one
    assert data["compared_count"] == 1     # the model saw only that one
    assert data["count"] == 1
    assert ai.compare_calls == 1


def test_a_distant_report_never_reaches_the_model(
    client, citizen_headers, authority_headers, located, ai
):
    """Proximity is cheap and deterministic; the model is neither."""
    anchor = _make_report(client, citizen_headers)
    _make_report(client, citizen_headers, lat=0.6)

    client.get(f"/api/v1/reports/{anchor}/potential-duplicates", headers=authority_headers)
    assert ai.compare_calls == 0


def test_a_nearby_report_the_ai_rejects_is_not_a_candidate(
    client, citizen_headers, authority_headers, located, app
):
    with app.app_context():
        set_ai_service(FakeAI(verdict=DuplicateVerdict(is_duplicate=False, confidence=0.1)))

    anchor = _make_report(client, citizen_headers)
    _make_report(client, citizen_headers, lat=0.5001)

    data = client.get(
        f"/api/v1/reports/{anchor}/potential-duplicates", headers=authority_headers
    ).get_json()["data"]
    assert data["nearby_count"] == 1
    assert data["count"] == 0


def test_a_low_confidence_match_is_filtered_out(
    client, citizen_headers, authority_headers, located, app
):
    """Below the threshold, a model's 'yes' is not worth a human's time."""
    with app.app_context():
        set_ai_service(FakeAI(verdict=DuplicateVerdict(is_duplicate=True, confidence=0.2)))

    anchor = _make_report(client, citizen_headers)
    _make_report(client, citizen_headers, lat=0.5001)

    data = client.get(
        f"/api/v1/reports/{anchor}/potential-duplicates", headers=authority_headers
    ).get_json()["data"]
    assert data["count"] == 0


def test_a_report_is_never_its_own_duplicate_candidate(
    client, citizen_headers, authority_headers, located, ai
):
    anchor = _make_report(client, citizen_headers)
    data = client.get(
        f"/api/v1/reports/{anchor}/potential-duplicates", headers=authority_headers
    ).get_json()["data"]
    assert data["nearby_count"] == 0
    assert all(c["report"]["id"] != anchor for c in data["candidates"])


def test_candidates_are_sorted_by_confidence(
    client, citizen_headers, authority_headers, located, app
):
    with app.app_context():
        set_ai_service(FakeAI(verdict=DuplicateVerdict(is_duplicate=True, confidence=0.8)))

    anchor = _make_report(client, citizen_headers)
    _make_report(client, citizen_headers, lat=0.5001)
    _make_report(client, citizen_headers, lat=0.5002)

    data = client.get(
        f"/api/v1/reports/{anchor}/potential-duplicates", headers=authority_headers
    ).get_json()["data"]
    scores = [c["ai"]["confidence"] for c in data["candidates"]]
    assert scores == sorted(scores, reverse=True)


def test_candidates_carry_distance_and_reasoning(
    client, citizen_headers, authority_headers, located, ai
):
    anchor = _make_report(client, citizen_headers)
    _make_report(client, citizen_headers, lat=0.5001)

    candidate = client.get(
        f"/api/v1/reports/{anchor}/potential-duplicates", headers=authority_headers
    ).get_json()["data"]["candidates"][0]

    assert candidate["distance_meters"] < 100
    assert candidate["ai"]["reasoning"] == "Same pothole."
    assert candidate["ai"]["confidence"] == 0.9


def test_the_response_reports_which_proximity_method_was_used(
    client, citizen_headers, authority_headers, located, ai
):
    """Phase 6's honesty flag must survive into this layer."""
    anchor = _make_report(client, citizen_headers)
    data = client.get(
        f"/api/v1/reports/{anchor}/potential-duplicates", headers=authority_headers
    ).get_json()["data"]
    assert data["proximity_method"] == "approximate"


def test_a_custom_radius_widens_the_search(
    client, citizen_headers, authority_headers, located, ai
):
    anchor = _make_report(client, citizen_headers)
    _make_report(client, citizen_headers, lat=0.505)  # ~550m

    narrow = client.get(
        f"/api/v1/reports/{anchor}/potential-duplicates", headers=authority_headers
    ).get_json()["data"]
    wide = client.get(
        f"/api/v1/reports/{anchor}/potential-duplicates?radius=2000",
        headers=authority_headers,
    ).get_json()["data"]

    assert narrow["nearby_count"] == 0
    assert wide["nearby_count"] == 1


@pytest.mark.parametrize("radius", ["abc", "-5", "0"])
def test_an_invalid_radius_is_rejected(
    client, citizen_headers, authority_headers, located, ai, radius
):
    anchor = _make_report(client, citizen_headers)
    response = client.get(
        f"/api/v1/reports/{anchor}/potential-duplicates?radius={radius}",
        headers=authority_headers,
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_radius"


def test_duplicate_search_degrades_to_proximity_when_the_ai_fails(
    client, citizen_headers, authority_headers, located, app
):
    """'Here is what is nearby' is still useful without the model."""
    with app.app_context():
        set_ai_service(FakeAI(fail=True))

    anchor = _make_report(client, citizen_headers)
    _make_report(client, citizen_headers, lat=0.5001)

    response = client.get(
        f"/api/v1/reports/{anchor}/potential-duplicates", headers=authority_headers
    )
    assert response.status_code == 200

    data = response.get_json()["data"]
    assert data["ai_available"] is False
    assert data["nearby_count"] == 1
    assert data["candidates"] == []


def test_an_already_merged_report_is_not_offered_as_a_master(
    client, citizen_headers, authority_headers, located, ai
):
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers, lat=0.5001)
    third = _make_report(client, citizen_headers, lat=0.5002)

    client.post(
        f"/api/v1/reports/{second}/mark-duplicate",
        json={"duplicate_of_id": first},
        headers=authority_headers,
    )

    data = client.get(
        f"/api/v1/reports/{third}/potential-duplicates", headers=authority_headers
    ).get_json()["data"]
    assert all(c["report"]["id"] != second for c in data["candidates"])


# --- marking duplicates ----------------------------------------------------


def test_marking_a_duplicate_sets_the_link_and_rejects(
    client, citizen_headers, authority_headers, located, ai, db
):
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers, lat=0.5001)

    response = client.post(
        f"/api/v1/reports/{second}/mark-duplicate",
        json={"duplicate_of_id": first, "reason": "Same pothole as the earlier report."},
        headers=authority_headers,
    )
    assert response.status_code == 200

    payload = response.get_json()["data"]["report"]
    assert payload["duplicate_of_id"] == first
    assert payload["is_duplicate"] is True
    assert payload["status"] == "rejected"

    report = db.session.get(Report, uuid.UUID(second))
    assert report.duplicate_of_id == uuid.UUID(first)
    assert report.status == ReportStatus.REJECTED


def test_the_decision_records_who_made_it(
    client, citizen_headers, authority, authority_headers, located, ai, db
):
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers, lat=0.5001)
    client.post(
        f"/api/v1/reports/{second}/mark-duplicate",
        json={"duplicate_of_id": first},
        headers=authority_headers,
    )

    decision = db.session.get(Report, uuid.UUID(second)).ai_metadata["duplicate_decision"]
    assert decision["decided_by_id"] == str(authority.id)
    assert decision["duplicate_of_id"] == first
    assert decision["decided_at"]


def test_the_master_report_is_untouched(
    client, citizen_headers, authority_headers, located, ai, db
):
    """Five people reporting one pothole is itself a signal; nothing is deleted."""
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers, lat=0.5001)
    client.post(
        f"/api/v1/reports/{second}/mark-duplicate",
        json={"duplicate_of_id": first},
        headers=authority_headers,
    )

    master = db.session.get(Report, uuid.UUID(first))
    assert master.status == ReportStatus.SUBMITTED
    assert master.duplicate_of_id is None
    assert len(master.duplicates) == 1


def test_a_report_cannot_be_its_own_duplicate(
    client, citizen_headers, authority_headers, located, ai
):
    report_id = _make_report(client, citizen_headers)
    response = client.post(
        f"/api/v1/reports/{report_id}/mark-duplicate",
        json={"duplicate_of_id": report_id},
        headers=authority_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "self_duplicate"


def test_the_database_also_refuses_a_self_duplicate(db, client, citizen_headers, located):
    """Defence in depth: the CHECK constraint, not just the service."""
    report_id = _make_report(client, citizen_headers)
    report = db.session.get(Report, uuid.UUID(report_id))
    report.duplicate_of_id = report.id
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_a_report_cannot_be_marked_twice(
    client, citizen_headers, authority_headers, located, ai
):
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers, lat=0.5001)
    third = _make_report(client, citizen_headers, lat=0.5002)

    client.post(
        f"/api/v1/reports/{second}/mark-duplicate",
        json={"duplicate_of_id": first},
        headers=authority_headers,
    )
    response = client.post(
        f"/api/v1/reports/{second}/mark-duplicate",
        json={"duplicate_of_id": third},
        headers=authority_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "already_duplicate"


def test_chains_of_duplicates_are_refused(
    client, citizen_headers, authority_headers, located, ai
):
    """A -> B -> C turns a flat relation into a graph every reader must walk."""
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers, lat=0.5001)
    third = _make_report(client, citizen_headers, lat=0.5002)

    client.post(
        f"/api/v1/reports/{second}/mark-duplicate",
        json={"duplicate_of_id": first},
        headers=authority_headers,
    )
    response = client.post(
        f"/api/v1/reports/{third}/mark-duplicate",
        json={"duplicate_of_id": second},
        headers=authority_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "master_is_duplicate"
    assert response.get_json()["error"]["details"]["merge_into_id"] == first


def test_a_verified_report_cannot_be_marked_duplicate(
    client, citizen_headers, authority_headers, located, ai
):
    """Rejecting it would leave its incident standing on a rejected report."""
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers, lat=0.5001)
    client.post(
        "/api/v1/incidents/from-report",
        json={"report_id": second},
        headers=authority_headers,
    )

    response = client.post(
        f"/api/v1/reports/{second}/mark-duplicate",
        json={"duplicate_of_id": first},
        headers=authority_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "report_already_verified"


def test_an_already_rejected_report_cannot_be_marked_duplicate(
    client, citizen_headers, authority_headers, located, ai
):
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers, lat=0.5001)
    client.patch(
        f"/api/v1/reports/{second}/status",
        json={"status": "rejected"},
        headers=authority_headers,
    )

    response = client.post(
        f"/api/v1/reports/{second}/mark-duplicate",
        json={"duplicate_of_id": first},
        headers=authority_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "report_already_rejected"


def test_marking_against_an_unknown_master_is_404(
    client, citizen_headers, authority_headers, located, ai
):
    report_id = _make_report(client, citizen_headers)
    response = client.post(
        f"/api/v1/reports/{report_id}/mark-duplicate",
        json={"duplicate_of_id": str(uuid.uuid4())},
        headers=authority_headers,
    )
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "master_report_not_found"


def test_marking_requires_a_master_id(client, citizen_headers, authority_headers, located, ai):
    report_id = _make_report(client, citizen_headers)
    response = client.post(
        f"/api/v1/reports/{report_id}/mark-duplicate", json={}, headers=authority_headers
    )
    assert response.status_code == 400
    assert "duplicate_of_id" in response.get_json()["error"]["details"]


# --- unmarking -------------------------------------------------------------


def test_unmarking_returns_the_report_to_review(
    client, citizen_headers, authority_headers, located, ai, db
):
    """Humans mis-merge; a wrongly folded report must not be stuck rejected."""
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers, lat=0.5001)
    client.post(
        f"/api/v1/reports/{second}/mark-duplicate",
        json={"duplicate_of_id": first},
        headers=authority_headers,
    )

    response = client.post(
        f"/api/v1/reports/{second}/unmark-duplicate", headers=authority_headers
    )
    assert response.status_code == 200

    report = db.session.get(Report, uuid.UUID(second))
    assert report.duplicate_of_id is None
    assert report.status == ReportStatus.UNDER_REVIEW
    assert "duplicate_decision" not in (report.ai_metadata or {})


def test_unmarking_a_non_duplicate_is_refused(
    client, citizen_headers, authority_headers, located, ai
):
    report_id = _make_report(client, citizen_headers)
    response = client.post(
        f"/api/v1/reports/{report_id}/unmark-duplicate", headers=authority_headers
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "not_a_duplicate"


def test_unmarking_preserves_an_earlier_analysis(
    client, citizen_headers, authority_headers, located, ai, db
):
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers, lat=0.5001)

    client.post(f"/api/v1/reports/{second}/analyze", headers=authority_headers)
    client.post(
        f"/api/v1/reports/{second}/mark-duplicate",
        json={"duplicate_of_id": first},
        headers=authority_headers,
    )
    client.post(f"/api/v1/reports/{second}/unmark-duplicate", headers=authority_headers)

    metadata = db.session.get(Report, uuid.UUID(second)).ai_metadata
    assert metadata["analysis"]["suggested_category"] == "road_damage"


# --- listing duplicates ----------------------------------------------------


def test_listing_duplicates_of_a_report_is_public(
    client, citizen_headers, authority_headers, located, ai
):
    """How many people independently reported the same problem is public interest."""
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers, lat=0.5001)
    client.post(
        f"/api/v1/reports/{second}/mark-duplicate",
        json={"duplicate_of_id": first},
        headers=authority_headers,
    )

    response = client.get(f"/api/v1/reports/{first}/duplicates")
    assert response.status_code == 200
    assert response.get_json()["data"]["count"] == 1
    assert response.get_json()["data"]["duplicates"][0]["id"] == second


def test_a_report_with_no_duplicates_lists_none(client, citizen_headers, located, ai):
    report_id = _make_report(client, citizen_headers)
    assert client.get(f"/api/v1/reports/{report_id}/duplicates").get_json()["data"][
        "count"
    ] == 0


# --- model and database ----------------------------------------------------


def test_deleting_a_master_preserves_its_duplicates(
    client, citizen_headers, authority_headers, located, ai, db
):
    """SET NULL: the folded reports revert to standing on their own."""
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers, lat=0.5001)
    client.post(
        f"/api/v1/reports/{second}/mark-duplicate",
        json={"duplicate_of_id": first},
        headers=authority_headers,
    )

    db.session.delete(db.session.get(Report, uuid.UUID(first)))
    db.session.commit()

    survivor = db.session.get(Report, uuid.UUID(second))
    assert survivor is not None
    assert survivor.duplicate_of_id is None


def test_the_relationship_is_navigable(
    client, citizen_headers, authority_headers, located, ai, db
):
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers, lat=0.5001)
    client.post(
        f"/api/v1/reports/{second}/mark-duplicate",
        json={"duplicate_of_id": first},
        headers=authority_headers,
    )

    master = db.session.get(Report, uuid.UUID(first))
    child = db.session.get(Report, uuid.UUID(second))
    assert child.duplicate_of is master
    assert child in master.duplicates


def test_ai_metadata_survives_a_round_trip(db, client, citizen_headers, located):
    """JSON on SQLite is text; nested structures must come back intact."""
    report_id = _make_report(client, citizen_headers)
    report = db.session.get(Report, uuid.UUID(report_id))
    report.ai_metadata = {"analysis": {"keywords": ["a", "b"], "confidence": 0.5}}
    db.session.commit()
    db.session.expire_all()

    reloaded = db.session.get(Report, uuid.UUID(report_id))
    assert reloaded.ai_metadata["analysis"]["keywords"] == ["a", "b"]
    assert reloaded.ai_metadata["analysis"]["confidence"] == 0.5


def test_reports_default_to_no_ai_metadata(client, citizen_headers, located, db):
    report_id = _make_report(client, citizen_headers)
    report = db.session.get(Report, uuid.UUID(report_id))
    assert report.ai_metadata is None
    assert report.duplicate_of_id is None


# --- statistics ------------------------------------------------------------


def test_statistics_report_provider_availability(client, db):
    data = client.get("/api/v1/reports/ai-statistics").get_json()["data"]
    assert data["ai_available"] is False
    assert data["provider"] == "null"
    assert data["total_reports"] == 0


def test_statistics_count_analysed_and_duplicate_reports(
    client, citizen_headers, authority_headers, located, ai
):
    first = _make_report(client, citizen_headers)
    second = _make_report(client, citizen_headers, lat=0.5001)

    client.post(f"/api/v1/reports/{first}/analyze", headers=authority_headers)
    client.post(
        f"/api/v1/reports/{second}/mark-duplicate",
        json={"duplicate_of_id": first},
        headers=authority_headers,
    )

    data = client.get("/api/v1/reports/ai-statistics").get_json()["data"]
    assert data["total_reports"] == 2
    assert data["analyzed_reports"] == 2  # the merge also writes metadata
    assert data["duplicate_reports"] == 1


def test_the_statistics_path_is_not_shadowed_by_the_id_route(client, db):
    assert client.get("/api/v1/reports/ai-statistics").status_code == 200


# --- earlier phases still work ---------------------------------------------


def test_previous_endpoints_are_unaffected(client, db):
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/reports").status_code == 200
    assert client.get("/api/v1/reports/statistics").status_code == 200
    assert client.get("/api/v1/incidents").status_code == 200
    assert client.get("/api/v1/projects").status_code == 200
    assert client.get("/api/v1/media/statistics").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401
