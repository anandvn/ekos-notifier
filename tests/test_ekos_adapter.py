import asyncio

from dbus_fast import Message, MessageType, Variant

from town_crier.ekos.adapter import EkosAdapter


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
