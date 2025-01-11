from collections import defaultdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from itertools import chain
from typing import override

from pynicotine.pluginsystem import BasePlugin
from pynicotine.transfers import TransferStatus
from pynicotine.userbrowse import BrowsedUser


class Plugin(BasePlugin):
    @override
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.settings = {
            "verbose": False,
            "check_distributed_search": False,
            "send_message": False,
            "open_private_chat": True,
            "message": (
                "Hey! I wanted to share my thoughts on private shares. "
                "While they can seem convenient, they often limit the community aspect of sharing "
                "and discovering new music. Private shares can create exclusivity, making it harder"
                " for others to access and enjoy the content. "
                "Let's keep the spirit of sharing alive by keeping our collections open!"
            ),
            "banned": [],
        }

        self.metasettings = {
            "verbose": {
                "description": "Verbose logging",
                "type": "bool",
            },
            "check_distributed_search": {
                "description": "Check users from distibuted search events",
                "type": "bool",
            },
            "send_message": {
                "description": "Send a message after banning",
                "type": "bool",
            },
            "open_private_chat": {
                "description": "Open chat tabs when sending private messages",
                "type": "bool",
            },
            "message": {
                "description": (
                    "Private chat message to send. Each line is sent as a separate message, "
                    "too many message lines may get you temporarily banned for spam!"
                ),
                "type": "textview",
            },
            "banned": {
                "description": "Banned users",
                "type": "list string",
            },
        }

        self.users = defaultdict(User)

    @override
    def init(self):
        self.users[self.config.sections["server"]["login"]].state = UserState.NoPrivateShares

        if self.settings["send_message"] and not self.settings["message"].strip():
            self.log("message is empty, disabling message sending")
            self.settings["send_message"] = False

        for username in chain(self.settings["banned"], self.config.sections["server"]["banlist"]):
            user = self.users[username]
            user.state = UserState.HasPrivateShares
            user.sent_message = True

            if not self.core.network_filter.is_user_banned(username):
                self.log(f"{username}: wasn't banned")
                self.core.network_filter.ban_user(username)

    @override
    def disable(self):
        for username, user in self.users.items():
            if user.state == UserState.RequestedShares:
                self.core.users.unwatch_user(username, context=self.internal_name)
                self.core.userbrowse.users[username].clear()

    @override
    def search_request_notification(self, searchterm, user, token):
        self.check_user(user, CheckReason.Search)

    @override
    def distrib_search_notification(self, searchterm, user, token):
        if self.settings["check_distributed_search"]:
            self.check_user(user, CheckReason.DistributedSearch)

    @override
    def incoming_private_chat_event(self, user, line):
        self.check_user(user, CheckReason.PrivateChat)

    @override
    def upload_queued_notification(self, user, virtual_path, real_path):
        self.check_user(user, CheckReason.UploadQueued)

    @override
    def upload_started_notification(self, user, virtual_path, real_path):
        self.check_user(user, CheckReason.UploadStarted)

    @override
    def user_stats_notification(self, user, stats):
        if stats["source"] != "peer":
            return

        self.check_shares(user)

    def check_user(self, username, reason):
        user = self.users[username]

        if self.settings["verbose"] or reason != CheckReason.DistributedSearch:
            user.emit_logs = True

        if user.state == UserState.NoPrivateShares:
            pass
        elif user.state == UserState.HasPrivateShares:
            if reason == CheckReason.UploadQueued or reason == CheckReason.UploadStarted:
                self.log(f"{username}: banned user tried to download: {reason}")
                self.ban_user(user, username)
        elif user.should_request_shares():
            if self.settings["verbose"] or reason != CheckReason.DistributedSearch:
                self.log(f"{username}: requesting user shares: {reason}")

            if username not in self.core.userbrowse.users:
                self.core.userbrowse.users[username] = BrowsedUser(username)

            self.core.users.watch_user(username, context=self.internal_name)
            self.core.userbrowse.request_user_shares(username)
        elif self.settings["verbose"] or reason != CheckReason.DistributedSearch:
            self.log(f"{username}: already requested user shares: {reason}")

    def check_shares(self, username):
        browsed_user = self.core.userbrowse.users[username]

        if browsed_user.num_folders is None or browsed_user.num_files is None:
            self.log(f"{username}: shares are None")
            return

        user = self.users[username]

        if len(browsed_user.private_folders) == 0:
            user.state = UserState.NoPrivateShares

            if user.emit_logs:
                self.log(f"{username}: user doesn't have private shares")
        else:
            self.log(f"{username}: user has private shares")
            user.state = UserState.HasPrivateShares
            self.ban_user(user, username)

        self.core.users.unwatch_user(username, context=self.internal_name)
        self.core.userbrowse.users[username].clear()

    def ban_user(self, user, username):
        if username not in self.settings["banned"]:
            self.settings["banned"].append(username)

        if self.core.network_filter.is_user_banned(username):
            if user.emit_logs:
                self.log(f"{username}: user is already banned")
        else:
            self.core.network_filter.ban_user(username)

            if user.emit_logs:
                self.log(f"{username}: banned user")

        aborted_transfers = 0

        for user_transfers in (
            self.core.uploads.queued_users,
            self.core.uploads.active_users,
            self.core.uploads.failed_users,
        ):
            transfers = user_transfers.get(username)

            if not transfers:
                continue

            for transfer in transfers:
                self.core.uploads._abort_transfer(transfer, status=TransferStatus.CANCELLED)

                aborted_transfers += 1

        if aborted_transfers != 0:
            self.log(f"{username}: aborted {aborted_transfers} transfers")

        if not user.sent_message and self.settings["send_message"]:
            if not self.settings["message"].strip():
                self.log("message is empty, disabling message sending")
                self.settings["send_message"] = False
                return

            user.sent_message = True

            for line in self.settings["message"].splitlines():
                self.send_private(
                    username,
                    line.rstrip(),
                    show_ui=self.settings["open_private_chat"],
                    switch_page=False,
                )


class User:
    def __init__(self):
        self.state = None
        self.requested_shares = None
        self.sent_message = False
        self.emit_logs = False

    def should_request_shares(self):
        now = datetime.now(timezone.utc)

        if self.state is None or (
            self.state == UserState.RequestedShares
            and (
                self.requested_shares is None
                or now - self.requested_shares >= timedelta(seconds=30)
            )
        ):
            self.state = UserState.RequestedShares
            self.requested_shares = now
            return True
        else:
            return False


class UserState(Enum):
    RequestedShares = 1
    HasPrivateShares = 2
    NoPrivateShares = 3


class CheckReason(Enum):
    Search = 1
    DistributedSearch = 2
    PrivateChat = 3
    UploadQueued = 4
    UploadStarted = 5
