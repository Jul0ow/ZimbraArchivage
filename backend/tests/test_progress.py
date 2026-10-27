from app import progress as progress_module
from app.progress import EXPIRATION_SECONDS, MAX_TRACKED_PER_SESSION, ProgressRegistry


def test_progress_is_tracked_per_session():
    registry = ProgressRegistry(clock=lambda: 0)

    progress = registry.start("jeton", "export-1")
    registry.advance(progress)
    registry.advance(progress)
    registry.finish(progress)

    assert registry.get("jeton", "export-1") == progress
    assert (progress.done, progress.finished) == (2, True)
    assert registry.get("autre-jeton", "export-1") is None


def test_inactive_exports_are_forgotten():
    now = 0.0
    registry = ProgressRegistry(clock=lambda: now)
    registry.start("jeton", "ancien")
    recent = registry.start("jeton", "recent")

    now = EXPIRATION_SECONDS / 2
    registry.advance(recent)
    now = EXPIRATION_SECONDS + 1
    registry.start("jeton", "nouveau")

    assert registry.get("jeton", "ancien") is None
    assert registry.get("jeton", "recent") is recent


def test_total_and_interruption_are_reported():
    registry = ProgressRegistry(clock=lambda: 0)
    progress = registry.start("jeton", "export-1")

    registry.set_total(progress, 10)
    registry.finish(progress, "session Zimbra expirée")
    # La raison de l'interruption n'est pas effacée par la fin normale du flux
    registry.finish(progress)

    assert (progress.total, progress.finished, progress.error) == (10, True, "session Zimbra expirée")


def test_tracked_exports_are_bounded_per_session():
    registry = ProgressRegistry(clock=lambda: 0)

    for i in range(MAX_TRACKED_PER_SESSION + 2):
        registry.start("jeton", f"export-{i}")
    registry.start("autre", "export-0")

    assert registry.get("jeton", "export-0") is None
    assert registry.get("jeton", "export-1") is None
    assert registry.get("jeton", f"export-{MAX_TRACKED_PER_SESSION + 1}") is not None
    assert registry.get("autre", "export-0") is not None


def test_tracked_exports_are_bounded_in_total(monkeypatch):
    monkeypatch.setattr(progress_module, "MAX_TRACKED", 3)
    registry = ProgressRegistry(clock=lambda: 0)

    for i in range(4):
        registry.start(f"jeton-{i}", "export-1")

    assert registry.get("jeton-0", "export-1") is None
    assert registry.get("jeton-3", "export-1") is not None


def test_expired_exports_are_forgotten_on_read():
    now = 0.0
    registry = ProgressRegistry(clock=lambda: now)
    registry.start("jeton", "ancien")

    now = EXPIRATION_SECONDS + 1

    assert registry.get("jeton", "ancien") is None
