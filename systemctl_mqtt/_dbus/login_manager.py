# systemctl-mqtt - MQTT client triggering & reporting shutdown on systemd-based systems
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

import datetime
import logging

import jeepney
import jeepney.io.blocking

import systemctl_mqtt._dbus

_LOGGER = logging.getLogger(__name__)

_LOGIN_MANAGER_OBJECT_PATH = "/org/freedesktop/login1"
_LOGIN_MANAGER_INTERFACE = "org.freedesktop.login1.Manager"


def get_login_manager_signal_match_rule(member: str) -> jeepney.MatchRule:
    return jeepney.MatchRule(
        type="signal",
        interface=_LOGIN_MANAGER_INTERFACE,
        member=member,
        path=_LOGIN_MANAGER_OBJECT_PATH,
    )


class LoginManager(systemctl_mqtt._dbus.Properties):  # pylint: disable=protected-access
    """
    https://freedesktop.org/wiki/Software/systemd/logind/

    $ python3 -m jeepney.bindgen \
        --bus unix:path=/var/run/dbus/system_bus_socket \
        --name org.freedesktop.login1 --path /org/freedesktop/login1
    """

    interface = _LOGIN_MANAGER_INTERFACE

    def __init__(self):
        super().__init__(
            object_path=_LOGIN_MANAGER_OBJECT_PATH, bus_name="org.freedesktop.login1"
        )

    # pylint: disable=invalid-name; inherited method names from Manager object

    def ListInhibitors(self) -> jeepney.low_level.Message:
        return jeepney.new_method_call(remote_obj=self, method="ListInhibitors")

    def LockSessions(self) -> jeepney.low_level.Message:
        return jeepney.new_method_call(remote_obj=self, method="LockSessions")

    def CanPowerOff(self) -> jeepney.low_level.Message:
        return jeepney.new_method_call(remote_obj=self, method="CanPowerOff")

    def ScheduleShutdown(
        self, *, action: str, time: datetime.datetime
    ) -> jeepney.low_level.Message:
        return jeepney.new_method_call(
            remote_obj=self,
            method="ScheduleShutdown",
            signature="st",
            body=(action, int(time.timestamp() * 1e6)),  # (type, usec)
        )

    def Suspend(self, *, interactive: bool) -> jeepney.low_level.Message:
        return jeepney.new_method_call(
            remote_obj=self, method="Suspend", signature="b", body=(interactive,)
        )

    # WIP: Hardcoded starting of ansible-pull.service ... first shot
    def StartAnsible(self, *, interactive: bool) -> jeepney.low_level.Message:
        return jeepney.new_method_call(
            # jeepney.wrappers.DBusErrorResponse: [org.freedesktop.DBus.Error.InteractiveAuthorizationRequired] ('Interactive authentication required.',)
            #
            # Userspace:
            # jeepney.wrappers.DBusErrorResponse: [org.freedesktop.DBus.Error.AccessDenied] ('Sender is not authorized to send message',)
            # Jan 03 08:04:14 fcos-41.hp.molecule.lab dbus-broker[1138]: A security policy denied :1.67 to send method call /org/freedesktop/login1:org.freedesktop.login1.Manager.StartUnit to org.freedesktop.login1.
            #
            # Root, Unknown Method:
            # jeepney.wrappers.DBusErrorResponse: [org.freedesktop.DBus.Error.UnknownMethod] ('Unknown method StartUnit or interface org.freedesktop.login1.Manager.',)
            remote_obj=self, method="StartUnit", signature="s", body=("ansible-pull.service",)
        )

    def Inhibit(
        self, *, what: str, who: str, why: str, mode: str
    ) -> jeepney.low_level.Message:
        return jeepney.new_method_call(
            remote_obj=self,
            method="Inhibit",
            signature="ssss",
            body=(what, who, why, mode),
        )


def get_login_manager_proxy() -> jeepney.io.blocking.Proxy:
    # https://jeepney.readthedocs.io/en/latest/integrate.html
    # https://gitlab.com/takluyver/jeepney/-/blob/master/examples/aio_notify.py
    return jeepney.io.blocking.Proxy(
        msggen=LoginManager(),
        connection=jeepney.io.blocking.open_dbus_connection(
            bus="SYSTEM",
            # > dbus-broker[…]: Peer :1.… is being disconnected as it does not
            # . support receiving file descriptors it requested.
            enable_fds=True,
        ),
    )


def _log_shutdown_inhibitors(login_manager_proxy: jeepney.io.blocking.Proxy) -> None:
    if _LOGGER.getEffectiveLevel() > logging.DEBUG:
        return
    found_inhibitor = False
    try:
        # https://www.freedesktop.org/wiki/Software/systemd/inhibit/
        (inhibitors,) = login_manager_proxy.ListInhibitors()
        for what, who, why, mode, uid, pid in inhibitors:
            if "shutdown" in what:
                found_inhibitor = True
                _LOGGER.debug(
                    "detected shutdown inhibitor %s (pid=%u, uid=%u, mode=%s): %s",
                    who,
                    pid,
                    uid,
                    mode,
                    why,
                )
    except jeepney.wrappers.DBusErrorResponse as exc:
        _LOGGER.warning("failed to fetch shutdown inhibitors: %s", exc)
        return
    if not found_inhibitor:
        _LOGGER.debug("no shutdown inhibitor locks found")


def schedule_shutdown(*, action: str, delay: datetime.timedelta) -> None:
    # https://github.com/systemd/systemd/blob/v237/src/systemctl/systemctl.c#L8553
    assert action in ["poweroff", "reboot"], action
    time = datetime.datetime.now() + delay
    # datetime.datetime.isoformat(timespec=) not available in python3.5
    # https://github.com/python/cpython/blob/v3.5.9/Lib/datetime.py#L1552
    _LOGGER.info("scheduling %s for %s", action, time.strftime("%Y-%m-%d %H:%M:%S"))
    login_manager = get_login_manager_proxy()
    try:
        # $ gdbus introspect --system --dest org.freedesktop.login1 \
        #       --object-path /org/freedesktop/login1 | grep -A 1 ScheduleShutdown
        # ScheduleShutdown(in  s arg_0,
        #                  in  t arg_1);
        # $ gdbus call --system --dest org.freedesktop.login1 \
        #       --object-path /org/freedesktop/login1 \
        #       --method org.freedesktop.login1.Manager.ScheduleShutdown \
        #       poweroff "$(date --date=10min +%s)000000"
        # $ dbus-send --type=method_call --print-reply --system --dest=org.freedesktop.login1 \
        #       /org/freedesktop/login1 \
        #       org.freedesktop.login1.Manager.ScheduleShutdown \
        #       string:poweroff "uint64:$(date --date=10min +%s)000000"
        login_manager.ScheduleShutdown(action=action, time=time)
    except jeepney.wrappers.DBusErrorResponse as exc:
        if (
            exc.name == "org.freedesktop.DBus.Error.InteractiveAuthorizationRequired"
            and exc.data == ("Interactive authentication required.",)
        ):
            _LOGGER.error(
                "failed to schedule %s: unauthorized; missing polkit authorization rules?",
                action,
            )
        else:
            _LOGGER.error("failed to schedule %s: %s", action, exc)
    _log_shutdown_inhibitors(login_manager)


def suspend() -> None:
    _LOGGER.info("suspending system")
    get_login_manager_proxy().Suspend(interactive=False)


def lock_all_sessions() -> None:
    """
    $ loginctl lock-sessions
    """
    _LOGGER.info("instruct all sessions to activate screen locks")
    get_login_manager_proxy().LockSessions()

# WIP: First shot
# def start_ansible() -> None:
#     _LOGGER.info("login_manager start ansible")
#     get_login_manager_proxy().StartAnsible(interactive=False)