import types


def _build_context(client, prefix="testbot"):
    return types.SimpleNamespace(
        application=types.SimpleNamespace(
            bot_data={"valkey_client": client, "valkey_prefix": prefix}
        )
    )


def test_update_application_fields_success(tmp_path, bot_modules):
    admin = bot_modules.admin
    storage = bot_modules.storage

    client = storage.InMemoryValkey()
    context = _build_context(client)
    session_key = "session-1"
    valkey_key = f"testbot:{session_key}"
    client.hset(
        valkey_key,
        mapping={
            "user_id": "42",
            "position": "Old",
            "photos": "",
        },
    )
    client.sadd("testbot:applications", valkey_key)

    photo_dir = tmp_path / "media"
    photo_dir.mkdir()
    first_photo = str(photo_dir / "first.jpg")
    second_photo = str(photo_dir / "second.jpg")

    result = admin.update_application_fields(
        context,
        session_key,
        42,
        position="New",
        photos=[first_photo, second_photo],
    )

    assert result is True
    record = client.hgetall(valkey_key)
    assert record["position"] == "New"
    assert record["photos"] == f"{first_photo},{second_photo}"


def test_update_application_fields_rejects_other_user(bot_modules):
    admin = bot_modules.admin
    storage = bot_modules.storage

    client = storage.InMemoryValkey()
    context = _build_context(client)
    session_key = "session-2"
    valkey_key = f"testbot:{session_key}"
    client.hset(
        valkey_key,
        mapping={
            "user_id": "99",
            "position": "Original",
        },
    )
    client.sadd("testbot:applications", valkey_key)

    result = admin.update_application_fields(
        context,
        session_key,
        42,
        position="Hacked",
    )

    assert result is False
    record = client.hgetall(valkey_key)
    assert record["position"] == "Original"
