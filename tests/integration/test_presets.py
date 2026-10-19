from tests.conftest import Tenant
from tests.helpers import image_bytes, pdf_bytes, upload_file


def create_preset(tenant: Tenant, **body: object) -> dict:  # type: ignore[type-arg]
    resp = tenant.post("/v1/presets", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


def test_lists_builtin_defaults_for_every_media_type(tenant: Tenant) -> None:
    presets = tenant.get("/v1/presets").json()
    assert {(p["media_type"], p["name"], p["builtin"]) for p in presets} == {
        ("image", "default", True),
        ("video", "default", True),
        ("document", "default", True),
    }
    image = next(p for p in presets if p["media_type"] == "image")
    assert [v["name"] for v in image["config"]["variants"]] == ["thumb", "medium", "large"]
    assert image["id"] is None


def test_custom_image_preset_drives_processing(tenant: Tenant) -> None:
    preset = create_preset(
        tenant,
        name="square",
        media_type="image",
        config={
            "variants": [{"name": "sq", "width": 120, "height": 120, "fit": "cover"}],
            "formats": ["webp"],
            "quality": 70,
        },
    )
    assert preset["builtin"] is False and preset["id"]
    assert any(p["name"] == "square" for p in tenant.get("/v1/presets").json())

    file_id = upload_file(tenant, "p.png", "image/png", image_bytes("PNG", (500, 300)), "square")
    body = tenant.get(f"/v1/files/{file_id}").json()
    assert body["preset"] == "square"
    assert [(v["name"], v["width"], v["height"]) for v in body["variants"]] == [
        ("sq.webp", 120, 120)
    ]


def test_preset_named_default_overrides_builtin(tenant: Tenant) -> None:
    create_preset(tenant, name="default", media_type="image", config={"formats": ["webp"]})
    presets = tenant.get("/v1/presets").json()
    images = [p for p in presets if p["media_type"] == "image"]
    assert len(images) == 1 and images[0]["builtin"] is False
    file_id = upload_file(tenant, "p.jpg", "image/jpeg", image_bytes("JPEG"))
    names = [v["name"] for v in tenant.get(f"/v1/files/{file_id}").json()["variants"]]
    assert names == ["large.webp", "medium.webp", "thumb.webp"]


def test_video_and_document_presets_validate_their_own_keys(tenant: Tenant) -> None:
    create_preset(tenant, name="lite", media_type="video", config={"max_height": 480, "crf": 28})
    create_preset(tenant, name="lite", media_type="document", config={"preview": False})
    file_id = upload_file(tenant, "a.pdf", "application/pdf", pdf_bytes(), preset="lite")
    names = [v["name"] for v in tenant.get(f"/v1/files/{file_id}").json()["variants"]]
    assert names == ["text.txt"]


def test_invalid_presets_are_rejected(tenant: Tenant) -> None:
    bad = [
        {"name": "x", "media_type": "image", "config": {"variants": []}},
        {"name": "x", "media_type": "image", "config": {"quality": 500}},
        {"name": "x", "media_type": "image", "config": {"unknown": 1}},
        {"name": "x", "media_type": "image", "config": {"variants": [{"name": "a"}]}},
        {"name": "x", "media_type": "video", "config": {"variants": []}},
        {"name": "Bad Name!", "media_type": "image", "config": {}},
        {"name": "x", "media_type": "audio", "config": {}},
    ]
    for body in bad:
        resp = tenant.post("/v1/presets", json=body)
        assert resp.status_code == 422, body
        assert resp.json()["error"]["code"] == "validation_error"


def test_duplicate_and_delete(tenant: Tenant, other_tenant: Tenant) -> None:
    preset = create_preset(tenant, name="p1", media_type="image", config={})
    dup = tenant.post("/v1/presets", json={"name": "p1", "media_type": "image", "config": {}})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "preset_exists"
    # same name for another media type or another project is fine
    create_preset(tenant, name="p1", media_type="document", config={})
    create_preset(other_tenant, name="p1", media_type="image", config={})

    assert other_tenant.delete(f"/v1/presets/{preset['id']}").status_code == 404
    assert tenant.delete(f"/v1/presets/{preset['id']}").status_code == 204
    assert tenant.delete(f"/v1/presets/{preset['id']}").status_code == 404
    resp = tenant.post(
        "/v1/uploads",
        json={"filename": "a.jpg", "content_type": "image/jpeg", "size": 5, "preset": "p1"},
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "unknown_preset"


def test_presets_are_private_to_their_project(tenant: Tenant, other_tenant: Tenant) -> None:
    create_preset(tenant, name="secret-recipe", media_type="image", config={})
    assert all(p["name"] != "secret-recipe" for p in other_tenant.get("/v1/presets").json())
    resp = other_tenant.post(
        "/v1/uploads",
        json={
            "filename": "a.jpg",
            "content_type": "image/jpeg",
            "size": 5,
            "preset": "secret-recipe",
        },
    )
    assert resp.status_code == 422


def test_job_keeps_config_snapshot_when_preset_is_deleted(tenant: Tenant) -> None:
    from sqlalchemy import select

    from app.db.models import Job
    from app.db.session import sync_session

    preset = create_preset(
        tenant, name="tiny", media_type="image", config={"variants": [{"name": "t", "width": 40}]}
    )
    upload_file(tenant, "a.jpg", "image/jpeg", image_bytes("JPEG"), preset="tiny")
    assert tenant.delete(f"/v1/presets/{preset['id']}").status_code == 204
    with sync_session() as s:
        job = s.scalars(select(Job)).one()
        assert [v["name"] for v in job.config["variants"]] == ["t"]
