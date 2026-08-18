from __future__ import annotations

import importlib.util
import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "rotate_hcp_team_tokens.py"
SPEC = importlib.util.spec_from_file_location("rotation", SCRIPT)
assert SPEC and SPEC.loader
rotation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rotation)

UTC = timezone.utc
NOW = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)


def token(token_id: str, expiry: str, team_id: str = "team-one") -> dict:
    return {
        "id": token_id,
        "attributes": {
            "description": f"description-{token_id}",
            "expired-at": expiry,
            "last-used-at": None,
        },
        "relationships": {"team": {"data": {"id": team_id, "type": "teams"}}},
    }


def github_target(**overrides) -> dict:
    target = {
        "name": "infra-production",
        "organization": "example-org",
        "team_id": "team-one",
        "initial_token_id": "at-old",
        "description_prefix": "github-actions/infra-production",
        "destination": {
            "type": "github_secret",
            "repository": "example-org/infrastructure",
            "secret_name": "HCP_TF_TOKEN",
        },
        "rotate_before_days": 30,
        "new_token_lifetime_days": 90,
        "overlap_hours": 24,
    }
    target.update(overrides)
    return target


class FakeClient:
    def __init__(self, tokens: list[dict]) -> None:
        self.tokens = tokens
        self.created: list[tuple[str, str, datetime]] = []
        self.deleted: list[str] = []
        self.validated: list[tuple[str, str, str | None]] = []
        self.validation_error: Exception | None = None

    def list_team_tokens(self, organization: str) -> list[dict]:
        return self.tokens

    def create_team_token(self, team_id: str, description: str, expires_at: datetime) -> tuple[str, str]:
        self.created.append((team_id, description, expires_at))
        return "at-new", "new-secret"

    def validate_token(self, secret: str, team_id: str, workspace_id: str | None = None) -> None:
        self.validated.append((secret, team_id, workspace_id))
        if self.validation_error:
            raise self.validation_error

    def delete_token(self, token_id: str) -> None:
        self.deleted.append(token_id)


class ConfigurationTests(unittest.TestCase):
    def test_invalid_boolean_fails_closed(self) -> None:
        with self.assertRaises(rotation.RotationError):
            rotation.as_bool("tru")

    def test_duplicate_destination_is_rejected(self) -> None:
        first = github_target()
        second = github_target(
            name="second",
            team_id="team-two",
            initial_token_id="at-two",
        )
        with self.assertRaisesRegex(rotation.RotationError, "Destination.*more than once"):
            rotation.validate_config({"targets": [first, second]})

    def test_lifetime_must_exceed_rotation_window(self) -> None:
        target = github_target(new_token_lifetime_days=30)
        with self.assertRaisesRegex(rotation.RotationError, "avoid rotation loops"):
            rotation.validate_config({"targets": [target]})

    def test_workspace_destination_requires_existing_sensitive_variable(self) -> None:
        target = github_target(
            destination={
                "type": "hcp_workspace_variable",
                "workspace_id": "ws-one",
                "variable_id": "var-one",
                "expected_key": "HCP_TF_TOKEN",
            }
        )

        class WorkspaceClient:
            def request(self, *args, **kwargs):
                return {
                    "data": {
                        "id": "var-one",
                        "attributes": {"key": "HCP_TF_TOKEN", "sensitive": False},
                    }
                }

        with self.assertRaisesRegex(rotation.RotationError, "must already be marked sensitive"):
            rotation.preflight_destination(WorkspaceClient(), target)


class StateTests(unittest.TestCase):
    def test_dry_run_reads_state_from_workflow_variable(self) -> None:
        raw = json.dumps({"version": 1, "targets": {"infra-production": {"current_token_id": "at-new"}}})
        with patch.dict(os.environ, {"HCP_TF_ROTATION_STATE_JSON": raw}, clear=True):
            state = rotation.load_state({}, dry_run=True)
        self.assertEqual(state["targets"]["infra-production"]["current_token_id"], "at-new")

    def test_candidate_created_state_fails_the_run(self) -> None:
        state = {
            "version": 1,
            "targets": {
                "infra-production": {
                    "pending": {
                        "status": "candidate-created",
                        "old_token_id": "at-old",
                        "new_token_id": "at-new",
                        "description": "candidate",
                        "created_at": "2026-08-18T07:00:00Z",
                        "revoke_after": "2026-08-19T07:00:00Z",
                    }
                }
            },
        }
        with self.assertRaisesRegex(rotation.RotationError, "awaiting reconciliation"):
            rotation.process_target(FakeClient([]), github_target(), state, "rotate", False, NOW, {})


class RotationLifecycleTests(unittest.TestCase):
    def test_rotation_publishes_then_marks_replacement_current(self) -> None:
        client = FakeClient([token("at-old", "2026-08-20T08:00:00Z")])
        state = {"version": 1, "targets": {}}
        published: list[str] = []
        with (
            patch.object(rotation, "save_state") as save_state,
            patch.object(rotation, "preflight_destination") as preflight,
            patch.object(rotation, "publish_secret", side_effect=lambda client, target, value: published.append(value)),
            patch.object(rotation.secrets, "token_hex", return_value="a1b2c3d4"),
        ):
            rotation.process_target(client, github_target(), state, "rotate", False, NOW, {})

        target_state = state["targets"]["infra-production"]
        self.assertEqual(published, ["new-secret"])
        self.assertEqual(client.validated, [("new-secret", "team-one", None)])
        self.assertEqual(target_state["current_token_id"], "at-new")
        self.assertEqual(target_state["pending"]["status"], "published")
        self.assertIn("a1b2c3d4", client.created[0][1])
        preflight.assert_called_once()
        self.assertEqual(save_state.call_count, 2)
        self.assertEqual(client.deleted, [])

    def test_failed_validation_revokes_unpublished_candidate(self) -> None:
        client = FakeClient([token("at-old", "2026-08-20T08:00:00Z")])
        client.validation_error = rotation.HCPApiError("invalid replacement")
        state = {"version": 1, "targets": {}}
        with (
            patch.object(rotation, "save_state") as save_state,
            patch.object(rotation, "preflight_destination"),
            patch.object(rotation, "publish_secret") as publish_secret,
        ):
            with self.assertRaisesRegex(rotation.RotationError, "candidate was revoked"):
                rotation.process_target(client, github_target(), state, "rotate", False, NOW, {})

        self.assertEqual(client.deleted, ["at-new"])
        self.assertNotIn("pending", state["targets"]["infra-production"])
        self.assertEqual(save_state.call_count, 2)
        publish_secret.assert_not_called()

    def test_rotation_dry_run_preflights_without_creating(self) -> None:
        client = FakeClient([token("at-old", "2026-08-20T08:00:00Z")])
        state = {"version": 1, "targets": {}}
        with patch.object(rotation, "preflight_destination") as preflight:
            rotation.process_target(client, github_target(), state, "rotate", True, NOW, {})

        preflight.assert_called_once()
        self.assertEqual(client.created, [])

    def test_missing_old_token_makes_cleanup_idempotent(self) -> None:
        client = FakeClient([token("at-new", "2026-11-16T08:00:00Z")])
        state = {
            "version": 1,
            "targets": {
                "infra-production": {
                    "current_token_id": "at-new",
                    "pending": {
                        "status": "published",
                        "old_token_id": "at-old",
                        "new_token_id": "at-new",
                        "description": "replacement",
                        "created_at": "2026-08-16T08:00:00Z",
                        "revoke_after": "2026-08-17T08:00:00Z",
                    },
                }
            },
        }
        with patch.object(rotation, "save_state") as save_state:
            rotation.process_target(client, github_target(), state, "rotate", False, NOW, {})

        self.assertNotIn("pending", state["targets"]["infra-production"])
        self.assertEqual(client.deleted, [])
        save_state.assert_called_once()

    def test_cleanup_refuses_old_token_from_another_team(self) -> None:
        client = FakeClient(
            [
                token("at-new", "2026-11-16T08:00:00Z"),
                token("at-old", "2026-09-01T08:00:00Z", team_id="team-other"),
            ]
        )
        state = {
            "version": 1,
            "targets": {
                "infra-production": {
                    "current_token_id": "at-new",
                    "pending": {
                        "status": "published",
                        "old_token_id": "at-old",
                        "new_token_id": "at-new",
                        "description": "replacement",
                        "created_at": "2026-08-16T08:00:00Z",
                        "revoke_after": "2026-08-17T08:00:00Z",
                    },
                }
            },
        }
        with patch.object(rotation, "save_state") as save_state:
            with self.assertRaisesRegex(rotation.RotationError, "team-other"):
                rotation.process_target(client, github_target(), state, "rotate", False, NOW, {})

        self.assertEqual(client.deleted, [])
        save_state.assert_not_called()

    def test_cleanup_refuses_when_replacement_is_expired(self) -> None:
        client = FakeClient(
            [
                token("at-new", "2026-08-18T07:00:00Z"),
                token("at-old", "2026-09-01T08:00:00Z"),
            ]
        )
        state = {
            "version": 1,
            "targets": {
                "infra-production": {
                    "current_token_id": "at-new",
                    "pending": {
                        "status": "published",
                        "old_token_id": "at-old",
                        "new_token_id": "at-new",
                        "description": "replacement",
                        "created_at": "2026-08-16T08:00:00Z",
                        "revoke_after": "2026-08-17T08:00:00Z",
                    },
                }
            },
        }
        with patch.object(rotation, "save_state") as save_state:
            with self.assertRaisesRegex(rotation.RotationError, "refusing to revoke"):
                rotation.process_target(client, github_target(), state, "rotate", False, NOW, {})

        self.assertEqual(client.deleted, [])
        save_state.assert_not_called()


class UrlSafetyTests(unittest.TestCase):
    def test_pagination_link_under_api_root_is_accepted(self) -> None:
        client = rotation.HCPClient("app.terraform.io", "bootstrap")
        self.assertEqual(
            client._resolve_url("/api/v2/organizations/example/team-tokens?page[number]=2"),
            "https://app.terraform.io/api/v2/organizations/example/team-tokens?page[number]=2",
        )

    def test_cross_origin_url_is_rejected(self) -> None:
        client = rotation.HCPClient("app.terraform.io", "bootstrap")
        with self.assertRaisesRegex(rotation.HCPApiError, "different origin"):
            client._resolve_url("https://attacker.example/api/v2/steal")

    def test_replacement_must_authenticate_as_expected_team(self) -> None:
        client = rotation.HCPClient("app.terraform.io", "bootstrap")
        response = {
            "data": {
                "relationships": {
                    "authenticated-resource": {
                        "data": {"id": "team-other", "type": "teams"}
                    }
                }
            }
        }
        with patch.object(client, "request", return_value=response):
            with self.assertRaisesRegex(rotation.HCPApiError, "expected HCP Terraform team"):
                client.validate_token("candidate", "team-one")


if __name__ == "__main__":
    unittest.main()
