import re

from homebase.compose import generate_docker_compose

FLOATING = {"latest", "stable", "release", "edge", "nightly", "master", "main"}


def test_compose_is_pinned_secretless_and_off_by_default():
    text = generate_docker_compose()
    assert "changeme" not in text.lower()
    assert not re.search(r"^\s*privileged\s*:", text, re.MULTILINE)
    images = []
    for line in text.splitlines():
        if "docker.sock" in line:
            assert ":ro" in line
        stripped = line.strip()
        if stripped.startswith("image:"):
            image = stripped.split(":", 1)[1].strip()
            assert ":" in image
            tag = image.rsplit(":", 1)[1]
            assert tag not in FLOATING
            assert any(ch.isdigit() for ch in tag)
            images.append(image)
    assert images
    assert text.count('profiles: ["later"]') == len(images)
    assert "${PIHOLE_WEBPASSWORD:?Set PIHOLE_WEBPASSWORD in .env}" in text
    assert "${IMMICH_DB_PASSWORD:?Set IMMICH_DB_PASSWORD in .env}" in text
    assert "auth=" not in text
