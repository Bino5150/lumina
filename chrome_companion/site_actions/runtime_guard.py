"""Dormant site-task ingress, trusted routing identities and UI delivery.

There is no actuator, observation provider, store initializer or model tool.
Context carries an object seal; the broker owns agent/session/task/dispatch
records independently. Missing identity never turns a protected dispatch into
an ordinary dispatch. Executable/delegating routes are closed in site tasks.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import os
import secrets
import threading
import weakref

from .model import ContractError
from .owner_intent import OwnerIntake, reopen_claim_store
from .review import ReviewController


# A role is not a grant. B1.4 deliberately admits no tool execution inside a
# site task: even observation tools remain unavailable until B2's live vet.
# This closes transitive thread/executor/delegation escape paths structurally.
ROUTES = {
    "chrome_status": "observation",
    "chrome_list_tabs": "observation",
    "chrome_get_active_tab": "observation",
    "chrome_get_url_title": "observation",
    "chrome_extract_visible_text": "observation",
    "chrome_get_links": "observation",
    "chrome_open_owner_url": "navigation",
    "chrome_switch_tab": "navigation",
    "chrome_follow_link": "navigation",
}


@dataclass(frozen=True, slots=True)
class TaskIdentity:
    identifier: str


@dataclass(frozen=True, slots=True)
class ReviewDelivery:
    identifier: str


class _Task:
    def __init__(self, route, chat, event, protected):
        self.handle = TaskIdentity(secrets.token_hex(32))
        self.route = route
        self.chat = chat
        self.event = event
        self.protected = protected
        self.alive = True
        self.intake = None
        self.admission = None
        self.review = None
        self.store = None
        self.failure = ""


def _execution():
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.current_thread(), task


class AgentRoute:
    """Created only by actual Agent construction, never caller/model IDs."""
    def __init__(self, broker, agent, registry, data_dir):
        self.broker = broker
        self.agent = weakref.ref(agent)
        self.registry = weakref.ref(registry)
        self.data_dir = data_dir
        self.session = secrets.token_hex(32)
        self.owner = agent.owner is True
        self.channel = agent.channel_id
        self.presenter = None
        self.deliver = None
        self.protected = set()


class RoutingBroker:
    def __init__(self):
        self._process = os.getpid()
        self._lock = threading.RLock()
        self._context = ContextVar("companion_trusted_dispatch", default=None)
        self._agents = weakref.WeakKeyDictionary()
        self._registries = weakref.WeakKeyDictionary()
        self._tasks = {}
        self._frames = {}
        self._deliveries = {}

    def bind_agent(self, agent, registry, data_dir):
        with self._lock:
            if agent in self._agents or registry in self._registries:
                raise ContractError("agent/registry already bound")
            route = AgentRoute(self, agent, registry, data_dir)
            self._agents[agent] = route
            self._registries[registry] = route
            return route

    def _origin(self):
        ambient = self._context.get()
        frames = self._frames.get(_execution(), ())
        anchored = frames[-1] if frames else None
        if ambient is not None and anchored is not None and ambient is not anchored:
            raise ContractError("ambiguous trusted dispatch identity")
        handle = anchored if anchored is not None else ambient
        if handle is None:
            return None
        task = self._tasks.get(getattr(handle, "identifier", None))
        if task is None or task.handle is not handle or not task.alive or os.getpid() != self._process:
            raise ContractError("missing/expired/copied trusted dispatch identity")
        return task

    @contextmanager
    def turn(self, agent, *, raw_text, source, chat, event):
        task = None
        token = None
        execution = _execution()
        with self._lock:
            try:
                route = self._agents.get(agent)
            except TypeError:  # legacy unbound Agent.chat test doubles
                route = None
            origin = self._origin()
            if origin is not None and origin.protected:
                raise ContractError("site task cannot borrow another ingress/runtime")
            if route is not None:
                if os.getpid() != self._process or route.agent() is not agent:
                    raise ContractError("agent runtime identity changed")
                protected = (route.owner and agent.owner is True and source == "OWNER_DIRECT"
                             and type(raw_text) is str and raw_text.startswith("/companion-action"))
                task = _Task(route, chat, event, protected)
                self._tasks[task.handle.identifier] = task
                self._frames.setdefault(execution, []).append(task.handle)
                token = self._context.set(task.handle)
                if protected:
                    route.protected.add(task.handle.identifier)
        try:
            if task is not None and task.protected:
                # Open existing pinned state only. A missing/corrupt store or
                # malformed intake still leaves THIS turn guarded, with no
                # authority; finally retires it. No setup/repair fallback.
                try:
                    task.store = reopen_claim_store(task.route.data_dir)
                    task.intake = OwnerIntake(agent, task.store, session=task.route.session,
                        task=task.handle.identifier, channel=str(task.route.channel),
                        chat=str(chat), authority_domain="companion/owner/" + str(task.route.channel))
                    task.admission = task.intake.capture(agent, source=source,
                                                        raw_text=raw_text, event_id=event)
                    if task.route.presenter is not None:
                        task.review = ReviewController(task.intake, task.route.presenter)
                except Exception as exc:
                    task.failure = type(exc).__name__
            yield task
        finally:
            if task is not None:
                with self._lock:
                    task.alive = False
                    task.route.protected.discard(task.handle.identifier)
                    try:
                        for delivery in list(self._deliveries.values()):
                            if delivery[1] is task:
                                self._close_delivery(delivery[0])
                        if task.review is not None:
                            task.review.invalidate()
                        if task.intake is not None:
                            task.intake.retire()
                        if task.store is not None:
                            task.store.close()
                    finally:
                        self._frames[execution].remove(task.handle)
                        if not self._frames[execution]:
                            del self._frames[execution]
                        self._context.reset(token)
                        del self._tasks[task.handle.identifier]

    @contextmanager
    def dispatch(self, registry, name):
        """Mandatory registry boundary; also covers direct/nested calls."""
        token = None
        handle = None
        execution = _execution()
        with self._lock:
            origin = self._origin()
            route = self._registries.get(registry)
            if os.getpid() != self._process:
                raise ContractError("copied process cannot inherit runtime identity")
            if origin is None:
                # Unscoped calls amid protected work are ambiguous. A real
                # unrelated turn supplies its own service-owned identity and
                # remains usable. Outside site work legacy calls are unchanged.
                if (route is not None and route.protected
                        or any(r.protected for r in self._registries.values())):
                    raise ContractError("missing trusted agent/session/task dispatch identity")
            else:
                if origin.route.registry() is not registry and (origin.protected or
                        (route is not None and route.protected)):
                    raise ContractError("dispatch cannot borrow another agent registry")
                if origin.protected:
                    role = ROUTES.get(name, "unknown_mutator")
                    raise ContractError("Companion site task: " + role + " route dormant; alternate execution denied")
                handle = origin.handle
                self._frames.setdefault(execution, []).append(handle)
                token = self._context.set(handle)
        try:
            yield
        finally:
            if handle is not None:
                with self._lock:
                    self._frames[execution].remove(handle)
                    if not self._frames[execution]:
                        del self._frames[execution]
                    self._context.reset(token)

    def handoff(self, agent):
        """Trusted service helper for async/thread/executor work, no model API.

        Captures the actual live seal, never an agent ID supplied by a caller.
        An expired/foreign handoff cannot turn into unguarded work.
        """
        with self._lock:
            task = self._origin()
            if task is None or task.route.agent() is not agent:
                raise ContractError("trusted originating task required")
            handle = task.handle

        @contextmanager
        def install():
            execution = _execution()
            with self._lock:
                current = self._origin()
                if current is not None and current.handle is not handle:
                    raise ContractError("foreign task cannot install a handoff")
                if self._tasks.get(handle.identifier) is not task or not task.alive or os.getpid() != self._process:
                    raise ContractError("retired/spawned runtime handoff")
                self._frames.setdefault(execution, []).append(handle)
                token = self._context.set(handle)
            try:
                yield
            finally:
                with self._lock:
                    self._frames[execution].remove(handle)
                    if not self._frames[execution]:
                        del self._frames[execution]
                    self._context.reset(token)
        return install

    def attach_presenter(self, agent, presenter, deliver):
        with self._lock:
            route = self._agents.get(agent)
            if route is None or not route.owner or route.presenter is not None or presenter is None:
                raise ContractError("trusted owner UI attachment required")
            route.presenter, route.deliver = presenter, deliver

    def queue_review(self, agent, task, kernel, request):
        """Trusted kernel-to-UI seam. No production provider creates a request."""
        from .kernel import AuthorityKernel
        with self._lock:
            if (type(task) is not _Task or self._tasks.get(task.handle.identifier) is not task
                    or self._origin() is not task or self._agents.get(agent) is not task.route
                    or not task.alive or not task.protected
                    or type(kernel) is not AuthorityKernel or kernel.intake is not task.intake
                    or kernel.review is not task.review or task.review is None):
                raise ContractError("foreign/retired review delivery")
            task.review._request(request)
            for previous in list(self._deliveries.values()):
                if previous[1] is task:
                    self._close_delivery(previous[0])
            delivery = ReviewDelivery(secrets.token_hex(32))
            self._deliveries[delivery.identifier] = [delivery, task, kernel, request, False, None]
            self._review_current(delivery, task.route.presenter)
            task.route.deliver(delivery)
            return delivery

    def _review_current(self, delivery, presenter):
        from core import emergency_stop
        entry = self._deliveries.get(getattr(delivery, "identifier", None))
        if (entry is None or entry[0] is not delivery or not entry[1].alive
                or entry[1].route.presenter is not presenter or emergency_stop.is_latched()
                or os.getpid() != self._process):
            raise ContractError("stale/untrusted review delivery")
        task, kernel, request = entry[1:4]
        task.review._request(request)
        admitted = task.intake.resolve(task.admission)
        observation = kernel._observation(request.snapshot.specification, admitted)
        if (observation.surface_digest != request.snapshot.surface_digest
                or observation.principal.fingerprint != request.snapshot.principal_digest
                or observation.principal.identity != request.snapshot.principal
                or kernel._epoch != request.snapshot.control_epoch):
            raise ContractError("review principal/scope changed; new owner review required")
        return entry

    def review_snapshot(self, delivery, presenter):
        with self._lock:
            return self._review_current(delivery, presenter)[3].snapshot

    def review_scope(self, delivery, presenter):
        with self._lock:
            task = self._review_current(delivery, presenter)[1]
            return task.route.agent(), task.chat

    def present_review(self, delivery, presenter, fingerprint):
        with self._lock:
            entry = self._review_current(delivery, presenter)
            entry[1].review.present(presenter, entry[3], fingerprint)
            entry[4] = True

    def approve_review(self, delivery, presenter, fingerprint):
        with self._lock:
            entry = self._review_current(delivery, presenter)
            if not entry[4] or entry[5] is not None:
                raise ContractError("unpresented/already decided review")
            approval = entry[1].review.approve(presenter, entry[3],
                event_id="ui:" + secrets.token_hex(32), displayed_fingerprint=fingerprint)
            entry[5] = approval
            return approval

    def _close_delivery(self, delivery):
        entry = self._deliveries.pop(delivery.identifier, None)
        if entry is not None and entry[5] is None:
            try:
                entry[1].review.cancel(entry[1].route.presenter, entry[3])
            except ContractError:
                pass

    def cancel_review(self, delivery, presenter):
        with self._lock:
            entry = self._deliveries.get(getattr(delivery, "identifier", None))
            if entry is not None and entry[0] is delivery and entry[1].route.presenter is presenter:
                self._close_delivery(delivery)


_broker = None
_broker_lock = threading.RLock()


def bind_agent(agent, registry, data_dir):
    global _broker
    with _broker_lock:
        if _broker is None:
            _broker = RoutingBroker()
        return _broker.bind_agent(agent, registry, data_dir)


@contextmanager
def turn(agent, **ingress):
    if _broker is None:
        yield None
    else:
        with _broker.turn(agent, **ingress) as task:
            yield task


@contextmanager
def dispatch_scope(registry, name):
    if _broker is None:
        yield
    else:
        with _broker.dispatch(registry, name):
            yield
