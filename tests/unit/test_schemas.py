"""Unit tests for HTTP request validation."""

import pytest
from pydantic import ValidationError

from app.api.schemas import TenantCreate, TenantUpdate


def test_tenant_create_accepts_valid_input_and_strips_name() -> None:
    body = TenantCreate(slug="acme-corp", name="  Acme Corp  ")
    assert body.slug == "acme-corp"
    assert body.name == "Acme Corp"


@pytest.mark.parametrize("slug", ["ab", "Acme", "acme_corp", "-acme", "acme-", "acme\n"])
def test_tenant_create_rejects_invalid_slug(slug: str) -> None:
    with pytest.raises(ValidationError, match="slug"):
        TenantCreate(slug=slug, name="Acme")


@pytest.mark.parametrize("name", ["", "   "])
def test_tenant_create_rejects_blank_name(name: str) -> None:
    with pytest.raises(ValidationError, match="name"):
        TenantCreate(slug="acme", name=name)


def test_tenant_create_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        TenantCreate.model_validate({"slug": "acme", "name": "Acme", "plan": "gold"})


def test_tenant_update_requires_version() -> None:
    with pytest.raises(ValidationError, match="version"):
        TenantUpdate.model_validate({"name": "Acme"})


def test_tenant_update_rejects_non_positive_version() -> None:
    with pytest.raises(ValidationError, match="version"):
        TenantUpdate(name="Acme", version=0)


def test_tenant_update_requires_a_change() -> None:
    with pytest.raises(ValidationError, match="at least one field"):
        TenantUpdate(version=1)
