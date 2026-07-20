"""STAB P2-2 — per-cloud region/location labels, never the UI's AWS-style default.

Live (screenshots 5 and 7, 2026-07-13): the availability step said "Queried AZURE ·
us-east-1" and "Queried GCP · us-east-1" — the UI context region (an AWS region) stamped
onto every cloud. The label (and the availability ping detail) must carry the cloud's own
region/location: the user's input when given, else that cloud's default.
"""

from __future__ import annotations

from app.agents.cloudops import display_region


def test_azure_shows_its_location_never_an_aws_region():
    assert display_region("azure", {"location": "westeurope"}, "us-east-1") == "westeurope"
    assert display_region("azure", {}, "us-east-1") == "eastus"          # the live bug shape


def test_gcp_shows_its_region_never_an_aws_region():
    assert display_region("gcp", {"region": "europe-west1"}, "us-east-1") == "europe-west1"
    assert display_region("gcp", {}, "us-east-1") == "us-central1"       # the live bug shape


def test_aws_keeps_input_then_ui_then_default_precedence():
    assert display_region("aws", {"region": "ap-south-1"}, "us-east-1") == "ap-south-1"
    assert display_region("aws", {}, "eu-west-2") == "eu-west-2"
    assert display_region("aws", {}, None) == "us-east-1"


def test_missing_inputs_never_crash_the_label():
    assert display_region("azure", None, None) == "eastus"
