from __future__ import annotations

import stat
import subprocess

import pytest

from datalab_chat.profiles import (
    EnvProfileCatalog,
    ProfileDraft,
    ProfileFormat,
    ProfileNotFound,
    ProfileValidationError,
)


def draft(
    name: str,
    provider_format: ProfileFormat,
    token: str | None,
    *,
    base_url: str = "https://llm.bank.local/v1",
    model_id: str = "risk-model",
) -> ProfileDraft:
    return ProfileDraft(
        display_name=name,
        provider_format=provider_format,
        base_url=base_url,
        token=token,
        model_id=model_id,
    )


def test_user_can_save_multiple_profiles_without_exposing_tokens(tmp_path):
    env_path = tmp_path / ".env"
    catalog = EnvProfileCatalog(env_path)

    giga = catalog.create(draft("Giga PROD", ProfileFormat.GIGACHAT, "giga-secret"))
    openai = catalog.create(
        draft(
            "OpenAI TEST",
            ProfileFormat.OPENAI,
            "openai-secret",
            base_url="http://127.0.0.1:11434/v1",
            model_id="local-risk-model",
        )
    )

    assert [profile.display_name for profile in catalog.list()] == [
        "Giga PROD",
        "OpenAI TEST",
    ]
    assert giga.to_public_dict() == {
        "id": giga.id,
        "display_name": "Giga PROD",
        "format": "gigachat",
        "base_url": "https://llm.bank.local/v1",
        "model_id": "risk-model",
        "has_token": True,
    }
    assert "token" not in giga.to_public_dict()
    assert catalog.resolve(openai.id).token == "openai-secret"

    reloaded = EnvProfileCatalog(env_path)
    assert reloaded.resolve(giga.id).token == "giga-secret"
    assert reloaded.resolve(openai.id).model_id == "local-risk-model"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_blank_token_keeps_the_previous_secret_during_edit(tmp_path):
    catalog = EnvProfileCatalog(tmp_path / ".env")
    profile = catalog.create(draft("Primary", ProfileFormat.GIGACHAT, "original"))

    updated = catalog.update(
        profile.id,
        draft(
            "Primary renamed",
            ProfileFormat.OPENAI,
            "",
            base_url="https://gateway.local/openai/v1",
            model_id="new-id",
        ),
    )

    assert updated.display_name == "Primary renamed"
    connection = catalog.resolve(profile.id)
    assert connection.token == "original"
    assert connection.provider_format is ProfileFormat.OPENAI
    assert connection.model_id == "new-id"


def test_profile_revision_is_stable_on_reload_and_rotates_on_secret_update(tmp_path):
    env_path = tmp_path / ".env"
    catalog = EnvProfileCatalog(env_path)
    profile = catalog.create(draft("Primary", ProfileFormat.GIGACHAT, "original"))
    original_revision = catalog.resolve(profile.id).revision

    assert len(original_revision) == 32
    assert EnvProfileCatalog(env_path).resolve(profile.id).revision == original_revision

    catalog.update(
        profile.id,
        draft("Primary", ProfileFormat.GIGACHAT, "rotated-secret"),
    )
    changed_revision = catalog.resolve(profile.id).revision

    assert changed_revision != original_revision
    assert "rotated-secret" not in changed_revision
    assert "revision" not in catalog.get(profile.id).to_public_dict()


def test_profile_revision_is_stable_for_identical_and_display_only_updates(tmp_path):
    catalog = EnvProfileCatalog(tmp_path / ".env")
    profile = catalog.create(draft("Primary", ProfileFormat.GIGACHAT, "original"))
    original_revision = catalog.resolve(profile.id).revision

    catalog.update(
        profile.id,
        draft("Primary", ProfileFormat.GIGACHAT, "original"),
    )
    assert catalog.resolve(profile.id).revision == original_revision

    catalog.update(
        profile.id,
        draft(
            "Primary renamed",
            ProfileFormat.GIGACHAT,
            "",
            base_url="https://llm.bank.local/v1/",
        ),
    )
    renamed = catalog.resolve(profile.id)
    assert renamed.display_name == "Primary renamed"
    assert renamed.revision == original_revision


@pytest.mark.parametrize(
    ("provider_format", "base_url", "token", "model_id"),
    [
        (
            ProfileFormat.OPENAI,
            "https://llm.bank.local/v1",
            "original",
            "risk-model",
        ),
        (
            ProfileFormat.GIGACHAT,
            "https://llm.bank.local/v2",
            "original",
            "risk-model",
        ),
        (
            ProfileFormat.GIGACHAT,
            "https://llm.bank.local/v1",
            "rotated-secret",
            "risk-model",
        ),
        (
            ProfileFormat.GIGACHAT,
            "https://llm.bank.local/v1",
            "original",
            "another-model",
        ),
    ],
)
def test_profile_revision_rotates_for_each_connection_change(
    tmp_path,
    provider_format,
    base_url,
    token,
    model_id,
):
    catalog = EnvProfileCatalog(tmp_path / ".env")
    profile = catalog.create(draft("Primary", ProfileFormat.GIGACHAT, "original"))
    original_revision = catalog.resolve(profile.id).revision

    catalog.update(
        profile.id,
        draft(
            "Primary",
            provider_format,
            token,
            base_url=base_url,
            model_id=model_id,
        ),
    )

    assert catalog.resolve(profile.id).revision != original_revision


def test_catalog_preserves_unrelated_env_values_and_special_characters(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("BANK_CA_BUNDLE=/etc/bank/ca.pem\n", encoding="utf-8")
    catalog = EnvProfileCatalog(env_path)

    profile = catalog.create(
        draft(
            'Model "A" # test',
            ProfileFormat.OPENAI,
            "token=#quoted value",
            base_url="https://gateway.local/api?tenant=risk",
            model_id="model:2026-07",
        )
    )

    reloaded = EnvProfileCatalog(env_path)
    assert reloaded.resolve(profile.id).token == "token=#quoted value"
    assert reloaded.get(profile.id).display_name == 'Model "A" # test'
    assert "BANK_CA_BUNDLE=/etc/bank/ca.pem" in env_path.read_text(encoding="utf-8")


def test_generated_env_is_safe_to_source_with_shell_metacharacters(tmp_path):
    env_path = tmp_path / ".env"
    marker = tmp_path / "must-not-exist"
    token = f"literal $VALUE `touch {marker}` $(touch {marker}) ' quote"
    catalog = EnvProfileCatalog(env_path)
    profile = catalog.create(draft("Shell-safe", ProfileFormat.OPENAI, token))
    token_key = f"DATALAB_PROFILE_{profile.id}_TOKEN"

    result = subprocess.run(
        [
            "/bin/sh",
            "-c",
            f'. "$1"; printf %s "${{{token_key}}}"',
            "source-test",
            str(env_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == token
    assert not marker.exists()
    assert catalog.resolve(profile.id).token == token


def test_existing_env_permissions_are_restricted_when_catalog_is_read(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text('DATALAB_PROFILE_IDS=""\n', encoding="utf-8")
    env_path.chmod(0o644)

    EnvProfileCatalog(env_path).list()

    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_delete_removes_profile_and_unknown_ids_are_explicit(tmp_path):
    catalog = EnvProfileCatalog(tmp_path / ".env")
    profile = catalog.create(draft("Temporary", ProfileFormat.GIGACHAT, "secret"))

    catalog.delete(profile.id)

    assert catalog.list() == []
    with pytest.raises(ProfileNotFound):
        catalog.get(profile.id)
    with pytest.raises(ProfileNotFound):
        catalog.delete("missing")


@pytest.mark.parametrize(
    "invalid",
    [
        draft("", ProfileFormat.GIGACHAT, "token"),
        draft("Name", ProfileFormat.GIGACHAT, ""),
        draft("Name", ProfileFormat.GIGACHAT, "token", base_url="gateway.local"),
        draft(
            "Name",
            ProfileFormat.GIGACHAT,
            "token",
            base_url="https://user:password@gateway.local/v1",
        ),
        draft("Name", ProfileFormat.OPENAI, "token", model_id=""),
    ],
)
def test_invalid_profiles_are_rejected(tmp_path, invalid):
    catalog = EnvProfileCatalog(tmp_path / ".env")

    with pytest.raises(ProfileValidationError):
        catalog.create(invalid)
