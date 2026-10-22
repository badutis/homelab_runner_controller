"""When to wake a machine and when to suspend it. Pure logic: no network, no clock, no disk.

Input: what GitHub shows right now (runners and queued jobs) and the controller's memory.
Output: a list of actions. So the logic can be tested without GitHub, Wake-on-LAN or SSH
(`tests/test_decide.py`).

A runner can take a job only when both come from the same source (project) and the runner
has ALL of the job's labels, the same way GitHub assigns jobs.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, FrozenSet, List, Optional, Tuple


@dataclass(frozen=True)
class Machine:
    name: str
    mac: str
    host: str
    ssh_user: str
    runner_prefixes: Tuple[str, ...]
    rank: int  # lower is woken first (faster machines before slower ones)

    def owns(self, runner_name: str) -> bool:
        return any(runner_name.startswith(p) for p in self.runner_prefixes)


@dataclass(frozen=True)
class Runner:
    name: str
    online: bool
    busy: bool
    labels: FrozenSet[str]
    source: str = ""


@dataclass(frozen=True)
class QueuedJob:
    labels: FrozenSet[str]  # lower case
    since: Optional[datetime] = None  # when the job was created (information only)
    source: str = ""
    id: Optional[int] = None


@dataclass(frozen=True)
class Observation:
    runners: Tuple[Runner, ...]
    queued: Tuple[QueuedJob, ...]
    # False when at least one source could not be read: it is then unknown whether any
    # runner of that machine is working, so nothing is suspended (waking is still allowed).
    complete: bool = True


@dataclass(frozen=True)
class Settings:
    idle: timedelta = timedelta(minutes=30)
    wake_timeout: timedelta = timedelta(minutes=3)
    wake_attempts: int = 2
    failed_backoff: timedelta = timedelta(minutes=30)
    alert_quiet: timedelta = timedelta(hours=6)
    # How long after a suspend command the machine's runners may still look online.
    sleep_grace: timedelta = timedelta(minutes=5)
    sleep_retry: timedelta = timedelta(minutes=10)
    # A job that waits this long although a runner that could take it is awake and free is
    # stuck (GitHub gives it to nobody): it no longer wakes machines or keeps them awake.
    # The time counts from when such a runner appeared, not from when the job was created;
    # otherwise an old job seen after a controller outage would never be served.
    stale_queue: timedelta = timedelta(minutes=30)
    weekly: timedelta = timedelta(days=7)
    weekly_hold: timedelta = timedelta(hours=1)


@dataclass
class MachineState:
    awake: bool = False
    last_busy: Optional[datetime] = None
    wake_sent: Optional[datetime] = None
    wake_attempts: int = 0
    failed_until: Optional[datetime] = None
    last_alert: Optional[datetime] = None
    sleep_sent: Optional[datetime] = None
    sleep_failures: int = 0
    # When the machine last woke up: while its runners connect one by one, those not yet
    # online are counted as on their way, not as missing.
    woke_at: Optional[datetime] = None
    hold_until: Optional[datetime] = None
    weekly_pending: bool = False
    # The last time at least one runner was online. Kept on disk together with hold_until:
    # GitHub removes a runner that has not connected for 14 days (docs/design.md).
    last_online: Optional[datetime] = None


@dataclass(frozen=True)
class Action:
    kind: str  # "wake" | "sleep" | "alert"
    machine: str
    reason: str


def can_take(runner: Runner, job: QueuedJob) -> bool:
    return runner.source == job.source and job.labels <= frozenset(l.lower() for l in runner.labels)


@dataclass
class Controller:
    machines: Tuple[Machine, ...]
    settings: Settings = field(default_factory=Settings)
    state: Dict[str, MachineState] = field(default_factory=dict)
    # In dry run nothing is sent, so "did not wake" is not a failure.
    dry_run: bool = False
    # job id -> when an awake, free runner could first have taken it
    served_since: Dict[int, datetime] = field(default_factory=dict)

    def __post_init__(self):
        for m in self.machines:
            self.state.setdefault(m.name, MachineState())

    # --- helpers ---------------------------------------------------------------------

    def _runners_of(self, machine: Machine, obs: Observation) -> List[Runner]:
        return [r for r in obs.runners if machine.owns(r.name)]

    def _machine_of(self, runner_name: str) -> Optional[Machine]:
        for m in self.machines:
            if m.owns(runner_name):
                return m
        return None

    def _going_to_sleep(self, name: str, now: datetime) -> bool:
        s = self.state[name]
        return s.sleep_sent is not None and now - s.sleep_sent < self.settings.sleep_grace

    def _usable(self, name: str, now: datetime) -> bool:
        """Awake and not being suspended: its free runners really can take work."""
        return self.state[name].awake and not self._going_to_sleep(name, now)

    def _waking(self, name: str, now: datetime) -> bool:
        s = self.state[name]
        return (
            not self._usable(name, now) and s.wake_sent is not None and now - s.wake_sent < self.settings.wake_timeout
        )

    def _track_served(self, obs: Observation, now: datetime) -> None:
        present = set()
        for job in obs.queued:
            if job.id is None:
                continue
            present.add(job.id)
            if job.id in self.served_since:
                continue
            for r in obs.runners:
                owner = self._machine_of(r.name)
                usable = owner is None or self._usable(owner.name, now)
                if r.online and not r.busy and usable and can_take(r, job):
                    self.served_since[job.id] = now
                    break
        self.served_since = {k: v for k, v in self.served_since.items() if k in present}

    def _fresh(self, obs: Observation, now: datetime) -> List[QueuedJob]:
        """Jobs that GitHub may still hand out (see Settings.stale_queue)."""
        return [
            j
            for j in obs.queued
            if j.id is None
            or j.id not in self.served_since
            or now - self.served_since[j.id] < self.settings.stale_queue
        ]

    def sleep_failed(self, name: str, now: datetime) -> bool:
        """Suspending failed (SSH error, or the machine refused). The machine counts as in
        use again and its idle time starts over. Returns True on the third failure in a
        row, when it is worth an alert."""
        s = self.state[name]
        s.sleep_sent = None
        s.last_busy = now
        s.sleep_failures += 1
        return s.sleep_failures >= 3

    def _alert(self, s: MachineState, name: str, reason: str, now: datetime, actions: List[Action]) -> None:
        if s.last_alert is None or now - s.last_alert >= self.settings.alert_quiet:
            s.last_alert = now
            actions.append(Action("alert", name, reason))

    def _send_wake(self, m: Machine, reason: str, now: datetime, actions: List[Action]) -> None:
        s = self.state[m.name]
        s.wake_sent = now
        s.wake_attempts = 1
        if s.sleep_sent is not None:
            # Waking a machine that was just suspended: its idle time starts over.
            s.sleep_sent = None
            s.last_busy = now
        actions.append(Action("wake", m.name, reason))

    # --- decision --------------------------------------------------------------------

    def step(self, obs: Observation, now: datetime) -> List[Action]:
        actions: List[Action] = []
        self._observe(obs, now, actions)
        self._track_served(obs, now)
        self._wake_for_queue(obs, now, actions)
        self._weekly(now, actions)
        if obs.complete:
            self._sleep_idle(obs, now, actions)
        return actions

    def _observe(self, obs: Observation, now: datetime, actions: List[Action]) -> None:
        for m in self.machines:
            s = self.state[m.name]
            runners = self._runners_of(m, obs)
            awake = any(r.online for r in runners)
            if awake and not s.awake:
                # Just woke up: idle time counts from now.
                s.last_busy = now
                s.wake_sent = None
                s.wake_attempts = 0
                s.failed_until = None
                s.sleep_sent = None
                s.sleep_failures = 0
                s.woke_at = now
                if s.weekly_pending:
                    s.hold_until = now + self.settings.weekly_hold
                    s.weekly_pending = False
            if not awake and s.awake:
                s.sleep_sent = None
            s.awake = awake
            if awake:
                s.last_online = now
                # An old wake no longer matters once the machine is awake; otherwise, when it
                # later went to sleep, the controller would at once wake it "again".
                if s.wake_sent is not None and now - s.wake_sent >= self.settings.wake_timeout:
                    s.wake_sent = None
                    s.wake_attempts = 0
            if any(r.busy for r in runners):
                s.last_busy = now
            if not awake and s.wake_sent is not None and now - s.wake_sent >= self.settings.wake_timeout:
                if s.wake_attempts < self.settings.wake_attempts and not self.dry_run:
                    s.wake_sent = now
                    s.wake_attempts += 1
                    actions.append(Action("wake", m.name, "again: did not wake up before"))
                else:
                    s.wake_sent = None
                    s.wake_attempts = 0
                    s.failed_until = now + self.settings.failed_backoff
                    s.weekly_pending = False
                    if not self.dry_run:
                        self._alert(
                            s, m.name, f"did not wake up after {self.settings.wake_attempts} attempts", now, actions
                        )

    def _wake_for_queue(self, obs: Observation, now: datetime, actions: List[Action]) -> None:
        demand: Dict[Tuple[str, FrozenSet[str]], int] = {}
        for job in self._fresh(obs, now):
            key = (job.source, job.labels)
            demand[key] = demand.get(key, 0) + 1
        for key in sorted(demand, key=lambda k: (k[0], sorted(k[1]))):
            probe = QueuedJob(labels=key[1], source=key[0])
            have = 0
            for r in obs.runners:
                if not (r.online and not r.busy and can_take(r, probe)):
                    continue
                owner = self._machine_of(r.name)
                if owner is None or self._usable(owner.name, now):
                    have += 1
            coming = 0
            candidates = []
            for m in sorted(self.machines, key=lambda m: (m.rank, m.name)):
                slots = sum(1 for r in self._runners_of(m, obs) if can_take(r, probe))
                if slots == 0:
                    continue
                s = self.state[m.name]
                if self._waking(m.name, now):
                    coming += slots
                elif (
                    self._usable(m.name, now) and s.woke_at is not None and now - s.woke_at < self.settings.wake_timeout
                ):
                    coming += sum(1 for r in self._runners_of(m, obs) if not r.online and can_take(r, probe))
                elif not self._usable(m.name, now) and (s.failed_until is None or now >= s.failed_until):
                    candidates.append((m, slots))
            missing = demand[key] - have - coming
            labels = ",".join(sorted(key[1] - {"self-hosted"}))
            for m, slots in candidates:
                if missing <= 0:
                    break
                missing -= slots
                self._send_wake(m, f"{demand[key]} queued job(s) with `{labels}`", now, actions)

    def _weekly(self, now: datetime, actions: List[Action]) -> None:
        for m in self.machines:
            s = self.state[m.name]
            if s.awake or self._waking(m.name, now):
                continue
            if s.failed_until is not None and now < s.failed_until:
                continue
            if s.last_online is None or now - s.last_online >= self.settings.weekly:
                self._send_wake(m, "weekly: the runners must connect", now, actions)
                s.weekly_pending = True

    def _sleep_idle(self, obs: Observation, now: datetime, actions: List[Action]) -> None:
        fresh = self._fresh(obs, now)
        for m in self.machines:
            s = self.state[m.name]
            if not s.awake:
                continue
            runners = self._runners_of(m, obs)
            if any(r.busy for r in runners):
                continue
            if any(can_take(r, j) for r in runners if r.online for j in fresh):
                continue
            if s.hold_until is not None and now < s.hold_until:
                continue
            if s.last_busy is None or now - s.last_busy < self.settings.idle:
                continue
            if s.sleep_sent is not None and now - s.sleep_sent < self.settings.sleep_retry:
                continue
            s.sleep_sent = now
            s.wake_sent = None
            s.wake_attempts = 0
            idle_min = int((now - s.last_busy).total_seconds() // 60)
            actions.append(Action("sleep", m.name, f"idle for {idle_min} min"))
