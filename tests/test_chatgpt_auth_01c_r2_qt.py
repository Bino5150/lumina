"""Real manager + real Settings card: code entry for added and re-registered accounts."""
import os

import pytest

if os.environ.get("LUMINA_REQUIRE_QT") == "1":
    import PySide6  # noqa: F401
else:
    pytest.importorskip("PySide6")

from core.chatgpt_auth.session import SessionState
from core.chatgpt_auth.store import STATUS_REAUTH_REQUIRED
from chatgpt_auth_fakes import FakeClock, FakeOpenAIAuth
from test_chatgpt_auth_01c_concurrency import doc_of, make_manager, sign_in
from test_chatgpt_auth_01c_settings_qt import make_card, pump, qapp  # noqa: F401


@pytest.mark.parametrize("reregister", [False, True], ids=["add-account", "invalid-registration"])
def test_existing_account_never_hides_new_start_code(qapp, tmp_path, reregister):
    clock = FakeClock()
    fake = FakeOpenAIAuth(clock=clock)
    try:
        manager = make_manager(tmp_path / "chatgpt", fake, clock)
        sign_in(manager)
        if reregister:
            with manager.store.locked():
                doc = manager.store.read()
                profile = doc.profile(doc.active_profile_id)
                profile.registration_invalid = True
                profile.status = STATUS_REAUTH_REQUIRED
                profile.credentials = None
                profile.pending_rotation = None
                manager.store.write(doc)
        launched = []
        manager._open_browser = lambda url: launched.append(url) or True
        card = make_card(qapp, manager)
        button = card.reconnect_btn if reregister else card.add_btn
        assert not button.isHidden()
        button.click()
        assert pump(qapp, card, lambda: launched and card._pending is not None
                    and card._status is not None and card._status.state is SessionState.AUTHORIZING)
        code = card._pending.start_code
        assert code and card.code_value_label.text() == code
        assert "ONE-TIME CODE" in card.code_label.text()
        assert "#FFFFFF" in card.code_value_label.styleSheet()
        assert not card.copy_btn.isHidden()
        card.copy_btn.click()
        assert qapp.clipboard().text() == code
        assert card.account_label.text().startswith("Connected account:")
        assert card.confirm_btn.isHidden()
        before = len(doc_of(manager).profiles)
        subject = "user-sub-A" if reregister else "user-sub-B"
        email = "owner@example.test" if reregister else "second@example.test"
        fake.browser(subject=subject, email=email)(launched[0], code)
        assert pump(qapp, card, lambda: not card.confirm_btn.isHidden())
        assert card.account_label.text() == f"Account to confirm: {email}"
        assert len(doc_of(manager).profiles) == before  # owner has not confirmed
        card.confirm_btn.click()
        assert pump(qapp, card, lambda: not card._busy and card._pending is None)
        assert len(doc_of(manager).profiles) == (before if reregister else before + 1)
        card.deleteLater()
    finally:
        fake.close()
