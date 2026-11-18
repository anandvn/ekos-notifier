import asyncio

from dbus_fast import Message, MessageType, Variant

from ekos_notifier.ekos.adapter import EkosAdapter


class Reply:
    def __init__(self, body=None, error=None):
        self.message_type = MessageType.ERROR if error else MessageType.METHOD_RETURN
        self.error_name = error
        self.body = body or []


class StubBus:
    """Answers Properties.Get from a dict; anything missing is UnknownObject, as when Ekos is
    stopped and its module objects don't exist."""

    def __init__(self, props):
        self.props = props
        self.calls = []

    async def call(self, msg):
        self.calls.append((msg.path, msg.member, list(msg.body)))
        if msg.member == "Get":
            key = (msg.path, msg.body[1])
            if key in self.props:
                return Reply([Variant("i", self.props[key])])
            return Reply(error="org.freedesktop.DBus.Error.UnknownObject")
        return Reply()


def adapter():
    events = []
    return EkosAdapter(events.append, clock=lambda: 5.0), events


def test_snapshot_reads_states_and_skips_missing_objects():
    a, events = adapter()
    bus = StubBus(
        {
            ("/KStars/Ekos", "ekosStatus"): 2,
            ("/KStars/Ekos", "indiStatus"): 2,
            ("/KStars/Ekos/Mount", "parkStatus"): 4,
            ("/KStars/Ekos/Scheduler", "status"): 2,
        }
    )
    asyncio.run(a.snapshot(bus))
    got = {(e.kind, e.data["state"]) for e in events}
    assert got == {
        ("ekos.state", "success"),
        ("indi.state", "success"),
        ("mount.park", "unparked"),
        ("scheduler.state", "running"),
    }
    assert all(e.data["snapshot"] for e in events)


def test_snapshot_of_one_module():
    a, events = adapter()
    bus = StubBus({("/KStars/Ekos/Mount", "parkStatus"): 1, ("/KStars/Ekos", "ekosStatus"): 2})
    asyncio.run(a.snapshot(bus, modules=["Mount"]))
    assert [(e.kind, e.data["state"]) for e in events] == [("mount.park", "parked")]


def test_on_message_decodes_kstars_signals_only():
    a, events = adapter()
    a.on_message(
        Message.new_signal(
            "/KStars/Ekos/Mount", "org.kde.kstars.Ekos.Mount", "newParkStatus", "(i)", [[1]]
        )
    )
    a.on_message(Message.new_signal("/org/other", "org.other", "newParkStatus", "(i)", [[1]]))
    a.on_message(
        Message(
            path="/KStars/Ekos",
            interface="org.kde.kstars.Ekos",
            member="start",
            message_type=MessageType.METHOD_CALL,
        )
    )
    assert [(e.ts, e.kind, e.data["state"]) for e in events] == [(5.0, "mount.park", "parked")]


def test_on_message_survives_a_bad_payload(caplog):
    a, events = adapter()
    a.on_message(
        Message.new_signal(
            "/KStars/Ekos/Mount",
            "org.kde.kstars.Ekos.Mount",
            "newParkStatus",
            "s",
            ["not a number"],
        )
    )
    assert events == [] and "could not decode" in caplog.text


def test_kstars_reappearing_triggers_a_snapshot():
    a, events = adapter()

    async def scenario():
        a._bus = StubBus({("/KStars/Ekos", "ekosStatus"): 0})
        a.on_message(
            Message.new_signal(
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "NameOwnerChanged",
                "sss",
                ["org.kde.kstars", ":1.9", ":1.42"],
            )
        )
        await asyncio.gather(*a._tasks)

    asyncio.run(scenario())
    assert [e.kind for e in events] == ["kstars.vanished", "kstars.appeared", "ekos.state"]
    assert events[-1].data["state"] == "idle"


class LogBus(StubBus):
    """Mount's logText (newest first, as Ekos keeps it)."""

    def __init__(self, lines=None):
        super().__init__({})
        self.lines = lines
        self.connected = True

    async def call(self, msg):
        if msg.member == "Get" and msg.body[1] == "logText":
            if self.lines is None:
                return Reply(error="org.freedesktop.DBus.Error.UnknownObject")
            return Reply([Variant("as", self.lines)])
        return await super().call(msg)


def mount_logs(events):
    return [e.data["text"] for e in events if e.kind == "ekos.log" and e.data["module"] == "Mount"]


def test_mount_log_lines_become_events_but_not_its_history():
    a, events = adapter()
    bus = LogBus(["2026-11-18T07:07:52 Meridian flip ready to start..."])
    asyncio.run(a.read_logs(bus))
    assert mount_logs(events) == []  # there before we connected
    bus.lines = [
        "2026-11-18T07:12:00 meridian flip failed, retrying in 4 minutes",
        "2026-11-18T07:11:59 Meridian flip failed, pier side not changed",
        *bus.lines,
    ]
    asyncio.run(a.read_logs(bus))
    assert mount_logs(events) == [
        "Meridian flip failed, pier side not changed",
        "meridian flip failed, retrying in 4 minutes",
    ]
    asyncio.run(a.read_logs(bus))
    assert len(mount_logs(events)) == 2


def test_mount_log_of_an_ekos_started_later_is_all_new():
    a, events = adapter()
    bus = LogBus(None)  # Ekos not started: no Mount object
    asyncio.run(a.read_logs(bus))
    bus.lines = ["2026-11-18T20:00:00 Status: inactive"]
    asyncio.run(a.read_logs(bus))
    assert mount_logs(events) == ["Status: inactive"]


class JobBus(StubBus):
    def __init__(self, job, frame_type):
        super().__init__({})
        self.job, self.frame_type = job, frame_type

    async def call(self, msg):
        if msg.member == "getActiveJobID":
            return Reply([self.job])
        if msg.member == "getJobFrameType":
            assert msg.body == [self.job]
            return Reply([self.frame_type])
        return await super().call(msg)


def test_active_capture_job_frame_type_is_read_and_sent_on_change():
    a, events = adapter()
    bus = JobBus(3, 2)
    asyncio.run(a.read_job(bus))
    asyncio.run(a.read_job(bus))  # same job: nothing new
    bus.job, bus.frame_type = 4, 0
    asyncio.run(a.read_job(bus))
    jobs = [e.data for e in events if e.kind == "capture.job"]
    assert jobs == [{"job": 3, "frame_type": "dark"}, {"job": 4, "frame_type": "light"}]
    bus.job = -1  # nothing active
    asyncio.run(a.read_job(bus))
    assert len([e for e in events if e.kind == "capture.job"]) == 2
