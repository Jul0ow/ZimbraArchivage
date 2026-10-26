from app.progress import EXPIRATION_SECONDS, ProgressRegistry


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
