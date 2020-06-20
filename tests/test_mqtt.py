# systemctl-mqtt - MQTT client triggering shutdown on systemd-based systems
#
# Copyright (C) 2020 Fabian Peter Hammerle <fabian@hammerle.me>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import logging
import threading
import time
import unittest.mock

import pytest
from paho.mqtt.client import MQTTMessage

import systemctl_mqtt

# pylint: disable=protected-access


@pytest.mark.parametrize("mqtt_host", ["mqtt-broker.local"])
@pytest.mark.parametrize("mqtt_port", [1833])
@pytest.mark.parametrize("mqtt_topic_prefix", ["systemctl/host", "system/command"])
def test__run(caplog, mqtt_host, mqtt_port, mqtt_topic_prefix):
    caplog.set_level(logging.DEBUG)
    with unittest.mock.patch(
        "socket.create_connection"
    ) as create_socket_mock, unittest.mock.patch(
        "ssl.SSLContext.wrap_socket", autospec=True,
    ) as ssl_wrap_socket_mock, unittest.mock.patch(
        "paho.mqtt.client.Client.loop_forever", autospec=True,
    ) as mqtt_loop_forever_mock, unittest.mock.patch(
        "gi.repository.GLib.MainLoop.run"
    ) as glib_loop_mock:
        ssl_wrap_socket_mock.return_value.send = len
        systemctl_mqtt._run(
            mqtt_host=mqtt_host,
            mqtt_port=mqtt_port,
            mqtt_username=None,
            mqtt_password=None,
            mqtt_topic_prefix=mqtt_topic_prefix,
        )
    assert caplog.records[0].levelno == logging.INFO
    assert caplog.records[0].message == "connecting to MQTT broker {}:{}".format(
        mqtt_host, mqtt_port
    )
    # correct remote?
    assert create_socket_mock.call_count == 1
    create_socket_args, _ = create_socket_mock.call_args
    assert create_socket_args[0] == (mqtt_host, mqtt_port)
    # ssl enabled?
    assert ssl_wrap_socket_mock.call_count == 1
    ssl_context = ssl_wrap_socket_mock.call_args[0][0]  # self
    assert ssl_context.check_hostname is True
    assert ssl_wrap_socket_mock.call_args[1]["server_hostname"] == mqtt_host
    # loop started?
    while threading.active_count() > 1:
        time.sleep(0.01)
    assert mqtt_loop_forever_mock.call_count == 1
    (mqtt_client,) = mqtt_loop_forever_mock.call_args[0]
    assert mqtt_client._tls_insecure is False
    # credentials
    assert mqtt_client._username is None
    assert mqtt_client._password is None
    # connect callback
    caplog.clear()
    mqtt_client.socket().getpeername.return_value = (mqtt_host, mqtt_port)
    with unittest.mock.patch(
        "paho.mqtt.client.Client.subscribe"
    ) as mqtt_subscribe_mock, unittest.mock.patch.object(
        mqtt_client._userdata, "acquire_shutdown_lock"
    ) as acquire_shutdown_lock_mock:
        mqtt_client.on_connect(mqtt_client, mqtt_client._userdata, {}, 0)
    acquire_shutdown_lock_mock.assert_called_once_with()
    mqtt_subscribe_mock.assert_called_once_with(mqtt_topic_prefix + "/poweroff")
    assert mqtt_client.on_message is None
    assert (  # pylint: disable=comparison-with-callable
        mqtt_client._on_message_filtered[mqtt_topic_prefix + "/poweroff"]
        == systemctl_mqtt._MQTT_TOPIC_SUFFIX_ACTION_MAPPING[
            "poweroff"
        ].mqtt_message_callback
    )
    assert caplog.records[0].levelno == logging.DEBUG
    assert caplog.records[0].message == "connected to MQTT broker {}:{}".format(
        mqtt_host, mqtt_port
    )
    assert caplog.records[1].levelno == logging.INFO
    assert caplog.records[1].message == "subscribing to {}".format(
        mqtt_topic_prefix + "/poweroff"
    )
    assert caplog.records[2].levelno == logging.DEBUG
    assert caplog.records[2].message == "registered MQTT callback for topic {}".format(
        mqtt_topic_prefix + "/poweroff"
    ) + " triggering {}".format(
        systemctl_mqtt._MQTT_TOPIC_SUFFIX_ACTION_MAPPING["poweroff"].action
    )
    # message callback
    caplog.clear()
    poweroff_message = MQTTMessage(topic=mqtt_topic_prefix.encode() + b"/poweroff")
    with unittest.mock.patch.object(
        systemctl_mqtt._MQTT_TOPIC_SUFFIX_ACTION_MAPPING["poweroff"], "action",
    ) as poweroff_action_mock:
        mqtt_client._handle_on_message(poweroff_message)
    poweroff_action_mock.assert_called_once_with()
    assert all(r.levelno == logging.DEBUG for r in caplog.records)
    assert caplog.records[0].message == "received topic={} payload=b''".format(
        poweroff_message.topic
    )
    assert caplog.records[1].message.startswith("executing action poweroff")
    assert caplog.records[2].message.startswith("completed action poweroff")
    # dbus loop started?
    glib_loop_mock.assert_called_once_with()
    # waited for mqtt loop to stop?
    assert mqtt_client._thread_terminate
    assert mqtt_client._thread is None


@pytest.mark.parametrize("mqtt_host", ["mqtt-broker.local"])
@pytest.mark.parametrize("mqtt_port", [1833])
@pytest.mark.parametrize("mqtt_username", ["me"])
@pytest.mark.parametrize("mqtt_password", [None, "secret"])
@pytest.mark.parametrize("mqtt_topic_prefix", ["systemctl/host"])
def test__run_authentication(
    mqtt_host, mqtt_port, mqtt_username, mqtt_password, mqtt_topic_prefix
):
    with unittest.mock.patch("socket.create_connection"), unittest.mock.patch(
        "ssl.SSLContext.wrap_socket"
    ) as ssl_wrap_socket_mock, unittest.mock.patch(
        "paho.mqtt.client.Client.loop_forever", autospec=True,
    ) as mqtt_loop_forever_mock, unittest.mock.patch(
        "gi.repository.GLib.MainLoop.run"
    ):
        ssl_wrap_socket_mock.return_value.send = len
        systemctl_mqtt._run(
            mqtt_host=mqtt_host,
            mqtt_port=mqtt_port,
            mqtt_username=mqtt_username,
            mqtt_password=mqtt_password,
            mqtt_topic_prefix=mqtt_topic_prefix,
        )
    assert mqtt_loop_forever_mock.call_count == 1
    (mqtt_client,) = mqtt_loop_forever_mock.call_args[0]
    assert mqtt_client._username.decode() == mqtt_username
    if mqtt_password:
        assert mqtt_client._password.decode() == mqtt_password
    else:
        assert mqtt_client._password is None


@pytest.mark.parametrize("mqtt_host", ["mqtt-broker.local"])
@pytest.mark.parametrize("mqtt_port", [1833])
@pytest.mark.parametrize("mqtt_password", ["secret"])
def test__run_authentication_missing_username(mqtt_host, mqtt_port, mqtt_password):
    with unittest.mock.patch("paho.mqtt.client.Client"):
        with pytest.raises(ValueError):
            systemctl_mqtt._run(
                mqtt_host=mqtt_host,
                mqtt_port=mqtt_port,
                mqtt_username=None,
                mqtt_password=mqtt_password,
                mqtt_topic_prefix="prefix",
            )


@pytest.mark.parametrize("mqtt_topic", ["system/command/poweroff"])
@pytest.mark.parametrize("payload", [b"", b"junk"])
def test_mqtt_message_callback_poweroff(caplog, mqtt_topic: str, payload: bytes):
    message = MQTTMessage(topic=mqtt_topic.encode())
    message.payload = payload
    with unittest.mock.patch.object(
        systemctl_mqtt._MQTT_TOPIC_SUFFIX_ACTION_MAPPING["poweroff"], "action",
    ) as action_mock, caplog.at_level(logging.DEBUG):
        systemctl_mqtt._MQTT_TOPIC_SUFFIX_ACTION_MAPPING[
            "poweroff"
        ].mqtt_message_callback(
            None, None, message  # type: ignore
        )
    action_mock.assert_called_once_with()
    assert len(caplog.records) == 3
    assert caplog.records[0].levelno == logging.DEBUG
    assert caplog.records[0].message == (
        "received topic={} payload={!r}".format(mqtt_topic, payload)
    )
    assert caplog.records[1].levelno == logging.DEBUG
    assert caplog.records[1].message.startswith(
        "executing action {} ({!r})".format("poweroff", action_mock)
    )
    assert caplog.records[2].levelno == logging.DEBUG
    assert caplog.records[2].message.startswith(
        "completed action {} ({!r})".format("poweroff", action_mock)
    )


@pytest.mark.parametrize("mqtt_topic", ["system/command/poweroff"])
@pytest.mark.parametrize("payload", [b"", b"junk"])
def test_mqtt_message_callback_poweroff_retained(
    caplog, mqtt_topic: str, payload: bytes
):
    message = MQTTMessage(topic=mqtt_topic.encode())
    message.payload = payload
    message.retain = True
    with unittest.mock.patch.object(
        systemctl_mqtt._MQTT_TOPIC_SUFFIX_ACTION_MAPPING["poweroff"], "action",
    ) as action_mock, caplog.at_level(logging.DEBUG):
        systemctl_mqtt._MQTT_TOPIC_SUFFIX_ACTION_MAPPING[
            "poweroff"
        ].mqtt_message_callback(
            None, None, message  # type: ignore
        )
    action_mock.assert_not_called()
    assert len(caplog.records) == 2
    assert caplog.records[0].levelno == logging.DEBUG
    assert caplog.records[0].message == (
        "received topic={} payload={!r}".format(mqtt_topic, payload)
    )
    assert caplog.records[1].levelno == logging.INFO
    assert caplog.records[1].message == "ignoring retained message"


def test_shutdown_lock():
    settings = systemctl_mqtt._Settings(mqtt_topic_prefix="any")
    lock_fd = unittest.mock.MagicMock()
    with unittest.mock.patch(
        "systemctl_mqtt._get_login_manager"
    ) as get_login_manager_mock:
        get_login_manager_mock.return_value.Inhibit.return_value = lock_fd
        settings.acquire_shutdown_lock()
    get_login_manager_mock.return_value.Inhibit.assert_called_once_with(
        "shutdown", "systemctl-mqtt", "Report shutdown via MQTT", "delay",
    )
    assert settings._shutdown_lock == lock_fd
    # https://dbus.freedesktop.org/doc/dbus-python/dbus.types.html#dbus.types.UnixFd.take
    lock_fd.take.return_value = "fdnum"
    with unittest.mock.patch("os.close") as close_mock:
        settings.release_shutdown_lock()
    close_mock.assert_called_once_with("fdnum")
