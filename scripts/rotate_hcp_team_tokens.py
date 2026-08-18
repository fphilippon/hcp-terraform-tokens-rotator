#!/usr/bin/env python3
"""Safely rotate HCP Terraform team tokens and GitHub Actions secrets.

The script deliberately uses only the Python standard library. It uses the
GitHub CLI for secret/variable writes so GitHub handles secret encryption.
Token values are never printed or persisted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


UTC = timezone.utc
GITHUB_SECRET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
STATE_VARIABLE = "HCP_TF_ROTATION_STATE"


class RotationError(RuntimeError):
    pass


class HCPApiError(RotationError):
    pass


class GitHubCliError(RotationError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_datetime(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RotationError(f"Invalid ISO 8601 timestamp: {value!r}")
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise RotationError(f"Invalid ISO 8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_datetime(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def as_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        raise RotationError(f"Boolean value must be true or false, got {value!r}")
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    raise RotationError(f"Boolean value must be true or false, got {value!r}")


def as_int(value: Any, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise RotationError(f"{field} must be an integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        parsed = int(value)
    else:
        raise RotationError(f"{field} must be an integer")
    if parsed < minimum:
        raise RotationError(f"{field} must be at least {minimum}")
    return parsed


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise RotationError(f"Configuration file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RotationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RotationError(f"Configuration root must be an object: {path}")
    return value


def require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RotationError(f"{field} must be a non-empty string")
    return value.strip()


def destination_identity(destination: dict[str, Any]) -> tuple[str, ...]:
    destination_type = destination["type"]
    if destination_type == "github_secret":
        return (
            destination_type,
            destination["repository"].lower(),
            str(destination.get("environment", "")).lower(),
            destination["secret_name"].upper(),
        )
    return (destination_type, destination["workspace_id"], destination["variable_id"])


def validate_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    targets = config.get("targets", [])
    if not isinstance(targets, list):
        raise RotationError("config.targets must be an array")
    if "state_variable" in config:
        state_variable = require_string(config["state_variable"], "config.state_variable")
        if state_variable != STATE_VARIABLE:
            raise RotationError(
                f"config.state_variable must be {STATE_VARIABLE!r} because the workflow reads that variable at job start"
            )
    if "state_repository" in config:
        raise RotationError(
            "config.state_repository is not supported; state must stay in the workflow repository so dry runs read it consistently"
        )

    seen_names: set[str] = set()
    seen_tokens: set[tuple[str, str]] = set()
    seen_destinations: set[tuple[str, ...]] = set()
    github_owners: set[str] = set()
    validated: list[dict[str, Any]] = []

    for index, target in enumerate(targets):
        field = f"config.targets[{index}]"
        if not isinstance(target, dict):
            raise RotationError(f"{field} must be an object")
        name = require_string(target.get("name"), f"{field}.name")
        organization = require_string(target.get("organization"), f"{field}.organization")
        require_string(target.get("team_id"), f"{field}.team_id")
        initial_token_id = target.get("initial_token_id")
        configured_current_id = target.get("current_token_id")
        if initial_token_id and configured_current_id and initial_token_id != configured_current_id:
            raise RotationError(f"{field} must not define conflicting initial_token_id and current_token_id values")
        configured_token_id = initial_token_id or configured_current_id
        configured_token_id = require_string(configured_token_id, f"{field}.initial_token_id")
        if name in seen_names:
            raise RotationError(f"Duplicate target name: {name}")
        seen_names.add(name)
        token_identity = (organization, configured_token_id)
        if token_identity in seen_tokens:
            raise RotationError(f"Token {configured_token_id} is configured more than once in {organization}")
        seen_tokens.add(token_identity)

        threshold_days = as_int(target.get("rotate_before_days", 30), f"{name}.rotate_before_days")
        lifetime_days = as_int(
            target.get("new_token_lifetime_days", 90), f"{name}.new_token_lifetime_days", minimum=1
        )
        if lifetime_days <= threshold_days:
            raise RotationError(
                f"{name}.new_token_lifetime_days must be greater than rotate_before_days to avoid rotation loops"
            )
        overlap_hours = as_int(target.get("overlap_hours", 24), f"{name}.overlap_hours", minimum=1)
        rotation_margin_hours = (lifetime_days - threshold_days) * 24
        if overlap_hours >= rotation_margin_hours:
            raise RotationError(
                f"{name}.overlap_hours must be shorter than the interval between token creation and its next rotation window"
            )
        if "description_prefix" in target:
            require_string(target["description_prefix"], f"{name}.description_prefix")
        if "validation_workspace_id" in target:
            require_string(target["validation_workspace_id"], f"{name}.validation_workspace_id")

        destination = target.get("destination")
        if not isinstance(destination, dict):
            raise RotationError(f"{name}.destination must be an object")
        destination_type = require_string(destination.get("type"), f"{name}.destination.type")
        if destination_type == "github_secret":
            repository = require_string(destination.get("repository"), f"{name}.destination.repository")
            parts = repository.split("/")
            if len(parts) != 2 or not all(parts) or any(part.strip() != part for part in parts):
                raise RotationError(f"{name}.destination.repository must use owner/repository format")
            github_owners.add(parts[0].lower())
            secret_name = require_string(destination.get("secret_name"), f"{name}.destination.secret_name")
            if not GITHUB_SECRET_NAME.fullmatch(secret_name) or secret_name.upper().startswith("GITHUB_"):
                raise RotationError(f"{name}.destination.secret_name is not a valid GitHub Actions secret name")
            if "environment" in destination:
                require_string(destination["environment"], f"{name}.destination.environment")
        elif destination_type == "hcp_workspace_variable":
            require_string(destination.get("workspace_id"), f"{name}.destination.workspace_id")
            require_string(destination.get("variable_id"), f"{name}.destination.variable_id")
            if "expected_key" in destination:
                require_string(destination["expected_key"], f"{name}.destination.expected_key")
        else:
            raise RotationError(f"{name}.destination.type is unsupported: {destination_type}")

        identity = destination_identity(destination)
        if identity in seen_destinations:
            raise RotationError(f"Destination for target {name} is configured more than once")
        seen_destinations.add(identity)
        validated.append(target)

    if len(github_owners) > 1:
        raise RotationError("All GitHub secret destinations must belong to the same repository owner")
    workflow_repository = os.environ.get("GH_REPO")
    if github_owners and workflow_repository:
        workflow_parts = workflow_repository.split("/", 1)
        if len(workflow_parts) != 2 or not all(workflow_parts):
            raise RotationError("GH_REPO must use owner/repository format")
        if workflow_parts[0].lower() not in github_owners:
            raise RotationError(
                "GitHub secret destinations must have the same owner as the rotation repository"
            )
    return validated


def safe_error_body(raw: bytes) -> str:
    try:
        decoded = json.loads(raw.decode("utf-8"))
        if isinstance(decoded, dict) and "errors" in decoded:
            return json.dumps(decoded["errors"], separators=(",", ":"))[:500]
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    return raw.decode("utf-8", errors="replace")[:500]


class HCPClient:
    def __init__(self, hostname: str, token: str) -> None:
        if not token:
            raise RotationError("HCP_TF_ROTATION_BOOTSTRAP is not set")
        base = hostname.strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            base = f"https://{base}"
        parsed = urlsplit(base)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise RotationError(f"Invalid HCP_TF_HOSTNAME: {hostname!r}")
        if parsed.query or parsed.fragment:
            raise RotationError("HCP_TF_HOSTNAME must not include a query string or fragment")
        base_path = parsed.path.rstrip("/")
        self.api_path = base_path if base_path.endswith("/api/v2") else f"{base_path}/api/v2"
        self.origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        self.base_url = f"{self.origin}{self.api_path}"
        self.token = token

    def _resolve_url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            url = path_or_url
        elif path_or_url.startswith("/"):
            url = f"{self.origin}{path_or_url}"
        else:
            url = f"{self.base_url}/{path_or_url}"
        parsed = urlsplit(url)
        expected = urlsplit(self.origin)
        if parsed.scheme != expected.scheme or parsed.netloc != expected.netloc:
            raise HCPApiError("Refusing to send an HCP Terraform token to a different origin")
        if parsed.path != self.api_path and not parsed.path.startswith(f"{self.api_path}/"):
            raise HCPApiError(f"Refusing HCP Terraform API URL outside {self.api_path}")
        if parsed.fragment:
            raise HCPApiError("Refusing HCP Terraform API URL with a fragment")
        return url

    def request(
        self,
        method: str,
        path_or_url: str,
        token: str | None = None,
        body: dict[str, Any] | None = None,
        *,
        redact_error_body: bool = False,
    ) -> dict[str, Any]:
        url = self._resolve_url(path_or_url)
        payload = None
        headers = {
            "Accept": "application/vnd.api+json",
            "Authorization": f"Bearer {token or self.token}",
            "User-Agent": "hcp-terraform-token-rotation/0.1",
        }
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/vnd.api+json"
        request = Request(url, data=payload, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = "response body redacted" if redact_error_body else safe_error_body(exc.read())
            raise HCPApiError(
                f"HCP Terraform API {method} {path_or_url} failed with HTTP {exc.code}: {detail}"
            ) from exc
        except URLError as exc:
            raise HCPApiError(f"HCP Terraform API {method} {path_or_url} failed: {exc.reason}") from exc
        if not raw:
            return {}
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise HCPApiError(f"HCP Terraform API returned invalid JSON for {method} {path_or_url}") from exc
        if not isinstance(decoded, dict):
            raise HCPApiError(f"HCP Terraform API returned a non-object response for {method} {path_or_url}")
        return decoded

    def list_team_tokens(self, organization: str) -> list[dict[str, Any]]:
        query = urlencode({"page[size]": "100", "sort": "expired-at"})
        next_url = f"organizations/{quote(organization, safe='')}/team-tokens?{query}"
        tokens: list[dict[str, Any]] = []
        seen_pages: set[str] = set()
        while next_url:
            resolved = self._resolve_url(next_url)
            if resolved in seen_pages:
                raise HCPApiError("HCP Terraform returned a repeated pagination link")
            seen_pages.add(resolved)
            response = self.request("GET", next_url)
            page = response.get("data") or []
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise HCPApiError("HCP Terraform team-token list returned invalid data")
            tokens.extend(page)
            links = response.get("links") or {}
            if not isinstance(links, dict):
                raise HCPApiError("HCP Terraform returned invalid pagination metadata")
            next_url = links.get("next")
            if next_url is not None and not isinstance(next_url, str):
                raise HCPApiError("HCP Terraform returned an invalid pagination link")
        return tokens

    def create_team_token(self, team_id: str, description: str, expires_at: datetime) -> tuple[str, str]:
        body = {
            "data": {
                "type": "authentication-tokens",
                "attributes": {
                    "description": description,
                    "expired-at": format_datetime(expires_at),
                },
            }
        }
        response = self.request("POST", f"teams/{quote(team_id, safe='')}/authentication-tokens", body=body)
        data = response.get("data")
        if not isinstance(data, dict):
            raise HCPApiError("HCP Terraform returned invalid data when creating the replacement")
        token_id = data.get("id")
        attributes = data.get("attributes")
        if not isinstance(attributes, dict):
            raise HCPApiError("HCP Terraform returned invalid token attributes when creating the replacement")
        secret = attributes.get("token")
        if not isinstance(token_id, str) or not token_id or not isinstance(secret, str) or not secret:
            raise HCPApiError("HCP Terraform did not return a token ID and secret when creating the replacement")
        return token_id, secret

    def validate_token(self, token: str, team_id: str, workspace_id: str | None = None) -> None:
        response = self.request("GET", "account/details", token=token)
        data = response.get("data")
        relationships = data.get("relationships") if isinstance(data, dict) else None
        authenticated_resource = (
            relationships.get("authenticated-resource") if isinstance(relationships, dict) else None
        )
        resource_data = (
            authenticated_resource.get("data") if isinstance(authenticated_resource, dict) else None
        )
        if (
            not isinstance(resource_data, dict)
            or resource_data.get("type") != "teams"
            or resource_data.get("id") != team_id
        ):
            raise HCPApiError("Replacement token did not authenticate as the expected HCP Terraform team")
        if workspace_id:
            self.request("GET", f"workspaces/{quote(workspace_id, safe='')}", token=token)

    def delete_token(self, token_id: str) -> None:
        self.request("DELETE", f"authentication-tokens/{quote(token_id, safe='')}")


def run_gh(args: list[str], *, input_text: str | None = None) -> str:
    if not os.environ.get("GH_TOKEN"):
        raise GitHubCliError("GH_TOKEN is not set; provide a GitHub App/PAT with secret and variable write permissions")
    command = ["gh", *args]
    result = subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        env=os.environ.copy(),
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise GitHubCliError(f"GitHub CLI command failed ({' '.join(command)}): {detail[:500]}")
    return result.stdout.strip()


def state_repository(config: dict[str, Any]) -> str:
    del config
    return os.environ.get("GH_REPO", "")


def parse_state(raw: str, source: str) -> dict[str, Any]:
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GitHubCliError(f"{source} does not contain valid JSON") from exc
    if not isinstance(state, dict):
        raise GitHubCliError(f"{source} must contain a JSON object")
    version = state.get("version", 1)
    if version != 1:
        raise GitHubCliError(f"{source} uses unsupported state version {version!r}")
    targets = state.get("targets", {})
    if not isinstance(targets, dict):
        raise GitHubCliError(f"{source}.targets must be an object")
    state["version"] = 1
    state["targets"] = targets
    return state


def load_state(config: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    environment_state = os.environ.get("HCP_TF_ROTATION_STATE_JSON")
    if environment_state:
        return parse_state(environment_state, "HCP_TF_ROTATION_STATE_JSON")
    if dry_run and not os.environ.get("GH_TOKEN"):
        return {"version": 1, "targets": {}}
    repository = state_repository(config)
    variable = STATE_VARIABLE
    if not repository:
        if dry_run:
            return {"version": 1, "targets": {}}
        raise GitHubCliError("GH_REPO is not set")
    raw = run_gh(["variable", "list", "--repo", repository, "--json", "name,value"])
    try:
        values = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise GitHubCliError("GitHub CLI returned invalid JSON while reading rotation state") from exc
    if not isinstance(values, list):
        raise GitHubCliError("GitHub CLI returned an invalid repository-variable list")
    for item in values:
        if not isinstance(item, dict):
            raise GitHubCliError("GitHub CLI returned an invalid repository-variable entry")
        if item.get("name") == variable and item.get("value"):
            return parse_state(item["value"], f"GitHub variable {variable}")
    return {"version": 1, "targets": {}}


def save_state(config: dict[str, Any], state: dict[str, Any]) -> None:
    repository = state_repository(config)
    variable = STATE_VARIABLE
    if not repository:
        raise GitHubCliError("GH_REPO is not set")
    serialized = json.dumps(state, sort_keys=True, separators=(",", ":"))
    run_gh(["variable", "set", variable, "--repo", repository], input_text=serialized)


def set_github_secret(target: dict[str, Any], secret: str) -> None:
    destination = target["destination"]
    repository = destination["repository"]
    secret_name = destination["secret_name"]
    environment = destination.get("environment")
    command = ["secret", "set", secret_name, "--app", "actions", "--repo", repository]
    if environment:
        command.extend(["--env", environment])
    # No --body argument: gh reads the secret value from stdin.
    run_gh(command, input_text=secret)


def set_workspace_variable(client: HCPClient, target: dict[str, Any], secret: str) -> None:
    destination = target["destination"]
    workspace_id = destination["workspace_id"]
    variable_id = destination["variable_id"]
    body = {
        "data": {
            "id": variable_id,
            "type": "vars",
            "attributes": {"value": secret},
        }
    }
    client.request(
        "PATCH",
        f"workspaces/{quote(workspace_id, safe='')}/vars/{quote(variable_id, safe='')}",
        body=body,
        redact_error_body=True,
    )


def preflight_destination(client: HCPClient, target: dict[str, Any]) -> None:
    destination = target["destination"]
    if destination["type"] == "github_secret":
        owner, repository = destination["repository"].split("/", 1)
        repository_path = f"repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
        run_gh(["api", "--method", "GET", "--silent", repository_path])
        environment = destination.get("environment")
        if environment:
            run_gh(
                [
                    "api",
                    "--method",
                    "GET",
                    "--silent",
                    f"{repository_path}/environments/{quote(environment, safe='')}",
                ]
            )
        return
    workspace_id = destination["workspace_id"]
    variable_id = destination["variable_id"]
    response = client.request(
        "GET", f"workspaces/{quote(workspace_id, safe='')}/vars/{quote(variable_id, safe='')}"
    )
    data = response.get("data")
    if not isinstance(data, dict) or data.get("id") != variable_id:
        raise RotationError(f"Target {target['name']} workspace variable could not be verified")
    attributes = data.get("attributes")
    if not isinstance(attributes, dict) or attributes.get("sensitive") is not True:
        raise RotationError(
            f"Target {target['name']} workspace variable must already be marked sensitive before rotation"
        )
    expected_key = destination.get("expected_key")
    if expected_key and attributes.get("key") != expected_key:
        raise RotationError(
            f"Target {target['name']} expected workspace variable key {expected_key!r}, "
            f"but API returned {attributes.get('key')!r}"
        )


def publish_secret(client: HCPClient, target: dict[str, Any], secret: str) -> None:
    destination_type = target["destination"]["type"]
    if destination_type == "github_secret":
        set_github_secret(target, secret)
        return
    if destination_type == "hcp_workspace_variable":
        set_workspace_variable(client, target, secret)
        return
    raise RotationError(f"Target {target.get('name')} has unsupported destination.type: {destination_type}")


def target_token_id(target: dict[str, Any], target_state: dict[str, Any]) -> str:
    token_id = target_state.get("current_token_id") or target.get("initial_token_id") or target.get("current_token_id")
    if not token_id:
        raise RotationError(f"Target {target.get('name')} has no initial_token_id/current_token_id")
    return token_id


def find_token(tokens: list[dict[str, Any]], token_id: str, team_id: str) -> dict[str, Any]:
    for token in tokens:
        if token.get("id") != token_id:
            continue
        relationships = token.get("relationships") or {}
        if not isinstance(relationships, dict):
            raise RotationError(f"Token {token_id} returned invalid relationship metadata")
        team = relationships.get("team") or {}
        team_data = team.get("data") if isinstance(team, dict) else None
        actual_team = team_data.get("id") if isinstance(team_data, dict) else None
        if not actual_team:
            raise RotationError(f"Token {token_id} did not include its team relationship")
        if actual_team != team_id:
            raise RotationError(f"Token {token_id} is associated with {actual_team}, not configured team {team_id}")
        return token
    raise RotationError(f"Configured token {token_id} was not found in the organization team-token inventory")


def print_inventory(target: dict[str, Any], token: dict[str, Any], now: datetime) -> None:
    attrs = token.get("attributes") or {}
    if not isinstance(attrs, dict):
        raise RotationError(f"Token {token.get('id')} returned invalid attributes")
    expiry = attrs.get("expired-at")
    if expiry:
        remaining = parse_datetime(expiry) - now
        remaining_hours = int(remaining.total_seconds() // 3600)
        expiry_text = f"{expiry} ({remaining_hours}h remaining)"
    else:
        expiry_text = "no expiry reported"
    print(
        f"{target.get('name')}: token_id={token.get('id')} "
        f"description={attrs.get('description') or '<legacy/undescribed>'} "
        f"expires={expiry_text} last_used={attrs.get('last-used-at')}"
    )


def pending_status(target_state: dict[str, Any]) -> str | None:
    pending = target_state.get("pending")
    return pending.get("status") if isinstance(pending, dict) else None


def validate_target_state(name: str, target_state: Any) -> dict[str, Any]:
    if not isinstance(target_state, dict):
        raise RotationError(f"State for target {name} must be an object")
    current_id = target_state.get("current_token_id")
    if current_id is not None:
        require_string(current_id, f"state.targets.{name}.current_token_id")
    pending = target_state.get("pending")
    if pending is None:
        return target_state
    if not isinstance(pending, dict):
        raise RotationError(f"State for target {name}.pending must be an object")
    status = pending.get("status")
    if status not in {"candidate-created", "published"}:
        raise RotationError(f"State for target {name} has unsupported pending status {status!r}")
    old_id = require_string(pending.get("old_token_id"), f"state.targets.{name}.pending.old_token_id")
    new_id = require_string(pending.get("new_token_id"), f"state.targets.{name}.pending.new_token_id")
    if old_id == new_id:
        raise RotationError(f"State for target {name} uses the same old and new token ID")
    require_string(pending.get("description"), f"state.targets.{name}.pending.description")
    created_at = parse_datetime(
        require_string(pending.get("created_at"), f"state.targets.{name}.pending.created_at")
    )
    revoke_after = parse_datetime(
        require_string(pending.get("revoke_after"), f"state.targets.{name}.pending.revoke_after")
    )
    if revoke_after <= created_at:
        raise RotationError(f"State for target {name} has revoke_after before or equal to created_at")
    if status == "published" and current_id != new_id:
        raise RotationError(f"Published state for target {name} does not point current_token_id at the replacement")
    if status == "candidate-created" and current_id == new_id:
        raise RotationError(f"Candidate state for target {name} incorrectly marks the candidate as current")
    return target_state


def process_target(
    client: HCPClient,
    target: dict[str, Any],
    state: dict[str, Any],
    mode: str,
    dry_run: bool,
    now: datetime,
    config: dict[str, Any],
) -> None:
    name = target.get("name")
    if not name or not target.get("organization") or not target.get("team_id"):
        raise RotationError("Each target needs name, organization, and team_id")

    target_state = validate_target_state(name, state["targets"].setdefault(name, {}))
    status = pending_status(target_state)
    if status == "candidate-created":
        raise RotationError(
            f"Candidate token {target_state['pending']['new_token_id']} is awaiting reconciliation; "
            "refusing to create another token"
        )

    tokens = client.list_team_tokens(target["organization"])
    current_id = target_token_id(target, target_state)
    current = find_token(tokens, current_id, target["team_id"])
    print_inventory(target, current, now)
    current_attributes = current.get("attributes")
    if not isinstance(current_attributes, dict):
        raise RotationError(f"Token {current_id} returned invalid attributes")
    expiry = current_attributes.get("expired-at")
    threshold_days = as_int(target.get("rotate_before_days", 30), f"{name}.rotate_before_days")

    pending = target_state.get("pending")
    if isinstance(pending, dict) and pending.get("status") == "published":
        revoke_after = parse_datetime(pending["revoke_after"])
        if now >= revoke_after:
            old_id = pending["old_token_id"]
            old_is_present = any(item.get("id") == old_id for item in tokens)
            if old_is_present:
                # Rotation state is editable metadata. Re-check the old token's
                # team relationship before performing the destructive action.
                find_token(tokens, old_id, target["team_id"])
                if not expiry or parse_datetime(expiry) <= now:
                    raise RotationError(
                        f"Replacement token {current_id} is expired or has no expiry metadata; "
                        "refusing to revoke the old token"
                    )
                if parse_datetime(expiry) <= now + timedelta(days=threshold_days):
                    raise RotationError(
                        f"Replacement token {current_id} is already inside its rotation window; "
                        "refusing old-token cleanup"
                    )
            if dry_run or mode == "inventory":
                if old_is_present:
                    print(f"{name}: would revoke old token {old_id} after overlap")
                else:
                    print(f"{name}: old token {old_id} is already absent; would clear pending state")
            else:
                if old_is_present:
                    client.delete_token(old_id)
                    print(f"{name}: revoked old token {old_id}")
                else:
                    print(f"{name}: old token {old_id} was already absent")
                target_state.pop("pending", None)
                save_state(config, state)
        else:
            print(f"{name}: replacement published; old token remains until {pending['revoke_after']}")

    if not expiry:
        print(f"{name}: no expiry reported; skipping")
        return
    if parse_datetime(expiry) > now + timedelta(days=threshold_days):
        return

    if isinstance(target_state.get("pending"), dict):
        print(f"{name}: pending rotation exists; skipping new rotation")
        return

    if mode == "inventory":
        print(f"{name}: would rotate; token is inside the configured policy window")
        return

    preflight_destination(client, target)
    if dry_run:
        print(f"{name}: destination preflight passed; would rotate inside the configured policy window")
        return

    lifetime_days = as_int(target.get("new_token_lifetime_days", 90), f"{name}.new_token_lifetime_days", minimum=1)
    overlap_hours = as_int(target.get("overlap_hours", 24), f"{name}.overlap_hours")
    prefix = target.get("description_prefix") or f"github-actions/{name}"
    description = f"{prefix}-{now.strftime('%Y%m%d%H%M%SZ')}-{secrets.token_hex(4)}"
    new_expiry = now + timedelta(days=lifetime_days)

    new_id, new_secret = client.create_team_token(target["team_id"], description, new_expiry)
    target_state["pending"] = {
        "status": "candidate-created",
        "old_token_id": current_id,
        "new_token_id": new_id,
        "description": description,
        "created_at": format_datetime(now),
        "revoke_after": format_datetime(now + timedelta(hours=overlap_hours)),
    }
    try:
        save_state(config, state)
    except RotationError:
        # The candidate secret is only available in this process. If metadata
        # cannot be recorded, remove the candidate rather than lose track of it.
        try:
            client.delete_token(new_id)
        except RotationError as cleanup_error:
            raise RotationError(f"Could not persist candidate state, and candidate cleanup also failed: {cleanup_error}")
        raise
    print(f"{name}: created replacement token {new_id} with expiry {format_datetime(new_expiry)}")

    try:
        client.validate_token(new_secret, target["team_id"], target.get("validation_workspace_id"))
    except RotationError as validation_error:
        try:
            client.delete_token(new_id)
        except RotationError as cleanup_error:
            raise RotationError(
                f"Replacement validation failed ({validation_error}); candidate revocation failed ({cleanup_error})"
            ) from validation_error
        target_state.pop("pending", None)
        try:
            save_state(config, state)
        except RotationError as state_error:
            raise RotationError(
                f"Replacement validation failed and candidate was revoked, but state cleanup failed ({state_error})"
            ) from validation_error
        raise RotationError(f"Replacement validation failed and candidate was revoked: {validation_error}") from validation_error
    publish_secret(client, target, new_secret)
    target_state["current_token_id"] = new_id
    target_state["pending"]["status"] = "published"
    save_state(config, state)
    print(f"{name}: published and validated replacement; old token remains until {target_state['pending']['revoke_after']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--mode", choices=("inventory", "rotate"), default="inventory")
    parser.add_argument("--dry-run", default="true", help="true/false; writes are disabled when true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dry_run = as_bool(args.dry_run)
    config = load_json(args.config)
    targets = validate_config(config)
    if not targets:
        print("No targets configured; nothing to do")
        return 0

    client = HCPClient(os.environ.get("HCP_TF_HOSTNAME", "app.terraform.io"), os.environ.get("HCP_TF_ROTATION_BOOTSTRAP", ""))
    state = load_state(config, dry_run)
    now = utc_now()
    errors = 0
    for target in targets:
        try:
            process_target(client, target, state, args.mode, dry_run, now, config)
        except RotationError as exc:
            errors += 1
            print(f"ERROR {target.get('name', '<unnamed>')}: {exc}", file=sys.stderr)
    if errors:
        print(f"Completed with {errors} target error(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RotationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
