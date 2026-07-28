"""Render a validated, environment-specific copy of the GCP manifests."""

from __future__ import annotations

import argparse
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_REGION = re.compile(r"^[a-z]+-[a-z]+\d$")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
_PROFILE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SENTINEL = re.compile(r"\b[A-Z][A-Z0-9_]+_PLACEHOLDER\b")

_REQUIRED_ENVIRONMENT = (
    "GCP_PROJECT_ID",
    "GCP_REGION",
    "IMAGE_TAG",
    "DOCUMENTS_BUCKET",
    "MODEL_ARMOR_TEMPLATE",
    "PUBLIC_URL",
    "LLM_PROFILE",
)


def _validate_scalar(name: str, value: str) -> str:
    if not value or any(character in value for character in ("\n", "\r", "\0")):
        raise ValueError(f"{name} must be one non-empty line")
    return value


@dataclass(frozen=True)
class DeploymentValues:
    project_id: str
    region: str
    image_tag: str
    documents_bucket: str
    model_armor_template: str
    public_url: str
    llm_profile: str
    langfuse_public_key: str = ""
    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_sample_rate: float = 0

    def __post_init__(self) -> None:
        scalar_values = {
            "GCP_PROJECT_ID": self.project_id,
            "GCP_REGION": self.region,
            "IMAGE_TAG": self.image_tag,
            "DOCUMENTS_BUCKET": self.documents_bucket,
            "MODEL_ARMOR_TEMPLATE": self.model_armor_template,
            "PUBLIC_URL": self.public_url,
            "LLM_PROFILE": self.llm_profile,
            "LANGFUSE_BASE_URL": self.langfuse_base_url,
        }
        for name, value in scalar_values.items():
            _validate_scalar(name, value)
        if self.langfuse_public_key:
            _validate_scalar("LANGFUSE_PUBLIC_KEY", self.langfuse_public_key)

        if not _PROJECT_ID.fullmatch(self.project_id):
            raise ValueError("GCP_PROJECT_ID is not a valid Google Cloud project ID")
        if not _REGION.fullmatch(self.region):
            raise ValueError("GCP_REGION is not a valid regional location")
        if not _COMMIT_SHA.fullmatch(self.image_tag):
            raise ValueError("IMAGE_TAG must be a 40-character lowercase Git commit SHA")
        if not _BUCKET.fullmatch(self.documents_bucket):
            raise ValueError("DOCUMENTS_BUCKET is not a valid GCS bucket name")
        if not _PROFILE.fullmatch(self.llm_profile):
            raise ValueError("LLM_PROFILE is not a valid profile name")

        expected_template_prefix = (
            f"projects/{self.project_id}/locations/{self.region}/templates/"
        )
        if not self.model_armor_template.startswith(expected_template_prefix):
            raise ValueError(
                "MODEL_ARMOR_TEMPLATE must belong to GCP_PROJECT_ID and GCP_REGION"
            )

        public = urlsplit(self.public_url)
        if (
            public.scheme != "https"
            or not public.hostname
            or public.username
            or public.password
            or public.port
            or public.path not in {"", "/"}
            or public.query
            or public.fragment
        ):
            raise ValueError("PUBLIC_URL must be an HTTPS origin without a path")

        langfuse = urlsplit(self.langfuse_base_url)
        if (
            langfuse.scheme != "https"
            or not langfuse.hostname
            or langfuse.username
            or langfuse.password
            or langfuse.query
            or langfuse.fragment
        ):
            raise ValueError("LANGFUSE_BASE_URL must be an HTTPS URL")

        if not 0 <= self.langfuse_sample_rate <= 1:
            raise ValueError("LANGFUSE_SAMPLE_RATE must be between 0 and 1")

    @property
    def public_host(self) -> str:
        hostname = urlsplit(self.public_url).hostname
        assert hostname is not None
        return hostname

    @property
    def replacements(self) -> dict[str, str]:
        return {
            "PROJECT_ID_PLACEHOLDER": self.project_id,
            "REGION_PLACEHOLDER": self.region,
            "IMAGE_TAG_PLACEHOLDER": self.image_tag,
            "DOCUMENTS_BUCKET_PLACEHOLDER": self.documents_bucket,
            "MODEL_ARMOR_TEMPLATE_PLACEHOLDER": self.model_armor_template,
            "PUBLIC_HOST_PLACEHOLDER": self.public_host,
            "PUBLIC_ORIGIN_PLACEHOLDER": self.public_url.rstrip("/"),
            "LLM_PROFILE_PLACEHOLDER": self.llm_profile,
            "LANGFUSE_PUBLIC_KEY_PLACEHOLDER": self.langfuse_public_key,
            "LANGFUSE_BASE_URL_PLACEHOLDER": self.langfuse_base_url.rstrip("/"),
            "LANGFUSE_SAMPLE_RATE_PLACEHOLDER": format(
                self.langfuse_sample_rate, ".12g"
            ),
            "APP_RELEASE_PLACEHOLDER": self.image_tag,
        }

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "DeploymentValues":
        missing = [name for name in _REQUIRED_ENVIRONMENT if not environment.get(name)]
        if missing:
            raise ValueError(
                "missing deployment environment variables: " + ", ".join(missing)
            )
        try:
            sample_rate = float(
                environment.get("LANGFUSE_SAMPLE_RATE") or "0"
            )
        except ValueError as exc:
            raise ValueError("LANGFUSE_SAMPLE_RATE must be a number") from exc

        return cls(
            project_id=environment["GCP_PROJECT_ID"],
            region=environment["GCP_REGION"],
            image_tag=environment["IMAGE_TAG"],
            documents_bucket=environment["DOCUMENTS_BUCKET"],
            model_armor_template=environment["MODEL_ARMOR_TEMPLATE"],
            public_url=environment["PUBLIC_URL"],
            llm_profile=environment["LLM_PROFILE"],
            langfuse_public_key=environment.get("LANGFUSE_PUBLIC_KEY", ""),
            langfuse_base_url=(
                environment.get("LANGFUSE_BASE_URL")
                or "https://cloud.langfuse.com"
            ),
            langfuse_sample_rate=sample_rate,
        )


def render_manifests(
    source: Path, output: Path, values: DeploymentValues
) -> None:
    if not source.is_dir():
        raise ValueError(f"manifest source does not exist: {source}")
    if output.exists():
        raise ValueError(f"render output already exists: {output}")

    shutil.copytree(source, output)
    unresolved: dict[Path, list[str]] = {}
    for path in output.rglob("*.yaml"):
        rendered = path.read_text(encoding="utf-8")
        for sentinel, value in values.replacements.items():
            rendered = rendered.replace(sentinel, value)
        path.write_text(rendered, encoding="utf-8")

        remaining = sorted(set(_SENTINEL.findall(rendered)))
        if remaining:
            unresolved[path.relative_to(output)] = remaining

    if unresolved:
        details = "; ".join(
            f"{path}: {', '.join(sentinels)}"
            for path, sentinels in sorted(unresolved.items())
        )
        raise ValueError(f"unresolved deployment sentinels: {details}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("infra/k8s"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    render_manifests(
        args.source,
        args.output,
        DeploymentValues.from_environment(os.environ),
    )


if __name__ == "__main__":
    main()
